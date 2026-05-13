PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS records (
  id TEXT PRIMARY KEY,
  subject TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (
    kind IN ('thought', 'idea', 'snag', 'decision', 'assumption', 'question', 'finding', 'plan', 'note', 'requirement', 'constraint', 'test_case', 'ui_note', 'interface_contract')
  ),
  status TEXT NOT NULL CHECK (
    status IN ('active', 'proposed', 'accepted', 'rejected', 'superseded', 'withdrawn', 'resolved', 'archived')
  ),
  validation_state TEXT NOT NULL DEFAULT 'unvalidated' CHECK (
    validation_state IN ('unvalidated', 'partially_validated', 'validated', 'contested', 'invalidated')
  ),
  summary TEXT,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL,
  created_by TEXT,
  validated_at TEXT,
  validated_by TEXT,
  validation_note TEXT,
  updated_at TEXT,
  valid_from TEXT,
  valid_until TEXT,
  export_visibility TEXT NOT NULL DEFAULT 'private' CHECK (
    export_visibility IN ('private', 'internal', 'shareable', 'public')
  )
);

CREATE INDEX IF NOT EXISTS idx_records_subject ON records(subject);
CREATE INDEX IF NOT EXISTS idx_records_status ON records(status);
CREATE INDEX IF NOT EXISTS idx_records_kind ON records(kind);
CREATE INDEX IF NOT EXISTS idx_records_created_at ON records(created_at);

CREATE TABLE IF NOT EXISTS record_aliases (
  record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
  alias TEXT NOT NULL,
  PRIMARY KEY (record_id, alias)
);

CREATE TABLE IF NOT EXISTS record_subjects (
  record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
  subject TEXT NOT NULL,
  relation TEXT NOT NULL DEFAULT 'related' CHECK (
    relation IN ('related', 'secondary', 'parent', 'derived')
  ),
  PRIMARY KEY (record_id, subject, relation)
);

CREATE INDEX IF NOT EXISTS idx_record_subjects_subject ON record_subjects(subject);

CREATE TABLE IF NOT EXISTS record_tags (
  record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
  tag TEXT NOT NULL,
  PRIMARY KEY (record_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_record_tags_tag ON record_tags(tag);

CREATE TABLE IF NOT EXISTS evidence (
  id TEXT PRIMARY KEY,
  record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
  type TEXT NOT NULL CHECK (
    type IN ('file', 'url', 'command', 'artifact', 'commit', 'pr', 'ticket', 'log', 'chat', 'note')
  ),
  uri TEXT NOT NULL,
  line INTEGER,
  label TEXT,
  note TEXT,
  observed_at TEXT,
  export_visibility TEXT NOT NULL DEFAULT 'private' CHECK (
    export_visibility IN ('private', 'internal', 'shareable', 'public')
  )
);

CREATE INDEX IF NOT EXISTS idx_evidence_record_id ON evidence(record_id);
CREATE INDEX IF NOT EXISTS idx_evidence_type ON evidence(type);
CREATE INDEX IF NOT EXISTS idx_evidence_uri ON evidence(uri);

CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
  subject TEXT NOT NULL,
  type TEXT NOT NULL CHECK (
    type IN ('html', 'image', 'snippet', 'pseudocode', 'markdown', 'json', 'yaml', 'text')
  ),
  content_type TEXT NOT NULL,
  storage_path TEXT NOT NULL UNIQUE,
  label TEXT,
  summary TEXT,
  source_uri TEXT,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  created_by TEXT,
  export_visibility TEXT NOT NULL DEFAULT 'private' CHECK (
    export_visibility IN ('private', 'internal', 'shareable', 'public')
  )
);

CREATE INDEX IF NOT EXISTS idx_artifacts_record_id ON artifacts(record_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_subject ON artifacts(subject);
CREATE INDEX IF NOT EXISTS idx_artifacts_type ON artifacts(type);
CREATE INDEX IF NOT EXISTS idx_artifacts_created_at ON artifacts(created_at);

CREATE TABLE IF NOT EXISTS record_artifact_associations (
  id TEXT PRIMARY KEY,
  record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
  artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
  relation TEXT NOT NULL CHECK (
    relation IN (
      'associated_with',
      'supports',
      'contradicts',
      'depends_on',
      'derived_from',
      'duplicates',
      'clarifies',
      'blocks',
      'implements',
      'raises_question',
      'answers_question',
      'verifies',
      'constrains',
      'illustrates'
    )
  ),
  strength REAL,
  note TEXT,
  source TEXT NOT NULL DEFAULT 'manual' CHECK (
    source IN ('manual', 'agent', 'import', 'inferred')
  ),
  created_at TEXT NOT NULL,
  created_by TEXT,
  UNIQUE (record_id, artifact_id, relation)
);

CREATE INDEX IF NOT EXISTS idx_record_artifact_associations_record ON record_artifact_associations(record_id);
CREATE INDEX IF NOT EXISTS idx_record_artifact_associations_artifact ON record_artifact_associations(artifact_id);
CREATE INDEX IF NOT EXISTS idx_record_artifact_associations_relation ON record_artifact_associations(relation);

CREATE TABLE IF NOT EXISTS saved_views (
  id TEXT PRIMARY KEY,
  subject TEXT NOT NULL,
  title TEXT NOT NULL,
  query_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  created_by TEXT,
  export_visibility TEXT NOT NULL DEFAULT 'private' CHECK (
    export_visibility IN ('private', 'internal', 'shareable', 'public')
  )
);

CREATE INDEX IF NOT EXISTS idx_saved_views_subject ON saved_views(subject);
CREATE INDEX IF NOT EXISTS idx_saved_views_created_at ON saved_views(created_at);
CREATE INDEX IF NOT EXISTS idx_saved_views_visibility ON saved_views(export_visibility);

CREATE TABLE IF NOT EXISTS record_associations (
  id TEXT PRIMARY KEY,
  from_record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
  to_record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
  relation TEXT NOT NULL CHECK (
    relation IN (
      'associated_with',
      'supersedes',
      'supports',
      'contradicts',
      'depends_on',
      'derived_from',
      'duplicates',
      'clarifies',
      'blocks',
      'implements',
      'raises_question',
      'answers_question',
      'verifies',
      'constrains',
      'illustrates'
    )
  ),
  strength REAL,
  note TEXT,
  source TEXT NOT NULL DEFAULT 'manual' CHECK (
    source IN ('manual', 'agent', 'import', 'inferred')
  ),
  created_at TEXT NOT NULL,
  created_by TEXT,
  UNIQUE (from_record_id, to_record_id, relation)
);

CREATE INDEX IF NOT EXISTS idx_record_associations_from ON record_associations(from_record_id);
CREATE INDEX IF NOT EXISTS idx_record_associations_to ON record_associations(to_record_id);
CREATE INDEX IF NOT EXISTS idx_record_associations_relation ON record_associations(relation);

CREATE TABLE IF NOT EXISTS record_events (
  id TEXT PRIMARY KEY,
  record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL CHECK (
    event_type IN (
      'created',
      'updated',
      'status_changed',
      'superseded',
      'withdrawn',
      'associated',
      'evidence_added',
      'artifact_added',
      'artifact_associated',
      'validation_changed',
      'tag_added',
      'export_visibility_changed'
    )
  ),
  event_at TEXT NOT NULL,
  event_by TEXT,
  note TEXT,
  payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_record_events_record_id ON record_events(record_id);
CREATE INDEX IF NOT EXISTS idx_record_events_event_type ON record_events(event_type);
CREATE INDEX IF NOT EXISTS idx_record_events_event_at ON record_events(event_at);

CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
  subject,
  summary,
  body,
  content='records',
  content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS records_ai AFTER INSERT ON records BEGIN
  INSERT INTO records_fts(rowid, subject, summary, body)
  VALUES (new.rowid, new.subject, new.summary, new.body);
END;

CREATE TRIGGER IF NOT EXISTS records_ad AFTER DELETE ON records BEGIN
  INSERT INTO records_fts(records_fts, rowid, subject, summary, body)
  VALUES ('delete', old.rowid, old.subject, old.summary, old.body);
END;

CREATE TRIGGER IF NOT EXISTS records_au AFTER UPDATE ON records BEGIN
  INSERT INTO records_fts(records_fts, rowid, subject, summary, body)
  VALUES ('delete', old.rowid, old.subject, old.summary, old.body);
  INSERT INTO records_fts(rowid, subject, summary, body)
  VALUES (new.rowid, new.subject, new.summary, new.body);
END;
