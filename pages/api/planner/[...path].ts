import type { NextApiRequest, NextApiResponse } from 'next';
import { backendRequest, requireBearer } from '../../../utils/server/backend';

export const config = { api: { bodyParser: { sizeLimit: '1mb' } } };

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  const parts = req.query.path;
  if (!Array.isArray(parts) || parts.some(p => typeof p !== 'string')) return res.status(404).end();
  const path = parts.join('/');
  const routes: Record<string, RegExp> = {
    POST: /^(questions\/check|relations\/plan|query-plans\/validate|queries\/execute|analysis\/tasks|governance\/status)$/,
    GET: /^(query-plans\/[0-9a-f]{32}|executions\/[0-9a-f]{32}|analysis\/tasks\/[0-9a-f]{32}|metrics\/[A-Za-z0-9_:.-]{1,160})$/,
  };
  if (!routes[req.method || '']?.test(path)) return res.status(404).end();
  if (!requireBearer(req, res)) return;
  try {
    const result = await backendRequest(req, '/v1/' + parts.map(encodeURIComponent).join('/'), req.method === 'POST' ? req.body : undefined);
    return res.status(result.status).json(result.data);
  } catch {
    return res.status(503).json({ error: { code: 'BACKEND_UNAVAILABLE', message: '受控规划服务暂时不可用' } });
  }
}
