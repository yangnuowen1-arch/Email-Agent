# db-schema.md — Email-Agent 数据库设计文档

> 变更约定：任何 schema 变更必须**先修改本文档**的表结构定义，再在「变更记录」节追加对应的 SQL 操作并备注时间。

## 1. ER 关系

```
email_accounts (1) ──────< (N) emails
     │                        │
 账号配置(输入)            已拉取邮件(输出)
     └── last_sync_uid ──────┘  ← 断点续传的桥梁
```

- 一条 `email_accounts` 记录对应一个真实邮箱，是程序的唯一输入源。
- `emails.account_id` 外键关联到账号，`(account_id, uid)` 唯一约束保证同一邮箱内邮件不重复入库。

## 2. 表结构

### 2.1 email_accounts — 邮箱账号配置表（输入）

```sql
CREATE TABLE email_accounts (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    host          TEXT NOT NULL,
    port          INT  NOT NULL DEFAULT 993,
    protocol      TEXT NOT NULL DEFAULT 'imap',
    username      TEXT NOT NULL,
    password      TEXT NOT NULL,
    use_ssl       BOOL NOT NULL DEFAULT TRUE,
    folder        TEXT NOT NULL DEFAULT 'INBOX',
    enabled       BOOL NOT NULL DEFAULT TRUE,
    last_sync_uid BIGINT NOT NULL DEFAULT 0,
    last_sync_at  TIMESTAMPTZ
);

COMMENT ON TABLE email_accounts IS '邮箱账号配置表（程序输入源）';
COMMENT ON COLUMN email_accounts.id IS '自增主键，emails 表外键引用它';
COMMENT ON COLUMN email_accounts.name IS '账号别名，用于日志与汇总报告展示';
COMMENT ON COLUMN email_accounts.host IS 'IMAP 服务器地址，如 imap.qq.com';
COMMENT ON COLUMN email_accounts.port IS '服务端口，SSL 下默认 993';
COMMENT ON COLUMN email_accounts.protocol IS '协议标识，factory 据此创建客户端；当前仅支持 imap';
COMMENT ON COLUMN email_accounts.username IS '登录用户名（通常为完整邮箱地址）';
COMMENT ON COLUMN email_accounts.password IS '登录密码/授权码；禁止写入任何日志';
COMMENT ON COLUMN email_accounts.use_ssl IS '是否启用 SSL/TLS 连接';
COMMENT ON COLUMN email_accounts.folder IS '要拉取的邮箱文件夹';
COMMENT ON COLUMN email_accounts.enabled IS '软开关：FALSE 的账号不会被调度';
COMMENT ON COLUMN email_accounts.last_sync_uid IS '增量断点：已成功入库的最大 UID；0 表示从未同步（首跑全量）';
COMMENT ON COLUMN email_accounts.last_sync_at IS '最近一次成功同步时间，仅运维观测用';
```

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | SERIAL | PK | 自增主键，emails 表外键引用它 |
| name | TEXT | NOT NULL | 账号别名，用于日志与汇总报告展示 |
| host | TEXT | NOT NULL | IMAP 服务器地址，如 `imap.qq.com` |
| port | INT | NOT NULL, DEFAULT 993 | 服务端口，SSL 下默认 993 |
| protocol | TEXT | NOT NULL, DEFAULT 'imap' | 协议标识，factory 据此创建客户端；当前仅支持 `imap` |
| username | TEXT | NOT NULL | 登录用户名（通常为完整邮箱地址） |
| password | TEXT | NOT NULL | 登录密码/授权码；**禁止写入任何日志** |
| use_ssl | BOOL | NOT NULL, DEFAULT TRUE | 是否启用 SSL/TLS 连接 |
| folder | TEXT | NOT NULL, DEFAULT 'INBOX' | 要拉取的邮箱文件夹 |
| enabled | BOOL | NOT NULL, DEFAULT TRUE | 软开关：FALSE 的账号不会被调度 |
| last_sync_uid | BIGINT | NOT NULL, DEFAULT 0 | **增量断点**：已成功入库的最大 UID；0 表示从未同步（首跑全量） |
| last_sync_at | TIMESTAMPTZ | 可空 | 最近一次成功同步时间，仅运维观测用 |

例子: INSERT INTO email_accounts (name, host, username, password) VALUES ('xxx@qq.com','imap.qq.com','xxx@qq.com','xxxx授权码') RETURNING id;

### 2.2 emails — 已拉取邮件表（输出）

```sql
CREATE TABLE emails (
    id          SERIAL PRIMARY KEY,
    account_id  INT REFERENCES email_accounts(id),
    uid         BIGINT NOT NULL,
    message_id  TEXT,
    subject     TEXT,
    sender      TEXT,
    recipients  TEXT[],
    sent_at     TIMESTAMPTZ,
    text_body   TEXT,
    html_body   TEXT,
    fetched_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (account_id, uid)
);

COMMENT ON TABLE emails IS '已拉取邮件表（程序输出）';
COMMENT ON COLUMN emails.id IS '自增主键';
COMMENT ON COLUMN emails.account_id IS '所属账号，外键 → email_accounts.id';
COMMENT ON COLUMN emails.uid IS '邮箱服务器内该文件夹下的 UID，与 account_id 组成幂等键';
COMMENT ON COLUMN emails.message_id IS 'RFC822 Message-ID 头，便于跨系统追溯去重';
COMMENT ON COLUMN emails.subject IS '已解码的主题；解析失败存空串而非 NULL 报错';
COMMENT ON COLUMN emails.sender IS '发件人地址';
COMMENT ON COLUMN emails.recipients IS '收件人列表（to + cc 合并存储）';
COMMENT ON COLUMN emails.sent_at IS '邮件 Date 头解析结果';
COMMENT ON COLUMN emails.text_body IS '纯文本正文';
COMMENT ON COLUMN emails.html_body IS 'HTML 正文；与 text_body 至少一个非空';
COMMENT ON COLUMN emails.fetched_at IS '本地拉取时间';
```

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| id | SERIAL | PK | 自增主键 |
| account_id | INT | FK → email_accounts.id | 所属账号 |
| uid | BIGINT | NOT NULL | 邮箱服务器内该文件夹下的 UID，与 account_id 组成幂等键 |
| message_id | TEXT | 可空 | RFC822 Message-ID 头，便于跨系统追溯去重 |
| subject | TEXT | 可空 | 已解码的主题；解析失败存空串而非 NULL 报错 |
| sender | TEXT | 可空 | 发件人地址 |
| recipients | TEXT[] | 可空 | 收件人列表（to + cc 合并存储） |
| sent_at | TIMESTAMPTZ | 可空 | 邮件 Date 头解析结果 |
| text_body | TEXT | 可空 | 纯文本正文 |
| html_body | TEXT | 可空 | HTML 正文；与 text_body 至少一个非空 |
| fetched_at | TIMESTAMPTZ | DEFAULT now() | 本地拉取时间 |

写入方式：批量 `INSERT ... ON CONFLICT (account_id, uid) DO NOTHING`。

## 3. 设计说明

### 为什么幂等键是 `(account_id, uid)` 而不是 message_id？

- UID 是 IMAP 协议内**同一邮箱文件夹中稳定递增**的标识，天然适合做增量断点（`last_sync_uid`）；
- message_id 由发件方生成，可能缺失或格式不合规，不适合做主键约束；
- 二者职责不同：uid 管「同步位置」，message_id 管「跨系统追溯」。

### 断点语义

1. 同步前读取 `last_sync_uid = N`；
2. 仅拉取 `UID > N` 的邮件；
3. 本批全部成功入库后，将 `last_sync_uid` 更新为本批最大 UID、刷新 `last_sync_at`；
4. 中途失败则不推进断点，下次重拉（配合 ON CONFLICT 幂等，重拉无副作用）。

## 4. 变更记录

> 本节只追加不修改；每条记录 = 日期 + 说明 + SQL 操作。

### 2026-08-25 初始建表

创建 `email_accounts`、`emails` 两张表，DDL 见第 2 节。

### 2026-08-25 补充表与字段注释

为两张表补充 `COMMENT ON TABLE / COLUMN` 注释，与第 2 节字段说明表保持一致；执行第 2 节 DDL 时注释会一并写入数据库元数据。

### 2026-08-25 数据访问层迁移至 SQLAlchemy ORM（表结构不变）

数据访问层由手写连接池（psycopg2 `ThreadedConnectionPool`）+ 原生 SQL repository 迁移为 SQLAlchemy 2.x ORM：
引擎与连接池由 `db/engine.py` 的 `create_engine` 统一管理；`Account`/`EmailMessage` 改为声明式 ORM 模型；
`repository/` 改为基于 `Session` 的薄封装（同一账号的邮件入库与断点推进共享一个 Session，由一次 `commit()` 原子提交）。
**本变更不修改任何表结构、约束或列定义**，原有 DDL（含 `(account_id, uid)` 唯一约束）保持不变，幂等写入改为 `pg_insert(...).on_conflict_do_nothing()`。
驱动统一使用 psycopg v3（连接串 `postgresql+psycopg://`），不再兼容 psycopg2。
