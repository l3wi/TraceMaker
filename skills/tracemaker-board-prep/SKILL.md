---
name: tracemaker-board-prep
description: Prepare a KiCad board for TraceMaker before placement or routing. Reviews the component-rule detections and writes the two files TraceMaker reads deterministically: the routing intent manifest (decoupling pin pairs, plane-via pads, no-branch nets) and the component-rule override file. Use when asked to prepare, classify, or write intent for a board before running tracemaker route/place.
---

# TraceMaker board preparation

TraceMaker never guesses electrical intent while it routes. Judgement happens here, once, and is written to files
that a person can review; TraceMaker then applies those files exactly and deterministically (same files + same
board + same budget = same output).

| Step | Who | Output |
|---|---|---|
| Detect categories and bind roles (decoupling caps, crystals, USB, Ethernet, regulators) | TraceMaker, deterministic | `tracemaker rules BOARD --json rules.json` |
| Draft intent from those detections | TraceMaker, deterministic | `--intent-draft draft.intent.json` |
| Decide what each part really is, which pin a capacitor serves, which nets must not branch, which detections are wrong | **You (this skill)** | `<board>.intent.json`, `<board>.tracemaker_rules.json` |
| Validate the files against the board | TraceMaker, deterministic | errors name the bad entry |
| Placement, routing, checking | TraceMaker, deterministic | routed board, `bench/intent_quality.py` report |

**Never** route, move, rotate, or edit the board or schematic here. Your outputs are the two JSON files and a short
review note.

## 1. Run the deterministic draft

```
tracemaker rules BOARD.kicad_pcb --project BOARD.kicad_pro --json /tmp/rules.json --intent-draft /tmp/draft.intent.json
tracemaker inspect BOARD.kicad_pcb --json /tmp/board.json      # pads (ref, num, net, layers), nets, zones
```

`rules.json` lists every detected instance with its confidence, evidence and bound roles (pads as `REF.NUM`).
The draft takes instances at confidence >= 70 only. Old boards (no pin names) are capped at 60: their USB and
Ethernet detections are missing from the draft and must be added by you if the evidence supports them.

## 2. Gather evidence (in this order of authority)

1. The schematic (`*.kicad_sch`, every sheet): symbol `lib_id`, `Value`, pin names and electrical types
   (`power_in`, `power_out`, `passive`), sheet names, notes. This is the strongest evidence.
2. The project: net classes and their patterns (`.kicad_pro` `net_settings`), custom rules (`.kicad_dru`).
3. Datasheets of the ICs, when available: which pins are supply pins and what decoupling each wants.
4. The board: footprints, values, net names, zones (which nets have planes, on which layers).
5. Net names alone are the weakest evidence. `USB_VBUS` is power, not USB data; `ETH_3V3` is a supply.

Write down, for yourself, what each relevant part is: IC, regulator, HF decoupling cap, bulk cap, bootstrap cap,
reference/VCAP cap, load cap, crystal/oscillator, ESD/TVS, common-mode choke, connector, ferrite, series resistor.
Only the parts that change the manifest matter; do not catalogue the whole board.

## 3. Review the draft, entry by entry

### connections (decoupling pin pairs)
Each `{"from": cap supply pad, "to": IC pin}` becomes its own short pad-to-pad track, never a branch off another
route. Keep an entry only when all hold:

- the capacitor is a local HF bypass (typically 10 nF–1 µF) between that supply and ground;
- the target is a supply **input** pin of an IC (`power_in`, VDD/VCC/AVDD/VDDIO/…), not a regulator output, an
  enable pin, or a pin of another capacitor;
- it is the pin the capacitor serves: adjacent in the schematic, or nearest on the board when the schematic
  does not say. One capacitor serves one pin; several capacitors may serve one pin.

Remove or retarget:
- bulk caps (>= 4.7 µF) and regulator input/output caps: these are power-path parts; keep them only if the
  regulator datasheet asks for a short cap-to-pin path, and then point them at the regulator pin;
- bootstrap, snubber, timing and filter caps (they are not supply to ground);
- reference/VCAP caps: keep, but target the VREF/VCAP pin;
- pairs whose pads TraceMaker cannot name uniquely (duplicate references, duplicate pad numbers): leave them out
  and say so in the review note;
- pairs to inner balls of a BGA (the draft already omits them): the capacitor reaches the ball through its fan-out
  via, which the manifest cannot name. A pad-to-pad track to an inner ball is usually unroutable and costs other nets.

Add capacitors the detection missed (custom footprints, references not starting with C, no pin names).

Every entry is a constraint the router pays for: on the benchmark sets the drafted manifests halve the long
capacitor detours but cost 1-7 % completion on some boards against `--tree-attach`. Keep entries that matter
electrically; an unroutable entry is relaxed and reported (`intent.relaxed` in the route JSON), not enforced.

### plane_pads (own via into a plane)
Each listed pad gets its own connection into a plane of its net, starting at the pad, instead of reaching the
plane through a neighbour's via. Only meaningful when the net has a plane (a zone on an inner or outer layer).

- Keep the ground pad of every kept decoupling capacitor.
- Keep the supply pad of a kept capacitor only when the supply itself is a plane or pour.
- Do **not** add IC ground pins of QFN/DFN packages with an exposed pad (their pins join the exposed pad and its
  via array), BGA balls (escape planning handles them), or pins on a separate analog-ground island unless the
  island is the plane you mean.

### no_branch_nets
Every connection of the net starts and ends on pads (or its plane), and routes avoid running along each other, so
the net is a chain of pad-to-pad segments with no T-junctions. Add a net only when the circuit needs it:

- USB 2.0 D+/D− data (not VBUS, not CC), Ethernet MDI pairs, LVDS/MIPI/HDMI/PCIe/SATA lanes;
- crystal lines between the IC pins and the crystal (load caps and a series resistor are fine: they are pads on
  the chain);
- single-load clocks and RF lines.

Never add power or ground nets, multi-drop buses (I2C, CAN, RS-485, shared SPI MISO/MOSI), or DDR address and
command nets (fly-by topology is intended branching).

## 4. Override file (optional, existing format, doc 15 §6.3)

When a detection is wrong (a mounting hole detected as HDMI, a header detected as USB), fix it at the source so
placement and the draft agree: write `<board>.tracemaker_rules.json`:

```json
{"version": 1,
 "deny":    {"H1": "hdmi_dp"},
 "assert":  {"J3": "usb2"},
 "disable": ["XTAL-04@Y2"],
 "set":     [{"rule": "DEC-01", "param": "max_mm", "value": 3}]}
```

Re-run step 1 with `--rules-override <file>` and review the new draft. The same file is read by
`tracemaker-place --rules-override` and by `tracemaker route --component-rules soft|on --rules-override` (it has no
effect on `route` with component rules off; the manifest is what `route --intent` reads).

## 5. Write the manifest

```json
{"version": 1,
 "board": "my_board.kicad_pcb",
 "comment": "Reviewed <date>: 7 decoupling pairs kept, 2 removed (LDO caps), USB data and crystal no-branch",
 "connections":    [{"from": "C3.1", "to": "U1.7", "why": "100 nF on U1 VDD (pin 7, power_in), schematic sheet mcu"}],
 "plane_pads":     [{"pad": "C3.2", "why": "C3 ground return, GND plane on In1.Cu"}],
 "no_branch_nets": [{"net": "/mcu/USB_D+", "why": "USB 2.0 data J1 -> U4 ESD -> U1"}]}
```

- Pads are `REF.NUM` exactly as `rules.json`/`board.json` print them; nets exactly as named on the board
  (hierarchical names include the sheet path).
- Every entry carries a `why` naming its evidence. An entry without evidence does not belong in the file.
- Unknown keys, unknown pads/nets, pads on different nets or a pad without a net are errors, by design.

## 6. Validate

```
tracemaker route BOARD.kicad_pcb --project BOARD.kicad_pro --intent BOARD.intent.json -o /tmp/check.kicad_pcb --work 1000000 --no-kb
```

The log line `routing intent: N connection(s), M plane pad(s), K no-branch net(s)` confirms the file resolved;
an error names the entry to fix. For benchmark boards, `bench/intent_quality.py` measures the result against the
same manifest (cap-to-pin track length, own-via lead, junctions on no-branch nets).

## 7. Review note (in chat, not a file)

List what you changed against the draft and why, what you could not decide (with the missing evidence), and
anything that needs a person: e.g. "U5 VDDA/VDD share C12; schematic does not say which pin it serves".
