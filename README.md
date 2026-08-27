# Email-Agent

当前可运行能力是：从 PostgreSQL 读取邮箱账号配置，通过 IMAP 增量拉取邮件，解析后写回 PostgreSQL。

```
email_accounts → IMAP → 解析邮件 → emails → 更新 last_sync_uid
```

产品目标还包括 LLM 邮件理解、回复草稿和人工审批后的 SMTP 外发；这些能力尚未接入当前运行入口。架构边界与分阶段计划见 [docs/architecture.md](docs/architecture.md)。

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
SYNC_MAX_WORKERS=5
SYNC_TIMEOUT_SECONDS=60
LOG_LEVEL=INFO
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
| 正常增量同步 | `uv run python -m app ingest` |
| 调试时最多拉取 20 封 | `uv run python -m app ingest --limit 20` |
| 忽略断点，全量同步 | `uv run python -m app ingest --full` |
| 已安装脚本入口 | `uv run email-agent ingest` |

`email-agent` 是命令名；`app` 是 Python 模块名。它们都通过 `ingest` 子命令执行同步；
裸执行 `python -m app` 或 `email-agent` 只显示帮助，不会访问邮箱或数据库。

### 参数说明

| 参数 | 说明 |
|---|---|
| `ingest` | 仅拉取 UID 大于 `last_sync_uid` 的新邮件；所有选中邮件安全处理后才推进断点。 |
| `--limit N` | 最多处理待拉取邮件中 UID 最小的 `N` 封。会写入邮件，但**不会**推进断点，因此适合调试，不适合正式同步。 |
| `--full` | 忽略断点并扫描整个文件夹；成功后推进断点。邮箱邮件很多时请谨慎使用。 |
| `--full --limit N` | 扫描全量范围但仅处理最旧的 `N` 封；不推进断点。 |

程序输出类似：

```text
inserted=1 skipped=0 failed=0 duration_ms=42
```

不要只看进程退出码：某个账号同步失败时，程序会继续处理其他账号。请检查汇总中的 `failed` 数量。

## 验证今天的邮件同步

当前程序按 UID 增量拉取，不提供“只拉今天”的 IMAP 筛选。验证时建议给测试邮箱发一封唯一主题的邮件，例如：

```text
[Email-Agent Test] 2026-08-26-001
```

然后运行正常增量同步：

```bash
uv run python -m app ingest
```

在 PostgreSQL 中确认这封邮件已写入。以下查询按上海自然日查看“今天被程序拉取”的邮件：

```sql
SET TIME ZONE 'Asia/Shanghai';

SELECT a.name, e.uid, e.subject, e.sender, e.sent_at, e.fetched_at
FROM emails AS e
JOIN email_accounts AS a ON a.id = e.account_id
WHERE e.subject = '[Email-Agent Test] 2026-08-26-001'
  AND e.fetched_at >= CURRENT_DATE
  AND e.fetched_at < CURRENT_DATE + 1
ORDER BY e.fetched_at DESC;
```

再次运行同一条同步命令且没有新邮件时，应看到 `inserted=0`、`skipped=0`，这说明断点生效。

注意：首次同步时 `last_sync_uid=0` 会拉取整个文件夹。建议用专门的测试邮箱，或先完成一次基线同步。`--limit 20` 不会推进断点，重复执行会再次拉取同一批候选邮件。若 IMAP 无法取回某个 UID，或该邮件无法解析，已成功的邮件仍可幂等入库，但该账号本轮不会推进断点，确保失败邮件下次可以重试。

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
│       ├── cli/             # 命令行入口
│       ├── core/            # 配置与组合根（Container）
│       ├── schemas/         # 不依赖 SDK/ORM 的数据契约
│       ├── ports/           # Service 所需的外部能力 Protocol
│       ├── services/        # 解析和同步等应用/领域逻辑
│       ├── providers/       # IMAP 等协议适配器
│       ├── db/              # SQLAlchemy 模型、Repository、存储适配器
│       ├── llm/             # 后续 LLM gateway 边界
│       └── memory/          # 后续运行状态/记忆边界
├── docs/
├── tests/
├── pyproject.toml
└── uv.lock
```

数据库表结构和变更记录见 [docs/db-schema.md](docs/db-schema.md)。
