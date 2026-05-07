# Decision Ledger Plan

## 1. Purpose

Decision Ledger is a structured memory and audit system for technical decisions,
ideas, assumptions, findings, and open questions.

The goal is not to replace human notes. The goal is to give human notes a
canonical structure so that both humans and LLM agents can reliably answer:

- What do we currently believe about this subject?
- What did we believe before, and when did it change?
- What evidence supported a claim or decision?
- Which records are associated even when they do not share a namespace?
- Which older records should no longer influence future reasoning?
- Can we export a browsable decision tree for another person or team?

Loose markdown is useful as an early human-facing step, but it is weak as a
system of record. It has no native supersession model, no reliable evidence
typing, no graph links, no timeline semantics, and no deterministic way to tell
an agent which records are current versus obsolete.

Decision Ledger should therefore use a structured canonical store and generate
human-readable projections from it.

## 2. Design Principles

### 2.1 Auditability over deletion

Records should not normally be deleted when they are no longer useful.
Instead, they should be superseded, withdrawn, rejected, or resolved.

The command:

```text
forget decisions on subject X from before 11am this morning
```

means:

```text
Find active records matching subject X before the given time.
Create a new event explaining why they should no longer guide reasoning.
Mark the older records as superseded or withdrawn.
Preserve the older records for audit history.
```

### 2.2 Structured metadata, free-form thought

The actual thought or decision should remain a large free-text field. Capture
must stay frictionless.

The surrounding metadata should be structured:

- subject
- kind
- status
- validation_state
- timestamps
- evidence links
- associations
- tags
- related subjects
- supersession links

This gives agents deterministic filters without forcing every idea into a rigid
template.

### 2.3 Markdown and HTML are projections

Markdown and static HTML are useful views, but they should not be canonical.

The canonical source should be namespace-scoped JSONL event files. From those
events we can regenerate:

- SQLite query projections
- markdown summaries
- static HTML wiki exports
- JSON search indexes
- graph exports
- reports for review or handover

### 2.4 Retrieval should be hybrid

No single retrieval method is enough.

Decision Ledger should combine:

- exact subject-prefix filtering
- record status filtering
- validation-state filtering
- full-text search
- explicit graph associations
- shared evidence lookups
- optional embedding-based semantic search
- timeline and supersession traversal

Embeddings are useful for discovery, but they are not an audit model.

## 3. Core Concepts

## 3.1 Records

A record is the primary unit of memory.

Record kinds:

- `thought`: exploratory thinking or possible direction
- `decision`: chosen direction or policy
- `assumption`: belief being relied upon
- `question`: unresolved issue
- `finding`: observed fact or diagnosis
- `plan`: intended sequence of work
- `note`: general contextual note

Record statuses:

- `active`: can influence future reasoning
- `proposed`: not yet accepted but still live
- `accepted`: approved or chosen
- `rejected`: considered and dismissed
- `superseded`: replaced by newer record
- `withdrawn`: intentionally removed from future reasoning
- `resolved`: answered or completed
- `archived`: retained but no longer central

The first version can keep statuses simple, but the schema should allow this
vocabulary.

Record validation states:

- `unvalidated`: captured but not checked
- `partially_validated`: some supporting evidence exists, but verification is incomplete
- `validated`: checked against sufficient evidence for the current use
- `contested`: credible evidence or review challenges it
- `invalidated`: evidence shows it should not be treated as true

Status and validation state are deliberately separate. Status describes whether
a record is current in the decision lifecycle. Validation state describes how
well the claim has been checked. A decision can be accepted while still relying
on unvalidated assumptions, and a finding can be validated but later superseded
by a newer finding.

Validation state should be changed by appending an audit event, not by silently
rewriting the record. A validation event should capture:

- validation_state
- validated_at
- validated_by
- validation_note

Evidence can later gain per-link roles such as supporting, contradicting,
source, reproduction, or review. The first implementation should keep
record-level validation state as the simple contract and continue attaching
evidence links for inspection.

## 3.2 Subject Namespace

Subjects use a dot-separated tree.

Examples:

```text
connected-ai.auth.oidc.client-persistence
connected-ai.auth.oidc.callback-allowlist
connected-ai.retrieval.wiki.page-contract
connected-ai.retrieval.semantic-profile.finnebrogue
connected-ai.dt.wikicorpus.export
connected-ai.environments.k3d.main-ai
codex.tooling.mcp.drawio
```

Recommended pattern:

```text
<domain>.<area>.<capability>.<specific-topic>
```

Rules:

- Keep subjects stable.
- Prefer four to five levels.
- Do not encode dates, version numbers, temporary namespaces, or branch names.
- Use aliases and tags for synonyms.
- Allow one primary subject plus secondary related subjects.
- Make prefix search a first-class operation.

The subject is a routing key, not the entire taxonomy.

## 3.3 Evidence

Evidence links anchor a thought or decision to inspectable material.

Evidence may point to:

- local files
- file lines
- URLs
- pull requests
- commits
- tickets
- command recipes
- captured command output artifacts
- logs
- chat/session references
- generated reports

Evidence should be structured from the beginning because it is the foundation
for auditability.

Evidence types:

```text
file
url
command
artifact
commit
pr
ticket
log
chat
note
```

Important distinction:

- A `command` is a recipe to obtain fresh evidence.
- An `artifact` is captured evidence from a specific point in time.

Example:

```yaml
evidence:
  - type: command
    uri: "kubectl get pods -n main-ai"
    note: "Command used to inspect live pod state"
  - type: artifact
    uri: "/home/neil/Dev/docs/evidence/2026-05-07-main-ai-pods.txt"
    note: "Captured pod state from original check"
    observed_at: "2026-05-07T11:18:00+01:00"
```

## 3.4 Associations

Namespace will be the dominant association mechanism, but it is not enough.

Records should form a graph using explicit associations.

Examples:

```text
record A supersedes record B
record A supports record B
record A contradicts record B
record A depends on record B
record A clarifies record B
record A raises question B
record A answers question B
record A is associated with record B
```

Associations should be canonical graph edges, not duplicated arrays inside
record bodies. The application can expose `associated_with` and
`associated_from` as derived views.

For symmetric relations like `associated_with`, store one edge and query both
directions. For directional relations like `supersedes` and `depends_on`, keep
direction meaningful and derive friendly inverse labels at read time.

Each association should optionally carry:

- relation
- note
- strength
- source
- created_at
- created_by

The note is important. A bare association becomes hard to interpret later.

## 3.5 Tags and Related Subjects

Tags are loose facets. They should catch cross-cutting concerns that do not
belong in the canonical subject.

Examples:

```text
mcp
oidc
openiddict
invalid_client
github-actions
local-k3d
dev-aks
customer-facing
```

Related subjects provide explicit secondary namespace membership.

Example:

```yaml
subject: connected-ai.auth.oidc.client-persistence
related_subjects:
  - connected-ai.environments.dev-aks.identity
  - connected-ai.runtime.identity.restart-behavior
```

## 4. Canonical Storage

## 4.1 Namespace JSONL Events First

The canonical store is an append-only set of JSONL event files under the
dot-separated subject tree.

Example:

```text
.decision-ledger/
  events/
    connected-ai/
      auth/
        oidc/
          client-persistence.jsonl
  ledger.sqlite
```

Rules:

- commit `events/**/*.jsonl`
- treat `ledger.sqlite` as generated
- write new events instead of editing old events for normal changes
- include the subject in each event even though the path implies it
- rebuild SQLite idempotently from the event tree

Reasons:

- local-first
- simple backup story
- git-friendly canonical history
- smaller per-topic files with fewer merge conflicts
- easy to query
- supports joins
- supports indexes over subject prefixes
- supports FTS5 for full-text search
- can later migrate to Postgres if needed
- does not require standing infrastructure

Recommended location:

```text
<ledger-home>/events/<subject path>.jsonl
<ledger-home>/ledger.sqlite
```

For this repository, schema migrations live under:

```text
schema/
```

## 4.2 Tables

Initial tables:

- `records`
- `record_aliases`
- `record_subjects`
- `record_tags`
- `evidence`
- `record_associations`
- `record_events`
- `records_fts`

The first implementation can keep this lean, but the schema should be designed
for:

- current-state queries
- audit-history queries
- supersession chains
- graph traversal
- evidence lookups
- validation-state filtering
- static export

## 4.3 Append-Only Events

There are two levels:

1. The current record table for easy querying.
2. An append-only `record_events` table for audit history.

For example, superseding old records can update their current status in
`records`, but it should also append events showing what changed and why.

Event examples:

- `created`
- `status_changed`
- `validation_changed`
- `superseded`
- `associated`
- `evidence_added`
- `tag_added`
- `export_visibility_changed`

This gives us practical query performance and an audit trail.

## 5. Key Operations

## 5.1 Add Record

Example:

```bash
decisions add connected-ai.auth.oidc.client-persistence \
  --kind thought \
  --summary "MCP clients may be clobbered by shared DB identity restarts"
```

The command should open an editor for the body unless `--body` or stdin is
provided.

## 5.2 Add Evidence

Examples:

```bash
decisions evidence add rec_123 \
  --type file \
  --uri /home/neil/Dev/fc.identity/src/Seeder.cs \
  --line 42 \
  --note "OpenIddict client seeding logic"

decisions evidence add rec_123 \
  --type url \
  --uri https://github.com/org/repo/actions/runs/123 \
  --note "Deployment failure that exposed the issue"
```

## 5.3 Associate Records

Example:

```bash
decisions associate rec_123 rec_456 \
  --relation depends_on \
  --note "The auth decision depends on the shared Identity DB ownership finding"
```

Read views should present:

```text
associated_out:
  - rec_456 depends_on
associated_in:
  - rec_789 supports
```

For symmetric `associated_with`, both records should show the relationship even
though only one canonical edge is stored.

## 5.4 Supersede Records

Specific supersession:

```bash
decisions supersede rec_old rec_new \
  --note "New implementation decision replaces the earlier proposal"
```

Bulk supersession by subject and time:

```bash
decisions supersede connected-ai.auth.oidc \
  --before "2026-05-07 11:00" \
  --replacement rec_new \
  --note "Earlier decisions were superseded by the 11am design revision"
```

The system should:

- find matching active/proposed/accepted records
- append events
- create `supersedes` associations from replacement to old records
- set old records to `superseded`
- set `valid_until`

## 5.5 Gather Records

Example:

```bash
decisions gather connected-ai.retrieval.wiki
```

Gather should combine:

- subject-prefix matches
- related-subject matches
- explicit associations
- shared evidence
- full-text matches
- optional embedding matches

Output should be grouped:

- current decisions
- active thoughts
- open questions
- accepted assumptions
- findings
- superseded/rejected history
- associated records outside namespace
- evidence index

## 5.6 Export Static Wiki

Example:

```bash
decisions wiki connected-ai.auth \
  --out ./public \
  --include-superseded \
  --profile internal
```

Profiles:

- `internal`: includes local paths, command evidence, and full notes
- `shareable`: strips or rewrites local absolute paths and sensitive evidence
- `public`: exports only records explicitly marked public/exportable

## 6. Static Wiki Export

## 6.1 Output Structure

Example:

```text
public/
  index.html
  subjects/
    connected-ai/
      index.html
      auth/
        index.html
        oidc/
          index.html
          client-persistence/
            index.html
  records/
    rec_20260507_113012_abcd/
      index.html
  assets/
    search-index.json
    graph.json
    styles.css
```

## 6.2 Namespace Pages

Each namespace page should show:

- breadcrumb trail
- child namespaces
- active decisions
- active thoughts
- open questions
- assumptions
- findings
- timeline
- associated records outside the namespace
- superseded/rejected records collapsed by default
- evidence summary

## 6.3 Record Pages

Each record page should show:

- subject
- kind
- status
- validation_state
- summary
- body
- timestamps
- tags
- related subjects
- evidence links
- outgoing associations
- incoming associations
- supersession chain
- export visibility

## 6.4 Static Search

Generate:

```text
assets/search-index.json
```

The first version can use lightweight client-side full-text search. For larger
exports, generate one index per namespace subtree.

Search result ranking should prefer:

1. exact subject matches
2. active/accepted/current records
3. title/summary matches
4. body matches
5. associated records
6. superseded records

## 6.5 Graph Export

Generate:

```text
assets/graph.json
```

The graph should include:

- records
- subjects
- associations
- supersession edges
- shared-evidence edges

This can later drive a visual graph page, but the first export can simply make
the data available.

## 7. LLM Interaction Model

LLM agents should query the ledger through tools rather than reading markdown
first.

Useful future MCP or CLI-backed operations:

```text
decision_add
decision_search
decision_gather
decision_supersede
decision_associate
decision_add_evidence
decision_validate_record
decision_export_wiki
decision_get_current_state
decision_get_audit_history
```

Important behavior:

- Agents should prefer active/accepted records.
- Agents should prefer validated records for audit-sensitive factual claims.
- Agents should label uncertainty when relying on unvalidated or partially
  validated records.
- Agents should disclose when they rely on superseded history.
- Agents should not silently treat obsolete records as current.
- Agents should preserve evidence links when summarizing.
- Agents should create supersession records rather than deleting history.

## 8. Retrieval Semantics

## 8.1 Current State

"What do we currently believe about X?" should:

- match subject prefix X
- include related subjects
- include active/accepted/proposed records
- exclude superseded/rejected/withdrawn by default
- summarize latest relevant decisions first
- show open questions separately

## 8.2 Audit History

"What did we believe before?" should:

- include superseded, rejected, withdrawn, and archived records
- show chronological order
- show supersession chains
- show status-change events
- show evidence that supported older views

## 8.3 Previous Thoughts

"Gather all previous thoughts about Y" should:

- start with namespace prefix matching
- include explicit associations
- include shared evidence
- include text search and semantic matches
- group current and obsolete records separately
- show why each associated record was included

## 8.4 Forgetting

"Forget X from before time T" should:

- never silently delete
- require a subject or record set
- create a supersession/withdrawal event
- mark matched records as no longer active
- optionally link to a replacement record
- record the command/request that caused the change

## 9. Export and Privacy

The ledger will contain sensitive material unless carefully managed.

Records need export visibility:

- `private`
- `internal`
- `shareable`
- `public`

Evidence may also need visibility. A public record may have private evidence
that should be omitted from public export.

Export profiles must be conservative:

- public export includes only public records and public evidence
- shareable export strips local absolute paths unless explicitly allowed
- internal export can include local paths and command evidence

## 10. Implementation Roadmap

## 10.1 Phase 1: Ledger Skeleton

Deliver:

- SQLite schema
- migration runner
- CLI command structure
- add/list/show records
- add evidence
- basic subject-prefix search
- full-text search

Success criteria:

- add a thought with body text
- attach file and URL evidence
- query by subject prefix
- query by text
- inspect the record as JSON or markdown

## 10.2 Phase 2: Associations and Supersession

Deliver:

- record association table
- associate command
- supersede command
- status-change events
- current-state query
- audit-history query

Success criteria:

- associate records across namespaces
- supersede a record without deleting it
- bulk supersede by subject and timestamp
- show incoming and outgoing associations

## 10.3 Phase 3: Static Wiki Export

Deliver:

- namespace tree export
- record pages
- search index JSON
- graph JSON
- internal/shareable/public profiles
- simple nginx-hostable output

Success criteria:

- export a subtree such as `connected-ai.auth`
- browse it without a backend
- search records client-side
- distinguish current and superseded records

## 10.4 Phase 4: Agent Integration

Deliver:

- stable machine-readable CLI output
- MCP wrapper or direct tool interface
- agent retrieval policy
- memory citation compatibility if needed
- optional embedding support

Success criteria:

- an agent can gather current context for a subject
- an agent can add a record with evidence
- an agent can supersede stale records by subject and time
- an agent can avoid obsolete records by default

## 11. Open Design Questions

- Should record IDs be ULIDs, timestamped slugs, or random UUIDs?
- Should status vocabulary be strict from the start or configurable?
- Should subjects have a registry table with descriptions and aliases?
- How much should the CLI infer subject aliases automatically?
- Should markdown export be one file per record, one file per namespace, or both?
- Should evidence artifacts be copied into the ledger directory for portability?
- Should local absolute paths be stored as-is, normalized relative to known roots,
  or both?
- Should embeddings live in SQLite as vectors, in sidecar files, or in a later
  dedicated service?

## 12. Initial Recommendation

Start with:

```text
namespace JSONL events as canonical store
SQLite as generated projection
FTS5 for search
explicit evidence table
explicit association graph
append-only events
CLI-first workflow
static HTML export as generated projection
optional embeddings later
```

Do not start with:

- markdown as canonical storage
- vector DB as canonical storage
- a complex knowledge graph database
- deletion-based forgetting
- duplicated association arrays as source of truth

This gives a small, durable system that can grow into richer retrieval without
losing audit semantics.
