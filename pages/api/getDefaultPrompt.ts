import { NextApiRequest, NextApiResponse } from 'next';

import { getDefaultPrompt } from '@/utils/server';

const handler = async (req: NextApiRequest, res: NextApiResponse) => {
  try {
    const data = await getDefaultPrompt();
    console.log('data', data);
    res.status(200).json(data);
  } catch (error) {
    console.error(error);
    res.status(500).json({ status: 500 });
  }
};

export default handler;
