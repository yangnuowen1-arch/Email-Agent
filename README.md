# Email-Agent

从 PostgreSQL 读取邮件账号配置，按账号并发连接邮箱服务器（IMAP），拉取邮件内容解析后存回 PostgreSQL。

## 功能特性

- **配置驱动**：邮箱账号全部存于 `email_accounts` 表，加账号不改代码
- **协议抽象**：`MailClient` 抽象基类 + 工厂，IMAP 为首个实现，未来可平滑接入其他协议
- **增量拉取**：每账号记录 `last_sync_uid` 断点，只拉新邮件，支持断点续传
- **幂等写入**：`(account_id, uid)` 唯一约束 + ON CONFLICT，重复执行不产生重复数据
- **并发与隔离**：ThreadPoolExecutor 账号级并发；单账号失败不影响整体，结束输出汇总报告
- **可测试**：客户端抽象可注入 Fake 实现，解析器为纯函数，单测无需真实邮箱

## 快速开始

### 1. 环境要求

- Python >= 3.11
- PostgreSQL（用于存账号配置与邮件）
- 一个开启 IMAP 服务的邮箱账号

### 2. 安装

```bash
git clone <repo-url> && cd Email-Agent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

### 3. 初始化数据库

在目标库中执行 [docs/db-schema.md](docs/db-schema.md) 第 2 节的建表语句（`email_accounts`、`emails`）完成初始化；后续表结构变更同样记录在该文档的「变更记录」中。

### 4. 配置

```bash
cp .env.example .env   # 按注释填写 DATABASE_URL 等
```

然后在 `email_accounts` 表中插入要同步的邮箱账号。

### 5. 运行

#### 5.1 启动方式

两种等价入口（`pyproject.toml` 已注册 console script）：

```bash
# 方式一：模块启动（推荐，IDE/调试友好）
python -m app
python -m app --help

# 方式二：可执行脚本（pip install 后可用）
email-agent
email-agent --help
```

前置条件：已完成第 3 步建表、第 4 步 `.env` 配置且 `email_accounts` 表中至少有一条 `enabled = TRUE` 的账号。

#### 5.2 启动参数

```bash
$ python -m app --help
usage: email-agent [-h] [--limit LIMIT] [--full]

Sync emails from IMAP to PostgreSQL incrementally

options:
  -h, --help     show this help message and exit
  --limit LIMIT  limit number of emails per account (debug/first-run, when set breakpoint not updated)
  --full         ignore last_sync_uid and fetch all emails
```

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `--limit N` | `int > 0` | `None`（不限） | 每账号最多拉取 N 封（按 UID 升序截断）。**调试用**：限量模式下即使成功也不推进 `email_accounts.last_sync_uid` 断点，下次仍会重拉，便于反复验证解析/入库逻辑。生产请勿带此参数 |
| `--full` | `flag` | `false` | 忽略断点全量拉取。`since_uid` 强制为 `0`（`UID SEARCH ALL`），常用于首跑、补漏或 `last_sync_uid` 异常后重建。`--full --limit 20` 表示“全量但只取前 20 封且不推进断点” |
| （无 `--workers`） | — | 见环境变量 | 并发数由 `SYNC_MAX_WORKERS` 决定，保持 CLI 最简。见 5.3 |

**典型用法：**

```bash
# 默认：增量拉取（只取 UID > last_sync_uid 的新邮件，成功后推进断点）
python -m app

# 首跑限量验证（不推进断点，可反复执行观察解析/日志）
python -m app --limit 20

# 忽略断点全量（会推进断点到本批最大 UID）
python -m app --full

# 全量限量组合（全量扫描但只取 20 封，不推进断点，调试用）
python -m app --full --limit 20
```

执行后终端会打印汇总表（`stdout`）且 `stderr` 输出 JSON 结构化日志，单账号失败仅日志告警，不中断其他账号：

```
Sync Summary (full=False, limit=None, total=2)
--------------------------------------------------------------------------------
account              fetched inserted skipped  max_uid  status
--------------------------------------------------------------------------------
qq (id=1)                  5        5       0      105  OK
gmail (id=2)               0        0       0        0  ERROR: IMAP connect failed ...
--------------------------------------------------------------------------------
Total: 2 accounts, 5 fetched, 5 inserted, 1 failed
```

重跑幂等：因 `emails` 表 `UNIQUE(account_id, uid)` + `ON CONFLICT DO NOTHING`，重复执行不产生重复数据；`limit` 模式外，断点仅在整批成功后推进，失败自动重试无副作用。

#### 5.3 环境变量（与 CLI 互补）

`AppConfig.from_env()` 从环境变量/`.env` 读取（见 `.env.example`）：

| 变量 | 必填 | 默认 | 说明 |
|------|------|------|------|
| `DATABASE_URL` | 是 | — | PostgreSQL 连接串 `postgresql://user:password@host:port/dbname`，缺失时 CLI 以 `exit 2` 报错（日志已脱敏为 `***:***@`） |
| `DB_POOL_MIN_SIZE` / `DB_POOL_MAX_SIZE` | 否 | `1` / `10` | 连接池大小，`init_pool` 时生效 |
| `SYNC_MAX_WORKERS` | 否 | `5` | `ThreadPoolExecutor` 账号级并发数，I/O 密集建议 5–10 |
| `SYNC_TIMEOUT_SECONDS` | 否 | `60` | 单账号同步超时（秒），超时记为 `ERROR: timeout after ...s` 并隔离 |
| `LOG_LEVEL` | 否 | `INFO` | `DEBUG/INFO/WARNING/ERROR`，控制 JSON 日志级别；日志为 JSON（`timestamp/level/name/message`），密码与 URL 已自动掩码 |

示例 `.env`：

```bash
DATABASE_URL=postgresql://user:password@localhost:5432/email_agent
SYNC_MAX_WORKERS=5
SYNC_TIMEOUT_SECONDS=60
LOG_LEVEL=INFO
```

## 项目结构

```
Email-Agent/
├── docs/db-schema.md            # 数据库设计文档
├── backend/
│   └── app/                     # 后端唯一 Python 包
│       ├── core/                # settings（AppConfig/Settings）+ bootstrap 组合根
│       ├── models/              # Account / EmailMessage 数据契约
│       ├── db/                  # 引擎与连接池
│       ├── repository/          # 数据读写（唯一写 SQL 的地方）
│       ├── providers/email/     # MailClient ABC + factory + imap 实现
│       ├── services/            # parsing 纯函数 + sync 编排与并发调度
│       ├── agent/               # LLM 智能体骨架（llm / memory / tools 支撑）
│       └── cli/                 # python -m app / email-agent 入口
├── frontend/                    # 前端应用（开始前端开发时创建）
└── tests/
    ├── unit/                    # 单元测试（与 backend/app 结构镜像）
    └── integration/             # 集成测试（需真实 PG / 邮箱）
```

## 开发

```bash
# 运行单元测试
pytest tests/unit

# Lint 与格式化
ruff check backend/app tests
ruff format --check backend/app tests
```

表结构见 [docs/db-schema.md](docs/db-schema.md)。
