import { beforeEach, describe, expect, it, vi } from 'vitest';
import axios from 'axios';
import manage from '../../pages/api/ontology/[...path]';
import knowledge from '../../pages/api/knowledge/[...path]';
import chat from '../../pages/api/chat';
vi.mock('axios', () => ({ default: { request: vi.fn() } }));
function response() {
  const res: any = {};
  for (const name of ['status', 'json', 'end', 'setHeader']) res[name] = vi.fn().mockReturnValue(res);
  return res;
}
function request(path: string[], extra = {}) {
  return { method: 'POST', headers: { authorization: 'Bearer signed' }, query: { path }, body: {}, ...extra } as any;
}
beforeEach(() => { vi.clearAllMocks(); process.env.SERVER_HOST = 'http://backend.invalid'; });
describe('P1 registry proxy', () => {
  it('requires verified admin for mutations', async () => {
    vi.mocked(axios.request).mockResolvedValue({ status: 200, data: { admin: false } });
    const res = response(); await manage(request(['changesets']), res);
    expect(res.status).toHaveBeenCalledWith(403);
    expect(axios.request).toHaveBeenCalledTimes(1);
  });
  it('preserves review conflicts and forwards the caller bearer', async () => {
    vi.mocked(axios.request).mockResolvedValueOnce({ status: 200, data: { admin: true } })
      .mockResolvedValueOnce({ status: 409, data: { error: { code: 'REVISION_CONFLICT' } } });
    const res = response(); const body = { expected_digest: 'hash', decision: 'approved', reason: 'reviewed' };
    await manage(request(['changesets', 'a'.repeat(32), 'review'], { body }), res);
    expect(res.status).toHaveBeenCalledWith(409);
    const call = vi.mocked(axios.request).mock.calls[1][0]!;
    expect(call.data).toEqual(body);
    expect(call.headers).toMatchObject({ Authorization: 'Bearer signed' });
  });
  it('does not allow arbitrary backend paths', async () => {
    const res = response(); await manage(request(['..', 'queryllm']), res);
    expect(res.status).toHaveBeenCalledWith(404);
    expect(axios.request).not.toHaveBeenCalled();
  });
  it('allows authenticated readers to search without admin privileges', async () => {
    vi.mocked(axios.request).mockResolvedValue({ status: 200, data: { release_id: 'r', candidates: [] } });
    const res = response(); await knowledge(request(['search'], { body: { question: 'sales' } }), res);
    expect(axios.request).toHaveBeenCalledTimes(1);
    expect(vi.mocked(axios.request).mock.calls[0][0]?.url).toBe('http://backend.invalid/v1/knowledge/search');
    expect(res.status).toHaveBeenCalledWith(200);
  });
  it('pins knowledge detail to a validated release query parameter', async () => {
    vi.mocked(axios.request).mockResolvedValue({ status: 200, data: {} });
    const res = response(); const release = 'a'.repeat(32);
    await knowledge(request(['entities','template:sales'], { method: 'GET', query: { path: ['entities','template:sales'], release_id: release } }), res);
    expect(vi.mocked(axios.request).mock.calls[0][0]?.url).toContain('template%3Asales?release_id=' + release);
  });
  it('rejects unauthenticated knowledge reads', async () => {
    const res = response(); await knowledge(request(['search'], { headers: {} }), res);
    expect(res.status).toHaveBeenCalledWith(401);
    expect(axios.request).not.toHaveBeenCalled();
  });
  it('does not expose management actions through the reader proxy', async () => {
    const res = response(); await knowledge(request(['releases']), res);
    expect(res.status).toHaveBeenCalledWith(404);
    expect(axios.request).not.toHaveBeenCalled();
  });
  it('forwards explicit published template parameters without asserted identity', async () => {
    vi.mocked(axios.request).mockResolvedValue({ status: 200, data: {} });
    const ontology = { release_id: 'a'.repeat(32), template_id: 'template:sales', parameters: ['上海'] };
    await chat(request([], { body: { messages: [], sourceIds: ['a'], ontology, userId: 'admin' } }), response());
    const call = vi.mocked(axios.request).mock.calls[0][0]!;
    expect(call.data).toMatchObject({ ontology });
    expect(call.data).not.toHaveProperty('userId');
  });
});
