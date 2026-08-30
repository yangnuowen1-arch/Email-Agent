"""ORM 模型定义：与数据库表结构 1:1 映射。

所有 SQLAlchemy 声明式模型集中在此文件，作为唯一的模型来源。
模型字段与约束严格对齐 docs/db-schema.md。
"""

from __future__ import annotations

from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, validates

from app.schemas.analysis import (
    ALL_INTENTS,
    EVIDENCE_BODY,
    EVIDENCE_SOURCES,
    PRIORITIES,
    SENTIMENTS,
    UNKNOWN_INTENT,
)
from app.schemas.knowledge import (
    ALL_KB_SOURCE_TYPES,
    ALL_KB_STATUSES,
    ALL_KB_TYPES,
    KB_EMBEDDING_DIMENSIONS,
    KB_SOURCE_TYPE_TEXT,
    KB_STATUS_ACTIVE,
)


class Base(DeclarativeBase):
    """所有 ORM 模型的声明式基类。"""

    # 统一元数据，便于集中管理表结构与迁移
    metadata = MetaData()


# 当前支持的邮件协议集合，新增协议时在此扩展，factory 会据此路由
_SUPPORTED_PROTOCOLS = {"imap"}


def _now_utc() -> datetime:
    """返回当前 UTC 时间，作为 fetched_at 的默认工厂。"""
    # 使用 UTC 时区，避免服务器本地时区差异导致的时间混乱
    return datetime.now(UTC)


class Account(Base):
    """邮箱账号 ORM 模型，对应数据库 email_accounts 表的一行。

    既是领域契约（供 parser/client/service/cli 使用），也是持久化实体：
    通过 Session 查询即返回该对象，跨线程传值时因 expire_on_commit=False
    可直接读取已加载的标量字段（如 host/port/username 等）。
    """

    __tablename__ = "email_accounts"

    # 主键，自增 ID，外键关联 emails.account_id
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 账号别名，用于日志和汇总报告展示，便于运维识别
    name: Mapped[str] = mapped_column(String)
    # IMAP 服务器地址
    host: Mapped[str] = mapped_column(String)
    # 服务端口，SSL 场景默认 993
    port: Mapped[int] = mapped_column(Integer, default=993)
    # 协议标识，当前仅支持 imap，factory 据此创建具体 MailClient
    protocol: Mapped[str] = mapped_column(String, default="imap")
    # 登录用户名，通常为完整邮箱地址
    username: Mapped[str] = mapped_column(String)
    # 登录密码/授权码，禁止写入任何日志
    password: Mapped[str] = mapped_column(String)
    # 是否启用 SSL/TLS 连接
    use_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    # 要拉取的邮箱文件夹，默认收件箱
    folder: Mapped[str] = mapped_column(String, default="INBOX")
    # 软开关：FALSE 的账号不会被调度
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 增量断点：已成功入库的最大 UID，0 表示从未同步（首跑全量）
    last_sync_uid: Mapped[int] = mapped_column(BigInteger, default=0)
    # 最近一次成功同步时间，仅运维观测用
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __init__(
        self,
        *,
        id: int | None = None,
        name: str,
        host: str,
        username: str,
        password: str,
        port: int = 993,
        protocol: str = "imap",
        use_ssl: bool = True,
        folder: str = "INBOX",
        enabled: bool = True,
        last_sync_uid: int = 0,
        last_sync_at: datetime | None = None,
    ) -> None:
        """构造后校验：确保所有字段满足业务约束（与表默认值保持一致）。"""
        # id 必须为正整数，数据库自增主键不可能为 0 或负数
        if id is not None and (not isinstance(id, int) or id <= 0):
            msg = f"id must be positive int, got {id!r}"
            raise ValueError(msg)
        # 别名和服务器地址不能为空，否则无法建立连接和展示日志
        if not name:
            raise ValueError("name must be non-empty")
        if not host:
            raise ValueError("host must be non-empty")
        # 端口必须在 TCP 合法范围内 1~65535
        if not isinstance(port, int) or not 1 <= port <= 65535:
            msg = f"port must be int in 1..65535, got {port!r}"
            raise ValueError(msg)
        # 协议必须在支持列表中，否则 factory 无法创建客户端
        if protocol not in _SUPPORTED_PROTOCOLS:
            msg = f"protocol must be one of {sorted(_SUPPORTED_PROTOCOLS)}, got {protocol!r}"
            raise ValueError(msg)
        # 登录凭证不能为空
        if not username:
            raise ValueError("username must be non-empty")
        if not password:
            raise ValueError("password must be non-empty")
        # 断点 UID 必须为非负整数，0 代表从未同步
        if not isinstance(last_sync_uid, int) or last_sync_uid < 0:
            msg = f"last_sync_uid must be int >=0, got {last_sync_uid!r}"
            raise ValueError(msg)
        # 文件夹不能为空，IMAP 拉取时需要明确指定
        if not folder:
            raise ValueError("folder must be non-empty")

        self.id = id  # type: ignore[assignment]
        self.name = name
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.protocol = protocol
        self.use_ssl = use_ssl
        self.folder = folder
        self.enabled = enabled
        self.last_sync_uid = last_sync_uid
        self.last_sync_at = last_sync_at

    @validates("protocol")
    def _validate_protocol(self, key: str, value: str) -> str:
        # 与 __init__ 保持一致的协议白名单校验（ORM 加载/赋值时也会触发）
        if value not in _SUPPORTED_PROTOCOLS:
            msg = f"protocol must be one of {sorted(_SUPPORTED_PROTOCOLS)}, got {value!r}"
            raise ValueError(msg)
        return value

    @validates("last_sync_uid")
    def _validate_last_sync_uid(self, key: str, value: int) -> int:
        if not isinstance(value, int) or value < 0:
            msg = f"last_sync_uid must be int >=0, got {value!r}"
            raise ValueError(msg)
        return value


class EmailMessage(Base):
    """已解析邮件 ORM 模型，对应数据库 emails 表的一行。

    parser 解析出的对象即该模型的瞬时实例（未绑定 Session），
    由 repository 通过幂等批量插入写入数据库。
    """

    __tablename__ = "emails"
    # (account_id, uid) 唯一约束保证同一邮箱内邮件不重复入库
    __table_args__ = (UniqueConstraint("account_id", "uid", name="emails_account_id_uid_key"),)

    # 数据库自增主键，插入前为 None
    id: Mapped[int | None] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 所属账号 ID，外键关联 email_accounts.id
    account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # 邮箱服务器内该文件夹下的 UID，与 account_id 组成幂等键
    uid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # RFC822 Message-ID 头，便于跨系统追溯去重，可能缺失
    message_id: Mapped[str | None] = mapped_column(String)
    # 已解码的主题，解析失败时存空串而非抛异常
    subject: Mapped[str] = mapped_column(String, default="")
    # 发件人地址
    sender: Mapped[str | None] = mapped_column(String)
    # 收件人列表（To + Cc 合并存储）
    recipients: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    # 邮件 Date 头解析结果，可能缺失或格式非法
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 纯文本正文
    text_body: Mapped[str | None] = mapped_column(Text)
    # HTML 正文
    html_body: Mapped[str | None] = mapped_column(Text)
    # 是否已处理（agent 读取后置 TRUE），默认未读
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 本地拉取时间，默认为当前 UTC 时间
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=_now_utc)

    def __init__(
        self,
        *,
        id: int | None = None,
        account_id: int,
        uid: int,
        message_id: str | None = None,
        subject: str | None = "",
        sender: str | None = None,
        recipients: list[str] | None = None,
        sent_at: datetime | None = None,
        text_body: str | None = None,
        html_body: str | None = None,
        is_read: bool = False,
        fetched_at: datetime | None = None,
    ) -> None:
        """构造后校验与容错归一：确保核心字段类型正确，缺失字段给予默认值。"""
        # account_id 和 uid 是幂等键的核心，必须为正整数/非负整数
        if not isinstance(account_id, int) or account_id <= 0:
            msg = f"account_id must be positive int, got {account_id!r}"
            raise ValueError(msg)
        if not isinstance(uid, int) or uid < 0:
            msg = f"uid must be int >=0, got {uid!r}"
            raise ValueError(msg)
        # subject 可能被外部传入 None，做容错归一为空串，避免后续拼接/展示报错
        if subject is None:
            subject = ""
        if not isinstance(subject, str):
            msg = f"subject must be str, got {type(subject).__name__}"
            raise TypeError(msg)
        # recipients 同理，None 归一为空列表
        if recipients is None:
            recipients = []
        if not isinstance(recipients, list):
            msg = "recipients must be list[str]"
            raise TypeError(msg)

        self.id = id
        self.account_id = account_id
        self.uid = uid
        self.message_id = message_id
        self.subject = subject
        self.sender = sender
        self.recipients = recipients
        self.sent_at = sent_at
        self.text_body = text_body
        self.html_body = html_body
        self.is_read = is_read
        # fetched_at 缺省时按构造时间填充（与入库时间一致）
        self.fetched_at = fetched_at if fetched_at is not None else _now_utc()

    @validates("account_id")
    def _validate_account_id(self, key: str, value: int) -> int:
        if not isinstance(value, int) or value <= 0:
            msg = f"account_id must be positive int, got {value!r}"
            raise ValueError(msg)
        return value

    @validates("uid")
    def _validate_uid(self, key: str, value: int) -> int:
        if not isinstance(value, int) or value < 0:
            msg = f"uid must be int >=0, got {value!r}"
            raise ValueError(msg)
        return value

    @validates("subject")
    def _validate_subject(self, key: str, value: str | None) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            msg = f"subject must be str, got {type(value).__name__}"
            raise TypeError(msg)
        return value

    @validates("recipients")
    def _validate_recipients(self, key: str, value: list[str] | None) -> list[str] | None:
        if value is None:
            return []
        if not isinstance(value, list):
            msg = "recipients must be list[str]"
            raise TypeError(msg)
        return value


# ---------------------------------------------------------------------------
# 邮件附件模型
# ---------------------------------------------------------------------------

_VALID_EVIDENCE_SOURCES = set(EVIDENCE_SOURCES)


class EmailAttachment(Base):
    """邮件附件 ORM 模型，对应数据库 email_attachments 表的一行。

    附件字节不上库：抓取时上传腾讯云 COS，本表只保留元数据与对象引用
    （storage_url / storage_key），extracted_text 缓存附件内容提取结果。
    """

    __tablename__ = "email_attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 所属邮件 ID，与 emails.id 对应
    email_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # 附件类别：image（含内嵌 cid 图）/ email（.eml）/ document
    kind: Mapped[str] = mapped_column(String, default="document")
    # 附件文件名，可能缺失
    filename: Mapped[str] = mapped_column(String, default="")
    # MIME 内容类型
    content_type: Mapped[str] = mapped_column(String, default="")
    # Content-Disposition：inline / attachment，可能缺失
    disposition: Mapped[str | None] = mapped_column(String)
    # 内嵌资源 Content-ID（去尖括号），仅 inline 图片等场景非空
    content_id: Mapped[str | None] = mapped_column(String)
    # 附件大小（字节），以解析时实际读取为准
    size: Mapped[int] = mapped_column(Integer, default=0)
    # COS 对象访问 URL；仅存元数据（未上传）时为 NULL
    storage_url: Mapped[str | None] = mapped_column(String)
    # COS 对象键，SDK 拉取与清理按 key 操作
    storage_key: Mapped[str | None] = mapped_column(String)
    # 内容提取缓存：.eml 解析文本 / 图片识别文本；未提取为 NULL
    extracted_text: Mapped[str | None] = mapped_column(Text)
    # 最近一次提取时间
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 入库时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, nullable=False
    )

    def __init__(
        self,
        *,
        id: int | None = None,
        email_id: int,
        kind: str = "document",
        filename: str = "",
        content_type: str = "",
        disposition: str | None = None,
        content_id: str | None = None,
        size: int = 0,
        storage_url: str | None = None,
        storage_key: str | None = None,
        extracted_text: str | None = None,
        extracted_at: datetime | None = None,
    ) -> None:
        """构造后校验：确保核心字段类型正确。"""
        if not isinstance(email_id, int) or email_id <= 0:
            msg = f"email_id must be positive int, got {email_id!r}"
            raise ValueError(msg)
        if kind not in ("image", "email", "document"):
            msg = f"kind must be one of ['email', 'image', 'document'], got {kind!r}"
            raise ValueError(msg)
        if not isinstance(size, int) or size < 0:
            msg = f"size must be int >=0, got {size!r}"
            raise ValueError(msg)

        self.id = id  # type: ignore[assignment]
        self.email_id = email_id
        self.kind = kind
        self.filename = filename
        self.content_type = content_type
        self.disposition = disposition
        self.content_id = content_id
        self.size = size
        self.storage_url = storage_url
        self.storage_key = storage_key
        self.extracted_text = extracted_text
        self.extracted_at = extracted_at

    @validates("email_id")
    def _validate_email_id(self, key: str, value: int) -> int:
        if not isinstance(value, int) or value <= 0:
            msg = f"email_id must be positive int, got {value!r}"
            raise ValueError(msg)
        return value


# ---------------------------------------------------------------------------
# 邮件结构化分析模型
# ---------------------------------------------------------------------------

_VALID_SENTIMENTS = set(SENTIMENTS)
_VALID_PRIORITIES = set(PRIORITIES)
_VALID_STATUSES = {"analyzed", "failed"}
_VALID_INTENTS = set(ALL_INTENTS)


class EmailAnalysis(Base):
    """邮件结构化分析 ORM 模型，对应数据库 email_analyses 表的一行。

    一封邮件至多一份分析结果（email_id UNIQUE），由 agent 节点 1 写入。
    """

    __tablename__ = "email_analyses"
    __table_args__ = (UniqueConstraint("email_id", name="email_analyses_email_id_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 分析对象邮件 ID，唯一约束支撑 ON CONFLICT DO UPDATE 幂等重跑
    email_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # 冗余所属账号，便于按账号查询分析结果
    account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # 核心主意图，用于后续路由；未知时为 UNKNOWN_INTENT
    primary_intent: Mapped[str] = mapped_column(String, default=UNKNOWN_INTENT)
    # 多意图列表：[{"category": "...", "confidence": 0.95, "reasoning": "..."}]
    intents: Mapped[list] = mapped_column(JSONB, default=list)
    # AI 全局综合判定总结
    reasoning_summary: Mapped[str] = mapped_column(Text, default="")
    # 提取的关键业务实体：{"order_id": "...", "date": "..."}
    entities: Mapped[dict] = mapped_column(JSONB, default=dict)
    # 发件人情绪
    sentiment: Mapped[str] = mapped_column(String, default="neutral")
    # 处理优先级
    priority: Mapped[str] = mapped_column(String, default="P2")
    # 建议调用的 Tool 名列表
    suggested_tools: Mapped[list] = mapped_column(JSONB, default=list)
    # 分析状态：analyzed / failed
    status: Mapped[str] = mapped_column(String, default="analyzed")
    # 失败原因，仅 status=failed 时非空
    error: Mapped[str | None] = mapped_column(Text)
    # 产出分析的模型名
    model: Mapped[str | None] = mapped_column("model", String)
    # 检测到的源语言 ISO 639-1 代码（detect_and_translate 节点产出；zh/unknown/外文码，未翻译为空）
    source_language: Mapped[str | None] = mapped_column(String)
    # 主题中文译文，仅非中文业务邮件非空
    translated_subject: Mapped[str | None] = mapped_column(Text)
    # 正文中文译文，仅非中文业务邮件非空
    translated_text: Mapped[str | None] = mapped_column(Text)
    # 主意图证据来源：body（壳层正文）/ attached_email（.eml 附件）/ image（图片）/ mixed
    intent_evidence_source: Mapped[str] = mapped_column(String, default=EVIDENCE_BODY)
    # 首次分析时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, nullable=False
    )
    # 最后更新时间，ON CONFLICT DO UPDATE 时刷新
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, onupdate=_now_utc, nullable=False
    )

    def __init__(
        self,
        *,
        id: int | None = None,
        email_id: int,
        account_id: int,
        primary_intent: str = UNKNOWN_INTENT,
        intents: list | None = None,
        reasoning_summary: str = "",
        entities: dict | None = None,
        sentiment: str = "neutral",
        priority: str = "P2",
        suggested_tools: list | None = None,
        status: str = "analyzed",
        error: str | None = None,
        model: str | None = None,
        source_language: str | None = None,
        translated_subject: str | None = None,
        translated_text: str | None = None,
        intent_evidence_source: str = EVIDENCE_BODY,
    ) -> None:
        """构造后校验：确保白名单字段值合法，必填字段非空。"""
        if not isinstance(email_id, int) or email_id <= 0:
            msg = f"email_id must be positive int, got {email_id!r}"
            raise ValueError(msg)
        if not isinstance(account_id, int) or account_id <= 0:
            msg = f"account_id must be positive int, got {account_id!r}"
            raise ValueError(msg)
        if sentiment not in _VALID_SENTIMENTS:
            msg = f"sentiment must be one of {sorted(_VALID_SENTIMENTS)}, got {sentiment!r}"
            raise ValueError(msg)
        if priority not in _VALID_PRIORITIES:
            msg = f"priority must be one of {sorted(_VALID_PRIORITIES)}, got {priority!r}"
            raise ValueError(msg)
        if primary_intent not in _VALID_INTENTS:
            msg = f"primary_intent must be one of {sorted(_VALID_INTENTS)}, got {primary_intent!r}"
            raise ValueError(msg)
        if status not in _VALID_STATUSES:
            msg = f"status must be one of {sorted(_VALID_STATUSES)}, got {status!r}"
            raise ValueError(msg)
        if status == "failed" and not error:
            msg = "status='failed' requires non-empty error"
            raise ValueError(msg)
        if intent_evidence_source not in _VALID_EVIDENCE_SOURCES:
            msg = (
                f"intent_evidence_source must be one of {sorted(_VALID_EVIDENCE_SOURCES)}, "
                f"got {intent_evidence_source!r}"
            )
            raise ValueError(msg)

        self.id = id  # type: ignore[assignment]
        self.email_id = email_id
        self.account_id = account_id
        self.primary_intent = primary_intent
        self.intents = intents if intents is not None else []
        self.reasoning_summary = reasoning_summary
        self.entities = entities if entities is not None else {}
        self.sentiment = sentiment
        self.priority = priority
        self.suggested_tools = suggested_tools if suggested_tools is not None else []
        self.status = status
        self.error = error
        self.model = model
        self.source_language = source_language
        self.translated_subject = translated_subject
        self.translated_text = translated_text
        self.intent_evidence_source = intent_evidence_source


# ---------------------------------------------------------------------------
# 知识库模型（RAG，与邮件链路零接线）
# ---------------------------------------------------------------------------

_VALID_KB_TYPES = set(ALL_KB_TYPES)
_VALID_KB_SOURCE_TYPES = set(ALL_KB_SOURCE_TYPES)
_VALID_KB_STATUSES = set(ALL_KB_STATUSES)


class KbDocument(Base):
    """知识库文档 ORM 模型，对应数据库 kb_documents 表的一行。

    文档级承载管理语义：source_key 幂等重入库、content_hash 变更检测、
    status 软下线；检索粒度在 KbChunk。与邮件链路无任何外键关联。
    """

    __tablename__ = "kb_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 知识类型：faq / sop / compliance（白名单见 app/schemas/knowledge.py）
    kb_type: Mapped[str] = mapped_column(String, nullable=False)
    # 文档标题，展示与运维定位用
    title: Mapped[str] = mapped_column(String, nullable=False)
    # 来源形态：mail / file / text
    source_type: Mapped[str] = mapped_column(String, default=KB_SOURCE_TYPE_TEXT)
    # 来源幂等键：同来源重入库不产生重复数据
    source_key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    # 原文 SHA-256，变更检测：hash 不同才重新分块与嵌入
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    # 知识状态：active（参与检索）/ archived（归档下线）
    status: Mapped[str] = mapped_column(String, default=KB_STATUS_ACTIVE)
    # 创建时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, nullable=False
    )
    # 最后更新时间，重入库/归档时刷新
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, onupdate=_now_utc, nullable=False
    )

    def __init__(
        self,
        *,
        id: int | None = None,
        kb_type: str,
        title: str,
        source_key: str,
        content_hash: str,
        source_type: str = "text",
        status: str = KB_STATUS_ACTIVE,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
    ) -> None:
        """构造后校验：白名单字段必须合法，标识字段非空。"""
        if kb_type not in _VALID_KB_TYPES:
            msg = f"kb_type must be one of {sorted(_VALID_KB_TYPES)}, got {kb_type!r}"
            raise ValueError(msg)
        if not isinstance(title, str) or not title:
            msg = f"title must be non-empty str, got {title!r}"
            raise ValueError(msg)
        if source_type not in _VALID_KB_SOURCE_TYPES:
            msg = (
                f"source_type must be one of {sorted(_VALID_KB_SOURCE_TYPES)}, got {source_type!r}"
            )
            raise ValueError(msg)
        if not isinstance(source_key, str) or not source_key:
            msg = f"source_key must be non-empty str, got {source_key!r}"
            raise ValueError(msg)
        if not isinstance(content_hash, str) or not content_hash:
            msg = f"content_hash must be non-empty str, got {content_hash!r}"
            raise ValueError(msg)
        if status not in _VALID_KB_STATUSES:
            msg = f"status must be one of {sorted(_VALID_KB_STATUSES)}, got {status!r}"
            raise ValueError(msg)

        self.id = id  # type: ignore[assignment]
        self.kb_type = kb_type
        self.title = title
        self.source_type = source_type
        self.source_key = source_key
        self.content_hash = content_hash
        self.status = status
        if created_at is not None:
            self.created_at = created_at
        if updated_at is not None:
            self.updated_at = updated_at


class KbChunk(Base):
    """知识库分块 ORM 模型，对应数据库 kb_chunks 表的一行。

    检索粒度：content 是命中后喂给 LLM 的原文，embedding 是其向量。
    kb_type 冗余自所属文档，类型过滤免 join；重入库时按 document_id 整篇换块。
    """

    __tablename__ = "kb_chunks"
    # (document_id, chunk_index) 唯一：块在文档内有序且不重复，可按序还原上下文
    __table_args__ = (
        UniqueConstraint(
            "document_id", "chunk_index", name="kb_chunks_document_id_chunk_index_key"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 所属文档 ID，逻辑外键 → kb_documents.id（无物理外键约束）
    document_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # 冗余自文档的 kb_type，检索按类型过滤免 join
    kb_type: Mapped[str] = mapped_column(String, nullable=False)
    # 文档内块序号（0 起）
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # 切片原文，检索命中后喂给 LLM 的就是这一列
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 块向量：PG 上为 pgvector 定长列；sqlite 单测经 with_variant 退化为 JSON 列
    embedding: Mapped[list[float]] = mapped_column(
        Vector(KB_EMBEDDING_DIMENSIONS).with_variant(JSON(), "sqlite"), nullable=False
    )
    # 产出该向量的模型名：不同模型向量不可比，检索强制同模型匹配
    embedding_model: Mapped[str] = mapped_column(String, nullable=False)
    # 块级补充过滤条件；metadata 是 SQLAlchemy Declarative 保留属性名，属性改用 meta
    meta: Mapped[dict] = mapped_column(
        "metadata", JSONB().with_variant(JSON(), "sqlite"), default=dict
    )
    # 入库时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, nullable=False
    )

    def __init__(
        self,
        *,
        id: int | None = None,
        document_id: int,
        kb_type: str,
        chunk_index: int,
        content: str,
        embedding: list[float],
        embedding_model: str,
        meta: dict | None = None,
        created_at: datetime | None = None,
    ) -> None:
        """构造后校验：向量维度必须与列定义一致，白名单字段必须合法。"""
        if not isinstance(document_id, int) or document_id <= 0:
            msg = f"document_id must be positive int, got {document_id!r}"
            raise ValueError(msg)
        if kb_type not in _VALID_KB_TYPES:
            msg = f"kb_type must be one of {sorted(_VALID_KB_TYPES)}, got {kb_type!r}"
            raise ValueError(msg)
        if not isinstance(chunk_index, int) or chunk_index < 0:
            msg = f"chunk_index must be int >=0, got {chunk_index!r}"
            raise ValueError(msg)
        if not isinstance(content, str) or not content:
            msg = f"content must be non-empty str, got {content!r}"
            raise ValueError(msg)
        if not isinstance(embedding, list) or len(embedding) != KB_EMBEDDING_DIMENSIONS:
            msg = (
                f"embedding must be list of {KB_EMBEDDING_DIMENSIONS} floats, "
                f"got length {len(embedding) if isinstance(embedding, list) else embedding!r}"
            )
            raise ValueError(msg)
        if not all(isinstance(x, (int, float)) for x in embedding):
            msg = "embedding must contain only numbers"
            raise ValueError(msg)
        if not isinstance(embedding_model, str) or not embedding_model:
            msg = f"embedding_model must be non-empty str, got {embedding_model!r}"
            raise ValueError(msg)

        self.id = id  # type: ignore[assignment]
        self.document_id = document_id
        self.kb_type = kb_type
        self.chunk_index = chunk_index
        self.content = content
        self.embedding = embedding
        self.embedding_model = embedding_model
        self.meta = meta if meta is not None else {}
        if created_at is not None:
            self.created_at = created_at
