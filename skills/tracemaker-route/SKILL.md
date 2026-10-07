---
name: tracemaker-route
description: Route a prepared KiCad 10 board with the TraceMaker autorouter and sign it off with KiCad's refilled DRC. Chooses options (work budget, soft zones, vias off pads, differential pairs), reads the route summary, compares KiCad DRC against the unrouted baseline and decides whether the result is clean, incomplete or illegal. Use after tracemaker-board-prep, when the user wants a board autorouted.
---

# Routing with TraceMaker

## For humans

TraceMaker routes the unrouted connections of a placed KiCad board and writes a new `.kicad_pcb`.
It never moves or rips locked items and keeps everything it did not touch byte for byte. KiCad stays the
judge: a route is finished only when KiCad's DRC, after refilling the zones, reports no unconnected items
and no errors the unrouted board did not have.

Prepare the board first (`tracemaker-board-prep`).

## For agents

### Inputs

- A prepared board (preflight run, blocks fixed) with its `.kicad_pro`/`.kicad_dru` beside it.
- The baseline DRC of that unrouted board (board-prep step 5).
- The `tracemaker` binary (`tracemaker version`; build instructions are in the TraceMaker README).

Never write over the canonical board. Route to a new file in the project folder (so KiCad finds the
project's rules for it), check it, then promote it with a backup.

### 1. Choose options

Start from the preflight's suggested command, then:

| Situation | Options |
|---|---|
| Any run you want to reproduce | `--work N` (deterministic: same input, same options, same output on any machine). `--time` is only a wall-clock safety net, default 120 s |
| Board with ground/power planes on inner layers | `--soft-zones --keep-vias-off-pads` (cut and connect into planes, keep vias out of SMD pads < 2 mm) |
| Two-layer board with pours | Try with and without `--soft-zones`: on the two-layer demos it helped StickHub (31 → 10 open) and multichannel_mixer (11 → 4) but not pic_programmer (10 → 13) or interf_u (43 → 46) |
| Differential pairs (`+`/`-`, `P`/`N` names) | `--diff-pairs`; `--pair-skew-mm X` for a skew limit |
| Dense BGA / QFN | Escape planning runs in two of the eight portfolio variants by default; `--escape-plan` turns it on in all. `--escape-report` lists which deep BGA pins connected |
| Blind/buried vias allowed by the board | `--blind-vias` |
| Component-aware keep-outs (crystals, switcher inductors) | `--component-rules report` writes a sidecar `.kicad_dru` to review; `on` also routes with the keep-outs |

Work budget: start with `--work 10000000` for a board of a few hundred connections, measure, then raise.
More work is not always better on soft-zone boards (one demo went 10 → 14 open between 10M and 50M), so
keep the best run, not the last. `--variants` (default: all with `--work`) runs a portfolio of router
configurations and keeps the best; `--threads` only changes speed when `--work` is set, never the result.

```
tracemaker route BOARD.kicad_pcb -o BOARD-routed.kicad_pcb --json route.json --work 10000000 [options]
```

Exit code 0 means every connection the router planned was routed; 3 means some were not.

### 2. Read the summary

`route.json`: `routed` / `connections`, `tracks`, `vias`, `failures` (one line per unrouted connection), and with
`--soft-zones` also `plane_connections` and `zones_needing_refill`. The router's count is not sign-off:
with soft zones, plane connections exist only once KiCad refills.

### 3. Sign off with KiCad

```
python3 <this skill>/scripts/signoff.py BOARD.kicad_pcb BOARD-routed.kicad_pcb --route-json route.json
```

It runs `kicad-cli pcb drc --refill-zones` on both boards and reports unconnected items before and after,
errors the routing added (by type, with examples), and a verdict: `CLEAN` (exit 0), `INCOMPLETE` (1),
`ILLEGAL` (2). Errors present in the unrouted board are not counted against the route.

| Added error | Cause | What to do |
|---|---|---|
| `starved_thermal` | New copper blocked thermal spokes; the router does not model them | Re-route near that pad, widen spokes, or fix by hand |
| `solder_mask_bridge` near a logo | The router ignores unfilled mask circles | Rule area over the logo, re-route |
| `clearance` / `shorting_items` with a zone | Plane cut or refill effect | Check zone priority and clearance; re-route with different options |
| Anything else involving new tracks or vias | A router defect | Report it with the board and command |

Unconnected items left: raise `--work`, try the other zone mode, check the preflight's escape report, or
route the remainder by hand. Do not loosen design rules to make DRC pass.

### 4. Hand back to KiCad

- The routed file's zone fills are stale where new copper cut them: open it in KiCad and refill (`B`)
  before reviewing or exporting.
- If the user had the board open, tell them to close without saving before promoting the routed file over
  the canonical one, then reopen.
- Regenerate teardrops if the design uses them; add ground stitching vias after routing.
- Finish with the project's export flow (kistack `kicad-export`: refilled DRC with
  `--schematic-parity`, Gerbers, BOM, positions).

### Live view

`--view` streams the routing to a browser viewer (port 8766), `--record FILE` saves it for replay. Useful
when the user wants to watch; not needed for correctness.
