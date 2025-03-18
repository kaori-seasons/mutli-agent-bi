import type { NextApiRequest, NextApiResponse } from 'next';

import { IncomingForm } from 'formidable';
import { createReadStream } from 'fs';
import path from 'path';
import { Readable } from 'stream';
import { SERVER_HOST } from '@/utils/app/const';


export const config = {
  api: {
    bodyParser: false,
  },
};

export default async function handler(
  req: NextApiRequest,
  res: NextApiResponse,
) {
  if (req.method === 'POST') {
    const form = new IncomingForm({
      maxFileSize: 5 * 1024 * 1024, // 5MB
    });

    const apiurl = `${SERVER_HOST}/upload`
    form.parse(req, async (err, fields, files) => {
      if (err) {
        console.error('解析文件错误:', err);
        return res.status(500).json({ error: '文件上传失败' });
      }

      const file = files.file as unknown as any;
      console.log('file', file);
      if (!file) {
        return res.status(400).json({ error: '没有文件被上传' });
      }

      try {
        // 读取文件内容到内存
        const fileContent = await readFileContent(file[0]);
        console.log('文件内容:', fileContent.slice(0, 100) + '...'); // 仅打印前100个字符
        // 例如：解析 Excel 或 CSV 文件

        // Todo：在这里处理文件内容 比如上传
        const response = await fetch(apiurl, {
          method: 'POST',
          headers: {}, // 如果您需要加一些自定义头
          body: fileContent, // 文件流
        });
        if (!response.ok) {
          // http状态码非200
          res.status(500).json({ error: '文件处理失败' });
          return;
        }
        const result = await response.json(); // 如果您返回的是JSON content-type也要是json 这么解析
        console.log('result.code', result.code); // 假如返回的对象里有个属性叫code
        // 返回成功响应
        res.status(200).json({
          message: '文件上传并处理成功',
          filename: file.originalFilename,
          size: file.size,
        });
      } catch (error) {
        console.error('处理文件错误:', error);
        res.status(500).json({ error: '文件处理失败' });
      }
    });
  } else {
    res.setHeader('Allow', ['POST']);
    res.status(405).end(`Method ${req.method} Not Allowed`);
  }
}

// 辅助函数：读取文件内容
function readFileContent(file: any): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: any[] = [];
    let stream;

    if (typeof file === 'string') {
      // 如果 file 是字符串（文件路径）
      stream = createReadStream(file);
    } else if (file && file.filepath) {
      // 如果 file 是一个对象并且有 filepath 属性
      stream = createReadStream(file.filepath);
    } else if (file && file.path) {
      // 某些版本的 Formidable 使用 path 而不是 filepath
      stream = createReadStream(file.path);
    } else if (file && file.buffer) {
      // 如果 file 是一个包含 buffer 的对象
      stream = Readable.from(file.buffer);
    } else {
      // 如果无法识别 file 的格式
      return reject(new Error('Invalid file format'));
    }

    stream.on('data', (chunk) => chunks.push(Buffer.from(chunk)));
    stream.on('error', (err) => reject(err));
    stream.on('end', () => resolve(Buffer.concat(chunks).toString('utf-8')));
  });
}
