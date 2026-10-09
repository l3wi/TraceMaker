# 08 — KiCad integration

> Part of the TraceMaker plan. Start at [`../PLAN.md`](../PLAN.md).
> Facts below were verified on 2026-10-02; sources in [`../research/kicad-and-benchmarks-2026-10.md`](../research/kicad-and-benchmarks-2026-10.md).

## 1. Target versions

| Item | Target |
|---|---|
| KiCad | **10.0.x** (10.0.6 stable as of 2026-08-29); read 9.0 files too; track KiCad 11 (master) changes |
| Board file version | `20260206` (10.0). Accept older versions KiCad 10 accepts; **write the same version that was read** |
| Schematic file version | `20260306` (10.0) |
| IPC client | `kicad-python` (`kipy`) 0.8.x |
| `kicad-cli` | 10.0.x, natively or in Docker `kicad/kicad:10.0.6` (no GUI needed) |

## 2. Three integration paths

| Path | Used for | How |
|---|---|---|
| **Files** (primary) | CLI, batch, benchmarks, schematic-to-board | Native C++ s-expression readers/writers for `.kicad_pcb`, `.kicad_sch`, `.kicad_pro` (JSON), `.kicad_dru`, `.kicad_mod`, `fp-lib-table`/`sym-lib-table` |
| **`kicad-cli`** (judge and cross-check) | DRC sign-off, netlist cross-check, zone refill | Subprocess; `pcb drc --format json --severity-all --exit-code-violations --all-track-errors [--schematic-parity] [--refill-zones]` |
| **IPC API** (live in pcbnew) | Plugin: route the open board, show results immediately, one undo step | Python plugin with `kipy`; out-of-process engine |

## 3. File I/O (`src/io/kicad`)

- One generic **s-expression lexer/parser** producing a lossless tree (tokens, original formatting of
  untouched nodes, unknown nodes preserved). Typed views (board, footprint, pad, track…) read from the
  tree; writes patch the tree.
- **Round-trip guarantee**: parse → write without changes is byte-identical (gate of M1). Changes touch
  only the nodes TraceMaker owns: footprint `at`/`layer`/flipped pads, new `segment`/`arc`/`via` nodes,
  removed tracks it ripped.
- **Project rules**: `.kicad_pro` (JSON) holds net classes, net-class patterns/assignments, board design
  settings (min track width, clearances, via sizes, hole clearances), and severity settings. `.kicad_dru`
  holds custom rules (constraint keywords in the research note §A). The rule compiler (doc 03) consumes both.
  By default both files use the board's stem. `--project FILE.kicad_pro` overrides that source on `route`,
  `drc`, `escape`, `rules`, and `inspect --json`; its same-stem `.kicad_dru` wins too, never the renamed
  board's rules. An explicit project that is missing/unreadable is an error. Routing logs the absolute
  project/DRU paths (loaded or absent) and every reader/compiler warning before routing, and includes
  `rules: {project, dru, warnings, project_loaded, dru_loaded}` in its JSON summary.
  The reader starts with KiCad 10's built-in minimums, applies legacy board setup/net classes, then
  overrides individual values supplied by the project. Missing project keys keep the board value or
  built-in default; an explicitly supplied zero stays zero. Missing projects are warned, not treated
  as zero-minimum designs (D100).
- **Schematic**: netlist from the hierarchical `.kicad_sch` set (symbols, pins, labels, power symbols, net
  class directives, DNP / exclude-from-board flags). Cross-checked in tests against
  `kicad-cli sch export netlist --format kicadsexpr`.
- **Footprints**: from the board if present; otherwise resolved through `fp-lib-table` to `.kicad_mod` files
  (schematic-to-board mode builds the footprints itself, like "Update PCB from Schematic").
- **Specctra DSN reader / SES writer** for the Freerouting fixture set, plus a **DSN writer** so Freerouting
  can be run as a baseline on our own `.kicad_pcb` fixtures (KiCad 10 has no `kicad-cli` DSN export, and the
  SWIG exporter disappears in KiCad 11).

### 3.1 Project-less and missing-key rule defaults (D100)

KiCad 10.0.3's [`include/board_design_settings.h`](https://github.com/KiCad/kicad-source-mirror/blob/10.0.3/include/board_design_settings.h)
defines `DEFAULT_TRACKMINWIDTH`, `DEFAULT_VIASMINSIZE`, `DEFAULT_MINTHROUGHDRILL` and the other
`DEFAULT_*` constants below. Its
[`BOARD_DESIGN_SETTINGS` constructor](https://github.com/KiCad/kicad-source-mirror/blob/10.0.3/pcbnew/board_design_settings.cpp)
sets the annular width to `(DEFAULT_VIASMINSIZE - DEFAULT_MINTHROUGHDRILL) / 2` and
`m_resetParamsIfMissing = false`: an absent project key preserves the constructor value or a value
read from the legacy board. This matters because the JSON parameter's annular-width reset default
is not the constructor value; use the value actually judged, not that parameter declaration.

| Board constraint | Default (mm) |
|---|---:|
| Overall clearance / minimum connection width | 0 / 0 |
| Minimum track width | 0.2 |
| Minimum via diameter / annular width | 0.5 / 0.1 |
| Minimum through-hole drill | 0.3 |
| Minimum microvia diameter / drill | 0.2 / 0.1 |
| Hole-to-copper / hole-to-hole clearance | 0.25 / 0.25 |
| Copper-to-edge clearance | 0.5 |
| Silk clearance / mask-to-copper clearance / mask web width | 0 / 0 / 0 |

Default net-class values come from
[`common/netclass.cpp`](https://github.com/KiCad/kicad-source-mirror/blob/10.0.3/common/netclass.cpp):
clearance and track width 0.2 mm, via diameter/drill 0.6/0.3 mm, microvia diameter/drill 0.3/0.1 mm,
differential-pair width/gap/via-gap 0.2/0.25/0.25 mm. The class track/via dimensions are routing
defaults, not minimum constraints: a legal neck-down is bounded by the board/custom rules.

`tests/integration/no_project_defaults.py` probes thin tracks, undersized vias/drills/annular rings,
close holes, hole-to-copper and edge clearances, with no project and with omitted project keys. On local `kicad-cli 10.0.3` its
messages report respectively 0.2000, 0.5000, 0.3000, 0.1000, 0.2495, 0.2500 and 0.5000 mm; the
hole-to-hole message includes KiCad's 0.0005 mm checking tolerance (the configured rule is 0.25).
Foreign-net clearance is 0.2000 mm. The same test checks that TraceMaker reports the formerly missed
thin-track violation and routes a legacy 0.15 mm class at a legal width, both without a project and
with a partial project omitting the minimum-width key. Unit tests cover every reader default,
explicit zero, individual project overrides and preserved legacy minimums.

OtterCast's 0.15 mm GND stubs came from the router's **neck-down escalation**, not a board net class:
its board has neither legacy `net_class` nor `trace_min`. Previously the reader supplied track
minimum 0, so `Router::neck_width` could lower the 0.25 mm fallback class width to 0.15 mm, and the
exact rule check agreed with that incorrect minimum. KiCad instead judged against its 0.2 mm
minimum. Reading the real defaults fixes both search/escape widths and the exact DRC without
special-casing short tracks or GND.

**Measured correction, macOS arm64 / KiCad 10.0.3:** paired runs
`defaults-quick-before`/`defaults-quick-after` (1 M work, one variant) and
`defaults-gnd-before`/`defaults-gnd-after` (3 M work, one variant, `--soft-zones`, refill).
Both used four board jobs and no knowledge base; timing differed with machine load and is not a speed claim.

| Tier | Routed / planned before → after | Mean KiCad completion | Clean boards | Added routing errors | Byte-identical outputs |
|---|---:|---:|---:|---:|---:|
| quick (30) | 1,720 / 2,018 → 1,720 / 2,018 | 89.20% → 89.20% | 12 → 12 | 0 → 0 | 30/30 |
| gndplane (14) | 2,832 / 3,849 → 2,780 / 3,849 | 76.49% → 75.71% | 1 → 1 | 105 → 45 | 13/14 |

| Ground-plane board | Routed before → after / planned | Added KiCad routing errors before → after |
|---|---:|---|
| Retro1DecodingModules_AddressDecoderModule | 35 → 35 / 35 | 0 → 0 |
| Radar-System_fmcw_radar | 64 → 64 / 74 | 0 → 0 |
| rxadc_14_rxadc_14 | 115 → 115 / 124 | 0 → 0 |
| TinyTracker_ub-minimal | 126 → 126 / 159 | 0 → 0 |
| LoRaPP_loramod | 150 → 150 / 164 | 0 → 0 |
| PDB_OSD_HARDWARE_Quadcopter Power Board | 136 → 136 / 138 | 0 → 0 |
| Aria_Aria | 156 → 156 / 239 | 0 → 0 |
| f4fc_f4fc | 123 → 123 / 208 | 45 `items_not_allowed` → 45 (unchanged output) |
| FMCW_RADAR_Radar MCU | 238 → 238 / 258 | 0 → 0 |
| Ttl_wlan_radar | 145 → 145 / 282 | 0 → 0 |
| mppt-2420-hpx_mppt-2420-hpx | 250 → 250 / 403 | 0 → 0 |
| draco_draco | 289 → 289 / 418 | 0 → 0 |
| kitspace_OtterCastAudioV2 | 259 → 207 / 533 | 60 `track_width` → 0 |
| front-end-modules_LimeSDR_Sony | 746 → 746 / 814 | 0 → 0 |

No board gains KiCad routing errors. Quick is entirely byte-identical because its stored legacy
rules still win. Only OtterCast changes in the ground-plane tier: its unconnected records rise
252 → 303 (GND 18 → 20, signals 234 → 283), completion 46.61% → 35.81%, vias 157 → 136.
It has **zero new-copper track-width violations and zero added routing errors**, but is not complete.
The ground-plane completion comparison gate fails on this board; the old higher completion used
illegal copper and is not a valid target to preserve by weakening the minimum. The remaining
45 `items_not_allowed` errors on f4fc are a pre-existing, byte-identical result, not hidden as a pass.
Raw KiCad error totals (including pre-existing errors and categories excluded from routing-added
metrics) are quick 237 → 237, gndplane 1,326 → 1,268; no board's total increases.
OtterCast alone is 85 → 27: its 60 thin-track errors disappear, 12 fixed-input errors stay, and
`starved_thermal` is 13 → 15 (the unrouted input had 20). The legal route therefore heals two fewer
existing thermal problems than the old illegal route; those are not silently omitted from this report.
The new integration test passes; `tm_tests` passes 111 cases / 635,046 assertions, with one CUDA-only
Philox test skipped on this Metal build.



## 4. Rule coverage policy

- Every rule TraceMaker cannot interpret is reported as a warning with its location, and the router uses
  the most conservative interpretation (for example, the largest clearance that could apply).
- Phase 1 custom-rule conditions: `A.NetClass`, `A.NetName`, `A.Type`, `A.Layer`, `A.insideArea(...)`,
  `A.intersectsArea(...)`, `A.isPlated()`, `A.Pad_Type`, `A.Reference`, boolean `&& || !`. Others later.
- The router also enforces `disallow track/via` by net and layer, and `physical_hole_clearance` between holes
  and copper of any net (doc 05 §27). Positional `disallow` rules are warned and left to the DRC.

## 5. IPC plugin (`kicad_plugin/`)

Facts that shape it:
- KiCad 10 IPC runs only with a **running pcbnew GUI** (headless server is a KiCad 11 feature).
- 10.0 has **no IPC call to read design rules** and **no IPC call to run DRC**. The plugin therefore reads the
  rules from the project files on disk (the plugin knows the project path) and runs `kicad-cli pcb drc` on a
  saved copy when the user asks for sign-off. On KiCad 11 it switches to `GetBoardDesignRules` /
  `GetCustomDesignRules`.

Flow:
1. Plugin (`plugin.json`, Python, own venv with `kicad-python`) starts or connects to `tracemakerd`.
2. Reads the board via `SaveDocumentToString` (exact file content) — simplest lossless transfer — plus
   project files from disk.
3. Engine routes (and places, if the user chose a placement mode); the viewer URL opens in a browser for the
   live view.
4. Results are applied in **one commit** (`begin_commit` → `CreateItems`/`UpdateItems`/`DeleteItems`/
   `FlipItems` → `push_commit`), so a single Ctrl-Z undoes the whole run. `RefillZones` afterwards.
5. Packaged for KiCad's Plugin and Content Manager (PCM).

```python
# sketch (kipy 0.8; exact class names to be checked against the installed version during M11)
from kipy import KiCad
kicad = KiCad()                               # uses KICAD_API_SOCKET / KICAD_API_TOKEN
board = kicad.get_board()
text = board.save_to_string()                 # SaveDocumentToString
result = engine.route(text, project_dir, mode="eco")
commit = board.begin_commit()
board.update_items(result.moved_footprints)   # position/orientation
board.create_items(result.new_tracks + result.new_vias)
board.remove_items(result.ripped_items)
board.push_commit(commit, "TraceMaker route")
board.refill_zones()
```

## 6. DRC judge (bench and sign-off)

```
kicad-cli pcb drc --format json --severity-all --all-track-errors --exit-code-violations \
                  --refill-zones --output out.json routed.kicad_pcb
```

- Run on the **input** board and on the **output** board; the score is the multiset difference by
  violation type and location ("added errors"), so pre-existing problems are not blamed on the router.
- Unconnected items are counted separately (they measure completion, not legality).
- Exit code 5 means violations exist; parse the JSON for the details.
- TraceMaker's `drc` uses the same status convention: 0 no reported violations/unconnected items, 5 either
  present, 1 runtime error. Its checks do not replace KiCad's sign-off. CLI usage errors return CLI11's
  nonzero usage status.

## 7. Pitfalls to design around

- Footprint flips mirror pad layers and change rotation conventions; test flips against KiCad's output.
- KiCad rotation is counter-clockwise in a y-down coordinate system (see `pcbgolf/placer.py` `rot_pt`).
- Zones must be refilled after routing; never write stale fills (use `--refill-zones` for the judge).
- Net-class assignment can come from the schematic (directives), pattern rules in `.kicad_pro`, or the
  board; resolve in KiCad's priority order.
- Locked items (`locked` flag) and user groups must never move or be ripped.

## 8. Implementation status (M11)

- **IPC action plugin** (`kicad_plugin/`): saves a copy of the open board with its project (`save_as(...,
  include_project=True)`; the rules come from the copied project files), routes it, and adds the new tracks and
  vias with `begin_commit` → `create_items` → `push_commit` (one undo step). Since 2026-10-06 also, each by an
  environment variable (README): `TRACEMAKER_REROUTE=1` removes the unlocked tracks and vias and routes the board
  again (`route --reroute`; locked copper, arcs and zones stay); `TRACEMAKER_PLACE=<mode>` runs `tracemaker-place`
  first and moves the footprints with `update_items` (side changes are not applied; when parts move, the old
  copper is routed again and the zones are refilled); `TRACEMAKER_REFILL=1` refills the zones. Removal, moves and
  new copper are one commit. KiCad records a zone refill as a step of its own, so with a refill the result takes
  two undos; that is why refill is off unless footprints moved. Not built: a long-running `tracemakerd` (each run
  starts the engine afresh). The plugin is tested offline against kicad-python 0.8 (`test_offline.py`).
- **Round trip in a running KiCad GUI** (`scripts/kicad_gui_roundtrip.py`, KiCad 10.0.6): pcbnew from the Docker
  image runs on an Xvfb display with the API server enabled (`kicad_common.json`), the first-run wizard is
  clicked away through XTEST, and the plugin talks to the live board over the API socket. On
  `multichannel_mixer-unrouted`: tracks 140 → 316 and vias 19 → 21 in the open board (80 connections in 30 s); one
  Ctrl+Z gives back 140 / 19, Ctrl+Y the result; KiCad's DRC of the board saved from the GUI: unconnected 148 → 65,
  no new errors. With `--place refine`: 65 footprints moved, old copper routed again, zones refilled, two undos
  restore tracks, vias and footprint positions, no new DRC errors. The script is not a ctest (it needs Docker,
  Xvfb and about three minutes); screenshots in `report/m11-gui/`.
- **Engine access**: the plugin imports the Python module `tracemaker` when it can and otherwise runs the
  `tracemaker` binary (`route --emit-items`). `TRACEMAKER_ARGS` uses CLI spelling for both (decision D17).
- **Python bindings** (`bindings/`, pybind11, CMake option `TM_BUILD_PYTHON`, on when pybind11 is found and the
  build is not a sanitizer build): `read_board`, `route`, `emit_items`, `drc`. `route` calls
  `tmk::app::run_route_job` (`src/app/route_job.hpp`), the function behind the CLI's `route` subcommand, so both
  give byte-identical boards (ctest `python_bindings` checks this on a PCBench board). The GIL is released while
  routing and checking. The module is built into `build/<preset>/python/` (D18).
  The CLI and Python also share option registration/parsing in `route_job.cpp` (D94): `route()` and
  `emit_items()` accept `args=[...]` in CLI spelling, including project overrides, component rules, zone/
  layer options, escape/differential-pair options and selective rerouting. Omitted values come from the
  shared job/router defaults; existing keywords remain compatible and `args` override them. The plugin
  forwards `TRACEMAKER_ARGS` directly; no second partial option parser remains.
  `--reroute-nets NET[,NET...]` validates all names, removes only the chosen nets' unlocked tracks/arcs/vias,
  and routes the board's remaining connections. Full `--reroute` retains its prior behaviour (arcs stay).
  Both preserve locked copper and all untouched file content.
  The summary/Python `unrouted` array includes net and pad references, `reason` (stable snake_case category),
  `detail` (the existing diagnostic), pad positions in mm (`a_mm`, nullable `b_mm` for zone targets) and
  copper layer names (`a_layers`, `b_layers`). Human `failures` strings remain. Classification uses the
  final outcome in the retained routing state's diagnostic, without changing routing decisions; a pad not
  attempted in that retained state is `not_attempted`, and an unrecognised future diagnostic is `unknown`.
  Categories include `boxed_in_source`, `boxed_in_target`, `enclosed_by_fixed_copper`, `no_path_in_window`,
  `search_budget`, `nogood_skip`, `rip_cap`, `protected_net`, `exact_check_rejected`, `layer_stranded`,
  `out_of_budget`, and `ripped_not_rerouted`. Pair diagnostics also classify as `pair_too_short`,
  `no_coupled_path`, or `would_enclose_pin`; all known router diagnostics are covered by unit tests.
  `route` returns 0 when every planned connection routes, 3 when output is written with unrouted
  connections, 1 on runtime error; CLI usage errors return CLI11's nonzero usage status.
  Completion (0) is **not** KiCad DRC sign-off.
- **PCM package**: `scripts/make_pcm_package.py` writes `build/pcm/tracemaker-<version>.zip` (metadata schema v2
  as shipped with KiCad 10, `type: plugin`, `runtime: ipc`, `kicad_version: 10.0`, the plugin under `plugins/`, a
  generated 64×64 `resources/icon.png`; reproducible archive). `scripts/validate_pcm.py` checks the layout and
  metadata and also runs kicad-python's official validator when it is installed; ctest `pcm_package` builds and
  validates the archive. The engine is not bundled by default (D19); `--binary`/`--module` bundle the Linux builds
  for use on the same machine. Installed through the Plugin and Content Manager of a running KiCad 10.0.6 (headless, as above; Install from
  File…): the manager lists TraceMaker 0.1.0 as installed and compatible, and the files are in
  `3rdparty/plugins/org_tracemaker_autoroute/` (`report/m11-gui/pcm-installed.png`). Not checked: that KiCad then
  builds the plugin's virtual environment and shows the toolbar button (the container had no network for pip).
