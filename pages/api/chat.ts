import type { NextApiRequest, NextApiResponse } from 'next';
import { backendRequest, requireBearer } from '@/utils/server/backend';

export const config = { api: { bodyParser: { sizeLimit: '1mb' } } };

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).end();
  }
  if (!requireBearer(req, res)) return;
  try {
    // Identity is derived by Flask from the verified access token, never from userId.
    const { messages, sourceIds, ontology } = req.body || {};
    const response = await backendRequest(req, '/queryllm', { messages, source_ids: sourceIds, ontology });
    res.status(response.status).json(response.data);
  } catch {
    res.status(503).json({ error: { code: 'BACKEND_UNAVAILABLE', message: '查询服务暂时不可用' } });
  }
}
