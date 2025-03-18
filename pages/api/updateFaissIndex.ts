import { NextApiRequest, NextApiResponse } from 'next';

import { updateFaissIndex } from '@/utils/server';

const handler = async (req: NextApiRequest, res: NextApiResponse) => {
  try {
    if (
      !req.query ||
      !req.query.key ||
      req.query.key !== 'B658BD25-F470-889F-9CD5-0E5FDF7C2DE3'
    ) {
      res.status(401).json({ status: 401 });
      return;
    }
    const data = await updateFaissIndex();
    console.log('updateFaissIndex Result---->', data);
    res.status(200).json(data);
  } catch (error) {
    console.error(error);
    res.status(500).json({ status: 500 });
  }
};

export default handler;
