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

