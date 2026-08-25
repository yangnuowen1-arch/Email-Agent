from __future__ import annotations

import argparse
import contextlib
import json
import logging
import re
import sys
from datetime import UTC, datetime

from email_agent.bootstrap import AppContext
from email_agent.config.settings import AppConfig
from email_agent.repository.email_accounts import AccountStore
from email_agent.service.sync import sync_all

logger = logging.getLogger(__name__)

# 用于掩码数据库 URL 中密码的正则，避免日志中泄露敏感信息
_URL_MASK_RE = re.compile(r"://[^@]+@")


def _mask_url(url: str) -> str:
    """将 URL 中的用户名密码部分替换为 ***:***。"""
    # 例如 postgresql://user:pass@host/db -> postgresql://***:***@host/db
    return _URL_MASK_RE.sub("://***:***@", url)


class JsonFormatter(logging.Formatter):
    """JSON 结构化日志格式化器，确保密码不会通过日志泄露。"""

    def format(self, record: logging.LogRecord) -> str:
        # 构建基础日志载荷，包含时间戳、级别、日志器名和消息
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }
        # 若有异常堆栈，一并格式化到载荷中
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc_info"] = self.formatException(record.exc_info)
        # 防御性脱敏：若消息中意外包含带凭证的 URL，进行掩码处理
        # 这是兜底措施，首要保障是调用方根本不将密码写入日志
        try:
            msg = str(payload["message"])
            # 仅当消息中同时包含 :// 和 @ 时才视为可能的 URL
            if "://" in msg and "@" in msg:
                payload["message"] = _URL_MASK_RE.sub("://***:***@", msg)
        except Exception:
            pass
        # 序列化为 JSON，ensure_ascii=False 保证中文正常显示
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str) -> None:
    """初始化全局日志配置，使用 JSON 格式输出到 stderr。"""
    # 将字符串级别转为 logging 级别常量，非法值回退到 INFO
    lvl = getattr(logging, level.upper(), logging.INFO) if isinstance(level, str) else level
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    # 移除之前已添加的 JsonFormatter 处理器，避免重复输出
    # 同时保留 pytest caplog 等其他处理器
    root.handlers = [
        h for h in root.handlers if not isinstance(getattr(h, "formatter", None), JsonFormatter)
    ]
    root.addHandler(handler)
    root.setLevel(lvl)
    # 确保本包的日志器也服从该级别，避免被父级配置覆盖
    logging.getLogger("email_agent").setLevel(lvl)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        prog="email-agent",
        description="Sync emails from IMAP to PostgreSQL incrementally",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "limit number of emails per account (debug/first-run, when set breakpoint not updated)"
        ),
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="ignore last_sync_uid and fetch all emails",
    )
    args = parser.parse_args(argv)
    # 校验 limit 必须为正整数，argparse 默认不做正数校验
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive int")
    return args


def print_summary(results, *, full: bool, limit: int | None) -> None:
    """打印同步汇总报告到 stdout，供运维直观查看。"""
    # 统计总量
    total_fetched = sum(r.fetched for r in results)
    total_inserted = sum(r.inserted for r in results)
    total_failed = sum(1 for r in results if r.error)
    header = f"Sync Summary (full={full}, limit={limit}, total={len(results)})"
    print(header)
    print("-" * 80)
    print(f"{'account':<20} {'fetched':>7} {'inserted':>8} {'skipped':>7} {'max_uid':>8}  status")
    print("-" * 80)
    for r in results:
        # 根据是否有错误决定状态文案
        status = "OK" if r.error is None else f"ERROR: {r.error}"
        # 截断过长的状态信息，保持表格可读
        if len(status) > 30:
            status = status[:27] + "..."
        label = f"{r.name} (id={r.account_id})"
        if len(label) > 20:
            label = label[:17] + "..."
        print(f"{label:<20} {r.fetched:>7} {r.inserted:>8} {r.skipped:>7} {r.max_uid:>8}  {status}")
    print("-" * 80)
    print(
        f"Total: {len(results)} accounts, {total_fetched} fetched, "
        f"{total_inserted} inserted, {total_failed} failed"
    )
    # 限量模式下提示断点未推进，避免用户误以为已增量
    if limit is not None:
        print("Note: --limit was set, breakpoint was NOT updated (debug mode).")


def _run_sync(args: argparse.Namespace, config: AppConfig) -> int:
    """执行同步主流程：初始化 AppContext→查询账号→并发同步→打印汇总。"""
    try:
        # 初始化组合根（内部 init_engine），失败则记录日志并返回非零退出码
        ctx = AppContext(config)
    except Exception as exc:
        logger.error("failed to init DB engine: %s", exc)
        return 1

    try:
        # 用一个 Session 查询所有启用账号，查询后关闭连接（账号对象随后为游离态只读使用）
        read_session = ctx.session_factory()
        try:
            accounts = AccountStore(read_session).get_enabled_accounts()
        except Exception as exc:
            logger.error("failed to fetch enabled accounts: %s", exc)
            return 1
        finally:
            with contextlib.suppress(Exception):
                read_session.close()

        # 无启用账号时直接提示并正常退出
        if not accounts:
            logger.info("no enabled accounts found")
            print("No enabled accounts.")
            return 0

        logger.info("found %s enabled accounts", len(accounts))

        # 并发同步所有账号，内部已做失败隔离；每个账号一个独立 Session（事务单元）
        results = sync_all(
            accounts,
            session_factory=ctx.session_factory,
            max_workers=config.sync_max_workers,
            timeout=config.sync_timeout_seconds,
            limit=args.limit,
            full=args.full,
        )

        # 打印汇总表格到 stdout
        print_summary(results, full=args.full, limit=args.limit)

        # 记录失败详情，区分完全成功和部分失败
        failed = [r for r in results if r.error]
        if failed:
            for r in failed:
                logger.error("account %s (id=%s) failed: %s", r.name, r.account_id, r.error)
            logger.warning("sync completed with %s/%s failures", len(failed), len(results))
        else:
            logger.info("sync completed successfully for %s accounts", len(results))

        # 按用户决策 4：无论是否有失败，都返回 0，仅通过日志区分
        return 0

    finally:
        # 退出前关闭所有连接，释放资源
        with contextlib.suppress(Exception):
            ctx.close_all()


def main(argv: list[str] | None = None) -> None:
    """CLI 入口函数，负责参数解析、配置加载、日志初始化和流程调度。"""
    # 先解析命令行参数，参数错误会直接退出
    args = parse_args(argv)

    try:
        # 从环境变量/.env 加载配置，缺失必填项时会抛 ValueError
        config = AppConfig.from_env()
    except ValueError as exc:
        # 配置加载失败时，使用基础日志配置，避免因日志未初始化而无输出
        logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(message)s")
        # 对错误信息中的 URL 进行脱敏
        msg = str(exc)
        if "://" in msg:
            msg = _mask_url(msg)
        logging.getLogger(__name__).error("config error: %s", msg)
        sys.exit(2)

    # 初始化结构化日志，级别来自配置
    setup_logging(config.log_level)

    # 记录启动参数（已脱敏），便于运维追溯
    logger.info(
        "starting sync full=%s limit=%s workers=%s timeout=%s",
        args.full,
        args.limit,
        config.sync_max_workers,
        config.sync_timeout_seconds,
    )

    # 执行同步主流程
    exit_code = _run_sync(args, config)
    sys.exit(exit_code)


if __name__ == "__main__":
    # 支持 python -m email_agent 直接运行
    main()
