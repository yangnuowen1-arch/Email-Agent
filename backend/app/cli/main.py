from __future__ import annotations

import asyncio

import typer

from app.core.container import Container
from app.core.settings import AppConfig

app = typer.Typer(
    help="email-agent-cli",
    invoke_without_command=True,
)


@app.callback()
def cli(ctx: typer.Context) -> None:
    """构建容器并存入上下文；具体工作由子命令执行。"""
    try:
        config = AppConfig.from_env()
    except ValueError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    try:
        container = Container(config)
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
def ingest(ctx: typer.Context) -> None:
    """从各启用账号拉取邮件并落库（并发数/超时由配置决定，无命令行参数）。"""
    container: Container = ctx.obj
    asyncio.run(_run(container))


async def _run(container: Container) -> None:
    log = container.logger
    log.info("ingest_started")

    report = await container.coordinator.ingest_accounts()

    log.info(
        "ingest_finished",
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

    # close_all 是异步生命周期；CLI 是同步边界，由这里桥接事件循环
    await container.close_all()
    log.info("container_closed")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
