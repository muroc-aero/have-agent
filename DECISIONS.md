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

## 17. Single DB adopted (spec §8.3): substrate + PROV tables in one muroc.db

Adopted as recommended. The two schemas have no table-name overlap
(substrate: study/job/job_dep/worker/verdict/event/schema_migration;
the-hangar analysis DB: entities/activities/prov_edges/run_cases/run_keys),
verified live — `have worker run --executor hangar` defaults `--omd-db` to
the substrate `--db`, so one muroc.db carries jobs, events, verdicts, PROV
entities, and idempotency keys. Split later only if worker write contention
shows up, per the spec's revisit clause.

## 18. Acceptance rides in the CHECK payload; no separate acceptance_ref file

Spec §4's CHECK payload example carries an `acceptance_ref:
acceptance/brelje.yaml`, but §5 defines the acceptance block inline in the
StudyRequest. DECOMPOSE embeds that block verbatim into each CHECK
payload_json — one source of truth, no file that can drift from the
submitted study. Only the parity reference CSV stays a file (it is data,
resolved against the worker's `--reference-root`).

## 19. Parity tolerance semantics: digitization sigma + flat-ridge fuel

The spec's flat tolerances (`mtow_kg: 0.03, fuel_burn_kg: 0.05`) assume an
exact reference. The digitized reference is not exact everywhere, and the
paper's own min-fuel optima sit on flat objective ridges at/above 500 Wh/kg
(paper Table 4 burns 520 lb at (500 nmi, 500 Wh/kg) where the-hangar's
reproduction burns 218 lb at <2 % objective difference — both legitimate).
So `is_parity` (a) widens the effective tolerance to `max(tol, 2*sigma/ref)`
using the per-cell digitization uncertainty in the reference CSV, and
(b) honors a per-row `fuel_check` class: strict below 500 Wh/kg, advisory
(warn, not fail) on the ridge, skip where the reference is near-all-electric.
MTOW parity is always strict. See refs/README.md.

## 20. omd run_study demoted (spec §8.1): substrate is the study of record

Adopted as recommended: have-agent's substrate is the source of truth for
studies; the-hangar's study layer remains a local batch executor for
offline/dev use (its docs already frame `omd-cli study run` that way).
Nothing to change in have-agent; the formal deprecation notice in
the-hangar's docs is that repo's own follow-up.

## 21. Analysis-mode solver non-convergence is invisible to assert_convergence

Observed in the live smoke: a Newton solve that fails to converge in an
analysis-mode run still records `status: completed` with one clean final
case, so range-safety's `assert_convergence` (run exists / case data /
no-NaN / objective history) passes it. Optimize-mode runs — every real
Brelje case — expose driver history and are covered. Fixing analysis-mode
detection needs the solver residuals surfaced by the-hangar (recording
level `solver`, or a status from run_plan); flagged for a the-hangar
follow-up rather than papered over in the check suite.

## 22. Second parity anchor: upstream openconcept HybridTwin run (planned)

Direction from the user (2026-07-04): the Brelje parity reference should
also be anchored on running the upstream code, not only on digitizing the
paper figure. Upstream openconcept ships the paper's own model as
`openconcept/examples/HybridTwin.py` — the mixed objective
(fuel_burn + MTOW/100), the full Fig-5 MDO design-variable/constraint set,
and the range × specific-energy sweep grid are all in the file (driver:
ScipyOptimizeDriver/SLSQP), so a code-anchored reference is a matter of
running that sweep and recording outputs.

Plan:
* Generate `refs/brelje_upstream_openconcept.csv` (same columns as the
  digitized CSV, `source=upstream-openconcept`) by driving HybridTwin's
  optimization path from the-hangar's vendored `upstream/openconcept` tree,
  so versions match what the-hangar itself wraps.
* Start with the 500-nmi column (4 cells) to cross-validate all three
  sources at once: paper Table 4 (exact), figure digitization (±sigma),
  upstream code. Expand toward the full 132-cell grid as compute allows —
  each cell is a full MDO solve.
* Repeatability: with the exact solver setup a run should be deterministic
  per environment; variability enters across environments (BLAS/library
  versions, dict-order effects a la the PYTHONHASHSEED-flaky the-hangar
  tests). Probe with K repeats at a few cells (varying PYTHONHASHSEED);
  if spread is nonzero, record it in the sigma columns so `is_parity`'s
  existing eff-tol widening applies unchanged. If zero, sigma stays 0 and
  the reference is exact.
* `is_parity` needs no code change: acceptance.parity.reference just points
  at (or merges in) the new CSV. Whether to check against both references
  or prefer code-anchored cells where available is an open question for
  when the data exists.

## 23. the-hangar follow-ups: tracking status

Where the bugs found-but-not-fixed in the-hangar are recorded for review:
* Analysis-mode solver non-convergence invisible to assert_convergence
  (#21 above) — filed as muroc-aero/the-hangar#94.
* run_study demotion/deprecation notice in docs (#20 above) — filed as
  muroc-aero/the-hangar#95.
* Two PYTHONHASHSEED-flaky tests failing intermittently on pristine main
  (`test_ocp_pyc_prop_slot_multilane`, `test_surrogate_mission_converges`)
  — NOT yet filed as an issue (noted in comments on the-hangar PRs #92 and
  #93); needs a the-hangar issue when someone with permissions files it.

## 24. Template ref's first path segment doubles as the solver capability

Observation from the first real Brelje run: `decompose_study` derives the
ANALYSIS jobs' `resource.requires` from the template ref's first path
segment (`solver = plan_ref.split("/", 1)[0]`, spec's capability match in
§2.3). So `ocp/brelje_kingair_fuel_mdo.yaml` requires solver `ocp` — but
pointing the template directly at the-hangar's on-disk layout
(`lane_b/fuel_mdo/plan.yaml`) silently produced `requires: ["lane_b"]`,
which no worker advertises, and the study sat queued forever.

* Adopted for v0 (no code change): treat `--plan-root` as a *plan store*
  whose first-level directories are solver tags, and copy/symlink real
  plans in (README "Real runs" documents the one-time setup). This matches
  the original template naming and keeps decompose deterministic.
* Raise for review: the coupling is implicit and the failure mode is
  silent (workers online, jobs queued, nothing claims). Options worth
  considering post-v0: an explicit optional `baseline.solver` key
  (defaulting to the current derivation), and/or a control-tick warning
  event when queued jobs require a capability no online worker advertises.

## 25. Lease sizing vs real MDO runtimes (first Brelje run findings)

Two related findings from the first real 132-case run:

* Worker crash on a reaped lease (fixed): a hard cell ran ~24 min; the
  lease (2 x est_runtime_s=240, min 600 s => 10 min) expired mid-solve and
  the reaper requeued the job. When the solve returned, the worker's stale
  `running -> failed` transition raised StaleState, which propagated out of
  `Worker._process` and killed the loop. Adopted: `_process` now treats
  StaleState as "the reaper took the job — drop the obsolete result and
  keep polling" (stats key `lost_lease`; regression tests in
  tests/test_worker.py).
* est_runtime_s is hardcoded to 240 in decompose (spec's 2x-est lease,
  min 10 min). Real Brelje MDO cells run 40 s to 25+ min (worker
  --timeout 3600), so slow cells are guaranteed to outlive their lease,
  get double-executed (idempotency replay only covers the same attempt),
  and burn max_attempts on reap requeues. One-time intervention for the
  running study: patched job resource_json est_runtime_s -> 2400 so the
  lease (4800 s) exceeds the executor timeout (3600 s). Raise for review:
  decompose should size est_runtime_s from the workload (e.g. an optional
  StudyRequest field, or clamp lease to >= the worker's --timeout);
  invariant worth enforcing: lease_duration > executor timeout.

## 26. StudyRequest `bind:` section (schema addition, user-directed 2026-07-05)

The §5 schema left sweep-key -> plan-path binding undefined; v0 pushed it
to the worker-side `--param-map` flags (#16), which made every deployment
repeat the study's binding out-of-band. Adopted the forward path #16
anticipated: an optional top-level `bind:` mapping
(`domain_key: plan_path | [plan_paths]`, list = fan-out to several paths).
DECOMPOSE translates each case's overrides and bakes the result into the
ANALYSIS payload as `plan_overrides`; the domain-keyed `overrides` field is
unchanged, because CHECK parity lookups and case identity key on it.
`HangarExecutor` prefers `plan_overrides` when present and falls back to
`param_map` otherwise, so existing studies and deployments are unaffected.
The binding now travels with the study (versioned in intent_yaml) instead
of living in shell history.

## 27. StudyRequest `cases:` list (schema addition, user-directed 2026-07-05)

§5 only defined factorial `sweep:` expansion. Added an optional top-level
`cases:` list ({`overrides`, optional `case_id`}); explicit cases append
after the sweep matrix in YAML order, ids are derived with the same
short-name scheme when omitted, and duplicate case_ids reject at parse
time. A study must have a `sweep:` and/or a `cases:` list. This unblocks
non-factorial studies (probes, DOE samples, boundary refinement) without
touching decompose's determinism; DOE *generators* (LHS etc.) remain a
possible later layer that would emit an explicit case list.

## 28. Warm-start retries implemented (spec §4/§5 completion)

`retry_strategy: warm_start_nearest_converged` was schema-only: triage
seeded `warm_start_run` from the *most recently finished* converged
sibling (a stub), and the first Brelje run wrote off 4 boundary cells
(e400_r650, e450_r650, e450_r700, e550_r800) that the original pipeline
recovered with neighbor warm starts. Now implemented as nearest-in-sweep:
distance over the union of override keys, numeric axes span-normalized
across candidates (Wh/kg and nmi weigh equally), non-numeric/missing keys
cost 1 on mismatch, ties break on case_id so the pick is deterministic and
replayable. The chosen run/case are recorded on the retry's
`decision.logged` event. the-hangar's `run_plan` already consumed
`warm_start_run` (seeds DV initials from the run's final case, best-effort
cold start on miss), so no the-hangar change was needed. Candidate pool is
converged siblings within the same study; cross-study warm starts are out
of scope (idempotency and provenance are study-scoped).

## 29. Cross-study design atlas: the missing read path (proposed, user-requested write-up 2026-07-06)

v0 writes provenance completely but reads it back only as one markdown
briefing per study. Nothing answers "what do we currently know about this
vehicle?" across studies: best-known result per design point, the
feasibility boundary, which conclusions rest on gated-and-accepted
evidence vs clean passes. The provenance is an archive; queryable views
are what make it clarify a design.

What already exists on the execution plane (verified 2026-07-06 against
the live muroc.db): the-hangar's per-run surfaces (`get_results`,
`get_run_summary`, `generate_plots`, `get_provenance`) work as-is on our
`run_ref`s, and range-safety's dashboard `view_study` renders a have-agent
study directly -- `run_plan` stamps `metadata.study = study_id` and names
the effective plan `{study_id}--{case_id}`, and the dashboard's v1 study
membership keys on exactly that (`ReadModel(db_path="muroc.db")
.view_study(<study_id>)` returned all 132 Brelje members with metric
matrices). Two things it cannot show, by design: (a) the control-plane
layer -- verdicts, gate outcomes, accept/reject decisions and their
rationale, retry/triage lineage, warm-start picks -- which lives in
have-agent's tables that no the-hangar viewer reads; (b) anything
cross-study, since its study view is a projection of one study id. (Also
note: omd's `get_study_status`/`get_study_results`/`plot_study` do NOT
work on have-agent studies -- they read a StudyStore state file that only
the-hangar's own `run_study` creates; have-agent drives `run_plan` per
case and replaces those with `have status`/`have report`.)

Proposal: a thin read layer, not a new viewer. (1) A stable design-point
identity: hash of the domain-keyed `overrides` (this is exactly why #26
kept `overrides` domain-keyed and separate from `plan_overrides`). (2) SQL
views joining substrate tables (job, verdict, event) with the-hangar's
results, giving best-known-result-per-design-point with its evidence
chain. (3) A `have atlas` command / standing artifact rendering the
current state of knowledge: converged region, feasibility boundary,
verdict provenance per cell, with `run_ref` links into the existing
the-hangar/range-safety views for drill-down. Cheap (views + a report
generator over data already written), and it unlocks cross-study warm
starts (#28's declared out-of-scope) nearly for free. Read-only: no new
write surface, no schema change, so it does not touch §0 rules.

## 30. Agent-generated StudyRequests: `have propose` (proposed, user-requested write-up 2026-07-06)

The substrate was built for "agents propose rows; humans approve rows"
(§0 rule 1), and `proposed` is the safety gate for exactly that -- but v0
has no agent on the proposing side; the only StudyRequest author is a
human writing YAML. DECOMPOSE/TRIAGE/REPORT are deterministic policy, not
judgment (the spec's §0 rule 2 allowance for LLM calls in those three is
so far unused).

Proposal: `have propose "<design question>"` -- an LLM, given the
question plus the atlas state (#29), drafts a StudyRequest that is
submitted normally and lands in `proposed` for human review like any
other study. Nothing below the YAML changes: the substrate is indifferent
to who authored `intent_yaml`, DECOMPOSE stays deterministic, and the
existing plan-proposal review step is the containment. Ordering
constraint: this lands after #29 -- generation without the read path
produces studies blind to what is already known; with it, the draft can
target the actual knowledge gap ("boundary unresolved between e400-e550
at long range; propose a `cases:` study with warm starts there"). Record
the prompt/context digest on the study's `decision.logged` event so the
proposal itself is auditable. Out of scope here: any auto-approval of
agent-authored studies.

## 31. Successor studies: closing the loop (proposed, user-requested write-up 2026-07-06)

REPORT currently ends a study's story; every follow-up is a human
noticing something in a briefing and hand-writing the next YAML. The
first real run produced the canonical example: 4 boundary cells written
off, whose retry study (`cases:` + warm starts, #27/#28) had to be
conceived manually.

Proposal: let a closed study propose its successor. Either REPORT gains
an optional recommendation step, or a human/agent command on a closed
study (`have follow-up <study_id>`, or #30's propose seeded with the
study's outcome) emits a new StudyRequest into `proposed` -- refine
around the feasibility boundary, retry infeasibles warm-started from the
parent study's converged neighbors, tighten a sweep where parity
degraded. Successor studies carry a `parent_study` ref in intent so
lineage is queryable by the atlas. Never auto-approved: the human
approves each turn of the crank. Combined with #29 and #30 this is the
full loop -- question -> study -> evidence -> updated atlas -> next
question -- with humans still touching exactly two states (§0 rule 4
unchanged).

## 32. Pluggable worker executors: `--executor pkg.module:factory` (adopted 2026-07-19)

The worker's executor and check suite were a closed two-way choice baked
into the CLI (`fake` | `hangar`), so any new execution backend meant a
have-agent patch. The immediate driver is the Lane C eval bridge: sibling
`hangar-evals` wants to run one eval seed (case x harness x model) per
ANALYSIS job and fold the graded record into a verdict, and have-agent
should not have to know that repo exists.

Decision: `--executor` additionally accepts a dotted plugin spec
`pkg.module:factory`. The loader (`plugins.py`) imports the module and
calls `factory(args)` with the parsed worker CLI namespace; the factory
returns `(executor, check_suite)` satisfying the existing protocols in
`executor.py` (`check_suite` may be None, accepting the worker's
FakeCheckSuite default). Factory-specific knobs travel via the repeatable
`--executor-opt KEY=VALUE`, exposed as `args.executor_opts`, so plugins
need no argparse surface of their own. Malformed specs, import failures,
wrong-shaped returns, and protocol violations all fail fast with a
`PluginError` before the worker registers.

Deliberately unchanged: the built-in names keep their hardcoded wiring
(no registry indirection for the common cases), the Executor/CheckSuite
protocols are untouched, and the import-path burden stays where the
hangar executor already put it -- the worker launch environment
(`uv --project the-hangar --with have-agent --with hangar-evals ...`).
