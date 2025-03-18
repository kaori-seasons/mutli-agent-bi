from flask import Flask,request,jsonify
from flask_restful import Resource, Api

from server import conf, llm, aws
import os
import requests

import datetime


conf.load_env(f"{os.getcwd()}/.env")

app = Flask(__name__)


@app.route('/query', methods=['POST'])
def post_endpoint():
    # 检查请求中是否有body内容
    if not request.json:
        return jsonify({'error': 'No JSON data provided'}), 400

    questions = request.json
    print(questions)
    bedrock = aws.get('bedrock-runtime', force_llm=True)
    return llm.query(questions, bedrock_client=bedrock)

if __name__ == '__main__':
    server_host = os.getenv("LLM_PROXY_SERVER","http://127.0.0.1:5020")
    index = server_host.find("://")
    server_host = server_host[index+3:]
    parts = server_host.split(":")
    port = int(parts[1])
    app.run(host="0.0.0.0", port=port)