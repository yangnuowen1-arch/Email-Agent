# Email-Agent Architecture

## Product boundary

Email-Agent is being evolved into a human-supervised enterprise-mail assistant:

1. read and archive incoming mail through IMAP;
2. classify mail, extract information, and prepare reply drafts through an LLM;
3. require a human approval before any outbound action is sent through SMTP.

The current runnable entry point covers only the first capability: **inbound
mail sync and archival**.  The codebase may add read-only tool and LLM-gateway
contracts ahead of a user-facing agent entry point, but those contracts do not
enable automatic mail analysis, approval, or SMTP delivery.  Those capabilities
have different side-effect and audit requirements and will be added as separate
use cases.

## Dependency direction

```text
CLI / future API
        |
        v
application services
        |
        +--> ports (Protocols) <---- IMAP / SQLAlchemy adapters
        |
        v
schemas (pure data contracts)

core/container.py creates and wires the concrete adapters.
```

`services` contains application and deterministic domain rules.  It must not
import SQLAlchemy models, repositories, an IMAP SDK, or environment variables.
`providers` contains protocol-specific adapters.  `db` contains SQLAlchemy
models and repositories plus the storage adapter.  `core` contains one settings
entry point and the composition root; it does not contain business workflows.

## Read-only mail-tool foundation

Before introducing an agent loop, the system may expose already archived mail
through a small set of model-visible, **read-only** tools:

```text
trusted caller scope
        |
        v
typed Tool (JSON-schema validation, per-call authorization, timeout)
        |
        v
mail-query service
        |
        v
query port <---- SQLAlchemy query adapter ---- emails
```

The trusted caller supplies the account IDs it is allowed to access.  The tool
must reject an explicitly requested account outside that scope, and the query
adapter must apply the same scope in its database predicate.  A missing email
is reported as a stable structured observation rather than revealing whether a
different account owns it.

The initial tools are intentionally narrow:

- `search_mail`: search previously stored mail by text and/or sender, returning
  metadata and a short text snippet;
- `get_email_context`: return a bounded plain-text context for one mail ID.

Tools call deterministic services, never ORM repositories or IMAP clients
directly.  Their inputs and result models use Pydantic so a gateway can export
JSON Schema to a provider while validating returned tool arguments locally.
The initial LLM gateway contract may carry messages, tool definitions, and tool
calls without selecting or configuring a concrete model provider.

The default tool set is built in `app.tools.registry`, not in the composition
root.  A future authenticated Agent/API request supplies the container's
already-wired services to that factory, so new default tools have one
discoverable registration point without making a tool registry process-global.

## Inbound sync contract

The inbound use case is:

> For every enabled mailbox, read the requested UID range, parse the available
> messages, store them idempotently, and advance the checkpoint only when the
> whole selected range was handled safely.

The public inputs and outputs are defined in `app.schemas`:

- `SyncRequest`: full versus incremental mode and an optional debug limit;
- `MailboxReadResult`: parsed mail input plus failed UIDs observed by the reader;
- `PersistResult`: inserted and duplicate counts returned by storage;
- `AccountSyncResult` and `SyncReport`: user-facing per-account and batch results.

The two application ports are intentionally small:

- `InboundMailbox` reads one mailbox from a UID boundary;
- `EmailSyncStore` lists enabled accounts and atomically persists a batch with an
  optional checkpoint update.

The storage port owns the transaction containing message insertion and
checkpoint update.  Splitting those operations into two independent calls would
allow a crash to lose the relationship between them.
The SQLAlchemy adapter also makes checkpoint writes monotonic, so overlapping
processes cannot replace a newer cursor with an older value read earlier.

### Checkpoint safety

`--limit` is a debug/replay mode: it may write mail but never advances the
checkpoint.  In normal or full mode, an unreadable or unparseable UID prevents
checkpoint advancement for that account.  Successfully parsed messages may
still be stored; a later retry is safe because `(account_id, uid)` is unique.
This trades an inexpensive duplicate read for avoiding permanent mail loss.

## Future capabilities

The next phases remain separate from syncing:

```text
stored mail --> analysis service --> reply-draft service --> pending approval
                                                               |
human approval ------------------------------------------------+
                                                               v
                                                    send-approved-draft --> SMTP
```

The LLM may produce only typed analysis and draft proposals.  It may not mark a
draft approved or send mail.  Approval will be a persisted, versioned state
machine, and the SMTP adapter will be reachable only from an
`SendApprovedDraft` use case after a fresh approval check.

## Testing boundary

- service tests use fake `InboundMailbox` and `EmailSyncStore` implementations;
- IMAP adapter tests mock only the IMAP client;
- SQLAlchemy adapter tests verify ORM mapping and transaction semantics;
- mail-tool tests use a fake query service to cover schema validation, account
  scope, not-found/error observations, and registry timeouts;
- future LLM tests use a scripted fake gateway, and future SMTP tests prove that
  an unapproved draft cannot trigger a send.
