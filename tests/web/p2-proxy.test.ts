import { beforeEach, expect, it, vi } from 'vitest';
import axios from 'axios';
import manage from '../../pages/api/ontology/[...path]';
vi.mock('axios', () => ({ default: { request: vi.fn() } }));
function response() {
  const res: any = {};
  for (const name of ['status', 'json', 'end', 'setHeader']) res[name] = vi.fn().mockReturnValue(res);
  return res;
}
beforeEach(() => { vi.clearAllMocks(); process.env.SERVER_HOST = 'http://backend.invalid'; });
it('allows only the explicit discovery paths and preserves backend errors', async () => {
  for (const [method, path] of [['GET', ['discovery', 'config']], ['GET', ['discovery', 'runs']], ['POST', ['discovery', 'runs']], ['GET', ['discovery', 'runs', 'a'.repeat(32)]], ['POST', ['discovery', 'runs', 'a'.repeat(32), 'prepare-review']]] as const) {
    vi.mocked(axios.request).mockResolvedValueOnce({ status: 200, data: { admin: true } }).mockResolvedValueOnce({ status: 409, data: { error: { code: 'STALE_EVIDENCE' } } });
    const res = response();
    await manage({ method, query: { path: [...path] }, headers: { authorization: 'Bearer signed' }, body: {} } as any, res);
    expect(res.status).toHaveBeenCalledWith(409);
  }
});
it('never proxies automatic publish or arbitrary discovery SQL actions', async () => {
  for (const action of ['publish', 'execute', 'approve', '../releases']) {
    const res = response();
    await manage({ method: 'POST', query: { path: ['discovery', 'runs', 'a'.repeat(32), action] }, headers: {} } as any, res);
    expect(res.status).toHaveBeenCalledWith(404);
  }
  expect(axios.request).not.toHaveBeenCalled();
});
it('requires verified admin for discovery reads as well as writes', async () => {
  vi.mocked(axios.request).mockResolvedValue({ status: 200, data: { admin: false } });
  const res = response();
  await manage({ method: 'GET', query: { path: ['discovery', 'runs'] }, headers: { authorization: 'Bearer reader' } } as any, res);
  expect(res.status).toHaveBeenCalledWith(403); expect(axios.request).toHaveBeenCalledTimes(1);
});
