import os
import json
import pandas as pd
from server import testcases
from server import conf
from server import aws, llm, api
import logging
from logging.handlers import RotatingFileHandler

logger = logging.getLogger(__name__)

# 设置日志级别
logger.setLevel(logging.INFO)
# 创建一个handler，用于写入日志文件
handler = RotatingFileHandler('logs/chatbi_test.log', maxBytes=100000, backupCount=3)
logger.addHandler(handler)


# 创建一个handler，用于将日志输出到控制台
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
logger.addHandler(console_handler)

# 定义日志格式
formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
handler.setFormatter(formatter)
console_handler.setFormatter(formatter)



# test_calculator.py
import unittest
import os

conf.load_env()
conf.load_sql_templates()

TEST_CASE_FOLDER = "server/testcases"

class TestSQL(unittest.TestCase):

    def setUp(self):
        data_files_folder = f"{os.getcwd()}/{TEST_CASE_FOLDER}"
        files_and_folders = os.listdir(data_files_folder)
        docs = [f"{data_files_folder}/{item}" for item in files_and_folders if item.endswith(".xlsx") or item.endswith(".csv")]
        print(docs)
        cases = list()
        for doc in docs:
            df = pd.read_excel(doc, sheet_name=0, header=None)
            print(len(df))
            for row_index in range(1, len(df)):
                question, sql_result = df.iloc[row_index, 0], df.iloc[row_index, 1]
                cases.append(
                    {
                        "question":question,
                        "expected":sql_result
                    }
                )
        self.cases = cases



    def test_sql(self):
        success = 0
        failed = 0
        bedrock_client = aws.get('bedrock-runtime')
        for test_case in self.cases:
            logger.info(test_case['question'])
            trace_id = "test_0101001"
            msg = [
                {
                    "role":"user",
                    "content":test_case['question']
                }
            ]
            fmt_sql = testcases.gen_sql(trace_id, msg)
            expected = test_case["expected"]

            compare = f"""期望的<sql>{expected}</sql>实际的<sql>{fmt_sql}</sql>请严格按如如下格式返回信息:
            {{
                "result":值是一个bool值,如果上述两个sql在mysql数据库查询后,返回的结果是一致,result 为True 否则为False,
                "reason":"解释原因"
            }},不要做其他任何解释"""

            msg2 = [{
                "role":"user",
                "content":compare
            }]
            
            try:
                result = llm.query(msg2, bedrock_client)
                parsed = json.loads(result)

                r = parsed["result"]
                logger.info(r)
                reason = parsed["reason"]
                self.assertEqual(r, True)
                success +=1
            except AssertionError as e:
                logger.warn(f"期望的SQL:\n{expected}")
                logger.warn(f"实际的SQL:\n{fmt_sql}")
                logger.error(reason)
                failed +=1
            except Exception as ex:
                logger.error(ex)
        logger.info(f"共运行 {success+failed} 测试用例,成功 {success}, 失败 {failed}")


if __name__ == '__main__':
    unittest.main()

