import boto3  
import csv
import io
from datetime import datetime, timedelta
import os
import threading
import time


def get(service_name:str, force_llm:bool=False):

    if service_name == "bedrock-runtime" and not force_llm:
        proxy_server =  os.getenv("LLM_PROXY_SERVER", "")
        if proxy_server:
            print(f"begin use proxy server:{proxy_server}")
            return {
                    'proxy_server':proxy_server
                } 
    
    is_dev =bool(os.getenv('DEV_MODEL'))
    if service_name == 's3' and is_dev:
        client = boto3.client(
            service_name,
            region_name = os.getenv('S3_REGION')
        )
        return client 

    if 'ACCESS_KEY' in os.environ and 'SECRET_ACCESS_KEY' in os.environ and len(os.environ['ACCESS_KEY']) == 20:              
       return boto3.client(
            service_name,
            region_name = os.getenv('DEFAULT_REGION'),
            aws_access_key_id=os.getenv('ACCESS_KEY'),
            aws_secret_access_key=os.getenv('SECRET_ACCESS_KEY')
       )

    return boto3.client(
            service_name,
            region_name = os.getenv('DEFAULT_REGION')
        )



  
def upload_file_to_s3(file_name, bucket_name, object_name=None):  
    """  
    上传文件到S3  
  
    :param file_name: 要上传的文件的路径  
    :param bucket_name: S3桶的名称  
    :param object_name: 上传到S3的对象名称。如果未指定，则使用文件名  
    :return: None  
    """  
    # 如果S3对象名称未指定，使用文件名  
    if object_name is None:  
        object_name = file_name  
  
    # 创建S3资源  
    s3_client = get("s3")

    try:  
        # 上传文件  
        s3_client.upload_file(file_name, bucket_name, object_name)  
        print(f"文件 {file_name} 已成功上传到S3桶 {bucket_name} 中，对象名称为 {object_name}")  
    except FileNotFoundError:  
        print(f"未找到文件 {file_name}")  
    except Exception as e:  
        print(f"上传文件时出错：{e}")


def upload_csv_to_s3(headers, db_results, bucket_name, file_name):
    """  
    上传csvs数据到S3  
  
    :param headers: csv的header信息
    :param db_results: csv的数据信息，数据信息如下：
    
        {
            "rows":"数据库查出来的行信息",
            "row_count":"数据的描述信息"
        }
    :param bucket_name: S3桶的名称  
    :param object_name: 上传到S3的对象名称。如果未指定，则使用文件名  
    :return: None  
    """  
    # 创建一个S3客户端
    s3_client = get("s3")

    # 将数据写入CSV文件
    output = io.StringIO()
    writer = csv.writer(output)
    
    # 写入列名
    # 写入行数据
    writer.writerow(headers)
    rows = db_results["rows"]
    for row in rows:
        items = [str(item) for item in row]
        writer.writerow(items)
    
    # 获取CSV内容
    csv_content = output.getvalue()
    output.close()

    # 将CSV内容转换为字节串
    csv_bytes = csv_content.encode('utf-8-sig')


    # 上传到S3
    
    now = datetime.now()

    # 格式化日期为 "YYYY-MM-DD" 格式
    formatted_date = now.strftime("%Y-%m-%d")
    s3_key = f"data/download/{formatted_date}/{file_name}.csv"
    try:
        s3_client.put_object(Bucket=bucket_name, Key=s3_key, Body=csv_bytes)
        print(f"文件已上传至 {bucket_name}/{s3_key}")
    except Exception as e:
        print(f"上传文件{bucket_name}/{s3_key}到s3错误：{e}")
        return None

    # 生成预签名URL
    try:
        expiration = datetime.now() + timedelta(minutes=10)  # URL过期时间
        presigned_url = s3_client.generate_presigned_url('get_object',
                                                         Params={'Bucket': bucket_name, 'Key': s3_key},
                                                         ExpiresIn=10*60)  # URL有效时间10分钟
        print(f"预签名URL: {presigned_url}")
        return presigned_url
    except Exception as e:
        print(f"生成预签名错误：{e}")
        return None

def delete_file(file_path, delay_seconds=120):
    """删除指定的文件，延迟指定的秒数后执行删除操作"""
    def _delete():
        time.sleep(delay_seconds)
        try:
            os.remove(file_path)
            print(f"File {file_path} has been deleted after {delay_seconds} seconds.")
        except OSError as e:
            print(f"Error: {file_path} : {e.strerror}")

    delete_thread = threading.Thread(target=_delete)
    delete_thread.start()

def save_2_local(headers, db_results, file_name):
     # 将数据写入CSV文件
    output = io.StringIO()
    writer = csv.writer(output)
    
    # 写入列名
    # 写入行数据
    writer.writerow(headers)
    rows = db_results["rows"]
    for row in rows:
        items = [str(item) for item in row]
        writer.writerow(items)
    
    # 获取CSV内容
    csv_content = output.getvalue()
    output.close()

    # 将CSV内容转换为字节串
    csv_bytes = csv_content.encode('utf-8-sig')

    now = datetime.now()

    # 格式化日期为 "YYYY-MM-DD" 格式
    formatted_date = now.strftime("%Y-%m-%d")
    download =os.getenv("DOWNLOADS")
    file_path =f"{download}/{formatted_date}/{file_name}.csv"

    directory = os.path.dirname(file_path)
    if not os.path.exists(directory):
        # 如果目录不存在，则创建目录
        os.makedirs(directory)
    with open(file_path, 'wb') as file:  # 'wb' 模式表示写入二进制数据
        file.write(csv_bytes)

    delete_file(file_path)

    server_host = os.getenv("SERVER_HOST","http://127.0.0.1:5020")
    index = server_host.find("://")
    server_host = server_host[index+3:]
    parts = server_host.split(":")
    host = parts[0]
    download_host =os.getenv("DOWNLOAD_HOST")
    if download_host.startswith("http"):
        return f"{download_host}/{formatted_date}/{file_name}.csv"
    else:
        return f"http://{download_host}/{formatted_date}/{file_name}.csv"
