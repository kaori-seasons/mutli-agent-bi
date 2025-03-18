import { Message } from '@/types/chat';

import {
  ACCESS_KEY,
  BEDROCK_MAX_TOKEN,
  BUCKET_NAME,
  DEFAULT_REGION,
  EXAMPLE_FILE_NAME,
  MYSQL_HOST,
  MYSQL_PWD,
  MYSQL_USER,
  PROMPT_FILE_NAME,
  RAG_FILE_NAME,
  RAG_SEARCH_LENGHT,
  SECRET_ACCESS_KEY,
  DEFAULT_MODEL_NAME
} from '../app/const';
import { queryHiveResult } from './hive';
import { searchMySQLResult } from './mysql';
import {searchRedshiftResult,tableDataToMdAndJson} from './redshift'

import {
  BedrockRuntimeClient,
  InvokeModelCommand,
  InvokeModelWithResponseStreamCommand,
} from '@aws-sdk/client-bedrock-runtime';

import { GetObjectCommand, S3Client } from '@aws-sdk/client-s3';
import json2md from 'json2md';

export class BedrockError extends Error {
  type: string;
  param: string;
  code: string;

  constructor(message: string, type: string, param: string, code: string) {
    super(message);
    this.name = 'BedrockError';
    this.type = type;
    this.param = param;
    this.code = code;
  }
}

const AWS_PARAM =
  ACCESS_KEY && SECRET_ACCESS_KEY
    ? {
        region: DEFAULT_REGION,
        credentials: {
          accessKeyId: ACCESS_KEY,
          secretAccessKey: SECRET_ACCESS_KEY,
        },
      }
    : { region: DEFAULT_REGION };

// 从S3获取配置文件
const getS3PromptConfig = async () => {
  return await getS3JsonFile(PROMPT_FILE_NAME);
};

const getSQLText = (mdSQL: string | undefined) => {
  if (mdSQL && mdSQL[1] === '"' && mdSQL[2] === '"') {
    mdSQL = mdSQL.slice(4);
    mdSQL = mdSQL.slice(0, mdSQL.length - 3);
  }
  return mdSQL
    ?.replaceAll('```sql', '')
    .replaceAll('```', '')
    .replaceAll('\n', ' ')
    .replaceAll('"""SELECT', 'SELECT')
    .replaceAll(';"""', ';')
    .trim();
};

// 请求Bedrock并查询数据
export const GetServiceResult = async (
  messages: Message[],
  modelType: string | undefined,
  modelId: string | undefined,
  requestId: string,
) => {
      const promptConfig = await getS3PromptConfig();
      const isHard = modelType ? modelType.toLowerCase() === 'bedrock-hard' : false;
      // 普通模式和Hard模式查询
      const bedrockResult = await bedrockStream(
        messages,
        promptConfig,
        modelId,
        isHard,
        requestId,
      );

      console.log("bedrock result again ---")
      console.log(bedrockResult)
      const noResultText = 'ERROR: You can only read data.';
      if (
        !bedrockResult ||
        !bedrockResult.bedrockSQL ||
        bedrockResult.bedrockSQL === noResultText
      ) {
        return {
          content: '哎呀，我思路有点乱，请重新问一次，多个点提示吧！',
          mdData: '',
          chartData: '',
          sql: '',
        };
      }
      // 格式化SQL
      const sqlText = getSQLText(bedrockResult.bedrockSQL);
      console.log('<--- SQL Text ' + requestId + ' ---> ', sqlText);
      if (!sqlText) {
        return {
          content: bedrockResult.bedrockSQL,
          mdData: '',
          chartData: '',
          sql: '',
        };
      }

      // const  tableData = await searchRedshiftResult(sqlText)
      const tableData = await searchMySQLResult(sqlText, "")
      console.log(tableData);
      let dataResult = null;

      console.log('===>[tableData] ==> ',tableData)

            
      if (!tableData) {
        console.log('===>[tableData] is null ==> ',tableData)
        dataResult = { mdData: '| day | avg_challenge_count | avg_pass_count |' , chartData: '' };

        return {
          content: `'' \n\n `,
          mdData: 'Error',
          chartData: '',
          sql: bedrockResult.bedrockSQL,
          chartType: '',
        }

      } else {
        dataResult = await tableDataToMdAndJson(
          tableData,
          promptConfig,
          bedrockResult,
          requestId,
        );

        const insightResult = await bedrockInsghtStream(
          messages,
          promptConfig,
          modelId,
          bedrockResult,
          dataResult.mdData,
          requestId,
        )

        console.log('InsightResult ' + requestId + ' ---> ', insightResult);

        console.log('bedrockResult.clarify ' + requestId + ' ---> ', bedrockResult.clarify);
      
        const responeData = bedrockResult.clarify
          ?  {
            content: `${insightResult} \n\n **问题解析**  \n\n ${bedrockResult.clarify}  \n\n\n  **解析结论** \n\n ${bedrockResult.reasoningFinal}`,
            // content: `ok! \n\n `,
            mdData: dataResult.mdData,
            chartData: dataResult.chartData,
            sql: bedrockResult.bedrockSQL,
            chartType: bedrockResult.bedrockColumn.chartType || '',
          }
          : {
            content: `${insightResult} \n\n `,
            // content: `ok! \n\n `,
            mdData: dataResult.mdData,
            chartData: dataResult.chartData,
            sql: bedrockResult.bedrockSQL,
            chartType: bedrockResult.bedrockColumn.chartType || '',
          };


        console.log('responeData ' + requestId + ' ---> ', responeData);
        return responeData;

      }
};

const bedrockRespone = async (
  bodyData: string,
  bedrockruntime: { send: (arg0: InvokeModelWithResponseStreamCommand) => any },
  requestId: string,
) => {
  console.log('bodyData ' + requestId + ' ---->', bodyData);
  const command = new InvokeModelWithResponseStreamCommand({
    body: JSON.stringify({
      prompt: bodyData,
      max_tokens_to_sample: BEDROCK_MAX_TOKEN,
      temperature: 0.001, // 控制随机性或创造性，0.1-1 推荐0.3-0.5，太低没有创造性，一点输入错误/语意不明确就会产生比较大偏差，且过低不会有纠错机制
      top_p: 0.99, // 采样参数，从 tokens 里选择 k 个作为候选，然后根据它们的 likelihood scores 来采样
      top_k: 3, // 设置越大，生成的内容可能性越大；设置越小，生成的内容越固定；
    }),
    modelId: DEFAULT_MODEL_NAME,
    contentType: 'application/json',
    accept: 'application/json',
  });
  const respone = await bedrockruntime.send(command);
  if (respone.$metadata.httpStatusCode !== 200) {
    console.error('Bedrock respone error >>>', respone);
    throw new Error(`Bedrock returned an error`);
  }
  const resultData = await resolveBody(respone);
  return resultData;
};


// 格式化Bedrock返回
// 逻辑是把MarkDown格式返回的头 换行符 SQL前后包裹的"""去掉
const changeBedrockResult = (bedrockRes: string | undefined) => {
  if (!bedrockRes) {
    return '';
  }

  bedrockRes = bedrockRes.replaceAll('\n', ' ').trim();
  if (
    (bedrockRes[0] === '"' && bedrockRes[1] === '"' && bedrockRes[2] === '"') ||
    (bedrockRes[0] === '`' && bedrockRes[1] === '`' && bedrockRes[2] === '`')
  ) {
    bedrockRes = bedrockRes.substring(3);
  }
  if (
    bedrockRes[0] === 'j' &&
    bedrockRes[1] === 's' &&
    bedrockRes[2] === 'o' &&
    bedrockRes[3] === 'n'
  ) {
    bedrockRes = bedrockRes.substring(4);
  }

  if (
    (bedrockRes[bedrockRes.length - 1] === '`' &&
      bedrockRes[bedrockRes.length - 2] === '`' &&
      bedrockRes[bedrockRes.length - 3] === '`') ||
    (bedrockRes[bedrockRes.length - 1] === '"' &&
      bedrockRes[bedrockRes.length - 2] === '"' &&
      bedrockRes[bedrockRes.length - 3] === '"')
  ) {
    bedrockRes = bedrockRes.slice(0, -3);
  }
  const regex = /\"\"\"(.*?)\"\"\"/;
  let match = regex.exec(bedrockRes);
  if (!match || match.length === 0) {
    bedrockRes = bedrockRes.replaceAll(/\s+/g, ' ').replaceAll("'''", '');
    return bedrockRes;
  }
  let replaceStr = bedrockRes;
  while (match && match.length > 0) {
    let changeItem = match[0];
    changeItem = changeItem.replaceAll('"', '\\"');
    changeItem = changeItem.replaceAll('\\"\\"\\"', '"');
    replaceStr = replaceStr.replace(match[0], changeItem);
    match = regex.exec(replaceStr);
  }
  bedrockRes = bedrockRes.replaceAll(/\s+/g, ' ').replaceAll("'''", '');

  bedrockRes = bedrockRes.replaceAll("\"\"\"", '\"');

  return bedrockRes;
};

const getStandUserMsg = (userMsg: string | any[]) => {
  userMsg[userMsg.length - 1].content = userMsg[
    userMsg.length - 1
  ].content.replaceAll('"', "'");

  userMsg[userMsg.length - 1].content = userMsg[
    userMsg.length - 1
  ].content.replaceAll('“', ' ');
  userMsg[userMsg.length - 1].content = userMsg[
    userMsg.length - 1
  ].content.replaceAll('”', ' ');

  userMsg[userMsg.length - 1].content = userMsg[
    userMsg.length - 1
  ].content.replaceAll('‘', "'");
  userMsg[userMsg.length - 1].content = userMsg[
    userMsg.length - 1
  ].content.replaceAll('’', "'");
  return userMsg;
};

const bedrockClaude3Respone = async (
  bodyData: any[],
  bedrockruntime: {
    send: (
      arg0: InvokeModelWithResponseStreamCommand | InvokeModelCommand,
    ) => any;
  },
  modelId: string,
  requestId: string,
) => {
  console.log(
    '<--- Bedrock Request (Claude3) ' + requestId + ' ---->',
    bodyData,
  );
  const requestBody: { role: string; content: string }[] = [];
  
  bodyData.forEach((item: { role: string; content: string }, index: number) => {
    if (!item.content) {
      item.content = 'continue';
    }
    if (index === 0) {
      if (item.role === 'system') {
        requestBody.push({ role: 'user', content: item.content });
        bodyData[1].role === 'user' &&
          requestBody.push({ role: 'assistant', content: 'ok' });
        return;
      } else if (item.role === 'assistant') {
        requestBody.push({ role: 'user', content: 'hello' });
        requestBody.push({
          role: 'assistant',
          content: item.content || 'hello',
        });
        return;
      } else {
        requestBody.push(item);
        return;
      }
    }

    if (
      item.role === 'user' &&
      requestBody[requestBody.length - 1].role !== 'assistant'
    ) {
      requestBody.push({ role: 'assistant', content: 'continue' });
      requestBody.push(item);
      return;
    }

    if (
      item.role === 'assistant' &&
      requestBody[requestBody.length - 1].role !== 'user' &&
      index !== bodyData.length - 1
    ) {
      requestBody.push({ role: 'user', content: 'continue' });
      requestBody.push(item);
      return;
    }
    if (index > 0 && item.role === requestBody[requestBody.length - 1].role) {
      return;
    }
    // requestBody.push(item);
    requestBody.push({role:item.role,content:item.content});

  });
  const command = new InvokeModelCommand({
    body: JSON.stringify({
      anthropic_version: 'bedrock-2023-05-31',
      messages: requestBody,
      max_tokens: BEDROCK_MAX_TOKEN,
      temperature: 0.01,
      top_p: 0.999,
      top_k: 3,
    }),
    modelId:
      modelId === 'anthropic-claude3'
        ? 'anthropic.claude-3-5-sonnet-20240620-v1:0'
        : 'anthropic.claude-3-haiku-20240307-v1:0',
    contentType: 'application/json',
    accept: 'application/json',
  });
  const respone = await bedrockruntime.send(command);
  if (respone.$metadata.httpStatusCode !== 200) {
    console.error('Bedrock respone error >>>', respone);
    throw new Error(`Bedrock returned an error`);
  }

  const resultData = await resolveClaude3Body(respone.body, requestId);
  return resultData;
};

const resolveClaude3Body = async (
  responeBody: any,
  requestId: string | undefined,
) => {
  // console.log('responeBody', responeBody);
  // 解析内容
  const resultStr = responeBody.transformToString('utf8');
  const resultJson = JSON.parse(resultStr);
  console.log(
    '<--- Bedrock Respone Claude3 ' + requestId + ' ---> ',
    resultJson,
  );
  return resultJson.content[0].text;
};

const bedrockStream = async (
  messages: Message[],
  promptConfig: any,
  modelId: string | undefined,
  isHardMode: boolean,
  requestId: string,
) => {
  try {
    const bedrockruntime = new BedrockRuntimeClient(AWS_PARAM);
    // const timeStr = `数据的时间范围:2020年1月至2022年12月`;
    const lastSixMsg = messages.slice(-11);
    // const userMsg = messages.filter((item) => item.role === 'user');
    console.log(
      '<--- Bedrock Message ' + requestId + ' ---> ',
      lastSixMsg[lastSixMsg.length - 1],
    );
    lastSixMsg[lastSixMsg.length - 1].content = lastSixMsg[
      lastSixMsg.length - 1
    ].content.replaceAll('"', "'");

    lastSixMsg[lastSixMsg.length - 1].content = lastSixMsg[
      lastSixMsg.length - 1
    ].content.replaceAll('“', ' ');
    lastSixMsg[lastSixMsg.length - 1].content = lastSixMsg[
      lastSixMsg.length - 1
    ].content.replaceAll('”', ' ');

    lastSixMsg[lastSixMsg.length - 1].content = lastSixMsg[
      lastSixMsg.length - 1
    ].content.replaceAll('‘', "'");
    lastSixMsg[lastSixMsg.length - 1].content = lastSixMsg[
      lastSixMsg.length - 1
    ].content.replaceAll('’', "'");

    const tableRequestStr = `${promptConfig.Overall.ScenarioSelectionPrompt} ${
      promptConfig.Overall.AllScenariosPrompt
    } ${promptConfig.DefaultPrompt || ''}。 你要回答的问题是: {${
      lastSixMsg[lastSixMsg.length - 1].content
    }}`;

    console.log("tableReqeust String is ===>");
    console.log(tableRequestStr)

    let queryTableName = '';
    const ragStr = await getRAGSearchStr(bedrockruntime, lastSixMsg, requestId);
    if (
      modelId === 'anthropic-claude3' ||
      modelId === 'anthropic-claude3-haiku'
    ) {
      queryTableName = await bedrockClaude3Respone(
        [
          ...lastSixMsg.slice(-11, -1),
          {
            role: 'user',
            content: tableRequestStr,
          },
        ],
        bedrockruntime,
        modelId,
        requestId,
      );
    } else {
      queryTableName = await bedrockRespone(
        `\n\nHuman: ${tableRequestStr} \n\nAssistant:`,
        bedrockruntime,
        requestId,
      );
    }

    console.log("queryTableName ==>")
    console.log(queryTableName)

    if (!promptConfig[queryTableName]) {
      return {
        bedrockSQL: null,
        queryTableName,
        bedrockColumn: null,
        content: `ERROR: No Table`,
      };
    }

    console.log("==> role prompt")
    console.log(promptConfig[queryTableName].RolePrompt)
  
    const bedrockBodyStr = isHardMode
      ? // 困难模式
        `${promptConfig[queryTableName].RolePrompt} ${
          promptConfig[queryTableName].TablePrompt
        } ${promptConfig[queryTableName].IndicatorsListPrompt} ${
          promptConfig[queryTableName].OtherPrompt
        },示例输出结构 ${JSON.stringify(promptConfig.Examples)}。${ragStr}。
        ${promptConfig.ChartPrompt}。 ${
          promptConfig.HardPrompt
        } 现在你要回答的问题是: {${JSON.stringify(
          lastSixMsg[lastSixMsg.length - 1].content,
        )}} `
      : // 普通模式
        `${promptConfig[queryTableName].RolePrompt} ${
          promptConfig[queryTableName].TablePrompt
        } ${promptConfig[queryTableName].IndicatorsListPrompt} ${
          promptConfig[queryTableName].OtherPrompt
        },示例输出结构 ${JSON.stringify(promptConfig.Examples)}。${ragStr},${
          promptConfig.ChartPrompt
        } 你要回答的问题是: {${lastSixMsg[lastSixMsg.length - 1].content}}`;
    let bedrockResult = null;
    if (modelId === 'anthropic-claude3') {
      bedrockResult = await bedrockClaude3Respone(
        [
          ...lastSixMsg.slice(-11, -1),
          { role: 'user', content: bedrockBodyStr },
        ],
        bedrockruntime,
        modelId,
        requestId,
      );
    } else {
      bedrockResult = await bedrockRespone(
        `\n\nHuman: ${bedrockBodyStr} \n\nAssistant:`,
        bedrockruntime,
        requestId,
      );
    }

    bedrockResult = changeBedrockResult(bedrockResult);

    console.log('== 1> bedrockResult' + ' -----> ', bedrockResult);

    isHardMode &&
      console.log('bedrockResult ' + requestId + ' -----> ', bedrockResult);
    const isJsonTxt = isJson(bedrockResult);

    console.log('== 2> bedrockResult' + ' -----> ', bedrockResult);

    // const dataResult = JSON.parse(bedrockResult.replace('```json','').replace('```','').replaceAll('"""','"'));
    const dataResult = isJsonTxt ? JSON.parse(bedrockResult) : bedrockResult;

    console.log('== 3> bedrockResult' + ' -----> ', bedrockResult);

    isHardMode &&
      console.log('DataResult Hard Mode' + requestId + ' -----> ', dataResult);


    console.log('== 3> !isJsonTxt' + ' -----> ', !isJsonTxt);
    console.log('== 3> !dataResult.finalSQL' + ' -----> ', !dataResult.finalSQL);
    // console.log('== 3> !isJsonTxt' + ' -----> ', !isJsonTxt);

    if (
      !isJsonTxt ||
      !dataResult.finalSQL ||
      dataResult.finalSQL.includes('ERROR: You can only read data.')
    ) {
      return {
        bedrockSQL: null,
        queryTableName,
        bedrockColumn: null,
        content: '哎呀，我思路有点乱，请重新问一次，多个点提示吧！',
      };
    }
    console.log('== 4> bedrockResult' + ' -----> ', bedrockResult);
    if (
      dataResult &&
      dataResult.columnList &&
      Array.isArray(dataResult.columnList) &&
      dataResult.columnList.length > 0
    ) {
      for (let index = 0; index < dataResult.columnList.length; index++) {
        dataResult.columnList[index] =
          dataResult.columnList[index].split(' AS ').length > 1
            ? dataResult.columnList[index].split(' AS ')[1]
            : dataResult.columnList[index];
      }
    }
    console.log('== 5> bedrockResult' + ' -----> ', bedrockResult);
    return {
      bedrockSQL: isJsonTxt ? dataResult.finalSQL : dataResult,
      queryTableName,
      bedrockColumn: { ...dataResult },
      reasoningFinal: dataResult.reasoningFinal,
      clarify: dataResult.clarify,
    };
  } catch (error) {
    console.error(
      'Bedrock request or respone error ' + requestId + ' >>>',
      error,
    );
    throw new Error(`Bedrock request or respone error`);
  }
};

const isJson = (data: string | undefined | null) => {
  if (!data) return false;
  try {
    return !!JSON.parse(data);
  } catch (error) {
    return false;
  }
};

const resolveBody = async (respone: any) => {
  // 解析内容
  let resultStr = '';
  for await (const chunk of respone.body as any) {
    const buffer = Buffer.from(chunk.chunk.bytes);

    // 将 Buffer 对象转换为字符串
    const convertedString: any = buffer.toString('utf8'); // 使用 'utf8' 编码
    if (convertedString) {
      const converted = JSON.parse(convertedString);
      resultStr += converted.completion;
    }
  }
  console.log('<--- Bedrock Respone ---> ', resultStr);
  return resultStr;
};

export const getDefaultPrompt = async () => {
  return await getS3JsonFile(EXAMPLE_FILE_NAME);
};

const bedrockInsghtStream = async (
  messages: Message[],
  promptConfig: any,
  modelId: string | undefined,
  bedrockResult: any,
  tableData: any,
  requestId: string,
) => {
  if (!promptConfig.InsightPrompt) {
    return '';
  }
  const bedrockruntime = new BedrockRuntimeClient(AWS_PARAM);
  const userMsg = messages.filter((item) => item.role === 'user');
  const requestStr = `Query: {${userMsg[userMsg.length - 1].content}} ${
    promptConfig.InsightPrompt
  } 数据是 ${JSON.stringify(tableData)}`;
  modelId = modelId ?? 'anthropic-claude3';

  const insightResult = await bedrockClaude3Respone(
    [
      {
        role: 'user',
        content: requestStr,
      },
    ],
    bedrockruntime,
    modelId,
    requestId,
  );
  return insightResult;
};

const mysqlDataToMdAndJson = (
  tableData: any,
  promptConfig: any,
  bedrockResult: any,
  requestId: string,
) => {
  if (!tableData || tableData.length === 0) {
    return {
      mdData: '',
      chartData: '',
    };
  }
  try {
    const resultList = formatDateFields(tableData);
    const headersList = Object.keys(resultList[0]);
    const mdData = json2md([
      { table: { headers: headersList, rows: resultList } },
    ]);
    const chartData = {
      entity_name: {} as any,
      index_value: {} as any,
    };
    if (tableData.length > 1) {
      const columnName =
        bedrockResult.bedrockColumn.columnList.length > 2
          ? bedrockResult.bedrockColumn.columnList
              .join('-')
              .replace(
                '-' +
                  bedrockResult.bedrockColumn.columnList[
                    bedrockResult.bedrockColumn.columnList.length - 1
                  ],
                '',
              )
          : bedrockResult.bedrockColumn.columnList[0];

      const indexName =
        bedrockResult.bedrockColumn.columnList[
          bedrockResult.bedrockColumn.columnList.length - 1
        ];
      resultList.forEach((item, index) => {
        let keyColumn = bedrockResult.bedrockColumn ? columnName : '';
        if (!keyColumn) {
          keyColumn =
            promptConfig[bedrockResult.queryTableName]?.KeyColumn[
              bedrockResult.queryTableName
            ];
          if (!keyColumn) {
            keyColumn = Object.keys(resultList[0])[0];
          }
        }
        const keyList = Object.keys(item);

        chartData.entity_name[index] = item[keyColumn]
          ? ('\''+item[keyColumn]+'\'').replace('T00:00:00.000Z','')
          : ('\''+item[keyList[0]]+'\'').replace('T00:00:00.000Z','');
        if (bedrockResult.bedrockColumn) {
          chartData.index_value[index] =
            keyList.length > 1
              ? item[indexName] ?? item[keyList[keyList.length - 1]]
              : '';
        } else {
          chartData.index_value[index] =
            item[
              tableData.ColumnMetadata[tableData.ColumnMetadata.length - 1]
                .label
            ] ?? 1;
        }
      });
    }
    return {
      mdData,
      chartData,
    };
  } catch (error) {
    console.error('mysqlDataToMdAndJson error ' + requestId + ' >>>', error);
    return {
      mdData: '',
      chartData: '',
    };
  }
};

const formatDateFields = (objArr: any[]) => {
  return objArr.map((obj: { [s: string]: unknown } | ArrayLike<unknown>) => {
    const newObj: any = {};
    for (const [key, value] of Object.entries(obj)) {
      console.log('value', value);
      if (value instanceof Date) {
        if (key === 'usage_date') {
          const currentTimeInMs = value.getTime();

          // 8小时的毫秒数(8 * 60 * 60 * 1000)
          const eightHoursInMs = 8 * 60 * 60 * 1000;

          // 计算新的毫秒数(当前时间 + 8小时的毫秒数)
          const newTimeInMs = currentTimeInMs + eightHoursInMs;

          // 创建新的Date对象,时间为当前时间加8小时
          const newDate = new Date(newTimeInMs);

          newObj[key] = newDate.toISOString().slice(0, 10);
        } else {
          newObj[key] = value.toISOString().slice(0, 19).replace('T', ' ');
        }
      } else {
        newObj[key] = value ?? '';
      }
    }
    return newObj;
  });
};

const getFaissResult = (embedding: number[]) => {
  return []
};

const getSampleRAG = async (
  bedrockruntime: BedrockRuntimeClient,
  question: string,
  requestId: string,
) => {
  const command = new InvokeModelCommand({
    body: JSON.stringify({
      inputText: question,
      embeddingConfig: { 
        "outputEmbeddingLength": 256
      },
    }),
    modelId: 'amazon.titan-embed-image-v1',
    contentType: 'application/json',
    accept: '*/*',
  });
  const vectorRespone = await bedrockruntime.send(command);
  const decoder = new TextDecoder('utf-8');
  const decodedString = decoder.decode(vectorRespone.body);
  const bodyObj = JSON.parse(decodedString);
  // console.log('bodyObj embedding--->', bodyObj.embedding);
  const embeddingResults = getFaissResult(bodyObj.embedding);
  if (!embeddingResults) {
    return [];
  }
  const sampleList = await getRAGSampleList(embeddingResults);
  console.log('RAG Result List ' + requestId + '------>', sampleList);
  return sampleList;
};

const getRAGSampleList = async (embeddingResults: number[]) => {
  const sampleJson = (await getS3JsonFile('ragSampleList.json')) as any;
  if (!sampleJson || sampleJson.length === 0) {
    return [];
  }
  // console.log('sampleJson', sampleJson);
  const result: any[] = [];
  embeddingResults.forEach((item) => {
    result.push(sampleJson[item]);
  });
  return result;
};

const getS3JsonFile = async (fileName: string | undefined) => {
  if (!fileName) {
    return [];
  }
  try {
    const getObjectParams = {
      Bucket: BUCKET_NAME,
      Key: fileName,
    };
    const s3Client = new S3Client(AWS_PARAM);
    const command = new GetObjectCommand(getObjectParams);
    const fileInfo: any = await s3Client.send(command);
    let fileResultStr = '';
    for await (const chunk of fileInfo.Body as any) {
      const buffer = Buffer.from(chunk);
      // 将 Buffer 对象转换为字符串
      const convertedString: any = buffer.toString('utf8'); // 使用 'utf8' 编码
      if (convertedString) {
        fileResultStr += convertedString;
      }
    }
    console.log('====>> ')
    console.log(JSON.parse(fileResultStr))
    return JSON.parse(fileResultStr);
  } catch (error) {
    console.log('getS3JsonFile', error);
    return [];
  }
};

const getRAGSearchStr = async (
  bedrockruntime: BedrockRuntimeClient,
  userMsg: string | any[],
  requestId: string,
) => {
  const getSampleRAGResult = await getSampleRAG(
    bedrockruntime,
    userMsg[userMsg.length - 1].content,
    requestId,
  );
  const dataList = getSampleRAGResult.map((item) => {
    let result = 'question:{' + item.question + '}';
    if (item.answerSQL) {
      result += ' result SQL:{' + item.answerSQL + '}';
    }
    if (item.answerInsight) {
      result += ' hint:{' + item.answerInsight + '}';
    }
    return result;
  });
  const ragStr = `, you should follow the examples as much as possible:[${dataList.join(
    ',',
  )}]`;
  // const ragStr = '';
  return ragStr;
};

export const updateFaissIndex = async () => {
  return [];
};
