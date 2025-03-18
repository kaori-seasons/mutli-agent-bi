from . import aws
from . import prompt
from . import sql
from . import api
from . import llm

def gen_sql(trace_id:str, msg:list):
    bedrock = aws.get('bedrock-runtime')
    bedrock_result = api.answer_template_sql(bedrock, msg, trace_id)
    if "error" in bedrock_result:

        prompt_content = prompt.get("PROMPT_FILE_NAME")
        is_hard = True
        bedrock_result =  api.answer(bedrock, msg, prompt_content, trace_id, is_hard)


    fmt_sql = sql.format_md(bedrock_result['bedrockSQL'])
    return fmt_sql