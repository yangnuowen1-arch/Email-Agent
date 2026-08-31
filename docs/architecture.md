# Email-Agent Architecture

## Product boundary

Email-Agent is being evolved into a human-supervised enterprise-mail assistant:

1. read and archive incoming mail through IMAP;
2. classify mail, extract information, and prepare reply drafts through an LLM;
3. require a human approval before any outbound action is sent through SMTP.

The runnable entry point covers inbound mail sync and a deliberately local
workflow CLI for manually exercising analysis, draft creation, and human
approval.  The CLI resolves a named profile to a server-configured actor, role,
and mailbox scope; it is an acceptance-test boundary, not a multi-user
authentication system.  It does not automatically analyze new mail, schedule
work, or deliver mail through SMTP.  Those capabilities have different
side-effect and audit requirements and will be added as separate use cases.

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
The provider-neutral LLM gateway carries messages, tool definitions, and tool
calls without selecting or configuring a concrete model provider.  An assistant
message retains the tool-call IDs, names, and arguments returned by the model;
each subsequent tool message references one of those IDs and contains a JSON
encoded structured observation.  This produces a replayable transcript for
the next model turn without allowing the model to choose its own account scope.

``ScriptedLLMGateway`` is a deterministic test double rather than a production
provider.  It records requests and returns a configured sequence of model
responses, so tool loops are tested without network access or model cost.

`GeminiLLMGateway` is the production adapter for the Gemini Developer API. It
maps this replayable transcript to Gemini content, function calls, and grouped
function responses; it keeps the API key in configuration only and maps only
network/limit/5xx failures to retryable gateway errors. The IMAP CLI does not
construct this adapter: an authenticated API or worker must explicitly inject
it, so synchronizing mail cannot unintentionally send content to a model.

The default tool set is built in `app.tools.registry`, not in the composition
root.  A future authenticated Agent/API request supplies the container's
already-wired services to that factory, so new default tools have one
discoverable registration point without making a tool registry process-global.

## Minimal LangGraph tool loop

The first agent runtime lives in `app.agent` and deliberately has a narrow,
read-only responsibility:

```text
trusted run request
        |
        v
model -- no tool call --> end
  |
  +-- tool calls --> registry dispatcher --> structured observations
                                     |
                                     +--------> model
```

The graph receives an injected `LLMGateway` and `ToolRegistry`; it never
imports ORM repositories, providers, or concrete LLM SDKs.  Trusted account
scope is input from the server-side caller and is reconstructed into a
`ToolContext` only at the dispatch node.  The model cannot widen that scope.
New runs accept only system and user messages; a future resume feature must
load prior assistant/tool transcript from trusted server-side storage rather
than accepting it from an untrusted request.

Each run has a generated `run_id`, a bounded number of model turns, a bounded
number of tool calls per model turn, a model-response timeout, an ordered tool
event trail (tool name, call ID, success, duration, and error code), and a
stable terminal reason.  Model and tool nodes also use a bounded retry policy:
only a typed `TransientLLMError` or a local timeout is retried. Authentication,
invalid output, and unexpected errors become a non-retryable terminal result.
Safe node events record the node, attempt, error category, and retry decision
without exposing provider exception text. A normal tool business failure still
remains a structured observation rather than a graph retry.

The graph does not install a persistent checkpointer yet: its state can contain
authorized email context and must not be silently persisted without an explicit
retention, encryption, and access-control design.

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

## Analysis, reply draft, and human review

The workflow below is implemented as application services and remains separate
from syncing.  A local CLI may invoke each step explicitly after resolving a
trusted local profile; a future API must derive the same authority from real
authentication rather than trusting request fields:

```text
stored mail --> analysis service --> reply-draft service --> pending approval
                                                               |
human approval ------------------------------------------------+--> approved
                                                                    |
                                                                    v
                                               future send-approved-draft --> SMTP
```

The LLM may produce only typed analysis and draft proposals. It may not attach
trusted IDs, widen account scope, mark a draft approved, or send mail. The
services persist analyses independently and append a new immutable draft
version plus a matching audit transition for every create, revision, submit,
approval, rejection, or withdrawal. The review state machine is:

```text
draft → pending_review → approved
                       └→ rejected → revised draft
pending_review → withdrawn draft
```

Each write uses the caller's expected version, so a concurrent edit fails with
a typed version conflict instead of overwriting an approved review. `approved`
is a business state only: there is still no SMTP adapter or send use case. A
future `SendApprovedDraft` use case must recheck the current approved version
inside its own transaction before it can reach SMTP.

## Testing boundary

- service tests use fake `InboundMailbox` and `EmailSyncStore` implementations;
- IMAP adapter tests mock only the IMAP client;
- SQLAlchemy adapter tests verify ORM mapping and transaction semantics;
- mail-tool tests use a fake query service to cover schema validation, account
  scope, not-found/error observations, and registry timeouts;
- LLM and agent tests use a scripted fake gateway to cover no-tool paths,
  model-to-tool-to-model transcript replay, typed transient retries, timeout
  exhaustion, non-retryable failures, and the maximum-turn/tool-call limits;
- workflow tests use fake analyzer, generator, and stores to cover account
  scope, typed analyses, immutable draft revisions, human approval transitions,
  and optimistic-concurrency conflicts; SQLAlchemy tests verify that a version
  and its audit transition commit together;
- a future SMTP test suite must prove that an unapproved or stale draft cannot
  trigger a send.
