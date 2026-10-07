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
- **Schematic**: netlist from the hierarchical `.kicad_sch` set (symbols, pins, labels, power symbols, net
  class directives, DNP / exclude-from-board flags). Cross-checked in tests against
  `kicad-cli sch export netlist --format kicadsexpr`.
- **Footprints**: from the board if present; otherwise resolved through `fp-lib-table` to `.kicad_mod` files
  (schematic-to-board mode builds the footprints itself, like "Update PCB from Schematic").
- **Specctra DSN reader / SES writer** for the Freerouting fixture set, plus a **DSN writer** so Freerouting
  can be run as a baseline on our own `.kicad_pcb` fixtures (KiCad 10 has no `kicad-cli` DSN export, and the
  SWIG exporter disappears in KiCad 11).

## 4. Rule coverage policy

- Conditions are checked structurally against an explicit supported registry: all AST leaves are
  diagnosed even when evaluation would short-circuit. Unknown/unparseable conditions conservatively
  match, but ignored unknown rules cannot waive known earlier restrictions and unknown numeric rules
  cannot weaken known floors/caps. Whole unreadable custom-rule files stop routing.
- Supported properties: `NetClass`, `NetName`, `Type`, item `Layer`, `Reference`, `Parent.Reference`,
  `Pad_Type`, local `Size_X` / `Size_Y`, `Width`, item-anchor `Position_X` / `Position_Y`. Bare `L`
  binds the context layer; it is distinct from `A.Layer`. Item properties/calls require `A.` / `B.` binding.
- Supported calls: `isPlated`, `existsOnLayer`, `inDiffPair`, `memberOfFootprint`, `insideArea`,
  `intersectsArea`, `enclosedByArea`, `intersectsCourtyard`, `intersectsFrontCourtyard`,
  `intersectsBackCourtyard`, and corresponding generic/front/back `inside...` courtyard aliases.
  `insideArea` and courtyard inside aliases mean intersection, not containment; `enclosedByArea`
  uses whole geometry. Reference/library-ID wildcards select footprints; area selectors include names
  and UUIDs. Courtyard geometry comes from actual closed per-side outlines, never placement hulls.
  Front/back courtyard variants swap the selected side on flipped footprints, following KiCad 10.
  Prepared area targets use their outline, not refilled target copper; zone-as-target fill-sensitive
  parity is not claimed. Polygon normalization/enclosure use pinned Clipper2 integer Boolean operations.
  For a zone being tested, intersection uses its fill; enclosure and courtyard predicates use its outline.
  Intersection deflates area outlines by 0.5 µm; courtyards use 5 µm deflation/curve tessellation and
  20 µm endpoint chaining. A selected missing/malformed courtyard or area outline produces an
  unsupported-symbol diagnostic and conservative matching, never a hull or pad-box fallback.
  Courtyard functions do not themselves restrict copper layers; use a layer predicate when required.
- The router enforces supported positional/item-dependent `disallow track/via` on actual candidates
  and final geometry (D65; doc 05 §16). Class-uniform static net/class/type disallows remain cached;
  item/geometry predicates, subtype spans and differing same-class net predicates bypass class caches/fields.
  Prepared selector polygons remain cached and reference-tested.
  `physical_hole_clearance` remains enforced against fixed copper.
- Existing `memberOfGroup` ancestry, `${Class:...}` selector metadata and `disallow hole/footprint/text`
  coverage are unsupported and preflight-blocked. Free new tracks/vias have no ownership membership.
  Matching hole predicates conservatively reject candidate vias because they create unchecked holes.
  Conservative fallback is not KiCad parity; unsupported existing items still need KiCad sign-off.
- `route` sends rule/project diagnostics to stderr before routing and includes `rule_warnings`
  (exactly `RuleEngine::warnings()`) and `project_warnings` arrays in its JSON summary.
- Numeric conditions support `< <= > >=`, unit literals (`mm`, `mil`, `in`) and local pad copper
  dimensions; rotation does not change these dimensions (doc 05 §19).
- `route --keep-vias-off-pads [MM]` appends a synthetic, net-independent `physical_hole_clearance` rule to a
  route-only rules copy. Project files, the output board's rules and `tracemaker drc` are unchanged.
  Compilation failure of a synthetic rule is an error, not an ignored warning. KiCad checks this preference
  only when the user adds the equivalent rule in doc 05 §19 to the board's `.kicad_dru`.
- `escape` uses the same route-domain preparation and accepts the rule/configuration options that change
  access, including soft zones, blind vias, component rules, a rules override and the small-pad via-hole
  preference (doc 05 §12, D66). Original input tracks/vias remain present; exact connectivity marks already
  joined pad obligations `satisfied`, rather than analysing them as unrouted pins.

### Escape JSON contract

`escape <board> --json <file>` retains the top-level `board`, `parts`, `pins` and `dead` fields. Each part
retains `ref`, `pitch_mm`, `pins`, `escapable`, `dead` and `hint`, and adds `results` and a `statuses` count
map; the top level also carries `statuses`. `pins` counts outstanding obligations, `escapable` counts exact
witnesses, and `dead` contains only searches exhausted in their configured finite domain.

A result identifies the board pad index (`pad`) and its logical pad number (`pin`), with `status`, `reason`,
`domain` and `witness`. Statuses are `satisfied`, `witness`, `exhausted` or `unknown`; exhaustion is not a
physical-impossibility proof and bounded/unsupported searches do not enter `dead`. A witness uses integer
nanometres (`units: "nm"`), `steps` with `a`/`b` coordinate pairs, copper-stack `layer` index and `width`,
and `vias` with `position`, `diameter`, `drill`, stack `top`/`bottom` and numeric `type`
(through=0, blind=1, micro=2). Its final `layer`, `end` and integer `cost` are also reported.

The top-level `configuration` records `soft_zones`, `blind_vias`, `keep_vias_off_pads_mm`,
`component_rules`, `rules_override`, `pitch_um`, `reference` and `work_budget`; `warnings` contains rule/domain
warnings. Keep these alongside the input project/rules when comparing verdicts. A soft-zone witness is
provisional until refill and plane/thermal/DRC sign-off. This report describes individual access, not
global thin-channel completion or simultaneous full-board routability; the clean-pass judge remains authoritative.

Route JSON includes `access_work`, split into `access_generation_work`, `access_neighbor_work`,
`access_check_work`, `access_expansion_work` and `access_lattice_work`. Their sum is `access_work`;
every access unit is also included in total `expansions`. Generation/metadata loops, neighbour visits,
exact geometry/width/via/goal checks and graph expansion are deterministic work, not just graph pops.


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

## 7. Pitfalls to design around

- Footprint flips mirror pad layers and change rotation conventions; test flips against KiCad's output.
- KiCad rotation is counter-clockwise in a y-down coordinate system (see `pcbgolf/placer.py` `rot_pt`).
- Zones must be refilled after routing; never write stale fills (use `--refill-zones` for the judge).
- With `route --soft-zones` (doc 05 §18, D61), only original fills intersected by new foreign-net copper lose
  their `filled_polygon` nodes; every untouched zone remains byte-identical. The route log and JSON summary
  report plane connections and zones needing refill. The engine's pre-refill connectivity is provisional:
  run the command in §6 and require zero unconnected items and no added errors before accepting the result.
- Net-class assignment can come from the schematic (directives), pattern rules in `.kicad_pro`, or the
  board; resolve in KiCad's priority order.
- Locked items (`locked` flag) and user groups must never move or be ripped.

## 8. Implementation status (M11)

- **IPC action plugin** (`kicad_plugin/`): saves a copy of the open board with its project (`save_as(...,
  include_project=True)`; the rules come from the copied project files), routes it, and adds the new tracks and
  vias with `begin_commit` → `create_items` → `push_commit` (one undo step). Not yet done from the sketch in §5:
  footprint moves, ripping existing copper, `RefillZones`, and a long-running `tracemakerd` (each run starts the
  engine afresh). The plugin is tested offline against kicad-python 0.8 (`test_offline.py`); the round trip in a
  running KiCad GUI is still open.
- **Engine access**: the plugin imports the Python module `tracemaker` when it can and otherwise runs the
  `tracemaker` binary (`route --emit-items`). `TRACEMAKER_ARGS` uses CLI spelling for both (decision D17).
- **Python bindings** (`bindings/`, pybind11, CMake option `TM_BUILD_PYTHON`, on when pybind11 is found and the
  build is not a sanitizer build): `read_board`, `route`, `emit_items`, `drc`. `route` calls
  `tmk::app::run_route_job` (`src/app/route_job.hpp`), the function behind the CLI's `route` subcommand, so both
  give byte-identical boards (ctest `python_bindings` checks this on a PCBench board). The GIL is released while
  routing and checking. The module is built into `build/<preset>/python/` (D18).
- **PCM package**: `scripts/make_pcm_package.py` writes `build/pcm/tracemaker-<version>.zip` (metadata schema v2
  as shipped with KiCad 10, `type: plugin`, `runtime: ipc`, `kicad_version: 10.0`, the plugin under `plugins/`, a
  generated 64×64 `resources/icon.png`; reproducible archive). `scripts/validate_pcm.py` checks the layout and
  metadata and also runs kicad-python's official validator when it is installed; ctest `pcm_package` builds and
  validates the archive. The engine is not bundled by default (D19); `--binary`/`--module` bundle the Linux builds
  for use on the same machine. Installing the archive through the PCM GUI has not been tried yet.
