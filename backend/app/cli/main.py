from __future__ import annotations

import asyncio

import typer

from app.core.container import Container, build_container
from app.core.settings import AppConfig
from app.schemas import SyncRequest

app = typer.Typer(
    help="email-agent-cli",
    invoke_without_command=True,
)


@app.callback()
def cli(ctx: typer.Context) -> None:
    """构建子命令所需的容器；裸命令仅显示帮助。"""
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()

    try:
        config = AppConfig.from_env()
    except ValueError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    try:
        container = build_container(config)
    except Exception as exc:
        typer.echo(f"failed to init DB engine: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    container.logger.info(
        "container_initialized",
        log_level=config.log_level,
        db_pool_min=config.db_pool_min_size,
        db_pool_max=config.db_pool_max_size,
    )
    ctx.obj = container


@app.command()
def ingest(
    ctx: typer.Context,
    full: bool = typer.Option(
        False,
        "--full",
        help="忽略断点，扫描整个邮箱文件夹。",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        min=1,
        help="最多处理 N 封邮件；指定后不会推进同步断点。",
    ),
) -> None:
    """从各启用账号拉取邮件并落库。"""
    container: Container = ctx.obj
    asyncio.run(_run(container, full=full, limit=limit))


async def _run(container: Container, *, full: bool, limit: int | None) -> None:
    log = container.logger
    log.info("ingest_started", full=full, limit=limit)

    try:
        report = await container.mail_sync.ingest(SyncRequest(full=full, limit=limit))

        log.info(
            "ingest_finished",
            full=full,
            limit=limit,
            inserted=report.total_inserted,
            skipped=report.total_skipped,
            failed=report.total_failed,
            duration_ms=report.duration_ms,
        )
        typer.echo(
            f"inserted={report.total_inserted} skipped={report.total_skipped} "
            f"failed={report.total_failed} duration_ms={report.duration_ms}"
        )
        for result in report.results:
            if result.error:
                log.error(
                    "account_ingest_failed",
                    account_id=result.account_id,
                    account_name=result.name,
                    error=result.error,
                )
                typer.echo(
                    f"  account {result.account_id} ({result.name}) ERROR: {result.error}",
                    err=True,
                )
    finally:
        # 无论同步是否在进程级失败，都要释放数据库连接池。
        await container.close_all()
        log.info("container_closed")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
