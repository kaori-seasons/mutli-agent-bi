import {
  MYSQL_DATABASE,
  MYSQL_HOST,
  MYSQL_PORT,
  MYSQL_PWD,
  MYSQL_USER,
} from '../app/const';

// import mysql from 'mysql2';
import mysql from 'promise-mysql';

// 创建一个数据库连接池
const pool = mysql.createPool({
  host: MYSQL_HOST,
  user: MYSQL_USER,
  database: MYSQL_DATABASE,
  password: MYSQL_PWD,
  waitForConnections: true,
  connectionLimit: 12,
  queueLimit: 0,
  port: MYSQL_PORT,
});

export const searchMySQLResult = async (
  querySQL: string,
  requestId: string,
) => {
  if (!querySQL) {
    return [];
  }
  try {
    // 创建一个数据库连接
    // const connection = await mysql.createConnection({
    //   host: MYSQL_HOST,
    //   user: MYSQL_USER,
    //   database: MYSQL_DATABASE,
    //   password: MYSQL_PWD,
    //   port: MYSQL_PORT,
    // });
    // const results = await (await (await pool).getConnection()).query(querySQL);
    const awaitPool = await pool;
    const awaitPoolCon = await awaitPool.getConnection();
    try {
      const results = await awaitPoolCon.query(querySQL);
      awaitPoolCon.release();
      // console.log('SearchMySQLResult ' + requestId + ' ----->', results);
      return results;
    } catch (error) {
      console.error(
        'SearchMySQLResultFisrt Error ' + requestId + ' ------->',
        error,
      );
      awaitPoolCon.release();
      throw error;
    }
  } catch (error) {
    console.error('SearchMySQLResult Error ' + requestId + ' ------->', error);
    return [];
  }
};
