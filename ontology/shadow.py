"""默认关闭的有界异步旁路；请求线程只采样与非阻塞入队。"""
from __future__ import annotations

import atexit
import hashlib
import json
import logging
import math
import os
import queue
import random
import re
import threading
import time
from dataclasses import dataclass

from .schema import stable_id
from .service import OntologyService
from .store import FileOntologyStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShadowConfig:
    enabled: bool = False
    sample_rate: float = 0.1
    queue_size: int = 128
    top_k: int = 5
    refresh_seconds: float = 5.0
    namespace: str = "default"

    @classmethod
    def from_env(cls):
        flag = os.getenv("ONTOLOGY_SHADOW_ENABLED", "false").lower()
        if flag not in {"true", "false"}:
            raise ValueError("ONTOLOGY_SHADOW_ENABLED 必须是 true 或 false")
        config = cls(flag == "true", float(os.getenv("ONTOLOGY_SHADOW_SAMPLE_RATE", "0.1")),
                     int(os.getenv("ONTOLOGY_SHADOW_QUEUE_SIZE", "128")),
                     int(os.getenv("ONTOLOGY_TOP_K", "5")),
                     float(os.getenv("ONTOLOGY_REFRESH_SECONDS", "5")),
                     os.getenv("ONTOLOGY_NAMESPACE", "default"))
        if not math.isfinite(config.sample_rate) or not 0 <= config.sample_rate <= 1:
            raise ValueError("旁路采样率必须在 0..1 范围内")
        if not 1 <= config.queue_size <= 10000 or not 1 <= config.top_k <= 100:
            raise ValueError("旁路队列/候选数量超出范围")
        if not math.isfinite(config.refresh_seconds) or not 0.1 <= config.refresh_seconds <= 3600:
            raise ValueError("旁路刷新时间必须在 0.1..3600 秒范围内")
        if not config.namespace.strip():
            raise ValueError("本体 namespace 不能为空")
        return config


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ShadowRecorder:
    def __init__(self, config, service=None, sink=None):
        self.config = config
        self._pid = os.getpid()
        self.service = service or OntologyService(FileOntologyStore(), config.top_k,
                                                  config.refresh_seconds, config.namespace)
        self.sink = sink or (lambda record: logger.info(json.dumps(record, ensure_ascii=False)))
        self.queue = queue.Queue(maxsize=config.queue_size)
        self.dropped = 0
        self.errors = 0
        self._stop = threading.Event()
        self._thread = None
        if config.enabled:
            self._thread = threading.Thread(target=self._run, name="ontology-shadow", daemon=True)
            self._thread.start()
            atexit.register(self.close)

    def submit(self, branch, question, trace_id, selected="", final_selection="",
               status="matched", llm_elapsed_ms=None):
        try:
            if os.getpid() != self._pid or not self.config.enabled or self._stop.is_set() or random.random() >= self.config.sample_rate:
                return False
            if branch not in {"scenario", "template"} or not isinstance(question, str):
                return False
            event = dict(branch=branch, question=question[:4096], trace_id=str(trace_id or "")[:128],
                         selected=str(selected or "")[:4096], final_selection=str(final_selection or "")[:4096],
                         status=status, llm_elapsed_ms=llm_elapsed_ms)
            self.queue.put_nowait(event)
            return True
        except queue.Full:
            self.dropped += 1
            return False
        except Exception:
            self.errors += 1
            return False

    def _record(self, event):
        start = time.monotonic()
        branch = event["branch"]
        candidates = self.service.scenario_candidates(event["question"]) if branch == "scenario" else \
                     self.service.template_candidates(event["question"])
        relation_ids = []
        if branch == "scenario" and event["final_selection"]:
            relation_ids = [r.id for r in self.service.get_relations(event["final_selection"])]
        chosen = event["final_selection"]
        chosen_id = stable_id("scenario" if branch == "scenario" else "template",
                              self.config.namespace, chosen) if chosen else None
        ids = [candidate.entity.id for candidate in candidates]
        trace = event["trace_id"]
        safe_trace = trace if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", trace) else digest(trace)
        record = dict(event="ontology_shadow", event_version=1, timestamp=time.time(),
                      trace_id=safe_trace, branch=branch, status=event["status"],
                      namespace=self.config.namespace, question_hash=digest(event["question"]),
                      revision=self.service.revision, cache_error_type=self.service.error_type,
                      candidates=[dict(id=c.entity.id, version=c.entity.version, score=c.score,
                                       reason=c.reason) for c in candidates],
                      legacy_selection_id=chosen_id,
                      raw_selection_hash=digest(event["selected"]) if event["selected"] else None,
                      top1_agreement=bool(chosen_id and ids and ids[0] == chosen_id),
                      topk_agreement=bool(chosen_id and chosen_id in ids),
                      relation_ids=relation_ids, recall_ms=round((time.monotonic() - start) * 1000, 3),
                      legacy_match_ms=event["llm_elapsed_ms"],
                      dropped_total=self.dropped, errors_total=self.errors)
        self.sink(record)

    def _run(self):
        while not self._stop.is_set():
            try:
                event = self.queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._record(event)
            except Exception as exc:
                self.errors += 1
                try:
                    self.sink(dict(event="ontology_shadow_error", error_type=type(exc).__name__,
                                   dropped_total=self.dropped, errors_total=self.errors))
                except Exception:
                    pass
            finally:
                self.queue.task_done()

    def close(self, timeout=0.2):
        atexit.unregister(self.close)
        if os.getpid() != self._pid:
            return
        self._stop.set()
        # 不长期保留停用记录器队列中的用户问题。
        while True:
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except queue.Empty:
                break
        if self._thread and threading.current_thread() is not self._thread:
            self._thread.join(timeout=max(0, timeout))


_recorder = None
_config = None
_lock = threading.Lock()


def configure():
    global _config, _recorder
    try:
        config = ShadowConfig.from_env()
        with _lock:
            if _recorder:
                _recorder.close()
            if config.enabled:
                logger.setLevel(logging.INFO)
                if not logger.handlers:
                    handler = logging.StreamHandler()
                    handler.setFormatter(logging.Formatter("%(message)s"))
                    logger.addHandler(handler)
                logger.propagate = False
            _config = config
            _recorder = ShadowRecorder(config) if config.enabled else None
    except Exception as exc:
        _config = ShadowConfig()
        _recorder = None
        try:
            logger.warning("ontology_shadow_disabled error_type=%s", type(exc).__name__)
        except Exception:
            pass


def record_comparison(*args, **kwargs):
    global _recorder
    try:
        if _config is None or not _config.enabled:
            return False
        if _recorder is None:
            if not _lock.acquire(blocking=False):
                return False
            try:
                if _recorder is None:
                    _recorder = ShadowRecorder(_config)
            finally:
                _lock.release()
        return _recorder.submit(*args, **kwargs)
    except Exception:
        return False


def _after_fork():
    global _recorder, _lock
    # 配置可继承，但线程/锁/队列必须由子进程自行创建。
    _recorder = None
    _lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)
