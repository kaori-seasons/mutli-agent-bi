import { requireAdmin } from '@/utils/server/backend';
import { NextApiRequest, NextApiResponse } from 'next';

import { getDefaultPrompt } from '@/utils/server';

const handler = async (req: NextApiRequest, res: NextApiResponse) => {
  if (req.method !== 'GET') { res.status(405).end(); return; }
  if (!(await requireAdmin(req, res))) return;
  try {
    const data = await getDefaultPrompt();

    res.status(200).json(data);
  } catch (error) {
    console.error('API operation failed');
    res.status(500).json({ status: 500 });
  }
};

export default handler;
