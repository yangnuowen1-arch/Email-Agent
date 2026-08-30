"""RAG 层统一异常：由 embedding / ingest / retriever 与 cli 引用。

类型化传播约定：rag 内部错误不裸抛 Exception，调用方按类型决定
降级（如检索失败回退无知识库草稿）或中止（如入库维度不符）。
"""


class RAGError(Exception):
    """RAG 知识库层的基类错误。"""


class EmbeddingDimensionError(RAGError):
    """embedding 模型返回维度与知识库列定义不一致时抛出。

    kb_chunks.embedding 为 vector(1536) 定长列，维度不符的向量既写不进
    也比不了余弦距离；这几乎总是网关模型选错，需在写库前拦截。
    """
