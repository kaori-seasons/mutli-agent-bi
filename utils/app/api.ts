import axios from 'axios';

import { SERVER_HOST } from './const';


export const getEndpoint = () => {
  return 'api/chat';
};


// 创建axios实例
export const http = axios.create({
  baseURL: SERVER_HOST,
  timeout: 600000,
  headers: {'X-Custom-Header': 'text2sql'}
});