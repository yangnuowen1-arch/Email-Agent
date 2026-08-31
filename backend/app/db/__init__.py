# 数据库门面对外导出，统一从此模块导入；生命周期由 core.container 管理
from app.db.db import (
    Account,
    Base,
    EmailAnalysisRecord,
    EmailMessage,
    ReplyDraftTransitionRecord,
    ReplyDraftVersionRecord,
)
from app.db.email_query_store import SqlAlchemyMailQueryStore
from app.db.email_sync_store import SqlAlchemyEmailSyncStore
from app.db.engine import Database, build_database
from app.db.mail_workflow_repositories import MailWorkflowRepository
from app.db.mail_workflow_store import SqlAlchemyMailWorkflowStore
from app.db.repositories import EmailAccountRepository, EmailRepository

__all__ = [
    "Account",
    "Base",
    "Database",
    "EmailAccountRepository",
    "EmailAnalysisRecord",
    "EmailMessage",
    "MailWorkflowRepository",
    "ReplyDraftTransitionRecord",
    "ReplyDraftVersionRecord",
    "SqlAlchemyMailQueryStore",
    "SqlAlchemyEmailSyncStore",
    "SqlAlchemyMailWorkflowStore",
    "EmailRepository",
    "build_database",
]
