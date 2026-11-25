import type { NextApiRequest, NextApiResponse } from 'next';
import axios from 'axios';

export function bearer(req: NextApiRequest): string | undefined {
  const value = req.headers.authorization;
  return typeof value === 'string' && /^Bearer [^\s]+$/.test(value) && value.length <= 16400
    ? value : undefined;
}

export async function backendRequest(req: NextApiRequest, path: string, data?: unknown, binary = false) {
  const base = process.env.SERVER_HOST;
  if (!base || !/^https?:\/\//.test(base)) throw new Error('Backend not configured');
  return axios.request({
    url: base.replace(/\/$/, '') + path,
    method: data === undefined ? 'GET' : 'POST',
    data,
    headers: { Authorization: bearer(req), 'Content-Type': 'application/json' },
    timeout: 65000,
    maxRedirects: 0,
    maxContentLength: 20 * 1024 * 1024,
    responseType: binary ? 'arraybuffer' : 'json',
    validateStatus: () => true,
  });
}

export function requireBearer(req: NextApiRequest, res: NextApiResponse): boolean {
  res.setHeader('Cache-Control', 'no-store');
  if (!bearer(req)) {
    res.status(401).json({ error: { code: 'AUTH_REQUIRED', message: '请先登录' } });
    return false;
  }
  return true;
}

export async function requireAdmin(req: NextApiRequest, res: NextApiResponse): Promise<boolean> {
  if (!requireBearer(req, res)) return false;
  try {
    const response = await backendRequest(req, '/auth/session');
    if (response.status !== 200) {
      res.status(response.status).json(response.data);
      return false;
    }
    if (response.data.admin !== true) {
      res.status(403).json({ error: { code: 'FORBIDDEN', message: '此操作需要管理员权限' } });
      return false;
    }
    return true;
  } catch {
    res.status(503).json({ error: { code: 'BACKEND_UNAVAILABLE', message: '服务暂时不可用' } });
    return false;
  }
}
