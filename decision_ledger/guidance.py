from __future__ import annotations


SERVER_INSTRUCTIONS = """Decision Ledger is an audit-oriented memory surface.

Use it before making or revising durable claims about a subject. Prefer
decision_list_topics to discover the subject tree, decision_gather for current
context, decision_search for fuzzy recall, and decision_show_record for exact
audit detail.

Best practices:
- Treat namespace JSONL event files as canonical. SQLite is a generated
  projection for fast query/search/export and can be rebuilt from events.
- Treat subject prefixes as deterministic scope and associations as graph scope.
- Prefer active, proposed, accepted, and resolved records for current reasoning.
- Do not treat superseded, rejected, withdrawn, or archived records as current
  unless the user explicitly asks for audit history.
- When a newer idea replaces older guidance, call a supersede tool. Do not ask
  the user to delete records for normal forgetting.
- Attach evidence for claims that may matter later. Use command evidence for
  rerunnable checks and artifact evidence for captured point-in-time output.
- Add association notes that explain why records are linked.
- Keep record bodies clear and free-form, but keep subject, kind, status,
  evidence, tags, and related subjects structured.
- Preserve the user's information. Do not shrink or pare down important detail
  just to make a tidy record. Instead, split long material into enough linked
  records that each separable decision, assumption, question, finding, or plan
  can carry its own subject, kind, status, evidence, tags, and supersession path.
- Prefer one record per durable claim or decision when pieces may be retrieved,
  challenged, evidenced, tagged, or superseded independently. Keep shared
  background in a parent or context record and associate child records back to it.
- When you find existing decisions, assumptions, or durable ideas buried in
  markdown, chat transcripts, tickets, or repo docs, process them into the
  decision tree as records and attach the source document as evidence.
- If a user says "forget X before time T", interpret it as supersede or withdraw
  from future reasoning while preserving audit history.
"""


CAPTURE_PROMPT = """Use Decision Ledger while working:

1. Gather current context with decision_gather for the relevant subject prefix.
2. If the user introduces a new idea, add it as a thought or question.
3. If the user chooses a direction, add or update by creating a decision record.
4. Attach evidence links for files, URLs, commands, tickets, logs, or artifacts.
5. Associate related records across namespaces when namespace alone is too weak.
6. Supersede older active records when the user says they are obsolete.
7. If existing markdown contains durable decisions or ideas, import them into
   the subject tree and link the markdown file as evidence.
8. Preserve detail, but split multi-decision material into linked records when
   separate parts need independent subjects, tags, evidence, statuses, or future
   supersession.

Do not delete audit history for normal forgetting. Supersede it and explain why.
"""


TOOL_GUIDANCE = {
    "decision_guidance": "Return the built-in Decision Ledger operating guidance. Call this when uncertain how to use the ledger.",
    "decision_rebuild_projection": "Rebuild the generated SQLite projection from canonical namespace JSONL event files. Use after pulling event changes from git or when SQLite is missing/stale.",
    "decision_add_record": "Create a thought, decision, assumption, question, finding, plan, or note. Use for durable context the user may want to retrieve later.",
    "decision_add_evidence": "Attach evidence to an existing record. Prefer this for audit-worthy claims, live checks, source files, URLs, commands, and captured artifacts.",
    "decision_associate_records": "Create a graph link between two records when subject namespace alone does not capture their relationship.",
    "decision_supersede_record": "Mark one record as superseded by another. Use this for normal forgetting instead of deleting old audit history.",
    "decision_supersede_subject_before": "Bulk-supersede current records under a subject prefix before a timestamp. Use for requests like 'forget decisions on X from before 11am'.",
    "decision_gather": "Gather current context for a subject prefix, including namespace records, associated records, and evidence.",
    "decision_search": "Full-text search record subject, summary, and body. Use for fuzzy recall before adding duplicate thinking.",
    "decision_show_record": "Show a complete record with tags, evidence, associations, and audit events.",
    "decision_list_records": "List records by subject prefix/status for quick navigation.",
    "decision_list_topics": "List subject topics in the decision tree with direct and subtree record counts. Use before gather when you need to discover available namespaces.",
    "decision_export_wiki": "Export a subject subtree as static HTML plus JSON indexes. Use for browsable audit packs that can be hosted by nginx.",
}
