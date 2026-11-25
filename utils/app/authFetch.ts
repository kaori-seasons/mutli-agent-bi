import { fetchAuthSession } from 'aws-amplify/auth';

export async function authFetch(url: string, options: RequestInit = {}): Promise<Response> {
  if (!url.startsWith('/api/') && !url.startsWith('api/')) throw new Error('Invalid API path');
  let token: string | undefined;
  try {
    token = (await fetchAuthSession()).tokens?.accessToken?.toString();
  } catch { /* Return the same explicit unauthenticated result as the server. */ }
  if (!token) {
    return new Response(JSON.stringify({ error: { code: 'AUTH_REQUIRED', message: '请重新登录' } }),
      { status: 401, statusText: 'Unauthorized', headers: { 'Content-Type': 'application/json' } });
  }
  const headers = new Headers(options.headers);
  headers.set('Authorization', 'Bearer ' + token);
  return fetch(url, { ...options, headers, redirect: 'error' });
}

export async function downloadResult(url: string) {
  if (!/^\/api\/downloads\/[0-9a-f]{32}$/.test(url)) throw new Error('请重新查询以生成有效下载');
  const response = await authFetch(url);
  if (!response.ok) throw new Error('文件已过期或无权下载');
  const objectUrl = URL.createObjectURL(await response.blob());
  const link = document.createElement('a');
  link.href = objectUrl;
  link.download = 'query-result.csv';
  link.click();
  setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
}
