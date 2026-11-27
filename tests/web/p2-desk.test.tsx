// @vitest-environment jsdom
import React from 'react';
import { createRoot, Root } from 'react-dom/client';
import { act, Simulate } from 'react-dom/test-utils';
import { beforeEach, afterEach, expect, it, vi } from 'vitest';
import ReviewDesk from '../../components/Ontology/ReviewDesk';
import { authFetch } from '../../utils/app/authFetch';
vi.mock('../../utils/app/authFetch', () => ({ authFetch: vi.fn() }));

let host: HTMLDivElement, root: Root;
let subject: string, status: string, available: boolean, fail: boolean;
const changeId = 'a'.repeat(32), runId = 'b'.repeat(32);
const run = { id: runId, source_id: 'a', state: 'complete', digest: 'artifact', artifact: {
  expires_at: 9999999999, observations: { schema_stable: true },
  document: { entities: [{ id: 'dataset:orders', name: '订单', kind: 'Dataset', availability: 'candidate', attributes: {} }] },
} };
const change = () => ({ id: changeId, author: 'author', status, digest: 'bound-digest', diff: { added: ['dataset:orders'] }, document: {} });
async function mount() { await act(async () => { root.render(<ReviewDesk />); }); }
function button(label: string) { return Array.from(host.querySelectorAll('button')).find(b => b.textContent === label)!; }
async function click(el: HTMLElement) { await act(async () => { el.click(); }); }
async function edit(el: Element, value: string) { await act(async () => { Simulate.change(el, { target: { value } } as any); }); }
beforeEach(() => {
  (globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host);
  subject = 'author'; status = 'validated'; available = false; fail = false;
  vi.clearAllMocks();
  vi.mocked(authFetch).mockImplementation(async (url, options) => {
    const path = url.replace('/api/ontology/', '');
    let data: any;
    if (fail) return { ok: false, json: async () => ({ error: { message: '无管理员权限' } }) } as Response;
    if (path === 'discovery/config') data = { enabled: true, subject, sources: [{ source_id: 'a', tables: ['orders'], sample_rows: 10 }] };
    else if (path === 'discovery/runs') data = options?.method === 'POST' ? run : { runs: [run] };
    else if (path === 'discovery/runs/' + runId) data = run;
    else if (path.endsWith('prepare-review')) { available = true; data = change(); }
    else if (path === 'changesets') data = { changesets: available ? [change()] : [] };
    else if (path.endsWith('/review')) { status = JSON.parse(options!.body as string).decision; data = change(); }
    else if (path === 'changesets/' + changeId) data = change();
    else if (path === 'releases' && options?.method === 'POST') { status = 'published'; data = { release_id: 'published-release' }; }
    else if (path === 'releases') data = { active: { release_id: status === 'published' ? 'published-release' : null, generation: status === 'published' ? 1 : 0 }, releases: [] };
    else throw new Error(path);
    return { ok: true, json: async () => data } as Response;
  });
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); });

it('loads candidate evidence and hands off a digest-bound change without publishing', async () => {
  await mount();
  await click(Array.from(host.querySelectorAll('button')).find(b => b.textContent?.includes(runId))!);
  expect(host.textContent).toContain('candidate');
  await click(host.querySelector('input[type=checkbox]')!);
  await edit(host.querySelectorAll('textarea')[1], '已核验物理映射');
  await click(button('移交为待审核变更'));
  const call = vi.mocked(authFetch).mock.calls.find(([url]) => url.endsWith('prepare-review'))!;
  expect(JSON.parse(call[1]!.body as string)).toMatchObject({ expected_digest: 'artifact', entity_ids: ['dataset:orders'], expected_active: null, expected_generation: 0 });
  expect(button('批准变更').disabled).toBe(true);
  expect(button('发布已批准变更').disabled).toBe(true);
  expect(vi.mocked(authFetch).mock.calls.some(([url, options]) => url.endsWith('/releases') && options?.method === 'POST')).toBe(false);
});

it('requires independent approval and a separate publish action', async () => {
  subject = 'reviewer'; available = true;
  await mount();
  await edit(host.querySelectorAll('select')[1], changeId);
  await edit(host.querySelector('textarea')!, '已独立复核');
  await click(button('批准变更'));
  expect(status).toBe('approved');
  expect(host.textContent).toContain('尚未发布');
  expect(button('发布已批准变更').disabled).toBe(false);
  await click(button('发布已批准变更'));
  expect(status).toBe('published');
  expect(host.textContent).toContain('published-release');
  const post = vi.mocked(authFetch).mock.calls.find(([url, options]) => url.endsWith('/releases') && options?.method === 'POST')!;
  expect(JSON.parse(post[1]!.body as string).expected_digest).toBe('bound-digest');
});

it('rejected changes cannot be published', async () => {
  subject = 'reviewer'; available = true;
  await mount(); await edit(host.querySelectorAll('select')[1], changeId);
  await edit(host.querySelector('textarea')!, '业务口径不正确');
  await click(button('拒绝变更'));
  expect(status).toBe('rejected'); expect(button('发布已批准变更').disabled).toBe(true);
});

it('shows access errors without exposing admin controls', async () => {
  fail = true; await mount();
  expect(host.querySelector('[role=alert]')?.textContent).toContain('无管理员权限');
  expect(button('开始采集')).toBeUndefined();
});

it('invalid JSON edits stop before any handoff request', async () => {
  await mount(); await click(Array.from(host.querySelectorAll('button')).find(b => b.textContent?.includes(runId))!);
  await click(host.querySelector('input[type=checkbox]')!);
  await edit(host.querySelectorAll('textarea')[0], '{invalid');
  await edit(host.querySelectorAll('textarea')[1], '核验');
  await click(button('移交为待审核变更'));
  expect(host.querySelector('[role=alert]')).not.toBeNull();
  expect(vi.mocked(authFetch).mock.calls.some(([url]) => url.endsWith('prepare-review'))).toBe(false);
});
