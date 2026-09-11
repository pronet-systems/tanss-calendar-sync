-- Zustandsdatenbank. Enthält die Verknüpfungen, das Änderungsprotokoll und die
-- Sicherungen gelöschter Termine. Die Verknüpfungen sind aus beiden Systemen
-- rekonstruierbar, Protokoll und Sicherungen nicht — deshalb gehört diese Datei
-- in die Datensicherung.

PRAGMA user_version = 1;

-- uid = KANONISCHE UID, nicht die rohe iCalUId.
-- sequence: -1 = Einzeltermin, sonst recurrenceRuleSequenceId (beginnt bei 0).
CREATE TABLE IF NOT EXISTS links (
  mailbox                  TEXT    NOT NULL,
  uid                      TEXT    NOT NULL,
  sequence                 INTEGER NOT NULL DEFAULT -1,
  travel_role              TEXT    NOT NULL DEFAULT 'main',
  tanss_support_id         INTEGER,
  graph_event_id           TEXT,
  tanss_employee_id        INTEGER NOT NULL,
  tanss_recurrence_rule_id INTEGER NOT NULL DEFAULT 0,
  series_master_id         TEXT,
  travel_group_id          TEXT,
  state                    TEXT    NOT NULL DEFAULT 'linked',
      -- linked | detached | deleted | ignored_pre_activation
  write_direction          TEXT    NOT NULL DEFAULT 'both',
      -- trägt den Schreibschutz für Abwesenheiten und Fahrt-Termine.
      -- Wird NUR in LinkRecord.for_new() abgeleitet, nie aus UserMapping.direction.
  last_hash_tanss          TEXT,
  last_hash_graph          TEXT,
  last_written_side        TEXT,
  last_written_at          INTEGER,
  last_seen_at             INTEGER,
  PRIMARY KEY (mailbox, uid, sequence, travel_role)
);

-- Kein UNIQUE auf tanss_support_id: Fahrtzeilen teilen sie sich, Occurrences haben 0.
CREATE INDEX IF NOT EXISTS idx_links_support ON links(tanss_support_id)
  WHERE tanss_support_id IS NOT NULL AND tanss_support_id > 0;
CREATE UNIQUE INDEX IF NOT EXISTS idx_links_event ON links(mailbox, graph_event_id)
  WHERE graph_event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_links_series ON links(tanss_recurrence_rule_id, sequence)
  WHERE tanss_recurrence_rule_id > 0;
CREATE INDEX IF NOT EXISTS idx_links_employee ON links(tanss_employee_id);

CREATE TABLE IF NOT EXISTS runs (
  id             INTEGER PRIMARY KEY,
  started_at     INTEGER NOT NULL,
  finished_at    INTEGER,
  created        INTEGER DEFAULT 0,
  updated        INTEGER DEFAULT 0,
  deleted        INTEGER DEFAULT 0,
  skipped        INTEGER DEFAULT 0,
  errors         INTEGER DEFAULT 0,
  aborted_reason TEXT,
  detail         TEXT
);

CREATE TABLE IF NOT EXISTS audit (
  id                       INTEGER PRIMARY KEY,
  run_id                   INTEGER NOT NULL,
  ts                       INTEGER NOT NULL,
  side                     TEXT    NOT NULL,   -- tanss | graph | system
  operation                TEXT    NOT NULL,
  outcome                  TEXT    NOT NULL,
  mailbox                  TEXT,
  uid                      TEXT,
  sequence                 INTEGER,
  travel_role              TEXT,
  tanss_support_id         INTEGER,
  graph_event_id           TEXT,
  tanss_recurrence_rule_id INTEGER DEFAULT 0,
  employee_id              INTEGER,
  reason                   TEXT    NOT NULL,
  trigger                  TEXT    NOT NULL,
  changed_fields           TEXT,
  backup_id                INTEGER,
  before                   TEXT,
  after                    TEXT,
  http_status              INTEGER,
  error                    TEXT,
  duration_ms              INTEGER
);

CREATE INDEX IF NOT EXISTS idx_audit_key     ON audit(mailbox, uid, sequence);
CREATE INDEX IF NOT EXISTS idx_audit_support ON audit(tanss_support_id);
CREATE INDEX IF NOT EXISTS idx_audit_series  ON audit(tanss_recurrence_rule_id, sequence);
CREATE INDEX IF NOT EXISTS idx_audit_user    ON audit(employee_id, ts);
CREATE INDEX IF NOT EXISTS idx_audit_run     ON audit(run_id);

-- Nichts wird gelöscht, ohne vorher gesichert zu werden.
CREATE TABLE IF NOT EXISTS deleted_backup (
  id               INTEGER PRIMARY KEY,
  deleted_at       INTEGER NOT NULL,
  side             TEXT    NOT NULL,
  trigger          TEXT    NOT NULL,
  mailbox          TEXT,
  uid              TEXT,
  sequence         INTEGER,
  travel_role      TEXT,
  tanss_support_id INTEGER,
  graph_event_id   TEXT,
  payload          TEXT    NOT NULL
);

-- Der deltaLink ist undurchsichtig und wird NIE zerlegt - das Zeitfenster steckt darin.
CREATE TABLE IF NOT EXISTS delta_tokens (
  mailbox        TEXT PRIMARY KEY,
  deltalink      TEXT,
  window_start   INTEGER,
  window_end     INTEGER,
  last_rebase_at INTEGER,
  updated_at     INTEGER
);

-- Der Listener schreibt nur hier hinein und quittiert sofort (3.1).
CREATE TABLE IF NOT EXISTS pending_events (
  id           INTEGER PRIMARY KEY,
  received_at  INTEGER NOT NULL,
  source       TEXT    NOT NULL,
  employee_id  INTEGER,
  payload      TEXT    NOT NULL,
  processed_at INTEGER,
  attempts     INTEGER DEFAULT 0,
  error        TEXT
);

CREATE TABLE IF NOT EXISTS leases (
  scope       TEXT PRIMARY KEY,
  holder      TEXT    NOT NULL,
  acquired_at INTEGER NOT NULL,
  expires_at  INTEGER NOT NULL
);

-- Löschungen werden terminiert, nicht synchron abgewartet (5.6).
CREATE TABLE IF NOT EXISTS pending_deletions (
  id            INTEGER PRIMARY KEY,
  scheduled_at  INTEGER NOT NULL,
  due_at        INTEGER NOT NULL,
  mailbox       TEXT,
  uid           TEXT,
  sequence      INTEGER,
  travel_role   TEXT,
  side          TEXT    NOT NULL,
  evidence      TEXT    NOT NULL,
  cancelled_at  INTEGER,
  cancel_reason TEXT,
  executed_at   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_pending_due ON pending_deletions(due_at)
  WHERE executed_at IS NULL AND cancelled_at IS NULL;

-- Ein ausgelöster Not-Aus ist ein Zustand, kein Logeintrag - sonst wäre
-- "länger als eine Stunde unquittiert" nicht prüfbar (8.5).
CREATE TABLE IF NOT EXISTS emergency_stop (
  id              INTEGER PRIMARY KEY,
  triggered_at    INTEGER NOT NULL,
  run_id          INTEGER,
  scope           TEXT    NOT NULL,
  kind            TEXT    NOT NULL,   -- bulk_delete | bulk_create_rebase
  counted         INTEGER,
  threshold       INTEGER,
  reason          TEXT    NOT NULL,
  acknowledged_at INTEGER,
  released_at     INTEGER,
  released_by     TEXT
);

CREATE INDEX IF NOT EXISTS idx_emergency_open ON emergency_stop(scope)
  WHERE released_at IS NULL;
