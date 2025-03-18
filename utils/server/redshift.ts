import {
    ACCESS_KEY,
    DEFAULT_REGION,
  } from '../app/const';
  
  import {
    DescribeStatementCommand,
    ExecuteStatementCommand,
    ExecuteStatementCommandInput,
    GetStatementResultCommand,
    RedshiftDataClient,
  } from '@aws-sdk/client-redshift-data';

  import json2md from 'json2md';

  import { Client } from 'pg';
  
  import { REDSHIFT_USER, REDSHIFT_PASSWORD, REDSHIFT_DATABASE, REDSHIFT_HOST, REDSHIFT_PORT,REDSHIFT_SSL } from '../app/const';


const sleepFun = (ms: number) => {
  return new Promise<void>((resolve) => setTimeout(() => resolve(), ms));
};

const checkCostData = (costStr: string | undefined) => {
  const regex = /cost=([0-9]+\.[0-9]+)..([0-9]+\.[0-9]+)/;
  if (!costStr) {
    return 0;
  }
  const match = costStr.match(regex);

  if (match) {
    const costStart = parseFloat(match[1]);
    const costEnd = parseFloat(match[2]);
    return Math.max(costStart, costEnd);
  } else {
    return 0;
  }
};

export const tableDataToMdAndJson = async (
    tableData: any,
    promptConfig: any,
    bedrockResult: any,
    requestId: any
  ) => {

    try {
      const resultList = tableData
      const mdData = json2md([
        { table: { headers: bedrockResult.bedrockColumn.columnList, rows: resultList } },
      ]);

      const chartData = {
        entity_name: {} as any,
        index_value: {} as any,
      };

      // if (tableData.ColumnMetadata.length > 1) {
      if (tableData.length > 1) {
        resultList.forEach((item:Dict, index:number) => {
          let keyColumn = bedrockResult.bedrockColumn
            ? bedrockResult.bedrockColumn.columnList[0]
            : '';
            
          if (!keyColumn) {
            keyColumn =
              promptConfig[bedrockResult.queryTableName]?.KeyColumn[
                tableData.ColumnMetadata[0].tableName
              ];
            if (!keyColumn) {
              keyColumn = tableData.ColumnMetadata[0].label;
            }
          }
  
          chartData.entity_name[index] = item[keyColumn];
          if (bedrockResult.bedrockColumn) {
            const keyList = Object.keys(item);
            chartData.index_value[index] =
              keyList.length > 1
                ? item[bedrockResult.bedrockColumn.columnList[1]] ??
                  item[keyList[1]]
                : '';
          } else {
            chartData.index_value[index] =
              item[tableData.ColumnMetadata[1].label] ?? 1;
          }
        });
      }
  
      return {
        mdData,
        chartData,
      };
    } catch (error) {
      console.error('tableDataToMdAndJson error >>>', error);
      return {
        mdData: '',
        chartData: '',
      };
    }
  };
  
  
interface Dict {
    [key: string]: any;
  }


export const query = async (client:Client, querySQL: string) => {
  let result: Dict = {
    status:'100'
  };
  
  try {
    // 执行查询
    client.query(querySQL, (err, res) => {
      if (err) {
        console.error('查询失败', err.stack);
        result.status='502'
        // return "Error: 查询失败";
      } else {
        console.log('查询结果:', res.rows);
        result.status='200'
        result = {
          ...result,
          data:res.rows.toString
        }
        console.log('查询结果2:', result);
      }

    });
    }catch (error) {
        console.error('queryResultError--->', error);
        result.status='503'
    }finally{
      // if (client){
      //   client.end(); // 关闭连接
      // }
      return result
    }
}


export const searchRedshiftResult = async (querySQL: string) => {

  const config = {
    user: REDSHIFT_USER,
    password: REDSHIFT_PASSWORD,
    database: REDSHIFT_DATABASE,
    host: REDSHIFT_HOST,
    port: REDSHIFT_PORT, // Redshift 默认端口
    ssl: REDSHIFT_SSL, // 使用 SSL 连接
  };

      
  // 创建客户端实例
  const client = new Client(config);

  return new Promise((resolve, reject) => {
    client.connect((err) => {
      if (err) {
        reject(err);
        return;
      }

      client.query(querySQL, (err, res) => {
        if (err) {
          reject(err);
        } else {
          resolve(res.rows);
        }
        client.end();
      });
    });
  });
}




// 最近用户日均的关卡挑战次数、通过次数如何