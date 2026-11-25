import type { NextApiRequest, NextApiResponse } from 'next';
import { backendRequest, requireBearer } from '@/utils/server/backend';

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'GET') return res.status(405).end();
  if (!requireBearer(req, res)) return;
  if (typeof req.query.id !== 'string' || !/^[0-9a-f]{32}$/.test(req.query.id)) {
    return res.status(404).end();
  }
  try {
    const response = await backendRequest(req, '/downloads/' + req.query.id, undefined, true);
    res.setHeader('Content-Type', response.status === 200 ? 'text/csv; charset=utf-8' : 'application/json');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    if (response.status === 200) res.setHeader('Content-Disposition', 'attachment; filename="query-result.csv"');
    res.status(response.status).send(response.data);
  } catch {
    res.status(503).json({ error: { code: 'BACKEND_UNAVAILABLE', message: '下载服务暂时不可用' } });
  }
}
