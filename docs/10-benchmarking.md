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

### Building unrouted fixtures

`bench/prepare_dac2020.py` `strip_routing` removes top-level `segment`/`arc`/`via` items and teardrop zones
(KiCad generates those from the tracks; left behind they are stray copper on the pads, and on the
complex_hierarchy demo they cost 74 of 87 connections), keeping everything else, zones included, byte for
byte. For the place-and-route set unlocked footprints are also reset to a pile outside the outline (what
KiCad's "Update PCB from Schematic" produces).

## 2. Metrics per board

| Metric | Definition |
|---|---|
| **Clean pass** | 100% connections routed **and** zero added KiCad DRC errors (input vs output, `kicad-cli pcb drc --refill-zones`: saved fills predate the routing, D63) |
| Completion | routed connections / routable connections (excluding pins proved dead) |
| Added DRC errors | by KiCad violation type |
| Vias, wirelength, bends | totals; also normalised to the reference routing where it exists |
| Wall time, work units | engine-reported, with hardware recorded |
| Placement (P&R set) | HPWL, lower-bound gap, crossings, courtyard overlaps (must be 0), moved parts |
| Determinism | output hash per board at 1 and N threads, GPU on/off |

### Routing quality score

**Before:** totals alone reward unfinished routing: less copper, fewer vias and undamaged planes.
Hand routing is useful context, not an optimum or a guaranteed legal board.

**What was built:** `bench/quality.py` supplies refilled metric records; `bench/score.py` scores them without
running KiCad. Compare the lexicographic key `(added_errors > 0, added_errors, unconnected)` first (lower
wins). Any added routing-relevant DRC error fails legality; among failures fewer errors wins. Completion
comes next, so smaller geometry never compensates for a missing connection. Only equal tiers use quality.
Let `C = connections - unconnected`, `positive(x) = max(0, x)`, and
`P = sum(w * term / scale) / sum(available w)`. Quality is `Q = 100 / (1 + P)` (higher is better).
Signal, plane and DFM sub-scores use the same formula on their own terms.

| Term | Weight | Scale | Evidence / rationale |
|---|--:|--:|---|
| Signal length / pad-centre MST, on complete non-plane nets open in input | 25 | 1 | [ISPD 2018/19](https://ispd.cc/contests/19/metrics_and_ranking.pdf), [PCBench baseline metrics](https://raw.githubusercontent.com/PCBench/PCBench/main/Baselines/Results_all.csv): physical length cost; never include unfinished-net MST or plane paths |
| Vias / C | 10 | 1 | Same sources: transition/fabrication cost per delivered connection |
| Positive increase in over-gap length / plane-adjacent length | 20 | 0.1 | [TI SLLA414A §3.5](https://www.ti.com/document-viewer/lit/html/SLLA414A/GUID-8285BF1D-20A9-413F-8564-77FF08AAF484): return-path continuity |
| Sum of positive plane coverage losses / sum of input coverage | 15 | 0.1 | [Plane design guidance](https://resources.altium.com/p/ground-plane-design-and-arrangement-high-performance-pcbs): preserve the input's own reference copper |
| Positive added fill islands / C | 10 | 0.1 | Same guidance: fragmentation beyond existing geometry |
| Positive added plane crossings by vias of another net / C | 5 | 1 | Coarse perforation-pressure proxy: each is an antipad; same-net stitching vias are not counted |
| Small-SMD-pad vias / C | 10 | 0.1 | [IPC-4761 protection classes](https://www.electronics.org/TOC/IPC-4761.pdf), [solder wicking](https://www.eurocircuits.com/technical-guidelines/pcb-assembly-guidelines/solder-escape-wick/): assembly-risk/cost proxy, not proof that a protected via-in-pad is illegal |
| (Sharp bends + sharp pad junctions) / C | 2 | 1 | [Acute-angle manufacturing check](https://www.altium.com/documentation/cstu/acute-angle): weak DFM proxy, not every right-angle corner is an acid trap |
| Narrowed length / total track length | 2 | 0.1 | Length-normalised bottleneck/current/impedance proxy; allowed neckdowns are not added DRC errors |
| 1 - differential-pair coupled share, where measured | 1 | 0.1 | [High-speed design checks](https://www.altium.com/documentation/altium-designer/pcb/high-speed-design): diagnostic, not impedance/skew sign-off |

Weights/scales are **TraceMaker policy**, not IPC thresholds or transplanted VLSI contest coefficients.
All plane deltas use the record's own refilled stripped-input `planes_input`; match by `(net, layer)`,
with missing output planes treated as lost coverage. Existing plane damage earns no negative penalty.
Duplicate plane keys use mean coverage and summed islands because the records lack polygon identities.
An input with zero adjacent-track exposure and zero gap defines baseline gap share zero.

No completed connection means quality is unavailable. Missing evidence and zero denominators remain null,
never perfect; report `available_weight` and `missing_terms`. Scores with different measured-term sets are
not directly comparable. `compare()` uses mutually available terms and matched nets open in input and
complete in both outputs, excluding plane nets in either board, for length/via/detour ratios. Its matched
via denominator is `sum(pads - 1)`,
an explicit pad-tree connection proxy, not a KiCad missing-item count. Plane/DFM terms remain board-level.
Pad-centre MST is a topology proxy, not a geometric lower bound; Steiner sharing and pad edges can yield
detour below one. Rational arithmetic decides verdicts; `quality_milli` is half-up-rounded `1000 * Q`.

Hand originals use the identical function and can lose. `versus_hand()` reports each term ratio, with null
and an explicit status for zero hand denominators; similarity to the original is never rewarded.
`python3 bench/score.py RECORD.json [RECORD.json ...] --hand original --md` prints a grouped table;
each file may hold one record or a list. `markdown(records, hand_label="original")` supplies the same
report to callers and displays failed judges without fabricating metrics.

**Results** (`bench/planes_eval.py`, 10M work units, one variant, 1 thread; 8 KiCad demos with their own
rules and zones, and 9 PCBench quick-tier boards, whose fixtures have no zones, against `raw.kicad_pcb`).
Open = unconnected after refill; Q = quality (higher is better, diagnostic within a tier); matched = track
length on nets both boards completed, candidate / hand.

| Board | Hand: legal, Q | Best TraceMaker (open, Q) | Matched length | Verdict |
|---|---|---|--:|---|
| complex_hierarchy | yes, 39.6 | base 0 open, 50.3; soft 0, 48.7 | 0.92 / 0.80 | TraceMaker (plane cost 1.26 vs 2.34) |
| multichannel_mixer | **24 clearance errors** | base 11, 55.8 | 0.98 | TraceMaker (hand illegal) |
| RoyalBlue54L-Feather | **2 errors** | soft 87, 34.9; soft+vop 92, 48.7 | 0.92 | TraceMaker (hand illegal) |
| pic_programmer, StickHub, interf_u, CM5, ColdFire | yes, 31–44 | 10–155 open | 0.85–1.27 | hand (complete) |
| 9 PCBench boards | 7 legal; 2 with 9–10 errors | all 0 open, Q 52.7–64.8 | 0.68–1.00 | TraceMaker 8 (kika only with `--keep-vias-off-pads`), hand 1 (scimpy: no vias against TraceMaker's 0.10 per connection) |

Soft zones cut open connections on the multilayer boards (CM5 118 → 82, StickHub 31 → 14, ColdFire
415 → 155, RoyalBlue 112 → 87) but put 0.16–0.78 vias per delivered connection in small SMD pads (DFM cost
1.1–5.7); `--keep-vias-off-pads` removes nearly all of them (DFM 0.01–0.5) for at most 10 more open
connections. With the teardrop zones left in the demo inputs, complex_hierarchy routed 15–25 % under the
same judge: a fixture artefact, not a router limit.

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

- Python, driven by manifest files (`bench/manifests/*.yaml`: board list, time budget, tier).
- Tiers: `quick` (~30 boards, < 5 min total, every change), `standard` (~300 boards, nightly), `full` (all).
- Runs N boards in parallel (one engine process per board, GPU jobs shared through the engine's queue).
- Each run writes `results/<run-id>/<board>.json` and a summary; `bench/report.py` renders Markdown and an
  HTML dashboard (per-board deltas vs baseline, scatter of time vs completion, failure-cause histogram from
  the failure memory).
- **Paired comparison**: same boards, same budget, same machine; report wins/losses/ties per board, a
  sign test, and the bootstrap confidence interval of the clean-pass difference.
- **Seeds**: best-of-1 is the headline; best-of-5 is reported separately (PCBWorld reports baselines as best
  of 5, so compare like with like).

## 5. Gates (used by the roadmap)

1. No board in `quick` gets worse in clean pass or completion.
2. Zero added KiCad DRC errors on every board that was clean before.
3. Time within budget on every board.
4. Determinism hash unchanged unless the change is meant to change results (then recorded).

## 6. Implementation status (2026-10-02)

- `bench/run.py` samples PCBench boards per Freerouting tier, routes each with a binary snapshot, judges with
  `kicad-cli pcb drc`, and compares with Freerouting's published per-board results (`benchmarks.json`).
- Samples: tier A 40 of 453 boards, tier B 40 of 560, tier C 30 of 122, tier D all 22 (seed 1).
- Added errors count only violations involving a track, via or arc (decision A16); other new KiCad reports are
  kept as diagnostics.
- Results go to `bench/results/<run>/` and the progress site's benchmark panel.
- Latest (`final8`): tier A 100% clean, B 65.0%, C 56.7%, D 50.0% (Freerouting 2.5.0-RC12: 100%, 50.0%, 46.7%,
  36.4%); no router-introduced DRC errors on any board.
