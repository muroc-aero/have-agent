-- have-agent substrate v0, DDL per have-agent-substrate-v0.md §2.
-- Table order differs from the spec's prose only where foreign keys demand it:
-- worker precedes job (job.assigned_worker REFERENCES worker). Column
-- definitions are verbatim.

-- §2.1 study
CREATE TABLE study (
  id            TEXT PRIMARY KEY,          -- ULID
  title         TEXT NOT NULL,
  intent_yaml   TEXT NOT NULL,             -- verbatim StudyRequest as submitted
  status        TEXT NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft','proposed','approved','running',
                                  'review','closed','aborted')),
  owner         TEXT NOT NULL,             -- 'human:alex'
  policy_json   TEXT NOT NULL,             -- §5
  plan_proposal_json TEXT,                 -- agent's decomposition, set at 'proposed'
  conclusion_ref TEXT,                     -- record_conclusion artifact/run ref
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  closed_at     TEXT
);

-- §2.3 worker
CREATE TABLE worker (
  id            TEXT PRIMARY KEY,           -- 'worker:laptop-m5', 'worker:vps-1'
  capabilities_json TEXT NOT NULL,          -- {"solvers":["ocp","oas"],"mem_mb":16384}
  capacity      INTEGER NOT NULL DEFAULT 1, -- concurrent jobs
  status        TEXT NOT NULL DEFAULT 'online'
                CHECK (status IN ('online','draining','offline')),
  last_heartbeat TEXT NOT NULL,
  meta_json     TEXT DEFAULT '{}'
);

-- §2.2 job
CREATE TABLE job (
  id            TEXT PRIMARY KEY,
  study_id      TEXT NOT NULL REFERENCES study(id),
  type          TEXT NOT NULL
                CHECK (type IN ('ANALYSIS','CHECK','TRIAGE','REPORT','WATCH')),
  state         TEXT NOT NULL DEFAULT 'proposed'
                CHECK (state IN ('proposed','approved','queued','assigned',
                                 'running','succeeded','failed','review',
                                 'accepted','rejected','triage','retry_spawned',
                                 'infeasible','escalated','cancelled')),
  priority      INTEGER NOT NULL DEFAULT 50,   -- 0 highest
  resource_json TEXT NOT NULL DEFAULT '{}',    -- §4
  payload_json  TEXT NOT NULL,                 -- §4, type-specific
  assigned_worker TEXT REFERENCES worker(id),
  lease_expires_at TEXT,                       -- claim lease; NULL unless assigned/running
  attempt       INTEGER NOT NULL DEFAULT 1,
  max_attempts  INTEGER NOT NULL DEFAULT 3,
  parent_job_id TEXT REFERENCES job(id),       -- retry/triage lineage
  run_ref       TEXT,                          -- the-hangar run_id once executed
  artifact_refs_json TEXT DEFAULT '[]',
  verdict_id    TEXT,                          -- latest verdict
  created_at    TEXT NOT NULL,
  state_updated_at TEXT NOT NULL
);

CREATE INDEX idx_job_sched ON job(state, priority, created_at);
CREATE INDEX idx_job_study ON job(study_id, state);

-- §2.2 dependencies (DAG)
CREATE TABLE job_dep (
  job_id      TEXT NOT NULL REFERENCES job(id),
  depends_on  TEXT NOT NULL REFERENCES job(id),
  PRIMARY KEY (job_id, depends_on)
);

-- §2.4 verdict
CREATE TABLE verdict (
  id          TEXT PRIMARY KEY,
  job_id      TEXT NOT NULL REFERENCES job(id),
  run_ref     TEXT,
  level       TEXT NOT NULL CHECK (level IN ('pass','warn','fail','error')),
  checks_json TEXT NOT NULL,     -- [{check, level, detail}, ...] from range-safety
  summary     TEXT,
  created_at  TEXT NOT NULL
);

-- §2.5 event (the common operating picture)
CREATE TABLE event (
  id          TEXT PRIMARY KEY,
  ts          TEXT NOT NULL,
  actor       TEXT NOT NULL,     -- 'human:alex' | 'agent:have' | 'worker:vps-1' | 'system:scheduler' | 'system:reaper'
  verb        TEXT NOT NULL,     -- controlled vocabulary, §3.3
  object_type TEXT NOT NULL CHECK (object_type IN ('study','job','worker','verdict')),
  object_id   TEXT NOT NULL,
  payload_json TEXT DEFAULT '{}',
  prov_ref    TEXT               -- PROV-Agent activity id when one exists
);

CREATE INDEX idx_event_object ON event(object_type, object_id, ts);
CREATE INDEX idx_event_ts ON event(ts);
