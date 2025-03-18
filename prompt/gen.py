SHOW_SCHEMA_F = """
SHOW FULL COLUMNS FROM {0};
"""

PROMPT_F = """
表{}的列信息如下如下：
<schema>{}</schema>
这里有一些样例数据,样例数据没行的数据和前面列信息顺序一致
<samples>{}</samples>

现在用户需要使用上面信息，对用户自己提出的问题，生成SQL。

你是一个提示词专家，请基于大模型cluade sonnet 3.5的官方提示词最佳实，以帮助用户生成SQL为目标，来对每个列的进行分析：
具体要求如下：
1. 根据列的名称和注释,以及样例数据，尽量生成这个列的详细描述
2. 判断这个列是维度还是度量。如果是维度，根据样例数据，判断这个维度是否可枚举
3. 如果这个维度列可以枚举，尽量猜测每一个维度列选项的含义
4. 分析完所有列后，使用30个字以内，对这张表的业务作用，做一个描述

分析的结果以如下json 格式返回
{{
    'table': '表的名称',
    'desc':'这张表作用的业务描述',
    'columns':[
        {{
            'name':'列的名称',
            'tips':'是维度，还是时间维度，还是度量',
            'option':'如果是维度，是否可枚举，如果是度量，内容为空',
            'desc':'这个列的具体含义',
            'option_desc':'如果是维度，并且可枚举，则每一个值的含义'
        }}
    ]
}}

这里有个具体的例子：
{{
    'table': 'order_detail',
    'desc': '这张表详细介绍了用户的详细信息，包括下单用户信息，下单时间，订单处理情况等',
    'columns':[
        {{
            'name':'status',
            'tips':'维度',
            'option':'可枚举',
            'desc':'描述商品当前处理情况',
            'option_desc':'订单状态，请注意，值*MUST*只能是这些值中的一个：  'unpaid' , 'paid'、 'cancel'、 'shipping'、 'finished'， unpaid 的含义是 订单未支付、paid的含义是订单已支付、cancel的含义是订单已支付、shipping的含义是商品在运送中、finished的含义是订单已经结束，用户已经收到订单中的商品，＊MUST＊必须理解每个值的含义'
        }}
    ]
}}
*请直接返回结果，不要做任何解释，确保返回的结果能够被转化成json对象*
"""


def get_table_schema(tb_name:str, conn):
    tsql =SHOW_SCHEMA_F.format(tb_name) 

    schema = list()
    with conn.cursor() as cursor:
        cursor.execute(tsql)
        conn.commit()

        rows = cursor.fetchall()
        for row in rows:
            field = dict()
            field['Name'] = row[0]
            field['Type'] = row[1]
            field['Key'] = row[4]
            field['Comment'] = row[8]
            schema.append(field)

        return schema





def get_sample_data(table_name:str, schema:dict, conn, sample_count=20):
    sql = list()
    sql.append("SELECT")


    column_str = ','.join([row['Name'] for row in schema])
    sql = f"SELECT DISTINCT {column_str} FROM {table_name} LIMIT {sample_count}"
    print(sql)

    with conn.cursor() as cursor:
        cursor.execute(sql)
        conn.commit()

        rows = cursor.fetchall()
        samples = [row for row in rows]
        return samples

def get_prompt(table_name:str, conn):
    schema = get_table_schema(table_name, conn)
    sample_data =  get_sample_data(table_name, schema, conn)
    prompt = PROMPT_F.format(table_name, schema, sample_data)
    return prompt







