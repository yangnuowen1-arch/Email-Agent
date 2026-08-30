from __future__ import annotations

import asyncio
import contextlib
import signal

import typer

from app.core.container import Container
from app.core.settings import AppConfig
from app.llm.errors import LLMConfigurationError
from app.rag.errors import RAGError

app = typer.Typer(
    help="email-agent-cli",
    invoke_without_command=True,
)


@app.callback()
def cli(ctx: typer.Context) -> None:
    """构建容器并存入上下文；具体工作由子命令执行。"""
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


@app.command(name="kb_ingest")
def kb_ingest(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="知识文件路径（utf-8 文本，如 Markdown）"),
    kb_type: str = typer.Option(..., "--kb-type", help="知识类型：faq / sop / compliance"),
    title: str | None = typer.Option(None, "--title", help="文档标题，默认取文件名"),
    source_key: str | None = typer.Option(None, "--source-key", help="幂等键，默认 file:<path>"),
) -> None:
    """知识文件切块嵌入后入库（kb_documents / kb_chunks）；同 source_key 重跑幂等。"""
    container: Container = ctx.obj
    if not container.config.database_url:
        typer.echo("DATABASE_URL is required for kb_ingest", err=True)
        raise typer.Exit(code=2)

    try:
        result = asyncio.run(
            container.knowledge_ingestor.ingest_file(
                path, kb_type=kb_type, title=title, source_key=source_key
            )
        )
    except LLMConfigurationError as exc:
        container.logger.error("kb_ingest_config_error", error=str(exc))
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except (RAGError, ValueError, OSError) as exc:
        container.logger.error("kb_ingest_failed", error=str(exc))
        typer.echo(f"kb_ingest error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    container.logger.info(
        "kb_ingest_done",
        document_id=result.document_id,
        source_key=result.source_key,
        action=result.action,
        chunk_count=result.chunk_count,
    )
    typer.echo(
        f"action={result.action} document_id={result.document_id} "
        f"chunks={result.chunk_count} source_key={result.source_key}"
    )


@app.command(name="kb_search")
def kb_search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="自然语言查询"),
    kb_type: str = typer.Option(..., "--kb-type", help="知识类型：faq / sop / compliance"),
    top_k: int = typer.Option(5, "--top-k", min=1, help="返回条数"),
) -> None:
    """知识库相似度检索：打印命中块原文与余弦距离（越小越相似）。"""
    container: Container = ctx.obj
    if not container.config.database_url:
        typer.echo("DATABASE_URL is required for kb_search", err=True)
        raise typer.Exit(code=2)

    try:
        hits = asyncio.run(container.knowledge_retriever.retrieve(kb_type, query, top_k=top_k))
    except LLMConfigurationError as exc:
        container.logger.error("kb_search_config_error", error=str(exc))
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except (RAGError, ValueError) as exc:
        container.logger.error("kb_search_failed", error=str(exc))
        typer.echo(f"kb_search error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    container.logger.info("kb_search_done", hits=len(hits), kb_type=kb_type)
    if not hits:
        typer.echo("no results")
        return
    for rank, hit in enumerate(hits, start=1):
        typer.echo(
            f"[{rank}] distance={hit.distance:.4f} document_id={hit.document_id} "
            f"chunk_id={hit.chunk.id}"
        )
        typer.echo(hit.content)
        typer.echo("---")


@app.command()
def listen(ctx: typer.Context) -> None:
    """常驻监听各启用账号的新邮件（IMAP IDLE），收到即落库；Ctrl+C 优雅退出。"""
    container: Container = ctx.obj
    if not container.config.database_url:
        container.logger.error("listen_requires_database")
        typer.echo(
            "DATABASE_URL is required for listen; set it in environment or .env",
            err=True,
        )
        raise typer.Exit(code=2)
    asyncio.run(_listen(container))


async def _listen(container: Container) -> None:
    log = container.logger
    loop = asyncio.get_running_loop()

    # SIGINT/SIGTERM → 请求监听停止；接收线程在当前 ping 周期内退出后统一释放容器
    def _request_stop() -> None:
        log.info("listen_stop_requested")
        container.listener.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_stop)

    log.info("listen_started")
    try:
        await container.listener.run()
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(sig)

        # close_all 是异步生命周期；CLI 是同步边界，由这里桥接事件循环
        await container.close_all()
        log.info("container_closed")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
