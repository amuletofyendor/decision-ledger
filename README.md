# Decision Ledger

Decision Ledger is a local-first record of thoughts, ideas, snags, decisions,
assumptions, questions, evidence, and associations. It is intended to give
humans and LLM agents a shared, auditable memory that is more precise than
loose markdown, but still easy to browse.

The core idea is:

- Namespace JSONL event files are the canonical store.
- SQLite is a generated projection for fast query, lexical/vector search, and
  wiki serving.
- Records are append-friendly and audit-oriented.
- Dot-separated subjects provide a stable namespace tree.
- Evidence links make claims inspectable.
- Validation state distinguishes checked claims from unvalidated ideas.
- Associations form a graph across records when namespace alone is not enough.
- `decision-wiki-server` makes the live namespace tree browsable on demand.
- Markdown remains a readable projection, not the source of truth.

Durable architecture decisions for this project live in the ledger itself under
the `decision-ledger` subject tree. Browse them with `decision-wiki-server`.

## Target Queries

Examples this project should support:

```text
Forget decisions on connected-ai.auth.oidc from before 11am this morning.
Gather all previous thoughts about connected-ai.retrieval.wiki.
Show current accepted decisions under connected-ai.auth.
Show superseded assumptions that influenced this decision.
Show everything associated with this record, even outside its namespace.
Serve connected-ai.auth as a live wiki for review.
```

In this context, "forget" means "exclude from future reasoning by marking as
superseded or withdrawn", not "delete audit history".

## Repository Contents

- [schema/001_initial.sql](schema/001_initial.sql): first-pass SQLite schema.
- [examples/example-record.yaml](examples/example-record.yaml): example record
  with evidence and associations.

## CLI Quick Start

Create a local development environment:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e . pytest
```

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

`rebuild` also attempts to rebuild the generated vector projection. If Ollama
or `sqlite-vec` is unavailable, the lexical projection still rebuilds and the
result reports vector search as unavailable. Use `--skip-vectors` to avoid the
embedding pass.

Add an idea:

```bash
./bin/decisions add connected-ai.auth.oidc.client-persistence \
  --kind idea \
  --summary "MCP dynamic clients may be clobbered by identity restarts" \
  --body "Idea: dynamic clients may be overwritten by another identity deployment sharing the same backing DB." \
  --tag mcp \
  --tag oidc
```

Use `kind=idea` for possible directions that have not yet been chosen. When an
idea becomes the selected direction, create a `kind=decision` record and link or
supersede the idea record if that history will matter later.

Use `kind=snag` for known issues, rough edges, cleanup items, and snag-list
entries that should be retrievable alongside the rest of the audit trail.

Store a self-contained HTML artifact or image artifact:

```bash
./bin/decisions artifact add-html connected-ai.demos.bubblebrook \
  --file ~/Downloads/demo.html \
  --summary "Bubblebrook demo HTML" \
  --visibility internal

./bin/decisions artifact add-image connected-ai.demos.bubblebrook \
  --file ~/Downloads/bubblebrook_overview.png \
  --summary "Bubblebrook overview diagram" \
  --visibility internal
```

Artifacts are copied into `.decision-ledger/artifacts/...` and indexed through
JSONL events. HTML artifacts are trusted local/team content; inline CSS and
inline JavaScript are allowed. The live wiki serves artifacts at
`/artifacts/<artifact_id>/content` and links them from the artifact record page.
HTML artifacts are not the persistence mechanism for views; they are ancillary
free-form material attached to ledger subjects or records.

The live wiki also serves dated subject views at
`/views/subjects/<subject/path>/index.html`, mixing records with embedded HTML
and image artifacts from that subtree. Saved views are separate query
definitions persisted through JSONL events into the generated SQLite projection;
they are linked from the wiki front page and rendered live from current ledger
data at `/saved-views/<view_id>.html`.

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

Run semantic vector search over ledger records:

```bash
./bin/decisions vector-search "local-first retrieval decisions"
```

Build a mixed subject view from records and artifacts:

```bash
./bin/decisions view connected-ai.bubblebrook.demo-artifacts
```

MCP consumers can use `decision_create_view` for a transient filtered view and
`decision_save_view` when a reusable view definition should appear in the wiki.

List open snags without dropping to SQLite:

```bash
./bin/decisions list --kind snag --exclude-status resolved --exclude-status superseded
```

Vector search uses the generated SQLite projection, not the canonical JSONL
event files. The default embedding provider is local Ollama:

```text
DECISION_LEDGER_OLLAMA_URL=http://127.0.0.1:11434
DECISION_LEDGER_VECTOR_MODEL=nomic-embed-text:latest
DECISION_LEDGER_VECTOR_DIMENSIONS=768
DECISION_LEDGER_VECTOR_MAX_TEXT_CHARS=8000
```

The embedding text schema is `record_text_v1`, covering subject, kind, status,
validation state, summary, body, tags, and related subjects. Vector metadata is
stored with provider, model, dimensions, text schema, and content hash so stale
rows can be rebuilt. Very large records are capped only for embedding input and
include a truncation marker with the original text hash; the canonical record
body remains in the event store and SQLite record projection.

The MCP-facing `decision_search` tool returns one structured hybrid result:
`combined` fused matches, the raw `lexical` result set, and the raw `vector`
result set or vector-unavailable status. This avoids requiring agents to make
separate lexical and vector calls for normal recall.

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

Serve a subject subtree as a live wiki:

```bash
./bin/decision-wiki-server decision-ledger \
  --home /home/neil/Dev/.decision-ledger \
  --port 8766
```

The wiki server serves each page on demand from the current SQLite projection
instead of prebuilding a static tree. This is the only supported wiki path.

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
- `decision_add_html_artifact`
- `decision_add_image_artifact`
- `decision_list_artifacts`
- `decision_validate_record`
- `decision_associate_records`
- `decision_supersede_record`
- `decision_supersede_subject_before`
- `decision_gather`
- `decision_view_subject`
- `decision_query_records`
- `decision_create_view`
- `decision_save_view`
- `decision_list_views`
- `decision_search`
- `decision_vector_search`
- `decision_show_record`
- `decision_list_records`
- `decision_list_topics`

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
- process durable decisions, ideas, and snag lists buried in markdown, tickets,
  transcripts, or repo docs into the subject tree, with the source document as
  evidence
- start `decision-wiki-server` for browsable wiki views
- use `decision_save_view` for reusable saved views; do not store rendered view
  HTML as an HTML artifact

The implementation follows the MCP stdio shape: newline-delimited JSON-RPC on
stdin/stdout, no stdout logging, `initialize`, `tools/list`, `tools/call`,
`prompts/list`, and `prompts/get`.
