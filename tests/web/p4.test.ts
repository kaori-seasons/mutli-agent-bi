import { beforeEach, expect, it, vi } from 'vitest';
import axios from 'axios';
import planner from '../../pages/api/planner/[...path]';
import ontology from '../../pages/api/ontology/[...path]';
vi.mock('axios', () => ({ default: { request: vi.fn() } }));
function response() { const r: any = {}; for (const k of ['status','json','end','setHeader']) r[k]=vi.fn().mockReturnValue(r); return r; }
function request(path: string[], method='POST', body: unknown={}) { return {method,query:{path},headers:{authorization:'Bearer reader'},body} as any; }
beforeEach(() => { vi.clearAllMocks(); process.env.SERVER_HOST='http://backend.invalid'; });
it('routes analysis through the authenticated reader proxy', async () => {
  vi.mocked(axios.request).mockResolvedValue({status:200,data:{state:'succeeded'}});
  const intent={source_ids:['a'],metric_ids:['metric:sales']};
  const res=response(); await planner(request(['analysis','tasks'],'POST',{intent,idempotency_key:'task'}),res);
  expect(axios.request).toHaveBeenCalledTimes(1);
  expect(vi.mocked(axios.request).mock.calls[0][0]).toMatchObject({url:'http://backend.invalid/v1/analysis/tasks',data:{intent,idempotency_key:'task'}});
});
it('requires bearer identity on task and governance status reads', async () => {
  for(const path of [['analysis','tasks'],['governance','status']]) {
    const res=response();await planner({...request(path),headers:{}},res);expect(res.status).toHaveBeenCalledWith(401);
  }
  expect(axios.request).not.toHaveBeenCalled();
});
it('keeps incident transitions out of the reader tool proxy', async () => {
  const res=response();await planner(request(['governance','incidents','a'.repeat(32),'transition']),res);
  expect(res.status).toHaveBeenCalledWith(404);expect(axios.request).not.toHaveBeenCalled();
});
it('rejects non-admin governance actions before dispatch', async () => {
  vi.mocked(axios.request).mockResolvedValue({status:200,data:{admin:false}});
  const res=response();await ontology(request(['governance','runs']),res);
  expect(res.status).toHaveBeenCalledWith(403);expect(axios.request).toHaveBeenCalledTimes(1);
  expect(vi.mocked(axios.request).mock.calls[0][0]?.url).toBe('http://backend.invalid/auth/session');
});
it('authorizes admin then forwards a revision-bound incident transition', async () => {
  vi.mocked(axios.request).mockResolvedValueOnce({status:200,data:{admin:true}}).mockResolvedValueOnce({status:200,data:{state:'acknowledged'}});
  const body={expected_revision:1,action:'acknowledge',owner:'owner',reason:'investigate'};
  const res=response();await ontology(request(['governance','incidents','a'.repeat(32),'transition'],'POST',body),res);
  expect(axios.request).toHaveBeenCalledTimes(2);expect(vi.mocked(axios.request).mock.calls[1][0]?.data).toEqual(body);
});
it('rejects path traversal and preserves failed verification response', async () => {
  const res=response();await planner(request(['analysis','tasks','../admin'],'GET'),res);expect(res.status).toHaveBeenCalledWith(404);
  vi.mocked(axios.request).mockResolvedValue({status:422,data:{state:'failed',evidence:{error_code:'VERIFICATION_FAILED'}}});
  const failed=response();await planner(request(['analysis','tasks']),failed);expect(failed.status).toHaveBeenCalledWith(422);
});
