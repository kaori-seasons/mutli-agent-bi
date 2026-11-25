import { beforeEach, describe, expect, it, vi } from 'vitest';
import axios from 'axios';
import { fetchAuthSession } from 'aws-amplify/auth';
import chat from '../../pages/api/chat';
import download from '../../pages/api/downloads/[id]';
import updateIndex from '../../pages/api/updateFaissIndex';
import defaultPrompt from '../../pages/api/getDefaultPrompt';
import { backendRequest } from '../../utils/server/backend';
import { authFetch } from '../../utils/app/authFetch';
import { getDefaultPrompt, updateFaissIndex } from '../../utils/server';

vi.mock('axios', () => ({ default: { request: vi.fn() } }));
vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: vi.fn() }));
vi.mock('../../utils/server', () => ({ getDefaultPrompt: vi.fn(), updateFaissIndex: vi.fn() }));

function response() {
  const res: any = {};
  for (const method of ['status', 'json', 'send', 'end', 'setHeader']) {
    res[method] = vi.fn().mockReturnValue(res);
  }
  return res;
}

function request(extra = {}) {
  return { method: 'POST', headers: { authorization: 'Bearer signed-token', 'x-user-id': 'admin' },
    body: { userId: 'admin', messages: [{ role: 'user', content: 'sales' }], sourceIds: ['a'] },
    query: {}, ...extra } as any;
}

beforeEach(() => {
  vi.clearAllMocks();
  process.env.SERVER_HOST = 'http://backend.invalid:5020';
});

describe('authenticated proxy', () => {
  it('rejects missing bearer before contacting backend', async () => {
    const res = response();
    await chat(request({ headers: { 'x-user-id': 'admin' } }), res);
    expect(res.status).toHaveBeenCalledWith(401);
    expect(axios.request).not.toHaveBeenCalled();
  });

  it('forwards bearer and selected sources, never asserted user identity', async () => {
    vi.mocked(axios.request).mockResolvedValue({ status: 200, data: { mdData: '|ok|' } });
    const res = response();
    await chat(request(), res);
    const call = vi.mocked(axios.request).mock.calls[0][0]!;
    expect(call.headers).toEqual({ Authorization: 'Bearer signed-token', 'Content-Type': 'application/json' });
    expect(call.data).not.toHaveProperty('userId');
    expect(call.data).toMatchObject({ source_ids: ['a'] });
    expect(call.maxRedirects).toBe(0);
    expect(res.status).toHaveBeenCalledWith(200);
  });

  it('preserves backend authentication and policy errors', async () => {
    vi.mocked(axios.request).mockResolvedValue({ status: 403, data: { error: { code: 'FORBIDDEN_SCOPE' } } });
    const res = response();
    await chat(request(), res);
    expect(res.status).toHaveBeenCalledWith(403);
    expect(res.json).toHaveBeenCalledWith({ error: { code: 'FORBIDDEN_SCOPE' } });
  });

  it('does not leak credentials from transport exceptions', async () => {
    vi.mocked(axios.request).mockRejectedValue(new Error('Authorization: Bearer secret'));
    const res = response();
    await chat(request(), res);
    expect(res.status).toHaveBeenCalledWith(503);
    expect(JSON.stringify(res.json.mock.calls)).not.toContain('secret');
  });

  it('rejects non-POST query requests', async () => {
    const res = response();
    await chat(request({ method: 'GET' }), res);
    expect(res.status).toHaveBeenCalledWith(405);
  });

  it('requires verified admin for global prompt assets', async () => {
    vi.mocked(axios.request).mockResolvedValue({ status: 200, data: { admin: false } });
    const res = response();
    await defaultPrompt(request({ method: 'GET' }), res);
    expect(res.status).toHaveBeenCalledWith(403);
    expect(getDefaultPrompt).not.toHaveBeenCalled();
  });

  it('ignores the former static index key', async () => {
    const res = response();
    await updateIndex(request({ headers: {}, query: { key: 'B658BD25-F470-889F-9CD5-0E5FDF7C2DE3' } }), res);
    expect(res.status).toHaveBeenCalledWith(401);
    expect(updateFaissIndex).not.toHaveBeenCalled();
  });

  it('permits admin index update after backend validation', async () => {
    vi.mocked(axios.request).mockResolvedValue({ status: 200, data: { admin: true } });
    vi.mocked(updateFaissIndex).mockResolvedValue({} as any);
    const res = response();
    await updateIndex(request(), res);
    expect(updateFaissIndex).toHaveBeenCalledOnce();
    expect(res.status).toHaveBeenCalledWith(200);
  });

  it('downloads only validated opaque IDs through an authenticated proxy', async () => {
    vi.mocked(axios.request).mockResolvedValue({ status: 200, data: Buffer.from('value\n10') });
    const res = response();
    await download(request({ method: 'GET', query: { id: 'a'.repeat(32) } }), res);
    expect(res.setHeader).toHaveBeenCalledWith('Cache-Control', 'no-store');
    expect(res.setHeader).toHaveBeenCalledWith('Content-Disposition', 'attachment; filename="query-result.csv"');
    expect(vi.mocked(axios.request).mock.calls[0][0]?.responseType).toBe('arraybuffer');
    vi.clearAllMocks();
    await download(request({ method: 'GET', query: { id: '../.env' } }), response());
    expect(axios.request).not.toHaveBeenCalled();
  });
});

describe('browser identity propagation', () => {
  it('uses the current access token, not an ID token or username', async () => {
    vi.mocked(fetchAuthSession).mockResolvedValue({ tokens: { accessToken: { toString: () => 'access' } } } as any);
    const mockFetch = vi.fn().mockResolvedValue(new Response('{}'));
    vi.stubGlobal('fetch', mockFetch);
    await authFetch('/api/chat', { method: 'POST' });
    expect(mockFetch.mock.calls[0][1].headers.get('Authorization')).toBe('Bearer access');
    expect(mockFetch.mock.calls[0][1].redirect).toBe('error');
  });

  it('returns an explicit login error without making an unauthenticated request', async () => {
    vi.mocked(fetchAuthSession).mockRejectedValue(new Error('session expired'));
    const mockFetch = vi.fn();
    vi.stubGlobal('fetch', mockFetch);
    expect((await authFetch('/api/chat')).status).toBe(401);
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('never sends an access token to an external download URL', async () => {
    await expect(authFetch('https://attacker.invalid')).rejects.toThrow();
    expect(fetchAuthSession).not.toHaveBeenCalled();
  });
});
