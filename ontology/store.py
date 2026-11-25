"""本地 POSIX 文件仓库：来源级替换、幂等版本、原子发布及回退。"""
from __future__ import annotations

import fcntl
import os
import re
import tempfile
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path

from .schema import COLLECTIONS, Snapshot, ValidationError, VersionCounter

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "var" / "ontology"
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024


def configured_path():
    return Path(os.getenv("ONTOLOGY_STORE_PATH", str(DEFAULT_PATH))).expanduser().resolve()


class OntologyStore(ABC):
    @abstractmethod
    def read(self, stage="approved") -> Snapshot:
        pass

    @abstractmethod
    def replace_sources(self, snapshot: Snapshot, sources, stage="approved") -> Snapshot:
        pass

    @abstractmethod
    def revision_token(self, stage="approved"):
        pass


class FileOntologyStore(OntologyStore):
    def __init__(self, path=None, lock_timeout=10.0, history_limit=20):
        self.path = Path(path) if path is not None else configured_path()
        self.lock_timeout = lock_timeout
        self.history_limit = history_limit
        if lock_timeout <= 0 or history_limit < 1:
            raise ValueError("锁超时与历史保留数量必须为正")

    def _file(self, stage):
        if stage not in {"pending", "approved"}:
            raise ValueError("stage 必须是 pending 或 approved")
        return self.path / (stage + ".json")

    def revision_token(self, stage="approved"):
        try:
            stat = self._file(stage).stat()
            return (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            return None

    def _read_file(self, path):
        with path.open("rb") as stream:
            data = stream.read(MAX_SNAPSHOT_BYTES + 1)
        if len(data) > MAX_SNAPSHOT_BYTES:
            raise ValidationError("快照超过容量上限")
        snapshot = Snapshot.from_json(data.decode("utf-8"))
        snapshot.verify_revision()
        return snapshot

    def read(self, stage="approved"):
        try:
            return self._read_file(self._file(stage))
        except FileNotFoundError:
            return Snapshot().seal()

    @contextmanager
    def _lock(self):
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(str(self.path / ".write.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("本体写入锁超时")
                    time.sleep(0.01)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _atomic_write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _publish(self, snapshot, stage):
        snapshot.seal()
        data = snapshot.to_json().encode("utf-8")
        if len(data) > MAX_SNAPSHOT_BYTES:
            raise ValidationError("快照超过容量上限")
        history = self.path / "history" / stage
        self._atomic_write(history / (snapshot.revision + ".json"), data)
        self._atomic_write(self._file(stage), data)
        # 历史清理失败不应将已成功的原子发布报告成失败。
        try:
            files = sorted(history.glob("*.json"), key=lambda path: path.stat().st_mtime_ns,
                           reverse=True)
            for path in files[self.history_limit:]:
                path.unlink()
        except OSError:
            pass
        return snapshot

    def replace_sources(self, snapshot, sources, stage="approved"):
        snapshot = Snapshot.from_dict(snapshot.to_dict())
        sources = {tuple(source) for source in sources}
        if not sources or any(len(source) != 2 for source in sources):
            raise ValidationError("必须显式提供 (namespace, asset) 来源集合")
        incoming = snapshot.entities()
        if any((e.source.namespace, e.source.asset) not in sources for e in incoming):
            raise ValidationError("写入实体不属于待替换来源")
        if stage == "pending" and any(e.review_status == "approved" for e in incoming):
            raise ValidationError("待审核区不能写入已审核实体")
        with self._lock():
            current = self.read(stage)
            old = {e.id: e for e in current.entities()}
            counters = {c.id: c.version for c in current.version_counters}
            counters.update({identity: max(counters.get(identity, 0), entity.version)
                             for identity, entity in old.items()})
            merged = Snapshot(warnings=[w for w in current.warnings if not any(
                w.startswith(f"[{namespace}:{asset}]") for namespace, asset in sources)]
                + snapshot.warnings)
            for name in COLLECTIONS:
                retained = [e for e in getattr(current, name)
                            if (e.source.namespace, e.source.asset) not in sources]
                retained_ids = {e.id for e in retained}
                fresh = []
                for entity in getattr(snapshot, name):
                    if entity.id in retained_ids:
                        raise ValidationError("同一实体由不同来源重复定义")
                    previous = old.get(entity.id)
                    if previous:
                        before, after = previous.to_dict(), entity.to_dict()
                        before.pop("version")
                        after.pop("version")
                        entity.version = previous.version + (before != after)
                    else:
                        entity.version = counters.get(entity.id, 0) + 1
                    counters[entity.id] = entity.version
                    fresh.append(entity)
                setattr(merged, name, sorted(retained + fresh, key=lambda e: e.id))
            merged.warnings = sorted(set(merged.warnings))
            merged.version_counters = [VersionCounter(identity, version)
                                       for identity, version in sorted(counters.items())]
            merged.seal()
            if merged.revision == current.revision:
                return current
            return self._publish(merged, stage)

    def rollback(self, revision, stage="approved"):
        self._file(stage)
        if not re.fullmatch(r"[0-9a-f]{64}", revision):
            raise ValidationError("无效历史修订")
        with self._lock():
            desired = self._read_file(self.path / "history" / stage / (revision + ".json"))
            try:
                if self.revision_token(stage) is None:
                    raise FileNotFoundError("当前快照缺失")
                current = self.read(stage)
            except (ValidationError, UnicodeError, OSError):
                # 显式回退允许修复损坏的当前文件，使用最近的良好历史保留版本高水位。
                current = None
                history = self.path / "history" / stage
                for path in sorted(history.glob("*.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True):
                    try:
                        current = self._read_file(path)
                        break
                    except (ValidationError, UnicodeError, OSError):
                        continue
                if current is None:
                    raise ValidationError("没有可用于恢复的有效历史")
            old = {e.id: e for e in current.entities()}
            counters = {c.id: c.version for c in current.version_counters}
            for entity in desired.entities():
                if entity.id in old:
                    before, after = old[entity.id].to_dict(), entity.to_dict()
                    before.pop("version")
                    after.pop("version")
                    entity.version = old[entity.id].version + (before != after)
                else:
                    entity.version = max(counters.get(entity.id, 0), entity.version) + 1
                counters[entity.id] = entity.version
            desired.version_counters = [VersionCounter(identity, version)
                                        for identity, version in sorted(counters.items())]
            return self._publish(desired, stage)
