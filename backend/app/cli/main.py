from __future__ import annotations

import asyncio

import typer

from app.core.container import Container
from app.core.settings import AppConfig
from app.llm.errors import LLMConfigurationError

app = typer.Typer(
    help="email-agent-cli",
    invoke_without_command=True,
)


@app.callback()
def cli(ctx: typer.Context) -> None:
    """构建容器并存入上下文；具体工作由子命令执行。

    默认不强制要求数据库；需要数据库的 ``sync`` / ``email-agent`` 子命令会自行校验。
    """
    try:
        config = AppConfig.from_env(require_database=False)
    except ValueError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    try:
        container = Container(config)
    except Exception as exc:
        typer.echo(f"failed to initialize container: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    container.logger.info(
        "container_initialized",
        log_level=config.log_level,
        db_pool_min=config.db_pool_min_size,
        db_pool_max=config.db_pool_max_size,
    )
    ctx.obj = container


@app.command(name="email_agent")
def email_agent(
    ctx: typer.Context,
) -> None:
    """邮件智能体：读取未分析邮件并进入意向分析，结果落库 email_analyses。"""
    container: Container = ctx.obj

    if not container.config.database_url:
        container.logger.error("email_agent_requires_database")
        typer.echo(
            "DATABASE_URL is required for email-agent; set it in environment or .env",
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        results = asyncio.run(container.email_coordinator.start_analyze())
    except LLMConfigurationError as exc:
        container.logger.error("agent_config_error", error=str(exc))
        raise typer.Exit(code=2) from exc

    analyzed = sum(1 for r in results if r.get("status") != "failed")
    failed = sum(1 for r in results if r.get("status") == "failed")

    for r in results:
        container.logger.info("email_analysis_result", result=r)

    typer.echo(f"analyzed={analyzed} failed={failed}")

@app.command()
def sync(ctx: typer.Context) -> None:
    """从各启用账号拉取邮件并落库（并发数/超时由配置决定，无命令行参数）。"""
    container: Container = ctx.obj
    if not container.config.database_url:
        container.logger.error("sync_requires_database")
        typer.echo(
            "DATABASE_URL is required for sync; set it in environment or .env",
            err=True,
        )
        raise typer.Exit(code=2)
    asyncio.run(_run(container))


async def _run(container: Container) -> None:
    log = container.logger
    log.info("sync_started")

    report = await container.synchronizer.sync_accounts()

    log.info(
        "sync_finished",
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
                "account_sync_failed",
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
