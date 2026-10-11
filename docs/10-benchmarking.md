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

The routing sets use `bench/prepare_inputs.py` (D106). It removes tracks/arcs and every via whose copper
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
the D106 routing sets.

## 2. Metrics per board

| Metric | Definition |
|---|---|
| **Clean pass** | 100% connections routed **and** zero added KiCad DRC errors (input vs output, `kicad-cli pcb drc`) |
| Completion | routed connections / routable connections (excluding pins proved dead) |
| Added DRC errors | by KiCad violation type |
| Vias, wirelength, bends | totals; also normalised to the reference routing where it exists |
| Wall time, work units | engine-reported, with hardware recorded |
| Placement (P&R set) | HPWL, lower-bound gap, crossings, courtyard overlaps (must be 0), moved parts |
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
| `--set NAME` | boards from `bench/sets/NAME.txt`; default fixture directories are `build/pcbench` for quick/mid and `build/modern-planes` |
| `--full` | also route the set's `#slow` boards (1,000 or more connections); everyday runs leave them out, the run before a commit includes them |
| `--work N` | deterministic work budget per board, no knowledge base; `--time` becomes a safety stop (3,600 s) |
| `--refill` | refill **and save** private analysis copies with `kicad-cli pcb drc --refill-zones --save-board`; reuse that same DRC report for judging, publish `boards/<name>.refilled.kicad_pcb` and its rules; raw output stays unchanged |
| `--human` | copy `<fixtures>/<name>/routed.kicad_pcb` instead of routing, same input/output judge/refill path, rows/summary marked `human: true`; no router binary needed |
| `--route-args=...` | extra `tracemaker route` options (also `TM_ROUTE_ARGS`) |
| `--fixtures DIR` | boards from `DIR/<name>/unrouted.kicad_pcb` instead of PCBench; a `.kicad_pro`/`.kicad_dru` beside it is copied next to the output so KiCad judges with the board's rules |
| `--tier`, `--limit`, `--seed` | sample PCBench boards by Freerouting tier instead of a set |
| `--boards NAME ...` | explicit subset, overriding the set roster for smoke/paired-cohort runs; the set name is still recorded |

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

**D106 smoke evidence (100 k work, not a full-tier rebaseline).** Two boards per set were routed with the new
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

`modern-planes` replaced `gndplane` (D105), whose boards were 13 KiCad 5 files without project rules. Every board is
pinned to a commit in `bench/sets/modern-planes.json` (repository, commit, board path, licence: Apache-2.0,
CERN-OHL-S/P, 0BSD, CC-BY-4.0). `bench/prepare_modern.py` fetches only the KiCad files and licence texts of each
commit into `bench/data/modern/` (≈ 220 MB) and writes `build/modern-planes/<name>/unrouted.kicad_pcb` with tracks/arcs
and track-contact vias removed, fixed vias/zones retained and fresh unrouted fills, plus project/custom rules
and the byte-original human as `routed.kicad_pcb`. All 16 originals have 0 unconnected items in prior KiCad 10.0.3 DRC with refilled
zones; their existing DRC errors (courtyard overlaps, starved thermals, Cynthion's 4 shorting items between net-tie
pads) are not routing errors. Sizes: six small (25-264 connections), five medium (355-967), five large (1,334-1,934).
KiCad 10 boards name nets in pad and zone references (`(net "GND")`); the reader handles both forms.

At 3 M work the large boards are barely half routed (router 63 % of 9,925 connections), so the set runs at 20 M
like `mid`. KiCad's DRC report lists at most 499 unconnected items, so the judged completion of the six boards with
more is meaningless (0 % for Cynthion, Reform, Glasgow even when the router connected 1,000+); read the route JSON's
`routed`/`connections` for them. The same cap affects two `mid` boards (Aleste, decelerator4030).

**Comparability cutover (D106).** Do not directly compare pre-D106 quick/mid results (including `bw-quick100-*`
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
binary from the base commit (a clean `origin/main` worktree) and run both on the same machine.

**Other tools.**

| Tool | Purpose | Status |
|---|---|---|
| `speed_ab.py` | engine speed A/B at a fixed work budget: wall and CPU time, instructions, memory, byte-identical outputs (`--demos` for KiCad demos) | maintained |
| `scripts/drc_parity.py`, `drc_broken_parity.py` | `tracemaker drc` against KiCad on demo and broken boards (ctest `kicad_drc_parity`, `kicad_drc_broken_parity`); counts are capped at KiCad's 199 per type | maintained |
| `quality.py`, `board_quality.py`, `quality_bench.py`, `compare.py`, `human_baseline.py` | gated run-directory human-parity scorecard (§7); board-level geometry metrics; TraceMaker against Freerouting versions | maintained |
| `check_env.py`, `prepare_dac2020.py`, `prepare_demos.py`, `make_place_sets.py` | tool checks and fixture preparation | maintained |
| `pair_eval.py`, `feasibility.py`, `crules_detect.py`, `crules_place.py`, `crules_tiers.py` | feature evaluations (diff pairs, escape feasibility, component rules) | kept for re-measuring their features |
| `m6_bench.py`, `place_m8.py`, `place_auto.py`, `place_flip.py`, `place_intent.py`, `place_variants.py`, `compare_placed.py`, `rescore_placed.py`, `dsn_place.py` | milestone evaluations of the global router and placement | historical; results are in the docs that cite them |
| `ses_import.py`, `record_fr.py` | import a Freerouting session; screen-record Freerouting | utilities |
| `cpu_sample.py` | per-thread CPU sampling from `/proc` | Linux only |

**Not built** (planned in the first version of this document): manifest files, a `standard` (~300 boards) and
`full` tier as named sets, `bench/report.py` with an HTML dashboard, and a sign test. The human-parity scorecard
provides paired-board bootstrap intervals (§7). Seeds: best-of-1 is the headline; best-of-5 is reported separately
where a comparison needs it (PCBWorld reports baselines as best of 5).

**Judge noise.** KiCad's `solder_mask_bridge` count can differ between runs on byte-identical boards when zones
are refilled (StickHub: 17, 15 and 13); read a change in that type alone as noise.

## 5. Gates (used by the roadmap)

1. No board in `quick` gets worse in clean pass or completion (`compare_runs.py`).
2. Zero added KiCad DRC errors on every board that was clean before (`compare_runs.py`).
3. Time within budget on every board (with `--work`, the budget is work units; wall time only stops the job).
4. Routed boards byte-identical unless the change is meant to change results (`compare_runs.py
   --expect-identical`; then the differences are recorded).

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

## 7. Better-board human-parity scorecard (D107)

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

