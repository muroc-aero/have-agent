# DECISIONS

Questions and interpretation calls made while implementing
[have-agent-substrate-v0.md](have-agent-substrate-v0.md). The spec was not
redesigned; entries marked **needs your call** block nothing but should be
confirmed or reversed. Implementation pointers reference
`src/have_agent/substrate.py`.

## 1. Event verb vocabulary gaps (needs your call)

Design rule 3 says every state transition is an event, but §3.3 has no verb
for three legal transitions. Added three verbs:

- `job.triage_started` for `failed -> triage`
- `study.started` for `approved -> running`
- `study.review_ready` for `running -> review`

Rename or veto freely; they are constants in `substrate.py` (`JOB_TRANSITIONS`,
`STUDY_TRANSITIONS`, `EVENT_VERBS`). No table schema was touched.

## 2. `escalated -> *` narrowed (needs your call)

§3.2 lists `escalated->*` as human-touchable. Implemented as the three
resolutions that make sense: `escalated -> queued` (re-run), `-> cancelled`
(kill), `-> infeasible` (write off). If you want e.g. `escalated -> accepted`
(human overrides a failure as good-enough), say so and it's one line in
`JOB_TRANSITIONS`.

## 3. Actor classes for approve / accept / cancel (updated)

Three transitions allow both human and machine actors; everything else is
strictly one class (human-required: `review->accepted|rejected`,
`escalated->*`; all remaining transitions reject humans, design rule 4):

- `proposed -> approved`: humans approve plans, but §4 says "retries within
  policy.auto_retry_max auto-approve", so agents must be able to approve too
  (also TRIAGE jobs, which are pure machine territory). The policy bound is
  enforced in `triage.py`/`control.py`, not at the transition layer.
- `succeeded -> accepted`: auto-accept policy vs human accept.
- pre-running `* -> cancelled`: study aborts cascade with a system actor
  even though §3.2 lists `proposed->cancelled` as human-touchable.
  Cancelling an *escalated* job stays human-only.

## 4. Table order in the migration

`worker` is created before `job` because `job.assigned_worker REFERENCES
worker(id)`. Column definitions are verbatim from §2.

## 5. `failed` terminality is dynamic

For REPORT dep semantics, "exhausted failed" (attempt >= max_attempts, no
triage pending) counts as terminal. That predicate lives in the scheduler's
runnable query (build step 4), not in the schema — `TERMINAL_JOB_STATES` in
`substrate.py` holds only the statically terminal states.

## 6. Event append-only enforcement (implemented)

Migration `0002_event_append_only.sql` adds `BEFORE UPDATE/DELETE` triggers
on `event` that `RAISE(ABORT)`. The spec's DDL is untouched; this only
mechanizes the "append-only" rule already in §2.5.

## 7. Study `running -> aborted` cascade

`study_transition` flips only the study row; cascade-cancelling that study's
pre-running jobs is the CLI/`abort` command's job (build step 5) so the whole
cascade stays a sequence of audited `transition()` calls rather than a bulk
UPDATE.

## 8. Timestamps

ISO-8601 UTC with `+00:00` offset (`datetime.now(UTC).isoformat()`),
microsecond precision — lexicographic order equals chronological order,
matching the TEXT columns and the event-ordering queries.

## 9. Claim protocol: CAS loop instead of `UPDATE ... RETURNING`

§2.3's single-statement claim would mutate `job.state` outside
`transition()`. `scheduler.claim_next` instead selects per policy and claims
via `transition(..., expected_state='queued')`; a lost race raises
`StaleState` and the worker re-selects. Same atomicity (the UPDATE is guarded
by `AND state='queued'`), one write surface. Heartbeat lease extension writes
`lease_expires_at` directly — the lease is not state.

## 10. Reaper exhaustion: `assigned -> failed` added

§2.3 says the reaper requeues expired leases with `attempt+1`, but says
nothing about a job that keeps expiring. Implemented: requeue while
`attempt < max_attempts`, else transition to `failed` (reason
`lease_expired`, `exhausted: true`), which flows into the normal triage
path. That needs one extra legal pair, `assigned -> failed` (worker died
between claim and start), emitting `job.failed`.

## 11. TRIAGE spawning and the failed->triage lock

`control.spawn_triage` flips the failed job to `triage` first (the CAS is
the spawn lock across concurrent control loops), then creates the TRIAGE
child with `parent_job_id` set; a repair arm re-creates the child for any
`triage`-state job missing one. TRIAGE jobs run at `priority - 10` so
diagnosis jumps the sweep queue.

## 12. Retries repoint dependents' `job_dep` rows

When triage spawns a retry, `job_dep.depends_on` rows pointing at the failed
job are UPDATEd to the retry — the lineage continues there, so its CHECK
gates on the retry and REPORT never waits on a `retry_spawned` husk.
`job_dep` is structure, not state or history, so an in-place UPDATE doesn't
violate append-only; the repoint is recorded in the `decision.logged` event.

## 13. Dep rule "policy auto-accepts that job type" (v0 reading)

Implemented as: a `succeeded` upstream satisfies a (non-REPORT) dep iff the
study policy has any `auto_accept` clause. This is what lets a CHECK run
against its succeeded-but-not-yet-accepted ANALYSIS — acceptance happens
*after* the check verdict, via `control.apply_verdict_gates` (pass ->
accepted, `gate_on` levels -> review). Per-job-type auto-accept lists can
slot in when a policy needs them.

## 14. case_id short names

`e500_r700`-style ids are derived deterministically: last dotted segment,
unit suffix stripped, last word's first letter(s), prefix extended on
collision (`battery.specific_energy_whkg -> e`, `mission.range_nm -> r`).
Values embed with `.` -> `p`, `-` -> `m`.

## 15. Study conclusion_ref written outside a status transition

`report.build_report` sets `study.conclusion_ref` when the briefing is
published (with the `report.published` event), while the study is still
`running` — conclusion_ref is an artifact pointer, not state, and the human
review that follows needs the briefing in hand.

## 16. HangarExecutor param binding (sweep keys are not plan paths)

§5 sweep keys are domain parameter names (`battery.specific_energy_whkg`),
but the-hangar's `run_plan(overrides=...)` takes omd plan-path expressions
(`components[mission].config.mission_range_nm`). The spec defines no
binding between them, so `HangarExecutor` takes a `param_map` at
construction (deployment config, like the plan root); unmapped keys pass
through unchanged, assumed to already be plan paths. If a `bind:` section
lands in the StudyRequest schema later, DECOMPOSE can bake plan paths into
the job payloads and the map goes away.
