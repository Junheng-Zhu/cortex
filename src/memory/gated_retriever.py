import numpy as np
from .vector_store import embedding_model, search_memory, add_memory

# 保存最近几轮对话的缓存（用于快速判断是否相关）
RECENT_HISTORY_CACHE = []

def should_retrieve(query: str, threshold: float = 0.65) -> bool:
    """
    门控函数：判断当前 query 是否需要检索记忆。
    原理：将 query 与最近 3 条对话摘要做余弦相似度比较。
    如果相似度低于阈值，说明是新话题，不需要检索（直接走工具/闲聊）。
    如果相似度高于阈值，说明是延续性话题，需要检索。
    """
    global RECENT_HISTORY_CACHE
    
    # 如果还没有任何历史记忆缓存，或者缓存少于 3 条，强制不检索（避免冷启动误判）
    if len(RECENT_HISTORY_CACHE) < 3:
        # 但如果 query 明显在问“之前”或“回忆”，还是检索一下
        if any(keyword in query for keyword in ["之前", "刚才", "刚才说的", "回忆", "上次", "笔记里"]):
            return True
        return False
    
    # 将 query 转为向量
    query_vec = embedding_model.encode(query)
    
    # 计算与最近 3 条缓存的平均相似度
    similarities = []
    for cached_text in RECENT_HISTORY_CACHE[-3:]:
        cached_vec = embedding_model.encode(cached_text)
        # 余弦相似度
        sim = np.dot(query_vec, cached_vec) / (np.linalg.norm(query_vec) * np.linalg.norm(cached_vec))
        similarities.append(sim)
    
    avg_sim = np.mean(similarities)
    print(f"[DEBUG] 门控相似度: {avg_sim:.3f} (阈值 {threshold})")
    
    return avg_sim > threshold

def remember_interaction(user_input: str, assistant_reply: str):
    """
    将一次完整的问答交互存入记忆库，并更新对话缓存。
    """
    global RECENT_HISTORY_CACHE
    
    # 只存储重要的交互（长度大于 5 个字，或者包含特定关键词）
    if len(assistant_reply) > 10 and ("笔记" in assistant_reply or "文件" in assistant_reply or "我帮你" in assistant_reply):
        summary = f"用户问: {user_input}，助手答: {assistant_reply[:50]}..."
        add_memory(summary, metadata={"type": "dialogue"})
        RECENT_HISTORY_CACHE.append(user_input)
        # 保持缓存不超过 20 条
        if len(RECENT_HISTORY_CACHE) > 20:
            RECENT_HISTORY_CACHE.pop(0)