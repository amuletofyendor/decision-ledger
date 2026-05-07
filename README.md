# Decision Ledger

Decision Ledger is a local-first record of thoughts, decisions, assumptions,
questions, evidence, and associations. It is intended to give humans and LLM
agents a shared, auditable memory that is more precise than loose markdown, but
still easy to browse and export.

The core idea is:

- SQLite is the canonical store.
- Records are append-friendly and audit-oriented.
- Dot-separated subjects provide a stable namespace tree.
- Evidence links make claims inspectable.
- Associations form a graph across records when namespace alone is not enough.
- Static HTML exports make any namespace subtree browsable in nginx.
- Markdown remains a readable projection, not the source of truth.

The detailed plan is in [docs/decision-ledger-plan.md](docs/decision-ledger-plan.md).

## Target Queries

Examples this project should support:

```text
Forget decisions on connected-ai.auth.oidc from before 11am this morning.
Gather all previous thoughts about connected-ai.retrieval.wiki.
Show current accepted decisions under connected-ai.auth.
Show superseded assumptions that influenced this decision.
Show everything associated with this record, even outside its namespace.
Export connected-ai.auth as a static wiki for review.
```

In this context, "forget" means "exclude from future reasoning by marking as
superseded or withdrawn", not "delete audit history".

## Repository Contents

- [docs/decision-ledger-plan.md](docs/decision-ledger-plan.md): full architecture
  and implementation plan.
- [schema/001_initial.sql](schema/001_initial.sql): first-pass SQLite schema.
- [examples/example-record.yaml](examples/example-record.yaml): example record
  with evidence and associations.

## CLI Quick Start

Run the CLI from this repo:

```bash
./bin/decisions init
```

By default this creates or migrates:

```text
~/.decision-ledger/ledger.sqlite
```

Use `--db` or `DECISION_LEDGER_DB` for a different ledger:

```bash
./bin/decisions --db /tmp/ledger.sqlite init
```

Add a thought:

```bash
./bin/decisions add connected-ai.auth.oidc.client-persistence \
  --kind thought \
  --summary "MCP dynamic clients may be clobbered by identity restarts" \
  --body "Working thought: dynamic clients may be overwritten by another identity deployment sharing the same backing DB." \
  --tag mcp \
  --tag oidc
```

Attach evidence:

```bash
./bin/decisions evidence add rec_... \
  --type file \
  --uri /home/neil/Dev/fc.identity \
  --note "Identity service source and seeding behavior"
```

Associate records:

```bash
./bin/decisions associate rec_... rec_... \
  --relation depends_on \
  --note "This auth thought depends on the shared DB ownership finding"
```

Gather current context for a namespace:

```bash
./bin/decisions gather connected-ai.auth
```

List available topics in the subject tree:

```bash
./bin/decisions topics connected-ai.auth --direct
```

Supersede a single record:

```bash
./bin/decisions supersede rec_old rec_new \
  --note "New decision replaces the earlier proposal"
```

Bulk supersede records under a namespace before a timestamp:

```bash
./bin/decisions supersede connected-ai.auth.oidc \
  --before "2026-05-07 11:00" \
  --replacement rec_new \
  --note "Earlier records were superseded by the 11am design revision"
```

Commands that support `--json` produce stable machine-readable output for
future agent integration.

Export a subject subtree as a static wiki:

```bash
./bin/decisions wiki connected-ai.auth \
  --out ./public \
  --all \
  --profile internal
```

The static export writes:

```text
public/
  index.html
  subjects/.../index.html
  records/.../index.html
  assets/styles.css
  assets/search-index.json
  assets/graph.json
```

Export profiles are deliberately conservative:

- `internal`: includes private, internal, shareable, and public records/evidence
- `shareable`: includes only shareable and public records/evidence
- `public`: includes only public records/evidence

Use `--clean` to remove the output directory before regenerating it.

## MCP Server

The repo also includes a dependency-free stdio MCP server:

```bash
./bin/decision-ledger-mcp
```

Use a specific ledger with `--db` or `DECISION_LEDGER_DB`:

```bash
./bin/decision-ledger-mcp --db ~/.decision-ledger/ledger.sqlite
```

Example Codex MCP config:

```toml
[mcp_servers.decision-ledger]
command = "/home/neil/Dev/decision-ledger/bin/decision-ledger-mcp"
args = ["--db", "/home/neil/.decision-ledger/ledger.sqlite"]
```

The MCP server exposes tools for:

- `decision_guidance`
- `decision_add_record`
- `decision_add_evidence`
- `decision_associate_records`
- `decision_supersede_record`
- `decision_supersede_subject_before`
- `decision_gather`
- `decision_search`
- `decision_show_record`
- `decision_list_records`
- `decision_list_topics`
- `decision_export_wiki`

It also exposes prompt templates:

- `decision-ledger-best-practices`
- `capture-decision-context`

The MCP surface deliberately bakes in usage guidance:

- gather current subject context before making durable claims
- prefer current records for reasoning
- treat superseded records as audit history unless explicitly requested
- supersede or withdraw records instead of deleting them for normal forgetting
- attach evidence for audit-worthy claims
- associate records across namespaces when subject prefix alone is insufficient
- process durable decisions and ideas buried in markdown, tickets, transcripts,
  or repo docs into the subject tree, with the source document as evidence
- export namespace subtrees as static HTML audit packs when a browsable handover
  is useful

The implementation follows the MCP stdio shape: newline-delimited JSON-RPC on
stdin/stdout, no stdout logging, `initialize`, `tools/list`, `tools/call`,
`prompts/list`, and `prompts/get`.
