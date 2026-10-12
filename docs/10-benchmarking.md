# 10 — Benchmarking and evaluation

> Part of the TraceMaker plan. Start at [`../PLAN.md`](../PLAN.md).
> Dataset facts verified 2026-10-02: [`../research/kicad-and-benchmarks-2026-10.md`](../research/kicad-and-benchmarks-2026-10.md).

## 1. Fixture sets

| Set | Source | Size | Format | Use |
|---|---|---|---|---|
| **FR-fixtures** | Freerouting `fixtures/` (GPL-3.0) | 155 DSN regression boards | DSN | Router robustness; direct comparison with Freerouting |
| **PCBench-FR** | Freerouting `scripts/benchmark` PCBench fixtures (from PCBench, MIT) | ~1,157 boards with unrouted + reference DSN, `.kicad_pcb`, `ground_truth.json` | kicad_pcb + DSN | **Main routing benchmark.** Freerouting publishes nightly results on exactly this set |
| **DAC2020 bm1–bm11** | github.com/DAC-2020-Submission-1703/PCB-Benchmarks | 11 hand-routed manufactured boards | kicad_pcb | Dense/BGA boards; compare vias and layers with the human routing |
| **KiCad demos** | KiCad 10.0 `demos/` | 19 boards | kicad_pcb | Real KiCad projects with schematics: rule ingestion, IPC plugin, schematic-to-board |
| **PCBWorld** | LGAI-Research/PCBWorld (BSD-3 code) | 679 real boards + 2 synthetic generators | kicad_pcb | Comparison with PCBWorld's published baselines (KiCad PNS, OrthoRoute, RL) |
| **RL_PCB** | LukeVassallo/RL_PCB (MIT) | small circuits | kicad_pcb | Placement comparison with a learned placer |
| **Place-and-route set** (own) | KiCad demos + PCBench boards with routing stripped and footprints scattered | built by script | kicad_pcb | Full schematic-to-routed-board evaluation |
| **tscircuit SRJ19 / SRJ20** | github.com/tscircuit `dataset-srj19` (MIT), `dataset-srj20` (no licence file) | 2 × 200 synthetic BGA breakout problems, two layers | Simple Route JSON, converted to kicad_pcb | Dense-package escape with a public reference figure (§7) |
| **Synthetic stress** (own) | generated: BGA fanout grids, dense buses, crossing lattices | parameterised | kicad_pcb | Unit benchmarks for escape, global routing and GPU kernels |

Licences differ per set; the harness downloads sets into `bench/data/` (git-ignored) from their sources
and never redistributes them.

`scripts/fetch_fixtures.sh` pins the three default sets to the commits the 0.8.0 results were measured on
(Freerouting `d216f90` of 2026-09-30, DAC 2020 `bee2888`, KiCad 10.0 `7a7d026`), so a fresh clone gets the same
boards, tiers and Freerouting result file; `TM_FIXTURES=latest` or `TM_REF_<SET>=<commit>` overrides a pin. Its
`derived` step builds the honest `quick`/`mid` inputs in `build/pcbench`, the legacy
`bench/data/dac2020_prepared` (`bench/prepare_dac2020.py`) and, once the placer is built, from-scratch set S.
The download pins were checked against upstream, not by a complete fresh-clone download run.

### Building unrouted fixtures

The routing sets use `bench/prepare_inputs.py` (D113). It removes tracks/arcs and every via whose copper
touches an original track on a layer in its span (including endpoint/interior/arc contact); it retains no-track
vias as fixed design copper, including thermal arrays and stitching/fences. This is a reproducible geometric
classification, not a claim to infer their electrical purpose. It keeps zone outlines/settings, drops old
`filled_polygon`/legacy `fill_segments`, and refills/saves the stripped input with KiCad 10 before routing.
After legacy conversion, missing zone/via UUIDs and every zero/duplicate UUID are replaced by deterministic
`uuid5` identifiers keyed by board name and stable item index, preserving valid unique IDs. If any identity
changed, a second refill/report is the authoritative result; fills and keepout judgment must see distinct rule
areas. Native dimension/display-text UUID aliases are de-duplicated once more after saving (identity-only,
no geometry changes). Each manifest records `identity_repairs` and `post_save_identity_repairs` by kind.
The human original is never rewritten.
`<fixtures>/<name>/` contains `unrouted.kicad_pcb`, byte-original `routed.kicad_pcb`, matching project/custom rules
beside both, and `preparation.json` (source/input hashes, removed tracks/vias, fixed vias, zones) plus preparation DRC.

Prepare with `python3 bench/prepare_pcbench.py --set quick --set mid`
and `python3 bench/prepare_modern.py`. The first uses PCBench's **published unrouted terminals** as authoritative:
PCBench renames duplicate references (e.g. `REF**`, repeated bypass-cap refs) and sometimes repairs its source.
Only fixed design copper is transplanted from `raw.kicad_pcb`, with nets mapped by **name**, after checking board
outline and copper-layer stack compatibility. Terminal-bearing net names, references and pad/net multiplicities
are verified against the published input; KiCad's equivalent legacy overbar syntax and mechanical NPTH numbering
conversion are accounted for, and unused standalone net declarations pruned by KiCad do not change terminals.
The human remains raw, whose source/counts are identified by PCBench `ground_truth.json`.
Every roster board stays scheduled. `HES-V2__autosave-hes` (raw has no Edge.Cuts, published has four),
`mechkeys_1800-controller` (raw outline has 22 graphics versus published 18), and
`decelerator4030_decelerator4030` (different legacy copper layer IDs and no raw Edge.Cuts versus five published)
retain the published unrouted board **without** raw copper transplantation. `LogicBoxen_LogicBoxen` drops its one
raw `VSS` zone because that net has no published match; it has no other raw zone to transplant. These four are not
valid human fill/resource parity comparisons: absence of restored copper is a coverage hole, not router failure
or a quality gain. No boards are excluded from the current set rosters.
The aggregate `build/pcbench/preparation.json` and each board's manifest record zone count,
`zone_preparation: transplanted|none`, `fixed_vias`, `design_copper_comparable`, `dropped_zone_nets` and `reason`;
new `boards.jsonl` rows carry that manifest in `preparation`. True preparation failures are recorded in
`exclusions.json`, exit nonzero, and cause run preflight to error rather than silently shrink the set.
Ref-based human comparisons must account for PCBench's renamed refs.
Legacy DAC2020 and place-and-route preparation keep their existing all-vias-stripped convention; they are not
the D113 routing sets.

## 2. Metrics per board

| Metric | Definition |
|---|---|
| **Clean pass** | 100% connections routed **and** zero added KiCad DRC errors (input vs output, `kicad-cli pcb drc`) |
| Completion | routed connections / routable connections (excluding pins proved dead) |
| Added DRC errors | by KiCad violation type |
| Vias, wirelength, bends | totals; also normalised to the reference routing where it exists |
| Wall time, work units | engine-reported, with hardware recorded: the time is the whole route job (every portfolio variant, D86), the work units (`expansions`) are the winning variant's only |
| Work profile | per phase (setup, first pass, negotiation, clean-up): expansions, flood cells, searches, cell checks, fixed-copper checks, cost-field cells and fields, commits, rips; deterministic, in the route JSON as `work` (winner) and `work_all_variants` (D88) |
| Route CPU time | user + system seconds of the `tracemaker route` process (`cpu_s` per board, `cpu_seconds` per run) |
| Placement (P&R set) | HPWL, lower-bound gap, crossings, courtyard overlaps (must be 0), moved parts |
| Style (informational) | per board `style`: connections, bends per connection, acute angles, stair-steps, short and off-angle segments, detour ratio, long detours (definitions in doc 05 §39.2), measured on the new copper by the engine and on the fixture's own routing (`style_ref`) by `tracemaker style`; totals in `summary.json` (`style`, `style_ref`, `style_same_boards`) and `report.md`; `compare_runs.py` lists boards whose metric changed. Never a gate |
| Determinism | output hash per board at 1 and N threads, GPU on/off |

## 3. Baselines

| Baseline | How it runs |
|---|---|
| **Freerouting** (current release, plus v2.5.0-RC12 as published) | Its own CLI on the DSN; SES imported back into `.kicad_pcb` by TraceMaker's SES reader; judged by the same KiCad DRC. Published nightly figure on PCBench: **74.6% clean, 74.8% fully routed** (v2.5.0-RC12, 1,157 fixtures) |
| **KiCad PNS / PCBWorld baselines** | Published PCBWorld numbers; optionally its environment (pins KiCad 9.0.8) |
| **OrthoRoute** | Published PCBWorld numbers (~1–2% clean pass on mixed boards) |
| **Human reference** | The original routing of PCBench and bm boards (vias, length) |
| **SA-PCB** | For placement HPWL on the P&R set |
| **TraceMaker previous commit** | Regression gate |

## 4. Harness (`bench/`)

`bench/run.py` routes each board of a set with a snapshot of the engine binary, judges input and output with
`kicad-cli pcb drc`, and writes `bench/results/<run-id>/{boards.jsonl, summary.json, report.md, boards/}`
(`summary.json` feeds the progress site). `bench/compare_runs.py` compares two runs board by board and checks the
gates of §5.

| Option | Effect |
|---|---|
| `--set NAME` | boards from `bench/sets/NAME.txt` (names the set in the summary); default fixture directories are `build/pcbench` for quick/mid and `build/modern-planes`, or the set's `#fixtures:` directory for SRJ |
| `--full` | also route the set's `#slow` boards (1,000 or more connections); everyday runs leave them out, the run before a commit includes them |
| `--work N` | deterministic work budget per board, no knowledge base; `--time` becomes a safety stop (3,600 s). Every requested portfolio variant gets the whole budget (eight by default); soft-zone refill repair has a separate budget equal to the first pass's |
| `--threads N` | router threads per board; with `--work` the default is the cores per job (at most 8), since the output does not depend on it (D47), but 1 with `--halving` in the route options, whose budget shares follow the thread count; without `--work` it is also the portfolio size (default 1) |
| `--refill` | independently refill **and save** private analysis copies with `kicad-cli pcb drc --refill-zones --save-board`; reuse that same DRC report for judging, publish `boards/<name>.refilled.kicad_pcb` and its rules; raw output stays unchanged |
| `--human` | copy `<fixtures>/<name>/routed.kicad_pcb` instead of routing, same input/output judge/refill path, rows/summary marked `human: true`; no router binary needed |
| `--route-args=...` | extra `tracemaker route` options (also `TM_ROUTE_ARGS`) |
| `--fixtures DIR` | boards from `DIR/<name>/unrouted.kicad_pcb` instead of PCBench; a `.kicad_pro`/`.kicad_dru` beside it is copied next to the output so KiCad judges with the board's rules |
| `--tier`, `--limit`, `--seed` | sample PCBench boards by Freerouting tier instead of a set |
| `--boards NAME ...` | explicit subset, overriding the set roster for smoke/paired-cohort runs; the set name is still recorded |
| `--intents DIR` | route with `DIR/<board>.intent.json`; draft missing manifests from the input (doc 05 §45). Freeze and review them before a matched comparison |

**Independent refill and accounting (D116).** Harness `--refill` invokes KiCad, not the engine refill.
The engine's `--soft-zones` refill/repair (doc 05 §36) informs its final connectivity and exit status, but
does not replace the independent judge or the saved analysis geometry. `bench/refill_parity.py` compares
engine-refilled connectivity against KiCad-refilled connectivity; differences and unsupported refill
features remain visible. An engine count alone is neither KiCad parity nor human-quality sign-off.
The harness retains whole-job `seconds`, `variant_seconds`, `variants`, route CPU time and `work` /
`work_all_variants`. Planned `routed` / `connections`, stop reasons and unrouted diagnostics describe the
selected routing pass; `unconnected_after_refill` is its separate engine refill judgment. Repair work is
charged even if its copper is rejected. No combined-revision validation is claimed by preserving either
branch's historical measurements below.


**Sets** (`bench/sets/`):

All three sets use `--refill --route-args="--variants 1 --soft-zones --keep-vias-off-pads --diff-pairs"` for the
plain **P** arm; **T** adds `--tree-attach`, **R** adds that plus `--intents DIR`. Use the same frozen binary,
work budget, input/rule hashes and wall safety stop (`--time 3600`) for every arm. A diagnostic ablation may turn a
flag off, but must not silently replace this protocol when a flag misbehaves. Human runs:
`python3 bench/run.py --set SET --human --refill --full --name human-SET`.

| Set | Boards | Preparation / default fixtures | Work | `#slow` (only with `--full`) | When |
|---|---|---|---:|---|---|
| `quick` | 30 PCBench tier-A roster boards (four documented copper-coverage warnings across quick/mid) | `prepare_pcbench.py --set quick` / `build/pcbench` | 100 M | none | every routing change |
| `mid` | 11 hard PCBench roster boards (same explicit coverage policy) | `prepare_pcbench.py --set mid` / `build/pcbench` | 200 M | 3 (Aleste, logicbone, decelerator4030) | search, plans and rip-up |
| `modern-planes` | 16 pinned open-hardware KiCad 7-10 boards; 4-8 copper layers and original rules | `prepare_modern.py` / `build/modern-planes` | 200 M | 5 (ecp5-dc-scm, glasgow, hackrf-pro, cm4-baseboard, reform-motherboard) | dense planes and intent |

One budget per set, high enough that most boards finish: lower budgets stop nearly every board on its work budget
and rank speed, not routing (at 1 M, 17 of 30 quick boards stopped on work; at 100 M none). Boards marked `#slow`
in the set file (1,000 or more connections; several still stop on work at 200 M) are prepared always and routed only
with `--full`, the run before committing. The old `planes` set (KiCad demos) is retired: `modern-planes` covers
boards with inner planes and original rules. Example: `python3 bench/run.py --set quick --work 100000000 --jobs 4
--refill --route-args="--variants 1 --soft-zones --keep-vias-off-pads --diff-pairs"` (one shell line).
Publish completion and stop reason, charging all work. Matching the human is success; credible improvements
receive credit. A budget-stopped board is censored, not proof of infeasibility, and resource totals alone cannot
rank it above human.

**D113 smoke evidence (100 k work, not a full-tier rebaseline).** Two boards per set were routed with the new
plain protocol and judged alongside their originals (`d106-smoke-{quick,mid,planes,modern}`,
`d106-human-{quick,mid,planes,modern}`). All 16 output analysis copies were saved; filled-polygon geometry differed
between router and original on all eight pairs. The first smoke recorded three `items_not_allowed` reports on
`C-BISCUIT_buck-reg-5v`; these were **preparation artifacts, not router defects**: KiCad's legacy conversion gave
eight zones the same all-zero UUID, confusing keepout areas with other copper zones. Making those IDs unique
clears the false violations; the preparation path now repairs IDs before its authoritative refill. The protocol
flags are unchanged. KiCad can similarly misreport keepouts on byte-original human boards whose converted
zones share zero IDs: preserve originals and report this validity limitation rather than call it a human defect.
The corrected same-budget/same-flag rerun (`d106-fixedids-final-quick`) routes C-BISCUIT 36/47 with 11 unconnected
and no added errors; its saved KiCad report has **zero** `items_not_allowed`. No router code or flag changed.
All 65 inputs were re-prepared and scanned after identity repair: **712,149 UUID fields checked, no zero,
duplicate or missing zone/via IDs**, source humans byte-identical and retained copper counts unchanged.
Repair occurrences (including final drawing-alias serialization) were quick 40 zero / 137 duplicate,
mid 349 zero / 104 duplicate, planes 0 zero / 22 duplicate and modern 0 zero / 207 duplicate.
The PCBench human originals also have existing defects; `plane_smd` is an intentionally unrouted synthetic test,
not a routed human-quality reference (its `--human` copy has 47 unconnected items).
Preparation produced all 65 roster fixtures: zone-bearing inputs quick 25/30, mid 8/11, planes 8/8, modern 16/16;
retained no-track vias 51/118/71/2,874 respectively. All remaining zone-free PCBench cases are either naturally
zone-free or the explicit coverage exceptions above. Repeated preparation of one legacy PCBench, demo and modern
example gave identical terminal sets, geometry/settings and fills. The legacy conversion regenerated some
UUIDs (bytes differ); demo and modern examples were byte-identical. Freeze the generated fixture artifacts and
their hashes across arms rather than regenerate legacy inputs independently.

`modern-planes` replaced `gndplane` (D112), whose boards were 13 KiCad 5 files without project rules. Every board is
pinned to a commit in `bench/sets/modern-planes.json` (repository, commit, board path, licence: Apache-2.0,
CERN-OHL-S/P, 0BSD, CC-BY-4.0). `bench/prepare_modern.py` fetches only the KiCad files and licence texts of each
commit into `bench/data/modern/` (≈ 220 MB) and writes `build/modern-planes/<name>/unrouted.kicad_pcb` with tracks/arcs
and track-contact vias removed, fixed vias/zones retained and fresh unrouted fills, plus project/custom rules
and the byte-original human as `routed.kicad_pcb`. All 16 originals have 0 unconnected items in prior KiCad 10.0.3 DRC with refilled
zones; their existing DRC errors (courtyard overlaps, starved thermals, Cynthion's 4 shorting items between net-tie
pads) are not routing errors. Sizes: six small (25-264 connections), five medium (355-967), five large (1,334-1,934).
KiCad 10 boards name nets in pad and zone references (`(net "GND")`); the reader handles both forms.

**Historical D112 baseline.** At 3 M work the large boards were barely half routed (router 63 % of 9,925
connections), motivating the former 20 M budget. D115 now specifies 200 M for `mid` and `modern-planes`.
KiCad's DRC report lists at most 499 unconnected items; capped counts cannot give trustworthy completion
(§9). Route JSON's `routed`/`connections` remains a diagnostic of planned routing, not a substitute for
independent uncapped connectivity. The cap also affects Aleste and decelerator4030 in `mid`.

**Comparability cutover (D113).** Do not directly compare pre-D113 quick/mid results (including `bw-quick100-*`
and published Freerouting DSN results) to these zone-restored inputs. Earlier planes/modern inputs removed all
thermal/stitching vias and retained human fills; earlier saved outputs did not contain the refills used by DRC.
The historical `final8`, D81 §6 figures, `mp20-*`, `bw-modern200-*` and `bw-planes300-*` remain valid descriptions
of their own inputs/flags, not this new regression/quality protocol. Rebaseline all arms before reporting a gain.
Old runs can still be scorecarded with freshly refilled analysis copies, but their missing fixed copper is not
repaired by refilling and must remain a coverage/comparability warning.

The harness classifies a **plane net** by the same >=60% zone-union test on *any* copper layer, regardless of
net spelling (power planes count too). Zone contours use even-odd holes and are clipped to the even-odd board
outline; arcs are tessellated with at most 1 micrometre sagitta, and outline endpoints join within 0.01 mm.
Rule areas and teardrops do not count, and stale fill polygons never determine plane ownership.
After KiCad's judge, each unconnected record counts once: plane if its item's bracketed net name owns a plane,
otherwise signal. `boards.jsonl` records `plane_nets` with qualifying per-layer coverage,
`plane_unconnected_before/after`, `signal_unconnected_before/after`, `plane_vias_added`, `signal_vias_added`,
and `plane_track_mm_added` / `signal_track_mm_added` (output minus input tracks/arcs/vias, measured in mm).
`summary.json` aggregates these counts and reports `plane_metrics_boards`; `report.md` gives a per-board table.
A malformed outline is recorded as `plane_metrics_error`, not silently classified as signals, and does not
change routing or KiCad judging. Existing metrics and routing arguments are unchanged.
The measurement/preparation regression tests run with
`python3 -m unittest discover -s bench -p 'test_*.py'`.

**Comparing.** `bench/compare_runs.py BEFORE AFTER [--expect-identical]` lists every board whose routed output
differs or got worse, the clean-pass and completion totals and router seconds, and exits 1 if a gate fails.
When plane metrics are present, it also shows plane/signal unconnected columns for every paired board.
`--expect-identical` adds "every routed board byte-identical" for speed-ups and refactors. Build the "before"
binary from the base commit (a clean `origin/main` worktree) and run both on the same machine. Route CPU time is
printed per board and in total. When both runs used the same `--work`, boards whose output is byte-identical did the
same work, so their CPU change is run-to-run noise or a change in speed that leaves the output alone; the report
prints that band and lists the boards (and the total) that moved beyond it, and at least 10 %. When every identical
board moved the same way by more than 10 % it says that too: one pair of runs cannot tell a faster binary from a
quieter machine, a second run of one binary can. All of it is a pointer for gate 5, not a failure.

**Other tools.**

| Tool | Purpose | Status |
|---|---|---|
| `speed_ab.py` | engine speed A/B at a fixed work budget: wall and CPU time, instructions, memory, byte-identical outputs (`--demos` for KiCad demos) | maintained |
| `scripts/drc_parity.py`, `drc_broken_parity.py` | `tracemaker drc` against KiCad on demo and broken boards (ctest `kicad_drc_parity`, `kicad_drc_broken_parity`); counts are capped at KiCad's 199 per type | maintained |
| `quality.py`, `board_quality.py`, `quality_bench.py`, `compare.py`, `human_baseline.py` | gated run-directory human-parity scorecard (§9); board-level geometry metrics; TraceMaker against Freerouting versions | maintained |
| `refill_parity.py` | `tracemaker drc --refill-zones` against `kicad-cli pcb drc --refill-zones`: unconnected items per net on boards with zones (`--dir … --glob raw.kicad_pcb --sample N` for PCBench; doc 05 §36) | maintained |
| `check_env.py`, `prepare_dac2020.py`, `prepare_demos.py`, `make_place_sets.py` | tool checks and fixture preparation | maintained |
| `pair_eval.py`, `feasibility.py`, `crules_detect.py`, `crules_place.py`, `crules_tiers.py` | feature evaluations (diff pairs, escape feasibility, component rules) | kept for re-measuring their features |
| `make_bus_boards.py`, `bus_eval.py` | generated bus boards and the bus evaluation (doc 05 §38): routes without and with the bus options at one budget, measures with `tracemaker buses`, judges with KiCad | kept for re-measuring bus routing |
| `m6_bench.py`, `place_m8.py`, `place_auto.py`, `place_flip.py`, `place_intent.py`, `place_variants.py`, `compare_placed.py`, `rescore_placed.py`, `dsn_place.py` | milestone evaluations of the global router and placement | historical; results are in the docs that cite them |
| `ses_import.py`, `record_fr.py` | import a Freerouting session; screen-record Freerouting | utilities |
| `cpu_sample.py` | per-thread CPU sampling from `/proc` | Linux only |

**Not built** (planned in the first version of this document): manifest files, a `standard` (~300 boards) and
`full` tier as named sets, `bench/report.py` with an HTML dashboard, and a sign test. The human-parity scorecard
provides paired-board bootstrap intervals (§9). Seeds: best-of-1 is the headline; best-of-5 is reported separately
where a comparison needs it (PCBWorld reports baselines as best of 5).

**Judge noise.** KiCad's `solder_mask_bridge` count can differ between runs on byte-identical boards when zones
are refilled (StickHub: 17, 15 and 13); read a change in that type alone as noise.

## 5. Gates (used by the roadmap)

1. No board in `quick` gets worse in clean pass or completion (`compare_runs.py`). Only boards the router
   finished count: the router's stop reason (`<board>.kicad_pcb.route.json`, `stop`) is `complete` in the after
   run. A board stopped by its work budget is listed, not gated, because its unconnected count shows where the
   budget ran out rather than what the change did; a board that finished before and no longer finishes fails (D129).
2. Zero added KiCad DRC errors on every board that was clean before (`compare_runs.py`).
3. Time within budget on every board (with `--work`, the budget is work units; wall time only stops the job).
4. Routed boards byte-identical unless the change is meant to change results (`compare_runs.py
   --expect-identical`; then the differences are recorded).
5. Quality is judged at equal work, speed at equal wall time. The work budget charges search expansions and flood
   cells only (the work profile shows what else a run did), so a change that moves work into dearer operations, such
   as reaching negotiation sooner, takes longer at the same work while routing more in the same time (doc 05 §37).
   A claim that a change is faster or slower needs runs at equal wall time; `compare_runs.py` points at the boards.

## 6. Implementation status (2026-10-02; sets, work budgets and refill 2026-10-08, D81)

- `bench/run.py` samples PCBench boards per Freerouting tier, routes each with a binary snapshot, judges with
  `kicad-cli pcb drc`, and compares with Freerouting's published per-board results (`benchmarks.json`).
- Samples: tier A 40 of 453 boards, tier B 40 of 560, tier C 30 of 122, tier D all 22 (seed 1).
- Added errors count only violations involving a track, via or arc (decision A16); other new KiCad reports are
  kept as diagnostics.
- Results go to `bench/results/<run>/` and the progress site's benchmark panel.
- Latest (`final8`): tier A 100% clean, B 65.0%, C 56.7%, D 50.0% (Freerouting 2.5.0-RC12: 100%, 50.0%, 46.7%,
  36.4%); no router-introduced DRC errors on any board.
- Sets, `--work`, `--refill` and `compare_runs.py` (D81), measured on `origin/main` `a5532a9`:
  - `--set quick --work 1000000 --route-args "--variants 1"` reproduces the earlier quick-tier run (made with
    `TM_ROUTE_ARGS`) byte for byte on all 30 boards: 40.0 % clean, 89.2 % completion, about 30 s with `--jobs 4`.
  - `--set mid --work 20000000`: 0 of 11 clean, 76.4 % completion, 3 minutes with `--jobs 4`.
  - `--set planes --work 3000000 --refill --route-args=--soft-zones`: 86.1 % completion. Without `--refill` the
    judge reads the stale fills, and without the copied project files KiCad judged demo outputs on default rules
    (hundreds of false `track_width` and `clearance` errors on RoyalBlue, StickHub and plane_smd).

- Timing (D86). The summary's `seconds` used to be the winning variant's own time. With `--work` every board runs
  all eight variants, and at one thread per board (the old default) they ran one after another, so a board's job
  took several times the reported figure (more than eight times when the winner is one of the quicker variants):
  on the `mid` set at 20 M, 6–16 s reported against 45–107 s of process time. `seconds` now covers the whole job,
  `variant_seconds` keeps the winner's time and `variants` lists every variant. A per-variant profile
  (decelerator4030, LimeSDR, logicbone at 5 M and 20 M) put 81–89 % of the process in the seven losing variants
  and under 2 % in reading, set-up, clean-up and writing. Times in runs made before D86, including the Freerouting
  comparisons, are the winner's only.
- Work profile and route CPU time (D88). The counters change no output (`quick` 30/30 byte-identical) and cost no
  measurable time. Two `mid` runs at 20 M (`--jobs 4 --threads 3`, the same work), `origin/main` against D87 (#21):
  the four byte-identical boards moved +9 % to +14 % in CPU time between the runs (noise of one sequential pair on
  this 14-core Mac, or a slow-down that left their output alone: one pair cannot tell);
  only sbc (+41 %) and kitspace_d20 (+24 %) moved beyond it. A 10 % threshold
  alone would have flagged seven boards. Single CPU-time comparisons of under about 15 % are not evidence here.

## 7. tscircuit BGA breakouts: sets `srj19` and `srj20` (D90, measured 2026-10-11)

The dense-package work (M9) had no public reference figure. tscircuit publishes 400 synthetic BGA breakout
problems in Simple Route JSON and a dashboard with its own router's results on them, so these two sets give one.
The format support is doc 08 §9.

**Data.** Fetched into `bench/data/tscircuit/` at pinned commits by `scripts/fetch_fixtures.sh tscircuit`, never
copied into this repository:

| Set | Source | Commit | Licence | Problems |
|---|---|---|---|---|
| `srj19` | github.com/tscircuit/dataset-srj19 (BGA breakouts with passive components on the other side) | `c1206f3` | MIT | 200, two layers, 11–87 connections (7,395), 0.15 mm traces, vias 0.3 / 0.2 mm |
| `srj20` | github.com/tscircuit/dataset-srj20 (BGAs with missing balls) | `9384fb8` | none stated (no licence file, none in `package.json`): local benchmarking only | 200, two layers, 11–108 connections (8,715), 0.12 mm traces, no via size given |

Both use a 0.8 mm ball pitch with 0.36 mm square pads, every connection has two points, and neither states a
clearance, a board-edge rule or blind vias.

**Conversion.** `bench/prepare_srj.py` (run by the fetch step once the engine is built) converts every problem
with `tracemaker srj-import --clearance-mm 0.1 --hole-to-hole-mm 0.1` into
`bench/data/tscircuit/boards/<set>/<sample>/unrouted.kicad_pcb` with its project. The two distances are those of
tscircuit's published pass criterion (below), so KiCad's judge and theirs test the same thing; the importer's own
default (0.15 mm, tscircuit's router fallback, and no hole rule) is reported as a variant. Via size for `srj20` is
tscircuit's fallback (0.3 / 0.15 mm). The script checks every board by counting: connections in the problem =
ratsnest the board implies = connections `tracemaker route` reports. All 400 convert and all 400 pass.
`bench/sets/srj19.txt` and `srj20.txt` name the fixture directory in a `#fixtures:` line, which `bench/run.py`
now reads, so the sets run by name.

**Judges.** The primary judge is unchanged: KiCad's DRC with the board's rules; a board is a clean pass when
nothing is unconnected and no error involving a track or via was added. The second score is tscircuit's own.
`bench/srj_score.py` exports every routed board with `tracemaker srj-export` and runs tscircuit's
`evaluateRelaxedDrc` on it: the function behind the "relaxed DRC" column of its dashboard, from
tscircuit-autorouter `5bcf774` (`lib/testing/`) with `@tscircuit/checks` 0.0.233, called as its benchmark calls it
(`scripts/benchmark/benchmark-run-task.ts`): 0.1 mm between copper of different nets, 0.1 mm between via holes
(same net included), traces continuous and inside the board. `scripts/fetch_fixtures.sh tscircuit-checker`
installs it (node, packages pinned to the versions that repository names). A board passes when every connection
is routed and the checker reports nothing. The checker was not re-implemented.

tscircuit's dashboard counts boards: "completed" is the share of problems its router solved outright, comparable
with *fully routed* below and not with mean completion; "relaxed DRC pass" is the share solved with no checker
error. Its figures, read on 2026-10-10 for its router at 1× effort: `srj19` 91.5 % completed, 89.5 % passing;
`srj20` 77 % and 77 %. They were not reproduced here: tscircuit's router was not run on this machine, and its run
time per problem is not known to us.

**Results.** `python3 bench/run.py --set <set> --work <N> --jobs 12 --threads 1 --route-args "--variants 1
--no-gpu"`, then `python3 bench/srj_score.py <run>`. All 200 boards of each set, defaults otherwise.

| Set | Work | Mean completion | Fully routed | Clean pass (KiCad) | Relaxed pass (tscircuit's checker) | Median s | p95 s |
|---|--:|--:|--:|--:|--:|--:|--:|
| srj19 | 1 M | 75.7 % | 23.0 % | 23.0 % | 23.0 % | 1.5 | 2.2 |
| srj19 | 10 M | 92.5 % | 60.5 % | 60.5 % | 60.0 % | 9.2 | 14.1 |
| srj19 | 50 M | 96.7 % | 79.5 % | 79.5 % | 78.5 % | 34.2 | 50.2 |
| srj19, tscircuit's router (its dashboard) | | | 91.5 % | | 89.5 % | | |
| srj20 | 1 M | 53.1 % | 9.5 % | 9.5 % | 9.5 % | 1.4 | 1.9 |
| srj20 | 10 M | 81.3 % | 33.5 % | 33.5 % | 33.5 % | 9.1 | 10.9 |
| srj20 | 50 M | 87.6 % | 50.0 % | 50.0 % | 50.0 % | 40.9 | 51.9 |
| srj20, tscircuit's router (its dashboard) | | | 77 % | | 77 % | | |

10 M is the sets' standard budget (the trend rows show how much the figure depends on it). Seconds are the
route's own wall time on one thread with twelve boards running at once on a machine shared with other jobs:
indicative only.

- TraceMaker is behind tscircuit's router on both sets at every budget tried: by 12 points of fully routed
  boards on `srj19` and 27 on `srj20` at 50 M.
- No run added a KiCad DRC error on any board (26 runs, 2,500 board routes): every fully routed board is a clean
  pass. What is missing is completion.
- KiCad and tscircuit's checker agree except on 1 board at 10 M and 2 at 50 M (`srj19`), which KiCad passes and
  the checker fails with "overlaps … (accidental contact)" between two traces. An independent distance
  calculation on the exported traces puts the copper of each reported pair at least 0.21 mm apart. The checker
  (`checkEachPcbTraceNonOverlapping`) also reports two separate collinear segments of different nets as touching
  at some spacings (reproduced with two-segment inputs), which a lattice router's output contains. The relaxed
  column is the checker's verdict as it is.
- TraceMaker's own count of routed connections is sometimes one lower than KiCad's (it joins tracks at their end
  points; KiCad 10 also joins copper that overlaps). The harness uses KiCad's count, and the export follows
  KiCad's rule; the two agreed on every board.

**Budget and portfolio**, first 50 problems of each set (fully routed = clean pass in every row):

| | srj19 | srj20 |
|---|--:|--:|
| 1 M, one variant | 22 % | 8 % |
| 10 M, one variant | 66 % | 34 % |
| 50 M, one variant | 88 % | 46 % |
| 200 M, one variant | 88 % (median 30 s, p95 172 s) | 56 % (median 151 s, p95 195 s) |
| 10 M, default portfolio (8 variants, each with the whole budget) | 88 % (median 143 s on one thread) | 48 % (median 135 s) |

`srj19` stops improving between 50 M and 200 M; `srj20` is still rising. The default portfolio at 10 M does as
well as one variant at 50 M for more total work.

**Escape options**, 10 M, one variant, first 50 problems:

| Option | srj19 fully routed (mean completion) | srj20 fully routed (mean completion) |
|---|--:|--:|
| none | 66 % (92.4 %) | 34 % (82.4 %) |
| `--escape-plan` | 60 % (93.0 %) | 32 % (81.4 %) |
| `--escape-flow` | 60 % (92.2 %) | 28 % (79.1 %) |
| `--escape-plan --via-in-pad` | 60 % (93.0 %) | 32 % (81.4 %) |
| `--via-in-pad` | byte-identical to none, 50/50 boards | byte-identical, 50/50 |
| `--blind-vias`, `--micro-vias` | byte-identical to none, 50/50 | byte-identical, 50/50 |

None helps. The escape plans route a few more connections on `srj19` but finish fewer boards on both sets. The
problems allow neither blind nor micro vias, so those two options correctly do nothing. `--via-in-pad` (which the
problems do not allow either: `allowViaInPad` is absent) changes no board, also not at 0.15 mm clearance where
`tracemaker escape` reports dead balls; why it finds no pad to use was not investigated. The router was not
tuned in this work.

**Stricter variant**: the importer's defaults (`bench/prepare_srj.py --clearance-mm default --hole-to-hole-mm
default`: 0.15 mm clearance, no hole-to-hole rule), 10 M, one variant, all 200 boards.

| Set | Mean completion | Fully routed = clean pass | Relaxed pass |
|---|--:|--:|--:|
| srj19 | 81.8 % | 14.5 % | 10.0 % |
| srj20 | 78.2 % | 29.0 % | 29.0 % |

At 0.15 mm a 0.15 mm trace does not fit between two balls of `srj19` (0.44 mm gap), so inner balls can leave
only through vias; `srj20`'s 0.12 mm traces still fit. The 9 `srj19` boards that are clean in KiCad and fail the
relaxed check have vias of one net closer than 0.1 mm hole to hole, which KiCad does not test without a
hole-to-hole rule: the reason the benchmark sets carry that rule.

**Limits of the comparison.** The judges differ (KiCad's DRC against tscircuit's checker; here they agree to
within 1 % of boards). The rules are an interpretation where the files are silent (clearance, hole spacing, via
size for `srj20`; no board-edge clearance, which tscircuit's relaxed check does not apply either). The budgets are not matched to
tscircuit's effort setting. The problems are synthetic and two-layer only.

## 8. M14: srj19/srj20 against tscircuit's current run, and fanout31 (2026-10-11)

tscircuit's dashboard run `38105514608-1` (commit `65522c8`): srj19 92.0 % completed / 90.0 % relaxed pass, srj20 78.5 % / 78.5 %
(360 s per board, 32 vCPU; median 19–23 s). TraceMaker, one variant, 50 M work, `--fast-bends` and the new default limits (40 rips, 40
passes): **srj19 176/200 = 88.0 %, srj20 117/200 = 58.5 %** (section 7: 79.5 % and 50.0 %). With the default eight-variant portfolio on the
boards that miss: srj19 +10, srj20 +9 (about 93 % and 63 %). Per-board comparison with the dashboard run, the gap sets
(`srj19-gap`, `srj20-gap`, `srj19-p1fail`, `srj20-p1fail`) and every experiment: doc 16 §10. `bench/fanout31_run.py` scores the 72 fan-out cases
(solved = every connection routed, no unconnected item after the zone refill); results in doc 16 §10.1.

Update, same day (doc 16 §11): with preferred layer directions and the package penalty switched on automatically for breakout boards, one variant at 50 M
gives **srj19 181/200 (90.5 %)** and **srj20 124/200 (62 %)**; the 16-variant portfolio (50 M each, 16 threads, median 164–168 s) closes 7 more on
srj19 (**188/200 = 94.0 %**, tscircuit 92.0 %) and 9 more on srj20 (**133/200 = 66.5 %**, tscircuit 78.5 %). `fanout31`: 57 of the 60 cases run solved
(tscircuit 48/72); AM3352 is not solved (6–23 connections open at best; tscircuit solves none of its 12 cases either).

## 9. Better-board human-parity scorecard (D114)

### Running a comparison

The scorecard needs Shapely (`bench/requirements.txt`; the rest of `bench/` is stdlib-only) and `kicad-cli` on
`PATH`: `python3 -m venv .venv && .venv/bin/pip install -r bench/requirements.txt`. The default inspection
binary is `build/macos-metal/src/app/tracemaker`; `TM_BINARY` selects another build and `KICAD_CLI` selects
the judge. Supply one human run (`bench/run.py --set SET --human --refill --name human-SET`) and all arms
together so their connected intersection is frozen:

```sh
.venv/bin/python bench/quality.py nb-modern-200M-P nb-modern-200M-T nb-modern-200M-R \
  --reference human-modern-planes --intents build/intents/modern-planes \
  --json build/quality-modern.json --md build/quality-modern.md --jobs 6
```

`--intents` is the manifest directory the R arm routed with (`bench/run.py --intents DIR` drafts missing ones
from the inputs).

Run arguments are paths or IDs under `bench/results/`. Markdown prints to stdout; JSON defaults to the first
run's `quality.json` when `--json` is omitted. `--intents` binds frozen cap-to-pin obligations, never generates
evaluation labels from the route under evaluation. Without it decoupling is N/A, not perfect. Other categories
remain available. Maximum parallelism is six board workers; no routing is launched.

Every run uses the same human roster, project/custom rules, KiCad version, measurement code and tolerances.
Missing scheduled outputs remain failures of evaluation coverage. Source, analysis, project, custom-rule,
placement and manifest hashes are published per board. Placement/rule mismatches prevent eligibility.
PCBench's published inputs rename duplicate references relative to the raw human boards. Terminal matching
therefore uses position within 1 µm, copper-layer set and net spelling, **not reference designators**.
Duplicate human labels get physical identities; ambiguous/unmatched manifest bindings or pads are coverage
holes, never route losses. The physical placement hash omits renamed reference/pad labels.
Original artifacts are untouched. Existing `boards/<board>.refilled.kicad_pcb` is the analysis source when
present; otherwise a copy is refilled **and saved** with `pcb drc --refill-zones --save-board` in
`<run>/quality_cache/`. Sidecars come from the run or `build/modern-planes`, `build/demos`, then PCBench.
Inspection and refill caches are content-addressed (binary/version and rule inputs included).
Saved native analysis copies share the preparation helper's deterministic repair of zero/duplicate UUIDs.
If identities change, a second refill/save produces the authoritative report; repair counts and helper hash
are published. Legacy loader zero-zone-ID keepout artefacts are not routing defects. This repair never changes
the original board, terminal positions or routing geometry.
An older human directory missing a board may use the fixture's preserved `routed.kicad_pcb`; the actual source
path/hash is published. No router result is ever substituted for a human original. Preparation records marking
design copper non-comparable suppress resource, return and plane-aware-cap grades as N/A coverage.

### Gates, not grade components

1. **Electrical completion:** zero independently judged KiCad unconnected items. Completion uses one common
   refilled input's `1 - U_after/U_before`, never engine planned/routed counts. At 499 reported items either
   count is censored, so completion is N/A. This is an interim item-list proxy, not exact terminal-component
   rank; `tracemaker drc` cannot replace it until uncapped plane/net-tie consistency is established.
   On a capped output list, listed nets are known incomplete; absent nets have unknown connectivity.
   Unknown items are N/A coverage rather than service losses, except endpoint paths positively qualified
   by the track/same-fill model. A KiCad-connected path outside that model is also N/A, not unroutable.
2. **New DRC errors:** zero added error instances against the common input. Canonical type, severity and involved
   item kind/net/layer/location signatures prevent a resolved old defect cancelling a different new one.
   Reference labels are removed and UUIDs are fallback identities only when position is absent, so legacy
   conversion and published-reference renaming do not manufacture new defects.
   Unlike the old harness, new non-track errors are included; noisy solder-mask reports require review.
3. **Small-pad process eligibility:** no via centres inside, or via copper within 0.25 mm of, single-layer SMD
   pads with minimum dimension below 2 mm (EP candidates excluded). A board whose human uses small-pad
   via-in-pad is exempt; raw copper/hole clearance counts still publish. Unknown pad geometry is not pass.
   This is the declared conservative screening profile, not a claim about a filled/capped or tented process.
4. **Thermal preservation:** retain the human's count of same-net vias in nonzero exposed-pad arrays. Explicit
   EP labels and inferred central large GND pads are marked by confidence. This detects lost arrays, not thermal
   sign-off; plating, dissipation and receiving-plane conductance are not inferred.

The gate table shows evaluation coverage, individual passes, eligible boards and censored boards. No category
improvement compensates for a gate failure. A connected-net absence from a capped DRC list is not connectivity
proof: its conditional quality is excluded conservatively. The full per-board gate/coverage record is in JSON.

### Separate graded categories

| Category | Measurements | Interpretation / limits |
|---|---|---|
| Decoupling | Effective cap-to-pin mm, human parity within 1 mm | Port of the `align.py` free-plane-hop lower bound, but contacts must reach the **same actual refilled island**, not a zone outline or arbitrary via; track/plane alternatives retained |
| Differential pairs | Fraction coupled at human modal gap, copper skew, via-count imbalance | Named high-speed candidates with two connected terminals per half; human gap ±max(0.025 mm,20%); not impedance/timing certification |
| Return paths | Adjacent broad-GND-plane uncovered fraction, reference-changing vias lacking contacting return via within 1 / 5.08 mm | Fresh fills only; unknown-reference mm/transitions publish; floating-island grounding and intentional Ethernet isolation need review |
| Power | Length-weighted median width, 35 µm series-track resistance, length fraction below human median width; class neck mm in JSON | Exact arc lengths; supply-name candidates; not loaded resistance/current capacity: plane bypasses and parallel paths omitted |
| Thermal | EP-array via counts and count retention | Frozen nonzero human candidates; repeated pad primitives/vias merged; candidate confidence retained |
| Pad entry | Off-axis (>15°), corner (15% of small dimension), wider-than-face flags per contacted pad | Shape-aware rectangle/roundrect heuristic; not an IPC violation; custom/unsupported geometry and omitted arcs publish |
| Routing craft | Acute exposed centreline junctions per net | Outward-ray angle below 90°, excluding pad/via-swallowed joints; ordinary 45° bends are not acute defects |
| Efficiency | Unique vias and exact segment/arc mm per fixed pad-ID spanning-rank proxy | Same connected nets; no router-dependent denominator; extra return/thermal vias or intentional tuning are not automatically waste |

Differential coupling measures straight parallel spans and concentric circular arcs directly (D124).
Three KiCad arc points recover centre, radius and signed sweep; angular interval intersections become
exact arc-length intervals, including reversed sweeps, wraparound and partial overlap. Arc centres may
differ by at most 10 nm to absorb integer-point fitting noise; the gap tolerance remains the declared
human-gap tolerance above. Independently tessellated concentric arcs no longer fail a straight-segment
direction test. Crossing straight lines and shifted-centre crossing arcs do not couple. Mixed line/arc
and nonconcentric-arc spans are not inferred as coupled: this is a geometric parallel-run proxy, not
electromagnetic field analysis. The return-plane metric retains its existing ≤1 µm arc chord tessellation;
its geometry and tolerances do not change.

Every metric has its own human-parity table; there is **no weighted total**. Better-than-human values receive
wins rather than being clipped at parity. Other practical equivalence bands are published in JSON:
0.01 mm skew/length, 0.001 coupling/gap/neck fraction, 0.000001 mm width or ohm resistance; integer counts
use exact equality. These are geometric comparison tolerances, not universal electrical requirements.

### Aggregation, missing obligations and noise

The headline conditional view uses the intersection of human-qualified, connected, assessable items in **every**
compared arm. Each metric reports its intersection size, macro per-board parity, pooled parity, wins/ties/losses,
worst obligations and not-assessable count. The adjacent all-obligation service view counts missing router paths
as losses; human-invalid bindings/paths are symmetric coverage holes. N/A categories never become 100%.
Whole-board geometry and manufacturing counts remain in JSON even when conditional quality cannot be graded.

For every arm pair, resample paired boards (SHA-256 counter RNG seed 107; 2,000 replicates) for a 95% bootstrap interval of macro-parity
differences, with board win/tie/loss counts. Curated-set intervals measure board sensitivity, not population
generalisation or routing-seed variance. Compare budgets only on a frozen cross-budget cohort, and publish
work-stop/runtime diagnostics: budget censoring is not proof of convergence.

The noise panel compares the human reference against itself and compares original human fills with identically
refilled-and-saved human fills. It publishes each metric's self-disagreement and changed/assessable items plus
maximum/mean absolute fill-dependent change. Original fills are **only a noise control**, never graded copper.
Deterministic identity is not an independent refill-repeatability test or validation of systematic model bias.
Known fill-topology/graph limitations and coverage holes must accompany results rather than becoming a defect
allowance. The prototype reviewers' outline-based decoupling results are intentionally not reproduced where
fresh fill/island contact disagrees; their craft thresholds and SI coupling definitions are preserved.

Historical board-level consumers use `bench/board_quality.py` (the former `quality.py` interface); the new
`quality.py` accepts run directories only. Measurement regressions run with
`python3 -m unittest discover -s bench -p 'test_*.py'` in the Shapely environment.

### Frozen interfaces and opt-in guide rules (D130, D134)

`bench/interfaces.py --set modern-planes --out build/interfaces/modern-planes` drafts schema-1
`<board>.interfaces.json` files from the **prepared inputs**, not router outputs. `quick` and `mid` use
`build/pcbench`; `--board NAME` restricts a set, `--fixtures DIR` selects another prepared input root,
and `--input INPUT.kicad_pcb --board NAME` explicitly selects an input/human board.
Hierarchy and token boundaries are retained; P/N, +/−, DP/DN, DP/DM, `.P/.N` and lowercase variants
are recognized. Ancillary USB-PD/HDMI lines, NCSI, analogue ID/QD pairs, SPI SCLK and generic LVDS
expansion names are not misclassified as the guide's data interfaces.

Names produce **candidate** obligations. Only independent native connector/IC pad `pinfunction`
evidence corroborating every lane/leg upgrades a draft to **confirmed**; a footprint outline,
filename or capacitor alone cannot. `--review` rechecks each obligation against physical input pad
metadata, protocol-specific IC functions and standardized connector contacts. It writes
`status: reviewed`, an evidence-bearing `review`, and `review_decision: confirmed` or
`rejected_for_grading`; rejected candidates remain visible, never silently vanish. Generic FPGA
SERDES, GPIO and FFC contacts cannot prove a protocol. A reviewed identity is not firmware or
operating-speed certification. Unknown modes remain null; only explicit connector/device topology
establishes the relevant slot/device-down mode.
Missing native pin functions can be supplemented by narrow manufacturer pin tables for INA226
DGS/MSOP-10 (TI SBOS547C), 24LC/AA/FC512 8-pin/DFN (Microchip DS21754M) and DP83848C LQFP-48
(TI SNLS266E). Their exact pin/package matches and source URLs appear in the review, rather than
promoting every pin on an I²C-capable chip. Unsupported packages and unverified pin maps stay candidate.

Integer-nm endpoints retain net spelling and copper-layer sets, independent of reference renaming.
Recognizable two-pad passive bridges join consistently named lane halves (or an anonymous half);
their physical endpoints remain in `series`. The drafter removes internal series pads and selects
exactly two outer component terminals per leg when the remaining pads are independently identified
ESD/TVS, rail-shunt/pull-up, test-point or electrically equivalent duplicate connector contacts.
Those branch contacts remain as optional `taps`, with an `endpoint_selection` explanation. Unknown
parts, multidrop buses and multiple unequal destination contacts retain all possible terminals and
remain ambiguous, not a farthest/shortest-path guess. A single explicit GND pour domain plus GND
contacts on every selected terminal component can establish reference-domain intent, not a
stackup certification. Integrated magnetics' internal cable-side nodes are not PCB nets and do not
establish an on-board isolated domain; unsupported reference/isolation declarations remain empty.

Add `--interfaces DIR --profile toradex-2015` to `bench/quality.py` to publish absolute guide-rule
results alongside unchanged human-parity grades. Without `--profile` no rule/coverage section or new
fields are emitted. The lazily imported `RULE_MODULES` registry supplies `measure_rules` implementations;
each receives the same frozen manifest/profile, fresh fills, connectivity states and analysis board.
Duplicate rule/application keys are rejected. The human is measured too; human violations remain visible.
Per-board provenance hashes the interface manifest. Coverage lists interface confidence and physically
unbound lanes; a missing manifest or unknown geometry/role/mode is N/A, never a pass.

The bundled profile transcribes section-7 geometric limits plus the general 4W same-net meander
spacing, 1.5W bend-segment and no-between-leg-item rules, plus a report-only no-reference-gap screen.
The gap screen requires declared reference domains; zero uncovered length is boundary under the shared
1 µm band, not an electrically certified pass. Clock/spread skew groups, TX/RX budgets and
slot/device-down modes stay distinct. Length/skew in millimetres are planar proxies, **not timing or
impedance certification**. Approximate lengths, conditional exceptions and other-signal gaps are
report-only. USB3's printed “minimum <500 µm” comparator is preserved as report-only, not silently
reversed. LVDS pair spacing is relative to intra-pair gap, not trace width. Cable-inclusive paths,
VGA filter boundaries and I²C capacitance-limited lengths require reviewed metadata. Impedance is
omitted until real stackup data and a validated solver exist. Numeric checks use the common 1 µm
boundary band; integer counts use an exact zero band. These old carrier-board recommendations are
explicitly opt-in context, not new hard gates or current interface-standard compliance.

An active profile always publishes interface coverage, including `status: missing` when no manifest
is supplied or a scheduled board's manifest is absent. Markdown also treats older arms without that
coverage field as missing, rather than failing report generation.

#### Reviewed-input smoke evidence (D135)

The `sc/integrate` 1936fe0 path/return module registry was exercised against the 11 default non-slow
`modern-planes` boards: `dp-modern-200M-T` versus `human-modern-planes`, `--profile toradex-2015
--jobs 2`. Both runs had an empty benchmark error map. Draft inputs took 89.1 s; reviewed inputs
took 90.2 s. Manifest hashes matched across both arms and the regenerated review files (22 bindings).

| Arm | Draft pass / fail / N/A | Reviewed pass / fail / N/A | N/A fraction, draft → reviewed |
|---|---:|---:|---:|
| Human | 74 / 25 / 392 | 145 / 40 / 322 | 79.84% → 63.51% |
| Router | 66 / 24 / 401 | 136 / 46 / 325 | 81.67% → 64.10% |

Reviewed topology adds 16 slot length/RX-via obligations, so denominators are 491 and 507 rather
than a silently narrowed cohort. Combined identity-related N/A falls from 440 to 90 and the
two-endpoint reason from 132 to 118. Unknown-reference N/A rises from 171 to 300 as more confirmed
paths become evaluable; no reference domain was invented to make these checks pass. Native
MAX96724 CSI and SN65DSI84 DSI/LVDS pin evidence exposes human as well as router skew violations.
All 57 prepared boards were reviewed: modern-planes 74 confirmed / 36 candidate interfaces,
quick 2 / 2 and mid 6 / 11. Genuine multidrop/ambiguous paths and generic FPGA/FFC mappings remain
N/A; the registered modules, not this drafter, determine which profile measures are implemented.

### Frozen endpoint and segment checks (D131)

`bench/quality_paths.py` measures the required electrical path between physical endpoints from a schema-1
interface manifest, including declared series parts joining split nets. Each differential lane supplies two
outer endpoints per leg; series-pad bindings may also appear in the lane's endpoint list when the
series entry supplies its own physical `endpoints` to exclude. P/N correspondence is resolved jointly
from their physical bindings: prefer endpoints on the same footprint, then nearest endpoint pairing;
equal evidence is ambiguous and yields N/A. The same correspondence is used for manifest traversal
and inferred parity endpoints. The manifest is frozen from the common input/human, never fitted to a
router arm. Extra ESD/test-point pads do not disqualify a unique path. Without explicit intent, the new
parity fields use only the human's physically unique two-pad candidate endpoints, reused unchanged
across arms. Pad dimensions and file order never choose opposite traversal directions.

The graph splits straight tracks and exact circular arcs at conductive junctions. Physical duplicate copper
and vias are merged; sub-nanometre intersection coordinates are snapped to integer nanometres. Pads are
equipotential contacts and series parts contribute zero planar length. Nonterminal leaves are peeled and
their copper length is reported separately as `stub_mm`; a stub's vias do not consume the endpoint-path
via budget. Disconnected endpoints, ambiguous bindings and connected loops produce N/A with reasons, not
an electrically convenient shortest-path result. Plane transport is not represented. Via barrels, component
internal lengths, package delays and unknown external cables are not inferred from the planar result.

| Measure | Definition |
|---|---|
| `intra_skew_mm` | Absolute difference between P and N endpoint-path lengths |
| `segment_skew_mm` | Worst absolute difference between the P/N lengths of successive sections bounded by matching ordered via, layer-changing-pad or series transitions; +1 mm before a via and −1 mm afterward is 1 mm, not zero |
| `group_skew_mm` | Per-lane mean of P/N lengths (`single` data lanes use their one path length), compared with the one `clock` lane or the spread over declared data/single/TX/RX lanes, selected by the profile's `group` |
| `max_length_mm` | Longest individual leg's endpoint-path planar length |
| `vias_per_leg` | Unique vias actually traversed on each leg, not total same-net vias; profile `role` and `mode` select budgets |
| `between_legs_items` | Unique unrelated tracks/arcs, pads, vias and same-layer filled islands intruding into the edge-to-edge corridor along parallel straight path sections; declared series pads and both legs' nets are excluded |

`segment_skew_mm` and `via_layer_mismatch` are additional differential-pair human-parity fields, with
0.01 mm and exact-count equivalence bands respectively. `via_layer_mismatch` counts ordered via transitions
with differing drilled layer spans or used entry/exit layers, plus unmatched transitions. Segment skew is
N/A when transition sequences cannot be matched. The historical whole-net `skew_mm` and via-imbalance
metrics retain their original meanings and values.

The absolute millimetre checks use the shared 1 µm boundary band; count checks have band zero. Human
violations remain visible and guide-derived limits are not new hard gates or current protocol certification.
Unknown interface identity, lane role, group membership or geometry is never a pass. Supported pad shapes
(rectangle, round rectangle, oval and circle) use the frozen `pad_metadata` where supplied, including
canonical renamed references; the board-reader fallback runs only when that map is absent.
Local-to-board pad rotation follows KiCad's y-down negative-angle convention. Without known shapes,
only inscribed-disk contact is represented, with potential corridor intrusions marked N/A.
Curved pair-corridor correspondence is currently N/A rather than a chord-based claim; foreign arcs beside
straight coupled sections use exact circular distance and copper width. All geometry and sums are ordered
by physical identity so item reordering cannot change a measurement or modal-gap tie.

Regression cases are in `bench/test_quality_paths.py`, including ±1 µm limits, skew cancellation, ESD
branches, series coupling, loops, via spans, foreign copper, fill-layer controls and shuffled copper order.

### Reference identity, isolated domains and decoupling return leads (D132)

`quality_returns` preserves each actual refilled island as `(net, layer, island)` with canonical
physical ordering. The original broad-ground `gap_fraction` and `no_return_*` measurements retain
their historical semantics when no interface manifest is supplied. Two additional lower-is-better
parity fields, `wrong_reference_mm` and `floating_reference_mm` (0.01 mm equivalence band), distinguish
route centreline over the wrong ground net and over an unanchored intended-reference island.
An island is anchored only if same-net pad copper reaches it directly, or through the existing
track/via contact graph; a floating island cannot give reference credit. A pad anchor is not proof
of earth grounding. Graph-unsupported connectivity is conservative coverage, not circuit sign-off.

Intended references come first from frozen interface `reference_nets`. Without that declaration,
the common human's fixed terminal geometry supplies a conservative inference: on every endpoint
footprint whose designator starts with `U`, choose the nearest GND/GNDD/DGND/AGND pin by squared
centre distance. All endpoint ICs must agree on one net; ties across ground nets, missing ground
pins and connector/passive-only evidence are N/A. This inference is frozen before evaluating
router arms, not rediscovered from routed copper. Direct `measure` callers pass that frozen map
as `intended_references`; missing maps are N/A. Explicitly allowed reference nets may be power
nets; their actual adjacent fills qualify for the new proxy, without changing historical GND
eligibility. This is an adjacent-layer geometric screen, not a dielectric/stackup-selected field model.

Confirmed manifest lanes with role `clock` or `single` also receive return measurements; without
a manifest the historical pair-only obligation set is unchanged. Declared `isolated_nets` are
excluded from missing-ground penalties and return-via credit. A signal whose entire declared
reference belongs to that isolated domain is likewise excluded. The Ethernet MDI profile's
`isolation_gap_mm` is the minimum projected copper edge distance across **all** layers between
isolated and other-domain fills, traces/arcs, vias and pads. Missing/unsupported pad shape metadata,
missing domain copper or unconfirmed interface intent is N/A. This projected 2-D distance does not
certify safety insulation, creepage or isolation voltage.

`quality_returns.measure_rules(d, interfaces, profile, ctx)` checks `reference_uncovered_mm` and
`isolation_gap_mm` with the shared 1 µm uncertainty band. By default reference coverage measures
**all lane-net track/arc copper**, including stubs, not an endpoint-only path. The optional
`ctx['lane_segments']` supplies per-net, ordered endpoint-path segment dicts in millimetres to
replace that source; a missing path in a supplied map is N/A, never an all-copper fallback.
The context also accepts `broad` reference layers and `pad_metadata` from the frozen shape reader.
Human reference failures remain visible alongside router failures.

Each frozen cap-to-supply-pin obligation also publishes `return_lead_mm` (lower better, 1 mm
equivalence band). Select the capacitor's unique ground pad and the nearest IC ground pin to
the obligated supply pin on their common, unambiguous ground net. The same track-or-free-plane-hop
lower bound then measures the ground-side lead; free travel requires contacts reaching the
**same grounded refilled island**. A missing return obligation/loss requires both no represented
ground path and a KiCad unconnected-item endpoint physically matching the cap ground pad or IC ground
pin on that ground net. Matching uses report positions within the rotated pad extent plus the 1 µm
identity tolerance, not reference names, because router fixtures may rename references.
An incomplete ground net elsewhere does not prove this obligation lost: unrepresented paths remain
N/A. A represented ground path retains its numeric lead despite unrelated breaks on the net.
Censored judgments (499 or more unconnected items) and missing/ambiguous ground bindings are N/A.
Aggregation uses the separate `return_connected` state only for `return_lead_mm`; the old
`effective_mm` supply metric and its supply connectivity are unchanged. Neither side is a loop-inductance estimate.

### Guide spacing and bend screens (D133)

`quality_spacing.py` adds two report fields on each known-connected signal net:
`self_spacing_violations` counts close-approach sites below 4W (Toradex 2015 §6.2 p17, R36), and
`short_bend_segments` counts straight bend segments below 1.5W (R37). They are published per net in the JSON
but are **not graded against the human**: both counts grow with the amount of length tuning, so a route that
never meanders would "win" while losing on skew (measured: human CSI lanes on gmsl-deserializer carry 80–140
sites each from tuning humps, the untuned router 225 in total). The guide's actual requirement is applied as
absolute rules on manifest lanes (`self_spacing_ratio >= 4`, `bend_segment_ratio >= 1.5`), judged identically
on the human and router. Supply/ground exclusions reuse `quality_craft.is_power`/`is_ground`. Historical
acute-junction values and thresholds are unchanged.

A **run** is a maximal equal-width straight centreline formed by coalescing degree-two collinear pieces,
or one circular arc with analytic endpoint tangents and exact circular path length.
A **non-local site** is an unordered same-layer run pair with no direct copper contact, common endpoint, or endpoint contacts to
the same pad/via, and no endpoint-to-endpoint centreline path at most `2*max(Wa,Wb)`. Its gap is minimum
centreline distance minus the two half-widths, clamped at zero; its ratio uses `max(Wa,Wb)`. This operational
neighbourhood removes ordinary connected corners, rather than applying 4W at a joint where separation
necessarily vanishes. One violating run pair counts once, independently of collinear file fragmentation.
The minimum ratio is N/A when no eligible site exists, not an artificial pass. Bend segments must have real
direction changes at both ends, degree-two joints, and endpoints outside pad/via copper; straight width
transitions and endpoints are not bends. Arc tessellation vertices are never bend segments; tangent continuity
uses a 1 µrad uncertainty tolerance for integer-rounded arc control points. Junction contact exclusions use
the existing rotated pad-envelope model, not custom-pad electrical certification. Invalid arcs/widths are N/A.

For frozen, reviewed interface lanes, `measure_rules(d, interfaces, profile, ctx)` supplies
`self_spacing_ratio`, `bend_segment_ratio`, `pair_gap_mm`, `pair_gap_ratio`, and `other_gap_mm`.
Wildcard profile interface rules apply to all declared lanes. Pair distance is the minimum same-layer
copper-edge distance to **other declared lanes**, across interface groups. Tracks, circular arcs, vias, supported
pads and supplied refilled signal fills participate. `other_gap_mm` reports distance to every other non-ground,
non-supply signal net; role-specific high/low-speed distances cannot be inferred from this scalar.

An optional interface `breakout_radius_nm` freezes endpoint-centred breakout disks (default zero: no exception).
Only pad-to-pad comparisons on a shared endpoint footprint, whose pad centres fall within their respective
lanes' declared disks, are excluded. Tracks/vias are never exempt. `excluded_pad_comparisons` publishes the
number of excluded same-layer comparisons, independently of spatial-index traversal. Do not choose these
disks from the evaluated router output. The LVDS ratio divides the minimum foreign-lane gap by the own-leg
gap **at that closest site**: project the closest own-copper point onto its track centreline, then measure to
the closest same-layer mate track, subtracting half-widths. A closest pad/via/fill/arc site, missing mate, or zero
own gap makes the ratio N/A. This local geometric screen is not an impedance measurement.

Integer-nanometre identities, canonical run/copper ordering and exact segment/polygon distances give
item-order-independent results. Round copper is polygonized with at least 128 chords per quadrant and at
most 0.1 µm sagitta, below the 1 µm geometric rule band. Ratio bands convert 1 µm by the minimum represented
lane width (self/bend) or local own-leg gap (pair); an explicit profile band overrides this. Unknown connectivity,
unreviewed/candidate intent, missing neighbour geometry, unsupported pads or invalid arcs yield N/A, never pass.
STRtrees prune distance candidates; `indexed=False` exposes linear reference paths tested for equivalence.
No field solver, crosstalk sign-off, optional close-parallel-length metric or new hard gate is introduced.

