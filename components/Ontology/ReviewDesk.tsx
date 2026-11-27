import React, { useEffect, useState } from 'react';
import Link from 'next/link';
import { authFetch } from '../../utils/app/authFetch';

type Entity = { id: string; kind: string; name: string; availability: string; attributes: Record<string, unknown> };
type Run = { id: string; source_id: string; state: string; error_code?: string; digest?: string;
  artifact?: { document: { entities: Entity[] }; observations: unknown; expires_at: number } };
type Change = { id: string; author: string; status: string; digest: string; diff?: unknown; document?: unknown; review?: unknown };
type Config = { enabled: boolean; subject: string; sources: { source_id: string; tables: string[]; sample_rows: number }[] };
const button = 'rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-900 hover:bg-slate-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600 disabled:cursor-not-allowed disabled:opacity-40';
const input = 'w-full rounded-lg border border-slate-300 bg-white p-3 text-slate-900 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-600';
const card = 'min-w-0 rounded-xl border border-slate-200 bg-white p-5 shadow-sm';
const stateNames: Record<string, string> = { running: '采集中', complete: '采集完整', partial: '采集不完整', failed: '采集失败', validated: '待审核', approved: '已批准，未发布', rejected: '已拒绝', published: '已发布' };

async function api(path: string, data?: unknown) {
  const response = await authFetch('/api/ontology/' + path, data === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error?.message || '请求失败，请刷新后重试');
  return result;
}

export default function ReviewDesk({ onSignOut }: { onSignOut?: () => void } = {}) {
  const [config, setConfig] = useState<Config>();
  const [source, setSource] = useState('');
  const [runs, setRuns] = useState<Run[]>([]);
  const [run, setRun] = useState<Run>();
  const [selected, setSelected] = useState<string[]>([]);
  const [edits, setEdits] = useState('{}');
  const [reason, setReason] = useState('');
  const [reviewReason, setReviewReason] = useState('');
  const [changes, setChanges] = useState<Change[]>([]);
  const [change, setChange] = useState<Change>();
  const [active, setActive] = useState({ release_id: null as string | null, generation: 0 });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  async function refresh() {
    const [r, c, h] = await Promise.all([api('discovery/runs'), api('changesets'), api('releases')]);
    setRuns(r.runs); setChanges(c.changesets); setActive(h.active);
  }
  async function action(work: () => Promise<void>) {
    setBusy(true); setError(''); setNotice('');
    try { await work(); } catch (e) { setError(e instanceof Error ? e.message : '操作失败'); }
    finally { setBusy(false); }
  }
  useEffect(() => { void action(async () => {
    const c: Config = await api('discovery/config');
    setConfig(c); setSource(c.sources[0]?.source_id || ''); await refresh();
  }); }, []);
  const chooseRun = async (id: string) => {
    setRun(await api('discovery/runs/' + id)); setSelected([]); setEdits('{}'); setReason('');
  };
  const chooseChange = async (id: string) => { setChange(await api('changesets/' + id)); setReviewReason(''); };
  const review = (decision: string) => action(async () => {
    if (!change) return;
    setChange(await api(`changesets/${change.id}/review`, { expected_digest: change.digest, decision, reason: reviewReason }));
    await refresh(); setNotice(decision === 'approved' ? '审核已批准。请核对后单独执行发布。' : '已拒绝。修订后需重新移交为新变更。');
  });

  return <main className="min-h-screen bg-slate-50 text-slate-900">
    <div className="mx-auto max-w-7xl space-y-6 px-4 py-8 sm:px-8">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div><p className="text-sm font-medium text-indigo-700">数据治理 / 本体管理</p><h1 className="mt-1 text-3xl font-semibold">候选与审核</h1>
          <p className="mt-2 max-w-3xl text-sm text-slate-600">采集结果仅进入候选区。人工核验后移交变更，由另一位管理员审核并显式发布为参考知识；自动采集不生成可执行 SQL。</p></div>
        <nav aria-label="管理导航" className="flex gap-3"><Link className={button} href="/">返回分析</Link>{onSignOut && <button className={button} onClick={onSignOut}>退出登录</button>}</nav>
      </header>
      <div className="rounded-lg border border-indigo-200 bg-indigo-50 px-4 py-3 text-sm">
        当前发布：<span className="break-all font-mono">{active.release_id || '尚未发布'}</span> · 代次 {active.generation}
        {config && <span className="ml-3">管理员：{config.subject}</span>}
      </div>
      <div aria-live="polite" role="status">{busy ? '正在处理，请勿重复提交…' : notice}</div>
      {error && <p role="alert" className="rounded-lg border border-red-300 bg-red-50 p-4 text-red-900">{error}</p>}
      {config && <div className="grid gap-6 lg:grid-cols-3">
        <section className={card + ' space-y-4'} aria-labelledby="collect-title">
          <h2 id="collect-title" className="text-xl font-semibold">1. 采集候选</h2>
          {!config.enabled && <p>自动采集未启用。由运维配置表列白名单后开启。</p>}
          <label className="block text-sm">允许的数据源<select className={input + ' mt-2'} value={source} onChange={e => setSource(e.target.value)} disabled={busy}>
            <option value="">请选择</option>{config.sources.map(s => <option key={s.source_id}>{s.source_id}</option>)}</select></label>
          {config.sources.filter(s => s.source_id === source).map(s => <p key={s.source_id} className="break-words text-sm text-slate-600">表：{s.tables.join('、')}；每表前缀样本上限 {s.sample_rows} 行，仅回传聚合统计。</p>)}
          <button className={button + ' mr-2'} disabled={busy || !source || !config.enabled} onClick={() => void action(async () => {
            const result = await api('discovery/runs', { source_id: source, idempotency_key: crypto.randomUUID() });
            setRun(result); setSelected([]); setEdits('{}'); setReason(''); await refresh();
            setNotice('采集作业已返回。请核对状态、范围和证据。');
          })}>开始采集</button>
          <button className={button} disabled={busy} onClick={() => void action(refresh)}>刷新列表</button>
          <ul className="space-y-2" aria-label="采集作业">{runs.map(r => <li key={r.id}><button className={button + ' w-full text-left'} disabled={busy} onClick={() => void action(() => chooseRun(r.id))}>
            {r.source_id} · {stateNames[r.state] || r.state}<span className="block break-all font-mono text-xs">{r.id}</span></button></li>)}</ul>
        </section>
        <section className={card + ' space-y-4 lg:col-span-2'} aria-labelledby="candidate-title">
          <h2 id="candidate-title" className="text-xl font-semibold">2. 人工核验与移交</h2>
          {!run ? <p className="text-slate-600">选择一项采集作业，检查候选和证据。</p> : <>
            <p>{stateNames[run.state] || run.state} {run.error_code || ''}</p>
            {run.artifact && <>
              <p className="text-sm text-slate-600">证据有效期至 {new Date(run.artifact.expires_at * 1000).toLocaleString()}。前缀样本不代表全量分布；样本关系通过也需人工确认业务角色。</p>
              <details><summary className="cursor-pointer font-medium">查看采集证据与范围</summary><pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-all rounded bg-slate-100 p-3 text-xs">{JSON.stringify(run.artifact.observations, null, 2)}</pre></details>
              <fieldset disabled={busy || run.state !== 'complete'} className="max-h-72 space-y-2 overflow-auto rounded-lg border p-3"><legend className="px-1 text-sm">选择需要移交的实体（依赖自动补齐）</legend>
                {run.artifact.document.entities.map(e => <label key={e.id} className="flex items-start gap-2 rounded p-1 text-sm hover:bg-slate-50">
                  <input className="mt-1" type="checkbox" checked={selected.includes(e.id)} onChange={event => setSelected(event.target.checked ? [...selected, e.id] : selected.filter(id => id !== e.id))} />
                  <span>{e.name} · {e.kind} · {e.availability}<span className="block break-all font-mono text-xs text-slate-500">{e.id}</span></span></label>)}
              </fieldset>
              <details><summary className="cursor-pointer font-medium">查看候选完整定义</summary><pre className="max-h-80 overflow-auto whitespace-pre-wrap break-all p-3 text-xs">{JSON.stringify(run.artifact.document, null, 2)}</pre></details>
              <label className="block text-sm">人工修订 JSON（实体 ID → 修订字段；attributes 需完整对象）<textarea className={input + ' mt-2 font-mono text-xs'} rows={6} value={edits} onChange={e => setEdits(e.target.value)} disabled={busy} /></label>
              <p className="text-xs text-slate-600">仅支持 name、description、owner、synonyms，以及概念/指标/关系的 attributes。指标必须补齐粒度、单位、时间和聚合口径；关系必须有通过的证据并确认 many-to-one 与连接方向。</p>
              <label className="block text-sm">人工核验理由<textarea className={input + ' mt-2'} rows={2} maxLength={2000} value={reason} onChange={e => setReason(e.target.value)} disabled={busy} /></label>
              <button className={button} disabled={busy || run.state !== 'complete' || !selected.length || !reason.trim()} onClick={() => void action(async () => {
                const result = await api(`discovery/runs/${run.id}/prepare-review`, { expected_digest: run.digest, entity_ids: selected, edits: JSON.parse(edits), reason, expected_active: active.release_id, expected_generation: active.generation });
                setChange(result); setReviewReason(''); await refresh(); setNotice('已生成待审核变更，尚未发布。请由另一位管理员审核。');
              })}>移交为待审核变更</button>
            </>}
          </>}
        </section>
        <section className={card + ' space-y-4 lg:col-span-3'} aria-labelledby="review-title">
          <h2 id="review-title" className="text-xl font-semibold">3. 独立审核与发布</h2>
          <label className="block text-sm">变更<select className={input + ' mt-2'} value={change?.id || ''} disabled={busy} onChange={e => { if (e.target.value) void action(() => chooseChange(e.target.value)); }}>
            <option value="">选择变更</option>{changes.map(c => <option key={c.id} value={c.id}>{stateNames[c.status] || c.status} · {c.author} · {c.id}</option>)}</select></label>
          {change && <>
            <p>提交者：{change.author} · 状态：{stateNames[change.status] || change.status}</p>
            <p className="break-all font-mono text-xs">审核摘要：{change.digest}</p>
            <details open><summary className="cursor-pointer font-medium">发布差异</summary><pre className="max-h-80 overflow-auto whitespace-pre-wrap break-all rounded bg-slate-100 p-3 text-xs">{JSON.stringify(change.diff, null, 2)}</pre></details>
            <details><summary className="cursor-pointer font-medium">完整变更及审核记录</summary><pre className="max-h-80 overflow-auto whitespace-pre-wrap break-all p-3 text-xs">{JSON.stringify(change, null, 2)}</pre></details>
            {change.author === config.subject && change.status === 'validated' && <p>提交者不能审核自己的变更。请由另一位有该源权限的管理员登录。</p>}
            <label className="block text-sm">审核理由<textarea className={input + ' mt-2'} rows={2} value={reviewReason} maxLength={2000} onChange={e => setReviewReason(e.target.value)} disabled={busy} /></label>
            <div className="flex flex-wrap gap-3"><button className={button} disabled={busy || change.status !== 'validated' || change.author === config.subject || !reviewReason.trim()} onClick={() => void review('approved')}>批准变更</button>
              <button className={button} disabled={busy || change.status !== 'validated' || change.author === config.subject || !reviewReason.trim()} onClick={() => void review('rejected')}>拒绝变更</button>
              <button className={button + ' border-indigo-400'} disabled={busy || change.status !== 'approved'} onClick={() => void action(async () => {
                const receipt = await api('releases', { changeset_id: change.id, expected_digest: change.digest });
                await chooseChange(change.id); await refresh(); setNotice('已发布版本：' + receipt.release_id);
              })}>发布已批准变更</button></div>
          </>}
        </section>
      </div>}
    </div>
  </main>;
}
