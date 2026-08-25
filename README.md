# Email-Agent

A small, conventional Python foundation for an email-focused AI agent. It keeps
configuration, prompts, LLM adapters, tools, memory, tests, examples, and eval
fixtures separate so each can grow independently.

## Quick start

```bash
uv sync
uv run email-agent
uv run pytest -q
```

Configuration is read from environment variables and an optional `.env` file.
The starter has no framework dependency; add one when the agent's workflow is
clear.
