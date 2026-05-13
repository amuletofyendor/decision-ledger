from __future__ import annotations


SERVER_INSTRUCTIONS = """Decision Ledger is an audit-oriented memory surface.

Use it before making or revising durable claims about a subject. Prefer
decision_list_topics to discover the subject tree, decision_gather for current
context, decision_search for combined lexical/vector recall, and
decision_show_record for exact audit detail.

Best practices:
- Treat namespace JSONL event files as canonical. SQLite is a generated
  projection for fast query/search/wiki serving and can be rebuilt from events.
- Treat subject prefixes as deterministic scope and associations as graph scope.
- Prefer active, proposed, accepted, and resolved records for current reasoning.
- Do not treat superseded, rejected, withdrawn, or archived records as current
  unless the user explicitly asks for audit history.
- Keep lifecycle status separate from validation state. Status answers whether
  the record is current; validation_state answers how well the claim has been
  checked. Do not imply an accepted decision or active thought is validated
  unless its validation_state says so.
- Prefer validated records for audit-sensitive factual claims. Use unvalidated
  and partially_validated records for brainstorming and recall, but label that
  uncertainty when it matters.
- When a newer idea replaces older guidance, call a supersede tool. Do not ask
  the user to delete records for normal forgetting.
- Attach evidence for claims that may matter later. Use command evidence for
  rerunnable checks and artifact evidence for captured point-in-time output.
- Use HTML or image artifacts when the user or LLM has created useful
  HTML-presented or visual material worth retaining. Artifacts are first-class
  captured objects with subjects, timestamps, provenance, visibility, and
  record associations; views are the later synthesis layer over records and
  artifacts.
- Use decision_view_subject when the user wants an associated slice of records
  and artifacts from a subject tree. Wiki subject views render these slices as a
  dated synthesis layer with records and embedded artifacts.
- Use decision_query_records for precise filtered retrieval such as all current
  snags, records by kind, or records excluding resolved/superseded statuses.
  Use decision_create_view to materialize a filtered slice as a stored HTML
  artifact with the query definition retained in the artifact record body.
- Inline CSS and inline JavaScript are acceptable in saved HTML artifacts for
  this trusted local/team ledger model.
- Add association notes that explain why records are linked.
- Keep record bodies clear and free-form, but keep subject, kind, status,
  evidence, tags, and related subjects structured.
- Preserve the user's information. Do not shrink or pare down important detail
  just to make a tidy record. Instead, split long material into enough linked
  records that each separable idea, snag, decision, assumption, question, finding, or plan
  can carry its own subject, kind, status, evidence, tags, and supersession path.
- Capture decisions, ideas, and snags even when they seem small, incidental, or
  unfinished. The intention is to retain as many useful fragments as possible
  before they are lost to context compaction or buried in markdown documents.
- Prefer one record per durable claim or decision when pieces may be retrieved,
  challenged, evidenced, tagged, or superseded independently. Keep shared
  background in a parent or context record and associate child records back to it.
- When you find existing decisions, assumptions, durable ideas, or snag lists
  buried in markdown, chat transcripts, tickets, or repo docs, process them into
  the decision tree as records and attach the source document as evidence.
  Imported material should normally start as unvalidated unless you also verify it.
- When the user asks to see, make, generate, or show a wiki, they usually want
  a browsable result. Prefer the live decision-wiki-server on a free localhost
  port so pages are rendered from current ledger data on demand. The live server
  is the canonical wiki path.
- If a user says "forget X before time T", interpret it as supersede or withdraw
  from future reasoning while preserving audit history.
"""


CAPTURE_PROMPT = """Use Decision Ledger while working:

1. Gather current context with decision_gather for the relevant subject prefix.
2. If the user introduces a new idea, add it as an idea.
3. If the user chooses a direction, create a decision record and associate or
   supersede the idea record it came from when that relationship matters.
4. Attach evidence links for files, URLs, commands, tickets, logs, or artifacts.
5. If an LLM or user creates a useful HTML page or image, save it with
   decision_add_html_artifact or decision_add_image_artifact and associate the
   resulting record with the relevant subject, demo, decision, or finding.
6. Set validation_state separately from status. Default to unvalidated until a
   claim has been checked against evidence; mark contested or invalidated when
   evidence points against it.
7. Associate related records across namespaces when namespace alone is too weak.
8. Supersede older active records when the user says they are obsolete.
9. If existing markdown contains durable decisions, ideas, or snag lists, import them into
   the subject tree and link the markdown file as evidence.
10. Preserve detail, but split multi-decision material into linked records when
   separate parts need independent subjects, tags, evidence, statuses, or future
   supersession.
11. Do not skip capture just because a decision, idea, or snag seems small. Retain it
   before context compaction or markdown burial can make it hard to recover.
12. If the user asks for a wiki, normally start decision-wiki-server on a free
   localhost port so the user can browse current ledger data immediately.

Do not delete audit history for normal forgetting. Supersede it and explain why.
"""


TOOL_GUIDANCE = {
    "decision_guidance": "Return the built-in Decision Ledger operating guidance. Call this when uncertain how to use the ledger.",
    "decision_rebuild_projection": "Rebuild the generated SQLite projection from canonical namespace JSONL event files. Use after pulling event changes from git or when SQLite is missing/stale.",
    "decision_add_record": "Create a thought, idea, snag, decision, assumption, question, finding, plan, or note. Use for durable context the user may want to retrieve later.",
    "decision_add_evidence": "Attach evidence to an existing record. Prefer this for audit-worthy claims, live checks, source files, URLs, commands, and captured artifacts.",
    "decision_add_html_artifact": "Store a complete trusted HTML artifact with inline CSS/JavaScript allowed. Use for useful HTML-presented material the user or LLM wants to retain with ledger auditability.",
    "decision_add_image_artifact": "Store an image artifact in the ledger. Use for diagrams, screenshots, generated images, and visual evidence that should be associated with ledger records.",
    "decision_list_artifacts": "List stored HTML and image artifacts by subject/type. Use before linking, opening, or building a view over artifacts.",
    "decision_validate_record": "Change a record's validation state without changing its lifecycle status. Use validated for checked claims, contested for disputed claims, and invalidated when evidence disproves a record.",
    "decision_associate_records": "Create a graph link between two records when subject namespace alone does not capture their relationship.",
    "decision_supersede_record": "Mark one record as superseded by another. Use this for normal forgetting instead of deleting old audit history.",
    "decision_supersede_subject_before": "Bulk-supersede current records under a subject prefix before a timestamp. Use for requests like 'forget decisions on X from before 11am'.",
    "decision_gather": "Gather current context for a subject prefix, including namespace records, associated records, and evidence.",
    "decision_view_subject": "Build a dated synthesis view for a subject prefix, containing ledger records and their stored artifacts.",
    "decision_query_records": "Precisely query records by subject, kind, status/excluded statuses, validation state, tags, date range, and inclusion options. Use when list/search are too blunt.",
    "decision_create_view": "Materialize a filtered record query as a stored trusted HTML artifact. Use for reviewable slices such as open snags across the ledger.",
    "decision_search": "Combined lexical and vector search over decision records. Use this as the default fuzzy recall tool before adding duplicate thinking.",
    "decision_vector_search": "Semantic vector search over record subject, metadata, tags, related subjects, summary, and body. Use directly only when isolating vector behavior.",
    "decision_show_record": "Show a complete record with tags, evidence, associations, and audit events.",
    "decision_list_records": "List records by subject prefix/status for quick navigation.",
    "decision_list_topics": "List subject topics in the decision tree with direct and subtree record counts. Use before gather when you need to discover available namespaces.",
}
