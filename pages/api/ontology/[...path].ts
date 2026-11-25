import type { NextApiRequest, NextApiResponse } from 'next';
import { ontologyProxy } from '@/utils/server/ontologyProxy';
export const config = { api: { bodyParser: { sizeLimit: '8mb' } } };
export default function handler(req: NextApiRequest, res: NextApiResponse) {
  return ontologyProxy(req, res, 'ontology');
}
