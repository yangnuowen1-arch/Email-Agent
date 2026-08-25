# AGENTS.md — Email-Agent 项目工作规范

> 本文档是项目协作的"宪法"：所有开发者与 AI 助手在动手写代码前必须先阅读并遵守。
> 结构或设计变更时，先改本文档，再改代码。

## 1. 项目概述

从 PostgreSQL 读取邮件账号配置，按账号并发连接邮箱服务器拉取邮件内容，解析后存回 PostgreSQL。

核心链路：

```
PG(email_accounts) → MailClient 抽象层(IMAP) → 解析(parsing) → PG(emails) → 回写同步断点
```

当前形态为一次性 CLI 任务；架构上要求未来可平滑演进为定时任务（scheduler 模块复用 service 层）。

## 2. 技术栈约定

| 项 | 选型 | 说明 |
|---|---|---|
| 语言 | Python ≥ 3.11 | 使用 dataclass、match 等现代特性 |
| 数据库 | PostgreSQL + SQLAlchemy 2.x ORM | 引擎内置连接池（`db/engine.py`）；模型即 ORM 实体 |
| 驱动 | psycopg v3（`psycopg[binary]`） | 连接串用 `postgresql+psycopg://`；遗留 `postgresql://` 由 `db/engine.normalize_db_url` 自动改写 |
| 邮件协议 | IMAP（imapclient 库） | 必须通过抽象基类 `MailClient` 访问 |
| 邮件解析 | 标准库 `email`（policy=default） | 不引入第三方 MIME 库 |
| 配置 | python-dotenv + dataclass | 程序配置只从环境变量/.env 读取 |
| 并发 | ThreadPoolExecutor | I/O 密集场景，账号级并发（每账号独立 Session） |
| 测试 | pytest | 单测（Mock Session）+ integration（真实 PG）分层 |
| Lint/格式化 | ruff | 提交前必须通过 |

## 3. 目录结构与分层职责

```
src/email_agent/
├── config/            # AppConfig：环境变量加载，缺参时报错清晰
├── models/            # Account / EmailMessage 等 ORM 模型（声明式，兼领域契约）
│   ├── base.py        #   DeclarativeBase 基类
│   ├── account.py     #   Account：email_accounts 表映射 + 字段校验
│   └── message.py     #   EmailMessage：emails 表映射 + 字段校验
├── db/                # 引擎与连接池（基础设施）
│   └── engine.py      #   create_engine + sessionmaker + dispose（替代原手写 pool）
├── repository/        # 唯一允许写 SQL 的地方，基于 Session 的薄封装
│   ├── email_accounts.py  # AccountStore：get_enabled_accounts / update_checkpoint
│   └── emails.py          # EmailStore：bulk_insert（幂等）
├── email_clients/           # 邮件协议抽象层（扩展点）
│   ├── base.py        #   MailClient ABC
│   ├── factory.py     #   protocol 字段 → 具体实现注册表
│   └── imap/          #   第一个具体实现（imapclient）
├── parsing/           # 原始 RFC822 字节 → EmailMessage，纯函数
├── service/           # 业务编排：单账号流程 + 并发调度 + 失败隔离
├── bootstrap.py       # 组合根：装配 config/engine/repository，CLI 唯一粘合点
└── cli/               # 入口 python -m email_agent
tests/
├── unit/              # 与 src 包结构镜像对应（Session 以 MagicMock 注入）
└── integration/       # 需要真实 PG 的测试（真实 Session + 幂等/事务验证）
```

**依赖方向规则（强制）**：

- 上层依赖下层：`cli → bootstrap → service → clients/parsing/repository → db/models`
- `bootstrap.py` 是唯一组合根：依赖 config/db/repository，由 `cli` 调用；除它外，任何人不得反向依赖上层
- `db/` 保持纯基础设施，不得 import `repository` / `service`
- `models/` 是全局数据契约（ORM 实体），所有层通过它传递数据；映射/校验集中在模型内，互相不直接 import 对方的内部实现
- `repository/` 之外不允许出现任何 SQL 字符串；repository 内 SQL 以 SQLAlchemy 表达式/方言 insert 表达
- `service/` 只依赖 `MailClient` 抽象接口与 `repository` 的 Store 类，绝不 import 具体 MailClient 实现（实例化只发生在 factory）

## 4. 关键设计决策

1. **协议适配器模式**：新增邮件协议 = 在 `clients/` 下新建子包实现 `MailClient` ABC，并在 `factory.py` 注册。调度层零改动。
2. **增量拉取断点**：每账号在 `email_accounts.last_sync_uid` 记录上次拉取位置，IMAP 用 `UID SEARCH UID n+1:*` 只取新邮件。首跑全量。
3. **ORM 即数据契约 + Session 即事务单元**：`Account`/`EmailMessage` 是声明式 ORM 模型，同时充当领域对象；每个账号线程持一个独立 `Session`（借一条连接、管一个事务）。跨表写操作（邮件入库 + 断点推进）由一次 `session.commit()` 原子提交；任意失败则一次 `session.rollback()` 全部回滚，天然满足多表事务一致性。
4. **幂等写入**：`emails` 表 `UNIQUE(account_id, uid)` 约束 + SQLAlchemy `pg_insert(...).on_conflict_do_nothing()`，重复执行不产生重复数据。
5. **失败隔离**：单账号同步全程 try/except 包裹，失败 `session.rollback()` 并继续其他账号；结束输出汇总报告。
6. **可测试性**：`sync_account` 通过参数注入 `session`/client 工厂/repository，测试用 FakeMailClient + MagicMock Session 全流程验证；时间等外部因素一律参数注入。
7. **安全红线**：密码、密码相关变量永不写入日志；`.env` 不入库。

## 5. 分阶段实施计划

> 每阶段完成必须有绿色测试作为验收证据；进度随开发更新勾选框。

### 阶段 0：工程骨架
- [ ] src 目录结构建立
- [ ] pyproject.toml（含 dev extras），`pip install -e ".[dev]"` 可安装
- [ ] `.env.example`、pytest/ruff 最小配置
- ✅ 验收：`python -m email_agent --help` 有响应

### 阶段 1：领域模型 + 配置（TDD 起点）
- [ ] `models/base.py`、`models/account.py`、`models/message.py` ORM 模型及字段契约测试
- [ ] `config/settings.py` 环境变量加载
- ✅ 验收：缺失/非法配置报错明确；模型单测绿

### 阶段 2：数据库访问层（已改为 SQLAlchemy ORM）
- [ ] 按 docs/db-schema.md 第 2 节 DDL 初始化 email_accounts / emails 两张表
- [ ] `db/engine.py` 引擎与连接池（替代原 `db/pool.py`）
- [ ] repository：Session 绑定的 AccountStore / EmailStore（get_enabled_accounts / update_checkpoint / 幂等批量插入）
- ✅ 验收：integration 测试连真实 PG 跑通建表→写入→幂等重插→跨表事务原子提交

### 阶段 3：邮件抽象层 + 解析器
- [ ] `email_clients/base.py` MailClient ABC：connect / fetch_emails(folder, since_uid) / close
- [ ] `email_clients/factory.py` 按 protocol 创建实例
- [ ] `parsing/parser.py`（TDD 重点区：编码、多收件人、空主题、HTML/纯文本双 body）
- [ ] `email_clients/imap/client.py` 基于 imapclient 实现
- ✅ 验收：parser 边界单测全绿；IMAP 实现可用 mock 连接对象单测

### 阶段 4：业务编排（Session 即事务单元）
- [ ] `service/sync.py`：建客户端→增量拉取→解析→批量入库→回写断点（共享 Session 一次提交）
- [ ] ThreadPoolExecutor 并发 + 账号级异常隔离（每账号独立 Session）
- [ ] 显式超时、每账号结果记录
- ✅ 验收：FakeMailClient + MagicMock Session 单测绿，覆盖「某账号抛异常」「重复执行不重复入库」「多表原子回滚」

### 阶段 5：CLI 入口 + 端到端联调
- [ ] `cli/main.py`：`--limit`（首跑限量）、`--full`（忽略断点全量）；汇总报告
- [ ] 结构化日志
- [ ] 真实邮箱联调：首跑全量 → 二跑增量为 0（验证断点生效）
- ✅ 验收：DB→IMAP→DB 完整链路跑通且重跑幂等

## 6. 工程规约

- **TDD 默认开启**：新功能与 bug 修复先写失败测试再实现；每个 bug 修复附带回归测试。
- **测试命名**：`test_<被测行为>_<场景>_<预期>.py::test_xxx`，行为导向而非方法名导向。
- **注释**：只在表达"为什么"时写注释；代码自解释优先。
- **错误处理**：显式捕获带上下文重新抛出，禁止裸 except 吞错。
- **数据库变更流程**：先更新 `docs/db-schema.md` 表结构定义 → 在其「变更记录」节追加带日期的 SQL 操作 → 写 integration 测试。
- **提交信息**：说明为什么改，而不只是改了什么。
