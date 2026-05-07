# Decision Ledger

Decision Ledger is a local-first record of thoughts, decisions, assumptions,
questions, evidence, and associations. It is intended to give humans and LLM
agents a shared, auditable memory that is more precise than loose markdown, but
still easy to browse and export.

The core idea is:

- Namespace JSONL event files are the canonical store.
- SQLite is a generated projection for fast query, search, and export.
- Records are append-friendly and audit-oriented.
- Dot-separated subjects provide a stable namespace tree.
- Evidence links make claims inspectable.
- Validation state distinguishes checked claims from unvalidated ideas.
- Associations form a graph across records when namespace alone is not enough.
- Static HTML exports make any namespace subtree browsable in nginx.
- Markdown and static HTML remain readable projections, not the source of truth.

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

By default this creates or migrates the nearest `.decision-ledger` folder found
by walking upward from the current directory. If none is found, it uses:

```text
~/.decision-ledger/ledger.sqlite
```

Use `--home`, `DECISION_LEDGER_HOME`, `--db`, or `DECISION_LEDGER_DB` for a
different ledger:

```bash
./bin/decisions --home ./.decision-ledger init
./bin/decisions --db /tmp/ledger.sqlite init
```

The canonical git-friendly layout is:

```text
.decision-ledger/
  events/
    connected-ai/
      auth/
        oidc/
          client-persistence.jsonl
  ledger.sqlite
```

Commit `events/**/*.jsonl`. Treat `ledger.sqlite` as generated; this repo's
`.gitignore` ignores `*.sqlite`.

CLI and MCP write operations append to `events/<subject path>.jsonl` first, then
apply the same event into SQLite. If `ledger.sqlite` is missing but event files
exist, the projection is rebuilt automatically on startup.

Rebuild the SQLite projection from canonical event files:

```bash
./bin/decisions rebuild
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

Set validation state separately from lifecycle status:

```bash
./bin/decisions add connected-ai.auth.oidc.client-persistence \
  --kind finding \
  --status active \
  --validation-state partially_validated \
  --summary "Dynamic clients may be clobbered" \
  --body "This has supporting evidence, but has not yet been reproduced end to end."

./bin/decisions validate rec_... \
  --state validated \
  --validated-by neil \
  --note "Confirmed against current OpenIddictApplications rows and restart logs"
```

`status` is lifecycle/currentness. `validation_state` is epistemic quality:

- `unvalidated`: captured but not checked
- `partially_validated`: some supporting evidence, still incomplete
- `validated`: checked against sufficient evidence for the current use
- `contested`: credible evidence or review challenges it
- `invalidated`: evidence shows it should not be treated as true

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
- `decision_rebuild_projection`
- `decision_add_record`
- `decision_add_evidence`
- `decision_validate_record`
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

- treat namespace JSONL event files as canonical and SQLite as a generated
  projection
- gather current subject context before making durable claims
- prefer current records for reasoning
- keep lifecycle status separate from validation state
- prefer validated records for audit-sensitive factual claims
- label uncertainty when relying on unvalidated or partially validated records
- treat superseded records as audit history unless explicitly requested
- supersede or withdraw records instead of deleting them for normal forgetting
- attach evidence for audit-worthy claims
- associate records across namespaces when subject prefix alone is insufficient
- preserve detail rather than shrinking source material, but split multi-decision
  material into linked records when parts need independent subjects, tags,
  evidence, statuses, or supersession paths
- process durable decisions and ideas buried in markdown, tickets, transcripts,
  or repo docs into the subject tree, with the source document as evidence
- export namespace subtrees as static HTML audit packs when a browsable handover
  is useful

The implementation follows the MCP stdio shape: newline-delimited JSON-RPC on
stdin/stdout, no stdout logging, `initialize`, `tools/list`, `tools/call`,
`prompts/list`, and `prompts/get`.
