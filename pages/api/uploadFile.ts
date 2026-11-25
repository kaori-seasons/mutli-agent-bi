import type { NextApiRequest, NextApiResponse } from 'next';
import { backendRequest, requireBearer } from '@/utils/server/backend';

// Authentication happens before receiving/parsing a multipart file. Import was a stub.
export const config = { api: { bodyParser: false } };

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') return res.status(405).end();
  if (!requireBearer(req, res)) return;
  try {
    const response = await backendRequest(req, '/upload', {});
    res.status(response.status).json(response.data);
  } catch {
    res.status(503).json({ error: { code: 'BACKEND_UNAVAILABLE', message: '服务暂时不可用' } });
  }
}
