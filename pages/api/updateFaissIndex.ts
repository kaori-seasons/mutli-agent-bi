import { requireAdmin } from '@/utils/server/backend';
import { NextApiRequest, NextApiResponse } from 'next';

import { updateFaissIndex } from '@/utils/server';

const handler = async (req: NextApiRequest, res: NextApiResponse) => {
  try {
    if (req.method !== 'POST') { res.status(405).end(); return; }
    if (!(await requireAdmin(req, res))) return;
    const data = await updateFaissIndex();

    res.status(200).json(data);
  } catch (error) {
    console.error('API operation failed');
    res.status(500).json({ status: 500 });
  }
};

export default handler;
