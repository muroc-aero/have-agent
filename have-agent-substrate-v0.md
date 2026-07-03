have-agent Task Substrate: Schema v0 Proposal
Status: draft for review. Target: single SQLite file, two workers, Brelje replication study as the canonical workload.

0. Design rules
1. The substrate is the only write surface for have-agent. Agents propose rows; humans approve rows; workers execute rows. No side channels.
2. The scheduler is deterministic. LLM calls happen only in DECOMPOSE (study to jobs), TRIAGE, and REPORT.
3. Every state transition is an event. Every non-deterministic choice is a decision log entry linked from an event.
4. Humans touch exactly two states: proposed and review. Everything else is machine territory.
5. One database. Substrate tables live alongside the PROV-Agent tables; events carry an optional prov_ref so the COP and provenance can never disagree.

1. Database
muroc.db, SQLite, WAL mode, busy_timeout=5000. Existing PROV-Agent tables untouched. New tables below. IDs are ULIDs (sortable, no coordination needed across workers).
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

2. Tables
2.1 study
CREATE TABLE study (
  id            TEXT PRIMARY KEY,          -- ULID
  title         TEXT NOT NULL,
  intent_yaml   TEXT NOT NULL,             -- verbatim StudyRequest as submitted
  status        TEXT NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft','proposed','approved','running',
                                  'review','closed','aborted')),
  owner         TEXT NOT NULL,             -- 'human:alex'
  policy_json   TEXT NOT NULL,             -- see §5
  plan_proposal_json TEXT,                 -- agent's decomposition, set at 'proposed'
  conclusion_ref TEXT,                     -- record_conclusion artifact/run ref
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  closed_at     TEXT
);
2.2 job
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
  resource_json TEXT NOT NULL DEFAULT '{}',    -- see §4
  payload_json  TEXT NOT NULL,                 -- see §4, type-specific
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
Dependencies (DAG):
CREATE TABLE job_dep (
  job_id      TEXT NOT NULL REFERENCES job(id),
  depends_on  TEXT NOT NULL REFERENCES job(id),
  PRIMARY KEY (job_id, depends_on)
);
A job is runnable iff state='queued' AND all depends_on jobs are in a terminal-success state (succeeded for CHECK deps on ANALYSIS is wrong; see dep semantics below).
Dep semantics v0: a dependency is satisfied when the upstream job reaches accepted OR (succeeded AND the study policy auto-accepts that job type). REPORT deps are satisfied when upstream jobs reach any terminal state (accepted,rejected,infeasible,cancelled, exhausted failed), because the report must cover failures too. Encode this as a dep_mode column if the special-casing gets ugly; v0 hardcodes REPORT semantics.
2.3 worker
CREATE TABLE worker (
  id            TEXT PRIMARY KEY,           -- 'worker:laptop-m5', 'worker:vps-1'
  capabilities_json TEXT NOT NULL,          -- {"solvers":["ocp","oas"],"mem_mb":16384}
  capacity      INTEGER NOT NULL DEFAULT 1, -- concurrent jobs
  status        TEXT NOT NULL DEFAULT 'online'
                CHECK (status IN ('online','draining','offline')),
  last_heartbeat TEXT NOT NULL,
  meta_json     TEXT DEFAULT '{}'
);
Workers are pull-based. Claim protocol (atomic in one transaction):
UPDATE job SET state='assigned', assigned_worker=:wid,
       lease_expires_at=:now_plus_lease, state_updated_at=:now
WHERE id = (SELECT id FROM job WHERE state='queued' AND <runnable> AND <fits :wid>
            ORDER BY priority, created_at LIMIT 1)
RETURNING id;
Lease default 2x estimated runtime, min 10 min. A reaper flips expired-lease jobs back to queued (increment attempt) and emits an event. Heartbeat extends the lease.
2.4 verdict
CREATE TABLE verdict (
  id          TEXT PRIMARY KEY,
  job_id      TEXT NOT NULL REFERENCES job(id),
  run_ref     TEXT,
  level       TEXT NOT NULL CHECK (level IN ('pass','warn','fail','error')),
  checks_json TEXT NOT NULL,     -- [{check, level, detail}, ...] from range-safety
  summary     TEXT,
  created_at  TEXT NOT NULL
);
2.5 event (the common operating picture)
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
Append-only. The tower is views over this table plus current-state reads of study/job/worker. No tower-owned storage, ever.

3. State machines
3.1 study
draft -> proposed -> approved -> running -> review -> closed
any non-terminal -> aborted
proposed: agent has attached plan_proposal_json. approved -> running happens on first job dispatch. running -> review when all jobs terminal. Human closes.
3.2 job
proposed --approve--> approved --enqueue--> queued --claim--> assigned
assigned --start--> running
running --ok--> succeeded --auto/human accept--> accepted
running --ok--> succeeded --gate--> review --human--> accepted | rejected
running --err--> failed --spawn TRIAGE--> triage
triage outcomes: retry_spawned (new job, parent_job_id set, attempt+1)
                 | infeasible | escalated
any pre-running state --cancel--> cancelled
Enforce transitions in one place (a transition(job_id, to_state, actor, payload) function that writes job + event atomically). Nothing else mutates job.state.
Human-touchable transitions: proposed->approved, proposed->cancelled, review->accepted|rejected, escalated->*, plus study-level approve/abort. Everything else is system/agent.
3.3 event verb vocabulary v0
study.submitted, study.plan_proposed, study.approved, study.aborted, study.closed
job.proposed, job.approved, job.enqueued, job.claimed, job.started,
job.succeeded, job.failed, job.lease_expired, job.retried, job.cancelled,
job.review_requested, job.accepted, job.rejected, job.escalated, job.infeasible
verdict.recorded
worker.registered, worker.heartbeat (sampled, not every beat), worker.draining, worker.offline
decision.logged        -- payload: {decision_id} pointing into PROV log_decision
report.published

4. Job payloads and resources
resource_json:
{"est_runtime_s": 240, "mem_mb": 4096, "requires": ["ocp"]}
payload_json by type:
ANALYSIS
{
  "plan_ref": "plans/brelje_hybrid_v1",
  "case_id": "e500_r700",
  "overrides": {"battery.specific_energy_whkg": 500, "mission.range_nm": 700},
  "warm_start_run": null
}
This assumes the one the-hangar refactor from the discussion: run_plan(plan_ref, overrides, case_id) as an idempotent single-case execution that emits job.started/succeeded/failed events and returns run_ref. Idempotency key: (study_id, case_id, attempt).
CHECK
{"run_ref": "<filled from dep at dispatch>", "check_suite": "brelje_parity_v1",
 "acceptance_ref": "acceptance/brelje.yaml"}
TRIAGE
{"failed_job_id": "...", "context": {"log_tail": true, "neighbor_cases": true}}
Output: an annotation event + zero or more proposed jobs (retries). Retries within policy.auto_retry_max auto-approve; beyond that they sit in proposed for the human.
REPORT
{"template": "study_briefing_v1", "include": ["carpet_plot","parity_table","triage_summary","metrics"]}

5. StudyRequest YAML v0 + policy
study: brelje_replication
title: Series-hybrid e_batt vs range sweep (Brelje & Martins replication)
owner: human:alex
baseline:
  template: ocp/tbm_series_hybrid       # the-hangar template ref
sweep:
  battery.specific_energy_whkg: [300, 400, 500, 600, 700, 800]
  mission.range_nm: [300, 400, 500, 600, 700, 800, 900, 1000]
outputs: [mtow_kg, fuel_burn_kg, battery_mass_kg, converged]
acceptance:
  parity:
    reference: refs/brelje_fig_digitized.csv
    metric: is_parity            # custom predicate module
    tolerances: {mtow_kg: 0.03, fuel_burn_kg: 0.05}   # relative
  convergence_rate_min: 0.90
  plausibility_suite: raymer_breguet_v1
policy:
  priority: 50
  compute_budget: {max_wall_hours: 6, workers: any}
  auto_retry_max: 2
  retry_strategy: warm_start_nearest_converged
  auto_accept: {verdict_level: pass}
  gate_on: [warn, fail]
  report: study_briefing_v1
Policy is stored verbatim in study.policy_json. The DECOMPOSE step turns sweep into the case matrix; anything the agent infers beyond the YAML (e.g. case ordering to enable warm starts) goes in plan_proposal_json and log_decision.

6. Scheduler policy interface
class SchedulerPolicy(Protocol):
    def select(self, runnable: list[Job], workers: list[WorkerSlot]) -> list[Assignment]: ...
v0 ships GreedyPriority (priority, then FIFO, first-fit on resources). Hungarian and LLMPlanner are later drop-ins; the interface plus the event log means any policy is replayable and benchmarkable against the same workload.

7. CLI surface v0 (have)
have submit <request.yaml>        # -> study draft->proposed (runs DECOMPOSE)
have review <study_id>            # render plan proposal / review inbox
have approve <study_id|job_id>
have reject <job_id> [--reason]
have status [study_id]            # queue + study views (tower-lite, terminal)
have events [--follow] [--object] # tail the COP
have worker run --id worker:vps-1 # start a pull worker loop
have report <study_id>            # open/print briefing artifact
have abort <study_id>

8. Open decisions (recommendations inline)
1. omd run_study: demote to "local batch executor" invoked only for offline/dev use; substrate is source of truth. Do not maintain two study objects. Recommend: demote now, deprecate after MVP.
2. run_plan override interface: add (plan_ref, overrides, case_id, warm_start_run) with idempotency on (study_id, case_id, attempt). This is the one the-hangar change to make before wiring workers.
3. DB location: single muroc.db with PROV + substrate tables. Recommend: yes; revisit only if worker write contention shows up, in which case events stay unified and job claiming moves to a queue table in a second file.
4. Reference data: digitize the Brelje & Martins carpet plot (MTOW vs range per e_batt) into refs/brelje_fig_digitized.csv before the demo; the parity check is the demo's spine.
5. CHECK execution: v0 runs range-safety checks in-process on the worker after the ANALYSIS job (still recorded as a separate CHECK job + verdict). Standalone range-safety MCP server comes when it moves out of the-hangar.
6. WATCH jobs (artifact-change triggers): schema supports the type; implement nothing in v0.

9. MVP metrics to log from day one
* wall clock: study submitted -> report published
* human minutes: sum of time between review-surface events (proxy: count of human events)
* recovery rate: retried-and-succeeded / failed
* provenance completeness: report claims with resolvable run_ref + decision chain / total claims
* queue efficiency: worker busy time / wall time
