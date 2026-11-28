import { beforeEach, expect, it, vi } from 'vitest';
import axios from 'axios';
import planner from '../../pages/api/planner/[...path]';
import chat from '../../pages/api/chat';
vi.mock('axios', () => ({ default: { request: vi.fn() } }));
function response() {
  const result: any = {};
  for (const key of ['status', 'json', 'end', 'setHeader']) result[key] = vi.fn().mockReturnValue(result);
  return result;
}
function request(path: string[], method = 'POST', body: unknown = {}) {
  return { method, query: { path }, headers: { authorization: 'Bearer reader' }, body } as any;
}
beforeEach(() => { vi.clearAllMocks(); process.env.SERVER_HOST = 'http://backend.invalid'; });

it('requires authentication before plan validation', async () => {
  const res = response(); await planner({ ...request(['query-plans', 'validate']), headers: {} }, res);
  expect(res.status).toHaveBeenCalledWith(401); expect(axios.request).not.toHaveBeenCalled();
});
it('forwards a structured intent to the reader endpoint without admin elevation', async () => {
  vi.mocked(axios.request).mockResolvedValue({ status: 201, data: { plan_id: 'plan' } });
  const intent = { source_ids: ['a'], metric_ids: ['sales'] };
  const res = response(); await planner(request(['query-plans', 'validate'], 'POST', { intent }), res);
  expect(axios.request).toHaveBeenCalledTimes(1);
  expect(vi.mocked(axios.request).mock.calls[0][0]).toMatchObject({ url: 'http://backend.invalid/v1/query-plans/validate', headers: { Authorization: 'Bearer reader' }, data: { intent } });
  expect(res.status).toHaveBeenCalledWith(201);
});
it('preserves policy and stale-binding errors without fallback calls', async () => {
  for (const status of [403, 409, 422]) {
    vi.mocked(axios.request).mockResolvedValue({ status, data: { error: { code: 'STALE_BINDING' } } });
    const res = response(); await planner(request(['queries', 'execute']), res);
    expect(res.status).toHaveBeenCalledWith(status);
  }
  expect(axios.request).toHaveBeenCalledTimes(3);
});
it('does not expose management or arbitrary SQL routes', async () => {
  for (const path of [['ontology', 'releases'], ['query-plans', 'publish'], ['queries', 'sql'], ['..', 'queryllm']]) {
    const res = response(); await planner(request(path), res); expect(res.status).toHaveBeenCalledWith(404);
  }
  expect(axios.request).not.toHaveBeenCalled();
});
it('requires exact receipt ID syntax on read endpoints', async () => {
  const res = response(); await planner(request(['executions', 'bad-id'], 'GET'), res);
  expect(res.status).toHaveBeenCalledWith(404); expect(axios.request).not.toHaveBeenCalled();
});
it('allows only the documented read routes and escapes metric IDs', async () => {
  vi.mocked(axios.request).mockResolvedValue({ status: 200, data: {} });
  for (const path of [['executions', 'a'.repeat(32)], ['query-plans', 'b'.repeat(32)], ['metrics', 'metric:sales']]) {
    const res = response(); await planner(request(path, 'GET'), res); expect(res.status).toHaveBeenCalledWith(200);
  }
  expect(vi.mocked(axios.request).mock.calls[2][0]?.url).toContain('metric%3Asales');
});
it('forwards explicit plan execution through the compatible chat envelope', async () => {
  vi.mocked(axios.request).mockResolvedValue({ status: 200, data: { status: 'success' } });
  const ontology = { plan_id: 'a'.repeat(32), idempotency_key: 'click-1' };
  await chat(request([], 'POST', { messages: [{ role: 'user', content: 'sales' }], sourceIds: ['a'], ontology, userId: 'admin' }), response());
  const call = vi.mocked(axios.request).mock.calls[0][0];
  expect(call?.data).toMatchObject({ ontology }); expect(call?.data).not.toHaveProperty('userId');
});
it('reports transport failures without returning synthetic query results', async () => {
  vi.mocked(axios.request).mockRejectedValue(new Error('offline'));
  const res = response(); await planner(request(['questions', 'check']), res);
  expect(res.status).toHaveBeenCalledWith(503);
  expect(res.json).toHaveBeenCalledWith({ error: { code: 'BACKEND_UNAVAILABLE', message: '受控规划服务暂时不可用' } });
});
