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
complex_hierarchy demo they cost 74 of the input's 87 unconnected items (KiCad count)), keeping everything
else, zones included, byte for byte. For the place-and-route set unlocked footprints are also reset to a
pile outside the outline (what KiCad's "Update PCB from Schematic" produces).

## 2. Metrics per board

| Metric | Definition |
|---|---|
| **Clean pass** | 100% connections routed **and** zero added routing-relevant KiCad DRC errors (input vs output, `kicad-cli pcb drc --refill-zones`: saved fills predate the routing; filter below, D63) |
| Completion | routed connections / required connections; finite escape-domain exhaustion is not a physically unroutable exclusion |
| Added DRC errors | by KiCad violation type |
| Vias, wirelength, bends | totals; also normalised to the reference routing where it exists |
| Wall time, work units | engine-reported, with hardware recorded |
| Placement (P&R set) | HPWL, lower-bound gap, crossings, courtyard overlaps (must be 0), moved parts |
| Determinism | output hash per board at 1 and N threads, GPU on/off |

Escape diagnostics (`D66`) distinguish satisfied/witness/exhausted/unknown. The legacy `dead_pins`
benchmark field counts only finite-domain exhaustion; `escape_unknown` records work/unsupported cases.
The optional `clean_pass_feasible` split includes only boards with zero exhausted **and** zero unknown
pins under the recorded domain. Legacy analyses without statuses are unknown, not feasibility evidence.
Neither diagnostic changes the required clean-pass denominator or substitutes for refilled KiCad sign-off.

### Routing quality score

**Before:** totals alone reward unfinished routing: less copper, fewer vias and undamaged planes.
Hand routing is useful context, not an optimum or a guaranteed legal board.

**What was built:** `bench/quality.py` supplies refilled metric records; `bench/score.py` scores them without
running KiCad. Compare the lexicographic key `(added_errors > 0, added_errors, unconnected)` first (lower
wins). Any added routing-relevant DRC error fails legality; among failures fewer errors wins. Completion
comes next, so smaller geometry never compensates for a missing connection. Only equal tiers use quality.
Legality counts positive input/output deltas of error-severity `starved_thermal` and `isolated_copper`
regardless of reported items; `clearance` and `shorting_items` count when a Zone or routed copper is
involved. Other types retain the Track/Via/Arc item filter. `bench/run.py` uses the same definition for
quick-tier `clean`. The verified 30-board option-off quick tier is byte-identical to the previous
baseline and its clean pass is unchanged at 43.3%.
Let `C = connections - unconnected`, `positive(x) = max(0, x)`, and
`P = sum(w * term / scale) / sum(available w)`. Quality is `Q = 100 / (1 + P)` (higher is better).
Signal, plane and DFM sub-scores use the same formula on their own terms.

| Term | Weight | Scale | Evidence / rationale |
|---|--:|--:|---|
| `signal_detour`: signal track/arc length / pad-centre MST, on complete non-plane nets open in input with available positive MST | 25 | 1 | [ISPD 2018/19](https://ispd.cc/contests/19/metrics_and_ranking.pdf), [PCBench baseline metrics](https://raw.githubusercontent.com/PCBench/PCBench/main/Baselines/Results_all.csv): exclude unfinished-net MST, plane paths and nets with unavailable MST from both length and MST sums |
| `vias_per_connection`: vias / C | 10 | 1 | Same sources: transition/fabrication cost per delivered connection |
| `unsupported_share_added`: positive increase in unsupported exposure share (unsupported length / plane-adjacent length) | 20 | 0.1 | [TI SLLA414A §3.5](https://www.ti.com/document-viewer/lit/html/SLLA414A/GUID-8285BF1D-20A9-413F-8564-77FF08AAF484): return-path continuity; union of adjacent-plane fills, including exposure outside plane outlines |
| `coverage_lost`: sum of positive plane coverage losses / sum of input coverage | 15 | 0.1 | [Plane design guidance](https://resources.altium.com/p/ground-plane-design-and-arrangement-high-performance-pcbs): preserve the input's own reference copper |
| `islands_added_per_connection`: sum of positive added fill islands / C | 10 | 0.1 | Same guidance: fragmentation beyond existing geometry |
| `plane_crossings_per_connection`: positive added foreign-net via/plane-layer crossings / C | 5 | 1 | Coarse perforation-pressure proxy: copper plus zone clearance must intersect the plane outline; once per via/layer, not per overlapping zone |
| `small_pad_vias_per_connection`: small-SMD-pad vias / C | 10 | 0.1 | [IPC-4761 protection classes](https://www.electronics.org/TOC/IPC-4761.pdf), [solder wicking](https://www.eurocircuits.com/technical-guidelines/pcb-assembly-guidelines/solder-escape-wick/): outer-layer copper required; assembly-risk/cost proxy, not proof that a protected via-in-pad is illegal |
| `sharp_per_connection`: (sharp bends + sharp pad junctions) / C | 2 | 1 | [Acute-angle manufacturing check](https://www.altium.com/documentation/cstu/acute-angle): weak DFM proxy, not every right-angle corner is an acid trap |
| `narrowed_share`: narrowed track/arc length / total track/arc length | 2 | 0.1 | Length-normalised bottleneck/current/impedance proxy; allowed neckdowns are not added DRC errors |
| `pair_uncoupled_share`: 1 - differential-pair coupled share, where measured | 1 | 0.1 | [High-speed design checks](https://www.altium.com/documentation/altium-designer/pcb/high-speed-design): diagnostic, not impedance/skew sign-off |

Weights/scales are **TraceMaker policy**, not IPC thresholds or transplanted VLSI contest coefficients.
All plane deltas use the record's own refilled stripped-input `planes_input`; match by `(net, layer)`,
with missing output planes treated as lost coverage. Existing plane damage earns no negative penalty.
Duplicate plane keys use mean coverage and summed islands because the records lack polygon identities.
An input with zero plane-adjacent length and zero unsupported length defines baseline unsupported share zero.

Unsupported exposure samples signal tracks and arcs at no more than 0.1 mm spacing along their actual
centrelines. Each point inside the board outline on a plane-adjacent layer is supported iff any fill on
either adjacent plane layer contains it (union, any net); outside plane outlines is unsupported, not
excluded. Samples outside the board outline contribute to neither numerator nor denominator.
Arcs also count in per-net/per-layer lengths. Small-pad vias require SMD-pad copper on the tested outer
layer; both F.Cu and B.Cu are checked, and paste-only pads are ignored. Foreign-net plane crossings count
only where via copper plus the zone's clearance intersects its outline, once per via/layer.
The inspect cache is schema- and binary-keyed so a changed inspector or record schema cannot reuse stale
geometry.

No completed connection means quality is unavailable. Missing evidence and zero denominators remain null,
never perfect; report `available_weight` and `missing_terms`. Scores with different measured-term sets are
not directly comparable. `compare()` uses mutually available terms and matched nets open in input and
complete in both outputs, excluding plane nets in either board, for length/via/detour ratios. Its matched
via denominator is `sum(pads - 1)`,
an explicit pad-tree connection proxy, not a KiCad missing-item count. Plane/DFM terms remain board-level.
Nets over 2000 pads have unavailable MST and contribute neither length nor MST to detour; matched detour
requires an available positive MST in both records. Pad-centre MST is a topology proxy, not a geometric
lower bound; Steiner sharing and pad edges can yield detour below one. Rational arithmetic decides
verdicts; `quality_milli` is half-up-rounded `1000 * Q`. In equal legality/completion tiers, `compare()`
ties when matched costs differ by less than 2% of the larger cost. This is a policy noise floor, not a
confidence interval.

Hand originals use the identical function and can lose. `versus_hand()` reports each term ratio, with null
and an explicit status for zero hand denominators; similarity to the original is never rewarded.
`python3 bench/score.py RECORD.json [RECORD.json ...] --hand original --md` prints a grouped table;
each file may hold one record or a list. `markdown(records, hand_label="original")` supplies the same
report to callers and displays failed judges without fabricating metrics.
`planes_eval.py --seeds N ...` defaults to seed 7 and records each seed separately. The report gives
median unconnected items and Q with min–max seed spread, per-seed wins/ties/losses versus the hand
original, and the best configuration per board. Configuration names and seed spread accompany claims;
a single-seed result is not a multi-seed verdict.

**Results** (2026-10-07, integrated binary, current refill judge/metrics, 10M work units, one
variant/thread, CPU fields). Eight KiCad demos use `base` (no options), `soft`
(`--soft-zones --plane-cut-cost 0`) and `soft+vop` (also `--keep-vias-off-pads`, 2 mm).
Nine PCBench quick-tier inputs use `base` and `vop` (`--keep-vias-off-pads`, 2 mm).
Seeds are 7/19/31; open = KiCad unconnected items after refill. Table values are medians [min–max over
seeds]; Q is diagnostic within a legality/completion tier, not a substitute for it. Matched length is
track/arc length on jointly completed input-open non-plane nets, candidate / hand.
Evidence: `bench/results/planes-eval-demos-10m` and `bench/results/planes-eval-pcbench-10m` (records and
report; reproduce with `bench/planes_eval.py BINARY --work 10000000 --seeds 7 19 31`). Full configuration counts are

| Board | Hand: legal, Q | Best TraceMaker (open, Q) | Matched length | Verdict (W/T/L over seeds vs hand) |
|---|---|---|--:|---|
| complex_hierarchy | yes, 39.483 | base: 0 [0–0], 50.418 [50.418–50.418] | 0.924 | base wins quality (3/0/0); plane cost 1.249 vs hand 2.350 |
| multichannel_mixer | no: 24 clearance; 48.359 | soft+vop: 4 [4–4], 44.579 [44.579–44.579] | 0.928 | soft+vop wins legality (3/0/0), but incomplete |
| RoyalBlue54L-Feather | no: 2 clearance; 31.673 | soft: 87 [87–87], 36.660 [36.660–36.660] | 0.922 | soft wins legality (3/0/0), but incomplete |
| StickHub | yes, 36.398 | soft: 10 [10–10], 34.050 [34.050–34.050] | 0.938 | hand wins completion (0/0/3) |
| CM5_MINIMA_3 | yes, 44.302 | soft: 62 [62–62], 46.357 [46.357–46.357] | 0.821 | hand wins completion (0/0/3) |
| kit-dev-coldfire-xilinx_5213 | yes, 44.180 | soft+vop: 125 [125–125], 52.171 [52.171–52.171] | 0.935 | hand wins completion (0/0/3) |
| pic_programmer | yes, 42.794 | base: 10 [10–10], 52.727 [52.727–52.727] | 0.966 | hand wins completion (0/0/3) |
| interf_u | no: 2 starved_thermal; 30.394 | base: **1 starved_thermal**, 43 [43–43], 43.843 [43.843–43.843] | 0.909 | base has fewer errors (3/0/0); neither board is legal |
| 9 PCBench boards | 4 legal, 5 fail; details below | base/vop both 0 [0–0] on all boards; per-board Q below | base 0.675–0.999; vop 0.677–0.999 | base: 8 W / 1 T per seed; vop: 9 W per seed; five wins are hand-legality failures |

All metric spreads are zero in these runs. With one variant, these seeds change UUIDs, not routing order
or geometry (the default connection order does not use the seed); outputs differ in bytes, metrics do not.
The three seeds are therefore **not robustness evidence** or independent routing trials.

On complex_hierarchy `soft` and `soft+vop` also finish legally: Q 47.109 [47.109–47.109], plane cost
1.687 versus hand 2.350, and W/T/L 3/0/0 each. `base` has the lower plane cost there.
On the other demos `soft` reduces open items versus `base` on CM5 (118 → 62), StickHub (31 → 10),
ColdFire (415 → 125), RoyalBlue (112 → 87) and multichannel_mixer (11 → 4), but worsens
pic_programmer (10 → 13) and interf_u (43 → 46). `soft` has 0.154–0.695 small-pad vias per delivered
connection on the five nonzero boards and zero on the other three. `soft+vop` has zero on all eight
(copper-only metric), at a cost of 0–14 extra open items versus `soft`.

**Legality limitations.** At 10M, StickHub `soft+vop` adds one `solder_mask_bridge`; interf_u `base`
adds one `starved_thermal`, `soft` and `soft+vop` two each. All interf_u candidates fail legality,
as does its hand original (two). Thermal-spoke starvation is not modeled by the router; StickHub's mask
bridges expose its pre-existing omission of unfilled mask circles on a logo. Both are open router work
items, now caught by the judge, not reasons to discard its error reports.

The 50M, seed-7 follow-up (`bench/results/planes-eval-demos-50m`) tests StickHub,
interf_u, multichannel_mixer and pic_programmer only; none additionally finishes. This is a single seed,
not a spread estimate:

| Board | base: open / Q | soft: open / Q | soft+vop: open / Q | Added errors |
|---|---:|---:|---:|---|
| StickHub | 31 / 45.737 | 14 / 32.098 | 19 / 31.658 | soft: 1 mask bridge; soft+vop: 4 |
| interf_u | 26 / 43.672 | 28 / 31.099 | 28 / 31.099 | base: 1 starved thermal; soft/soft+vop: 3 |
| multichannel_mixer | 11 / 55.865 | 4 / 42.228 | 4 / 44.579 | none |
| pic_programmer | 3 / 52.703 | 7 / 44.039 | 7 / 44.039 | none |

More work is not monotonic: StickHub `soft` worsens 10 → 14 open and acquires a mask bridge.
`soft+vop` improves 24 → 19 open but increases bridges from one to four. interf_u `soft`/`soft+vop`
improves 46 → 28 open but increases starved thermals from two to three.

**PCBench hand comparison.** Each row below has zero open items and zero added errors for both `base`
and `vop`; each Q is its seed median and its min–max spread is [Q–Q]. W/T/L counts the three seeds against
`raw.kicad_pcb`, not three different routing orders. `vop` leaves zero small-pad vias on all nine.

| Board | base Q | vop Q | base W/T/L | vop W/T/L |
|---|--:|--:|---|---|
| Dekada_dekada_TopoR_curves | 64.075 | 64.075 | 3/0/0 | 3/0/0 |
| Hardware_Playground_rpi_zero_ws2812 | 62.250 | 63.941 | 3/0/0 | 3/0/0 |
| Photodiode_dethead | 58.220 | 63.679 | 3/0/0 | 3/0/0 |
| esp-serial-terminal_esp-com | 57.396 | 60.313 | 0/3/0 | 3/0/0 |
| kika-in-space_DS8500 | 52.716 | 61.430 | 3/0/0 | 3/0/0 |
| kitspace_8_switch_array | 64.783 | 64.783 | 3/0/0 | 3/0/0 |
| komputer-klavier_KomputerKlavier | 64.387 | 64.387 | 3/0/0 | 3/0/0 |
| phone_rtty_interface_phone_rtty_rev_a | 64.663 | 64.663 | 3/0/0 | 3/0/0 |
| scimpy_volumebuffer | 62.942 | 62.942 | 3/0/0 | 3/0/0 |

Five PCBench hand originals fail the new judge: Hardware_Playground (12 `starved_thermal` and 10
`solder_mask_bridge`), Photodiode (6 `clearance`, 3 `shorting_items`), kika (5 `starved_thermal`),
scimpy (2 `starved_thermal`) and komputer (1 `starved_thermal`). Their unrouted inputs have no zones
while their raw hand boards can have zones: hand thermal failures therefore concern copper the candidate
never had. These hand comparisons are weak evidence of routing superiority, not a like-for-like
plane test. The esp `base` result ties under the 2% cost floor; `vop` wins there on quality.

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
- Added errors use the current refill-sensitive and Zone/routed-copper filters in §2 (D63, superseding
  decision A16's Track/Via/Arc-only filter); other new KiCad reports remain diagnostics.
- Results go to `bench/results/<run>/` and the progress site's benchmark panel.
- Latest (`final8`): tier A 100% clean, B 65.0%, C 56.7%, D 50.0% (Freerouting 2.5.0-RC12: 100%, 50.0%, 46.7%,
  36.4%); no router-introduced DRC errors on any board.
