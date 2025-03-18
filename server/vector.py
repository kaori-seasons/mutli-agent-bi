import os
import faiss
import sys

# 假设 RAG_SEARCH_LENGTH 是一个环境变量或者常数
RAG_SEARCH_LENGTH = os.getenv('RAG_SEARCH_LENGTH', '1')

def get_faiss_result(embedding):
    # Titan embedding 固定长度 1536
    try:
        # 初始化一个 Flat (L2) 索引
        index = faiss.IndexFlatL2(1536)
        
        # 获取当前工作目录
        fname = os.path.join(sys.getcwd(), 'faiss.index')
        
        # 加载保存的索引
        if os.path.exists(fname):
            index_loaded = faiss.read_index(fname)
        else:
            return []
        
        # 计算 k_value，使用环境变量 RAG_SEARCH_LENGTH 或默认值 '1'
        k_value = min(index_loaded.ntotal(), int(RAG_SEARCH_LENGTH))
        
        # 执行搜索
        embedding_results, _ = index_loaded.search(embedding, k_value)
        
        # 返回检索到的标签列表
        return embedding_results.tolist()
        
    except Exception as error:
        print(f'getFaissResult Error-----> {error}', file=sys.stderr)
        return []