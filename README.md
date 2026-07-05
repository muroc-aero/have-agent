# have-agent

Control plane for muroc-aero: a task substrate, deterministic scheduler, and
pull-worker protocol that drive engineering studies on
[the-hangar](https://github.com/muroc-aero/the-hangar) (the execution plane).

The design is fixed in [`have-agent-substrate-v0.md`](have-agent-substrate-v0.md);
deviations and adopted recommendations are logged in [`DECISIONS.md`](DECISIONS.md).
Core rules: one SQLite database is the only write surface, every job state
mutation goes through `transition()`, every transition is an append-only event,
and humans touch exactly two states — `proposed` and `review`.

```
StudyRequest YAML ── submit ──> study + ANALYSIS/CHECK/REPORT jobs (proposed)
                                     │  have approve
                                     ▼
                  worker(s): claim -> execute -> verdict -> gate
                                     │
                     pass ── auto-accept    warn/fail ── review inbox (you)
                                     │
                                     ▼
                     REPORT job -> artifacts/<study>/study_briefing_v1.md
```

## Install

Requires Python >= 3.11 and [uv](https://docs.astral.sh/uv/). The only runtime
dependency is PyYAML; the-hangar is imported lazily and only needed by workers
running the real executor.

```sh
git clone https://github.com/muroc-aero/have-agent && cd have-agent
uv sync
uv run have --help
```

Everything operates on one SQLite file (default `./muroc.db`, or `--db` /
`MUROC_DB`). It is created and migrated on first use — there is no init step.

## Quickstart (no the-hangar needed)

The fake executor simulates ANALYSIS runs and verdicts so you can walk the
whole loop in under a minute:

```sh
uv run have --db muroc.db submit examples/quickstart.yaml
#   study 01ABC... proposed: 4 cases, 9 jobs {'ANALYSIS': 4, 'CHECK': 4, 'REPORT': 1}

uv run have --db muroc.db approve <study_id>

# run one worker; inject a 'warn' verdict on one case to exercise the gate
uv run have --db muroc.db worker run --id worker:local-1 \
  --runtime 0.1 --check-level e300_r400:warn --idle-exit 10

uv run have --db muroc.db status
#   ANALYSIS accepted=3  review=1     <- warn was gated to your review inbox
uv run have --db muroc.db review <study_id>
uv run have --db muroc.db approve <job_id>     # accept the gated case
uv run have --db muroc.db report <study_id>    # print the briefing
uv run have --db muroc.db events --limit 50    # the append-only COP
```

What happened: `submit` ran DECOMPOSE (sweep -> case matrix -> proposed jobs),
`approve` released them, and the worker claimed jobs, ran the (fake) analyses,
recorded a verdict per CHECK, and applied the policy gates from the YAML —
`auto_accept: {verdict_level: pass}` accepted the clean cases,
`gate_on: [warn, fail]` routed the warn case to review. The worker also runs
the control tick (retries, lease expiry, gating) every poll, so workers + the
DB are the complete deployment; there is no separate scheduler daemon.

Failure handling is worth trying too: `--fail-case e300_r400:1` makes that
case fail on attempt 1, and you can watch the retry (`auto_retry_max`) recover
it in `events`.

Note (v0): REPORT depends on the CHECK jobs, not on review outcomes, so the
briefing can be generated while a gated case is still in review — it is a
snapshot; re-review decisions do not regenerate it.

## Real runs against the-hangar

Workers with `--executor hangar` drive the-hangar's `run_plan` per case and
run the range-safety CHECK suite (convergence + parity vs a digitized
reference + plausibility fences) in-process (spec §8.5). One `muroc.db` holds
both the substrate tables and the-hangar's provenance/results tables
(spec §8.3, DECISIONS #17) — pass the same path everywhere.

Requirements:

- a the-hangar checkout, set up per its README (vendored `upstream/` trees,
  `git submodule update --init`, `uv sync --all-packages`)
- the-hangar at or after PR #93 (idempotency keys silently land in the wrong
  DB file before it), plus PR #96 for the Brelje lane_b plan itself

One-time plan-store setup: `baseline.template` refs are
`<solver>/<name>.yaml` — the first segment doubles as the solver capability
the jobs will require (workers advertise theirs via `--solvers`, default
`ocp`). Populate a plan-store directory accordingly:

```sh
HANGAR=/path/to/the-hangar
mkdir -p plan-store/ocp
cp $HANGAR/packages/omd/demos/brelje_2018a/lane_b/fuel_mdo/plan.yaml \
   plan-store/ocp/brelje_kingair_fuel_mdo.yaml
```

The worker must run in an environment where both packages import; the
simplest way is uv's overlay:

```sh
uv run have --db muroc.db submit examples/brelje_replication.yaml
uv run have --db muroc.db approve <study_id>

uv run --project $HANGAR --with /path/to/have-agent \
  have --db muroc.db worker run --id worker:hangar-1 --executor hangar \
  --plan-root ./plan-store \
  --reference-root /path/to/have-agent \
  --mode optimize --timeout 3600
```

The pieces:

- `--plan-root` resolves the StudyRequest's `baseline.template` (a plan-store
  ref) to a plan file.
- The StudyRequest's `bind:` section maps domain sweep keys to plan parameter
  paths (a key may fan out to several paths); DECOMPOSE bakes the translation
  into each job payload, so it travels with the study and workers need no
  per-deployment flags. For studies without a `bind:`, the worker-side
  `--param-map SWEEP_KEY=PLAN_PATH` (repeatable) still applies as the
  fallback (DECISIONS #16, #26).
- `--reference-root` resolves `acceptance.parity.reference`
  (`refs/brelje_fig_digitized.csv`) — point it at this repo.
- `--omd-db` only if the-hangar's tables live in a separate file; by default
  the worker unifies on `--db`.

Note: uv caches the `--with` build of have-agent by project metadata, not
source contents — if you edit have-agent between worker launches, add
`--reinstall-package have-agent` so the workers pick up the change.

With `policy.retry_strategy: warm_start_nearest_converged`, TRIAGE seeds
each retry from the converged sibling nearest in sweep coordinates
(`warm_start_run` in the retry payload; the-hangar seeds the plan's DV
initials from that run's final case, cold-starting on a miss). The pick is
logged on the `decision.logged` event. This is how boundary cells that
diverge from cold starts get recovered.

Case verdicts fold worst-of: convergence (range-safety assertions), parity
(outputs vs the reference cell at the case's sweep coordinates, tolerances
from the YAML, widened by digitization uncertainty), and plausibility fences
(`raymer_breguet_v1`). The briefing's case table pulls MTOW / fuel burn /
battery mass from the verdicts.

## Reference data

`refs/brelje_fig_digitized.csv` is the parity anchor for the canonical
workload: the 132-cell min-fuel MDO grid of Brelje & Martins 2018
(AIAA 2018-4979, Fig 5), digitized from the paper figure with per-cell
uncertainty and validated against the paper's Table 4 to <= 0.12%. Method,
column semantics, and the regeneration command are in
[`refs/README.md`](refs/README.md). The digitizer itself is
`refs/tools/digitize_brelje_fig5.py`.

## CLI reference

| command | what it does |
|---|---|
| `have submit <yaml>` | create study from a StudyRequest, run DECOMPOSE, propose jobs |
| `have review <study_id>` | show plan proposal + review inbox (gated jobs with verdicts) |
| `have approve <study_id\|job_id>` | approve a proposed study/job; accept a job in review; close a study in review |
| `have reject <job_id> [--reason ..]` | reject a job in review (or cancel a proposed one) |
| `have status [study_id]` | job-state rollup per study + worker table |
| `have events [--object ID] [--follow]` | tail the append-only event log |
| `have worker run --id worker:NAME ..` | start a pull worker (fake or hangar executor) |
| `have abort <study_id>` | abort a study, cancel its not-yet-running jobs |
| `have report <study_id>` | print the published briefing artifact |

`have worker run --help` lists the executor flags (fake: `--runtime`,
`--fail-case`, `--permanent-fail`, `--check-level`; hangar: `--plan-root`,
`--param-map`, `--reference-root`, `--omd-db`, `--mode`, `--timeout`).

## Repo layout

```
have-agent-substrate-v0.md   the fixed design (read this first)
DECISIONS.md                 questions raised + recommendations adopted
src/have_agent/
  substrate.py               tables, transition(), events, verdicts
  decompose.py  scheduler.py  control.py  worker.py   the loop
  executor.py                Executor / CheckSuite protocols + fakes
  hangar_executor.py  checks.py                       the-hangar bindings
  report.py  triage.py  cli.py
refs/                        parity reference data + digitizer
examples/                    quickstart.yaml, brelje_replication.yaml
```

## Development

```sh
uv run pytest -q        # 434 tests, all stdlib/SQLite, no the-hangar needed
uv run ruff check .
```
