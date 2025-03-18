import { HIVE_HOST, HIVE_PORT, HIVE_PWD, HIVE_USER } from '../app/const';

import fs from 'fs';

const hive = require('hive-driver');
const { TCLIService, TCLIService_types } = hive.thrift;
const client = new hive.HiveClient(TCLIService, TCLIService_types);
const utils = new hive.HiveUtils(TCLIService_types);

// 创建 Hive 连接配置
const hiveConfig = {
  host: HIVE_HOST, // EMR 主节点公共 DNS
  port: HIVE_PORT, // Hive Server2 默认端口
  username: HIVE_USER, // 默认用户名
  authMechanism: 'PLAIN', // 认证机制,也可以使用 'NOSASL' 参考https://github.com/lenchv/hive-driver/blob/HEAD/docs/readme.md#connection Connection部分
  // authorizationID: HIVE_USER, // 授权 ID,与用户名相同
  fetchSize: 1000, // 每次获取的最大行数
  // 看客户的配置 参考https://github.com/lenchv/hive-driver/blob/c59ff1fb6e7bec49eda1b707cb39d8110a5a2326/docs/connections.md
  // options: {
  //   path: '/hive',
  //   https: true,
  //   cert: fs.readFileSync('/path/to/cert.crt'),
  //   key: fs.readFileSync('/path/to/cert.key'),
  //   // in case of self-signed cert
  //   ca: fs.readFileSync('/path/to/cert.ca'),
  // },
};

// 主函数
export const queryHiveResult = async (
  querySQL: string | undefined,
  requestId: string,
) => {
  console.log('----->Prepare hive connect.');
  let hiveClient;
  try {
    // if (HIVE_PWD) {
    if (true) {
      console.log('>> Going to connect. ');
      hiveClient = await client.connect(
        hiveConfig,
        new hive.connections.TcpConnection(),
        new hive.auth.PlainTcpAuthentication({
          username: HIVE_USER,
          password: HIVE_PWD,
        }),
      );
    } else {
      hiveClient = await client.connect(
        hiveConfig,
        new hive.connections.TcpConnection(),
        new hive.auth.NoSaslAuthentication(),
      );
    }

    const session = await hiveClient.openSession({
      client_protocol:
        TCLIService_types.TProtocolVersion.HIVE_CLI_SERVICE_PROTOCOL_V10,
    });
    console.log('session', session);
    const selectDataOperation = await session.executeStatement(querySQL, {
      runAsync: true,
    });
    await utils.waitUntilReady(selectDataOperation, false, () => {});
    await utils.fetchAll(selectDataOperation);
    await selectDataOperation.close();

    const result = utils.getResult(selectDataOperation).getValue();
    await session.close();
    await hiveClient.close();
    console.log(
      '<----- Query Hive Result ----->',
      JSON.stringify(result, null, '\t'),
      requestId,
    );

    return result;
  } catch (error) {
    console.log('<----- Query Hive Error ----->', error, requestId);
  } finally {
    hiveClient && (await hiveClient.close());
  }
};
