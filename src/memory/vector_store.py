import chromadb
import os
from sentence_transformers import SentenceTransformer
from typing import List, Dict, Any

print(">>> 正在加载 embedding_model... 可能会下载模型，请耐心等待...")

# 初始化嵌入模型（本地运行，轻量快速，约 80MB）
# 如果第一次运行，会自动下载模型，请稍等片刻
embedding_model = SentenceTransformer('all-MiniLM-L6-v2')

# 持久化存储路径（项目根目录下的 .memory_db）
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".memory_db")
os.makedirs(DB_PATH, exist_ok=True)

# 初始化 Chroma 客户端
chroma_client = chromadb.PersistentClient(path=DB_PATH)

# 获取或创建集合（相当于 SQL 中的表）
collection = chroma_client.get_or_create_collection(
    name="cortex_memories",
    metadata={"hnsw:space": "cosine"}  # 使用余弦相似度
)

def add_memory(text: str, metadata: Dict[str, Any] = None):
    """
    将一段文本存入向量库。
    - text: 要存储的内容（如对话摘要、笔记结论）
    - metadata: 附带信息，如时间、类型
    """
    if metadata is None:
        metadata = {}
    
    # 生成向量
    embedding = embedding_model.encode(text).tolist()
    
    # Chroma 需要唯一的 id，这里用自增计数（简易处理）
    count = collection.count()
    doc_id = f"mem_{count + 1}"
    
    collection.add(
        ids=[doc_id],
        embeddings=[embedding],
        documents=[text],
        metadatas=[metadata]
    )
    return doc_id

def search_memory(query: str, top_k: int = 3) -> List[str]:
    """
    根据查询文本，从向量库中检索最相似的记忆。
    返回最匹配的文本内容列表。
    """
    if collection.count() == 0:
        return []
    
    query_embedding = embedding_model.encode(query).tolist()
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k,
    )
    # 提取文档内容
    if results and 'documents' in results and results['documents']:
        return results['documents'][0]
    return []