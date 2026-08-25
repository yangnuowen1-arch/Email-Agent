# 计划：整理 src/email_agent/email_clients/ —— 移除对 models 的依赖

## 目标
`email_clients` 包自包含，不 import `email_agent.models.*`；同时清理冗余代码与 what 型注释。
对外 API 不变（新增 `AccountConfig` 导出），service 层与既有测试零改动。

## 已确认方案（用户选定）
在 base.py 声明本地具体类 `AccountConfig`（冻结 dataclass）替代对 ORM `Account` 的类型引用；
真实 ORM Account 字段同名鸭子类型兼容，原样传递。

## 变更清单

### 1. tests/unit/email_clients/test_decoupling.py（新增，TDD 先行）
- test_account_config_defaults_match_account_contract：AccountConfig 默认值契约（port=993/protocol=imap/use_ssl=True/folder=INBOX）
- test_imap_client_works_with_pure_config_without_orm_account：纯 AccountConfig + MagicMock client_cls 跑通 connect/fetch
- test_import_email_clients_does_not_load_models：子进程 import 后断言 sys.modules 无 email_agent.models

### 2. src/email_agent/email_clients/base.py
- 删 `if TYPE_CHECKING: from email_agent.models.account import Account`
- 新增：
  ```python
  @dataclass(frozen=True)
  class AccountConfig:
      name: str
      host: str
      username: str
      password: str
      port: int = 993
      protocol: str = "imap"
      use_ssl: bool = True
      folder: str = "INBOX"
  ```
- `MailClient.__init__(account: AccountConfig)`，原样保存传入对象
- 注释只保留"为什么"（异常不含密码、close 不掩盖原始异常、返回 False 不吞异常）

### 3. src/email_agent/email_clients/factory.py
- 删 TYPE_CHECKING 块；签名改 `create_client(account: AccountConfig)`
- 删除 `_lazy_register_defaults()`（注释声称的循环导入不存在，且模块底部已无条件调用=伪懒加载）
- 改为模块级 `_REGISTRY = {"imap": ImapMailClient}`（顶部直接 import）
- create_client 简化为直接属性访问 account.protocol

### 4. src/email_agent/email_clients/imap/client.py
- 删除 `if __import__("typing").TYPE_CHECKING:` hack，类型标注用 AccountConfig
- 异常处理合并为单一 `except Exception → wrap MailClientError`（原网络分支与兜底分支消息完全相同，属重复代码）；不再需要 socket/IMAPClientError import
- 删除防御性死代码 `except MailClientError: raise`（try 体不会抛它）
- fetch 内 `uids[:limit]` 后的空判断为死代码（limit>0 已校验），删除
- close() 用 contextlib.suppress 替代 except/pass；删 finally 冗余
- client_cls 参数注释规范化（测试注入 MagicMock 的原因）

### 5. src/email_agent/email_clients/__init__.py
- 导出 AccountConfig；__all__ 同步更新

### 6. imap/__init__.py
- 不变

## 验收
- pytest tests/unit/email_clients tests/unit/service -q 全绿
- ruff check 通过
- 对外行为不变：现有全部测试无需修改即通过
