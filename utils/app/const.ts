export const DEFAULT_TEMPERATURE = parseFloat(
  process.env.NEXT_PUBLIC_DEFAULT_TEMPERATURE || '1',
);

export const DEFAULT_REGION = process.env.DEFAULT_REGION;

export const BEDROCK_MAX_TOKEN = parseInt(
  process.env.BEDROCK_MAX_TOKEN || '2015',
);

export const DEFAULT_MODEL_NAME =
  process.env.DEFAULT_MODEL_NAME || 'anthropic.claude-v2';

export const ACCESS_KEY = process.env.ACCESS_KEY || '';
export const SECRET_ACCESS_KEY = process.env.SECRET_ACCESS_KEY || '';

export const REDSHIFT_USER = process.env.REDSHIFT_USER || '';
export const REDSHIFT_PASSWORD = process.env.REDSHIFT_PASSWORD || '';
export const REDSHIFT_DATABASE = process.env.REDSHIFT_DATABASE || '';
export const REDSHIFT_HOST = process.env.REDSHIFT_HOST || '';
export const REDSHIFT_PORT = parseInt(process.env.REDSHIFT_PORT|| '5439');
export const REDSHIFT_SSL =  true;


export const SERVER_HOST = process.env.SERVER_HOST;

export const MYSQL_DATABASE = process.env.MYSQL_DATABASE;
export const MYSQL_HOST = process.env.MYSQL_HOST;
export const MYSQL_PORT = parseInt(process.env.MYSQL_PORT || '3306');
export const MYSQL_USER = process.env.MYSQL_USER;
export const MYSQL_PWD = process.env.MYSQL_PWD;

export const HIVE_HOST = process.env.HIVE_HOST;
export const HIVE_PORT = parseInt(process.env.HIVE_PORT || '10000');
export const HIVE_USER = process.env.HIVE_USER;
export const HIVE_PWD = process.env.HIVE_PWD;
export const BUCKET_NAME = process.env.BUCKET_NAME;

export const BACK_USER_POOL_ID = process.env.BACK_USER_POOL_ID;

export const BACK_USER_CLIENT_ID = process.env.BACK_USER_CLIENT_ID;

export const PROMPT_FILE_NAME = process.env.PROMPT_FILE_NAME;

export const EXAMPLE_FILE_NAME = process.env.EXAMPLE_FILE_NAME;

export const RAG_SEARCH_LENGHT = process.env.RAG_SEARCH_LENGHT;

export const RAG_FILE_NAME = process.env.RAG_FILE_NAME;

export const BACK_USER_POOL_REGION = process.env.BACK_USER_POOL_REGION;

