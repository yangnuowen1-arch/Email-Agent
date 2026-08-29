# Email-Agent

从 PostgreSQL 读取邮箱账号配置，通过 IMAP IDLE 长驻监听各账号的新邮件，解析后写回 PostgreSQL。

```
email_accounts → IMAP IDLE 常驻监听 → 新邮件回调 → 解析邮件 → emails → 更新 last_sync_uid
```

## 环境要求

- Python 3.11 或更高版本
- [uv](https://docs.astral.sh/uv/)
- PostgreSQL
- 已开启 IMAP 的测试邮箱，以及对应的授权码或应用专用密码

## 快速开始

所有命令都在项目根目录执行。

### 1. 安装依赖

```bash
uv sync --extra dev
```

如果刚拉取代码、更新过包目录，或 `email-agent` 命令仍指向旧路径，刷新可编辑安装：

```bash
uv sync --extra dev --reinstall-package email-agent
```

### 2. 初始化数据库

在目标 PostgreSQL 数据库中执行 [docs/db-schema.md](docs/db-schema.md) 第 2 节的建表 SQL，创建：

- `email_accounts`：邮箱账号和同步断点
- `emails`：已解析并入库的邮件

### 3. 配置环境变量

```bash
cp .env.example .env
```

在 `.env` 中填写数据库连接串。不要提交 `.env`，也不要把邮箱授权码写进日志。

```dotenv
DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/email_agent
LISTEN_IDLE_PING_SECONDS=60
LISTEN_BACKOFF_INITIAL_SECONDS=1
LISTEN_BACKOFF_MAX_SECONDS=60
LOG_LEVEL=INFO

# LLM 智能体（运行 agent 命令必填 LLM_API_KEY；其余可选）
LLM_API_KEY=sk-xxx
LLM_MODEL=hy3
LLM_BASE_URL=https://opencode.ai/zen/go/v1
LLM_TEMPERATURE=0.2
LLM_MAX_TOKENS=4096
LLM_TIMEOUT_SECONDS=120
# 单个工具调用超时（可选，默认 30）
AGENT_TOOL_TIMEOUT_SECONDS=30
```

### 4. 添加测试邮箱

通过数据库客户端插入一条启用的账号配置。`password` 应填写邮箱的 IMAP 授权码或应用专用密码。

```sql
INSERT INTO email_accounts (
    name, host, port, protocol, username, password, use_ssl, folder, enabled
) VALUES (
    'test-mailbox',
    'imap.example.com',
    993,
    'imap',
    'your-email@example.com',
    'your-imap-app-password',
    TRUE,
    'INBOX',
    TRUE
);
```

检查启用账号与同步断点时，不要查询或输出密码列：

```sql
SELECT id, name, host, folder, enabled, last_sync_uid, last_sync_at
FROM email_accounts
ORDER BY id;
```

## 运行命令

源码包名是 `app`；旧的 `python -m email_agent` 已不再使用。

| 目的 | 命令 |
|---|---|
| 查看帮助 | `uv run python -m app --help` |
| 常驻监听新邮件（需 DATABASE_URL） | `uv run python -m app listen` |
| 运行 agent 任务（仅需 LLM_API_KEY，不依赖数据库） | `uv run python -m app agent "你的任务"` |
| 摘要最近邮件（需先 listen 落库） | `uv run python -m app agent "summarize my recent emails"` |
| 已安装脚本入口 | `uv run email-agent --help` |

`email-agent` 是命令名；`app` 是 Python 模块名。它们指向同一个 CLI 程序。注意：`python -m app` 不带子命令时只初始化容器后退出，**不会**自动监听；必须显式使用 `listen` 子命令。

### Agent 命令

`agent <task>` 是统一的自然语言入口：把任务交给 `EmailAgent`，由它按需调用已注册工具（如 `summarize_emails`）完成工作，并打印结果。

- 邮件类任务（如摘要）需要先跑 `listen` 把邮件落库；agent 通过工具读取最近邮件再归纳。
- `limit`、`account_id` 等由 LLM 从任务文本中解析后传给工具，无需额外命令行参数。
- 无 `DATABASE_URL` 时仍可运行不依赖邮件的任务；若任务需要邮件但无数据库，工具会返回错误并由 LLM 如实说明。

```bash
# 摘要最近邮件（参数由 LLM 从自然语言解析）
uv run python -m app agent "summarize my 5 most recent emails for account 3"

# 其它通用任务
uv run python -m app agent "写一封请假邮件，语气正式"
```

### 监听机制与参数说明

`listen` 子命令不接受命令行参数。它为每个启用账号维持一个长驻线程（IMAP IDLE）：新邮件到达时即时解析入库并推进账号的 `last_sync_uid` 断点；断线自动指数退避重连，从断点继续、不丢不重。

| 变量 | 说明 |
|---|---|
| `LISTEN_IDLE_PING_SECONDS` | IDLE 重发/健康检查周期（默认 60 秒），每次醒来做一次轻量搜索兜底丢事件；Ctrl+C 优雅退出的最长等待也受此值影响。 |
| `LISTEN_BACKOFF_INITIAL_SECONDS` | 断线重连退避起点（默认 1 秒），指数递增。 |
| `LISTEN_BACKOFF_MAX_SECONDS` | 断线重连退避上限（默认 60 秒）。 |

监听只接收**启动之后新到的邮件**，不回补历史存量；登录失败等不可恢复错误会终止该账号的监听并记录日志，其他账号不受影响。

## 验证新邮件监听

给测试邮箱发一封唯一主题的邮件，例如：

```text
[Email-Agent Test] 2026-08-29-001
```

保持 `listen` 运行，邮件应即时入库（无需重跑命令）。在 PostgreSQL 中确认：

```sql
SET TIME ZONE 'Asia/Shanghai';

SELECT a.name, e.uid, e.subject, e.sender, e.sent_at, e.fetched_at
FROM emails AS e
JOIN email_accounts AS a ON a.id = e.account_id
WHERE e.subject = '[Email-Agent Test] 2026-08-29-001'
ORDER BY e.fetched_at DESC;
```

再查账号断点应已推进到该邮件的 uid：

```sql
SELECT id, name, last_sync_uid, last_sync_at
FROM email_accounts
ORDER BY id;
```

注意：`listen` 只推送监听启动之后新到的邮件；监听期间到达的邮件由断点与唯一约束（`account_id, uid`）保证不丢不重。建议用专门的测试邮箱。

`fetched_at` 表示本程序何时拉取并入库；`sent_at` 来自邮件的 `Date` 头，可能受发件人时区或错误时间影响。

## 开发命令

```bash
# 全部测试
uv run pytest

# 仅运行单元测试
uv run pytest tests/unit

# 静态检查与格式检查
uv run ruff check backend/app tests
uv run ruff format --check backend/app tests

# 自动格式化
uv run ruff format backend/app tests
```

## 项目结构

```text
Email-Agent/
├── backend/
│   └── app/                 # Python 包：import app
│       ├── cli/             # 命令行入口（agent / listen 子命令）
│       ├── core/            # 配置、监听编排（EmailListener）与组合根（Container）
│       ├── db/              # SQLAlchemy 引擎、Session 与 ORM 模型、仓储
│       ├── providers/       # IMAP 等外部适配器
│       ├── services/        # 解析和同步编排
│       ├── tools/           # Agent 可调用的业务能力（含 ToolRegistry）
│       ├── llm/             # LLM 网关抽象与 OpenAI 兼容实现
│       └── agent/           # LangGraph 编排与提示词
├── docs/
├── tests/
├── pyproject.toml
└── uv.lock
```

数据库表结构和变更记录见 [docs/db-schema.md](docs/db-schema.md)。
