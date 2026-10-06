#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Plane-aware routing evaluation (doc 05 §18-19): KiCad demo projects routed without and with --soft-zones and
--keep-vias-off-pads, each judged by KiCad after a zone refill.

  bench/planes_eval.py TRACEMAKER_BINARY [--work 2000000] [--demos DIR/NAME ...] [--configs NAME=ARGS ...]

Boards are the demos' own projects with tracks and vias removed (bench/speed_ab.py prepare_demo), so their net
classes, custom rules and zones apply. Per board and configuration: routed connections, added vias, plane
connections, zones needing refill, and from `kicad-cli pcb drc --refill-zones` the unconnected items and the
errors added relative to the stripped input (unconnected items excluded). Vias touching an SMD pad smaller than
--small-pad-mm in both dimensions are counted with KiCad's own shapes when KiCad's Python (pcbnew) is available
(TM_KICAD_PYTHON, or the macOS KiCad.app default).
"""
import argparse
import collections
import json
import os
import pathlib
import subprocess

from speed_ab import prepare_demo

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEMOS = ["stickhub/StickHub", "multichannel/multichannel_mixer", "interf_u/interf_u", "pic_programmer/pic_programmer",
         "complex_hierarchy/complex_hierarchy", "royalblue54L_feather/RoyalBlue54L-Feather", "cm5_minima/CM5_MINIMA_3",
         "kit-dev-coldfire-xilinx_5213/kit-dev-coldfire-xilinx_5213"]
CONFIGS = ["base=", "soft=--soft-zones", "soft+vop=--soft-zones --keep-vias-off-pads"]
KICAD_PYTHON = os.environ.get("TM_KICAD_PYTHON",
                              "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3")
SMALL_PAD_VIAS = """
import sys, pcbnew
b = pcbnew.LoadBoard(sys.argv[1])
limit = int(float(sys.argv[2]) * 1e6)
pads = [p for p in b.GetPads() if p.GetAttribute() == pcbnew.PAD_ATTRIB_SMD]
n = 0
for v in b.GetTracks():
    if v.Type() != pcbnew.PCB_VIA_T:
        continue
    for p in pads:
        layer = pcbnew.F_Cu if p.IsOnLayer(pcbnew.F_Cu) else pcbnew.B_Cu
        size = p.GetSize(layer)
        if size.x >= limit or size.y >= limit or not v.IsOnLayer(layer):
            continue
        if p.GetEffectiveShape(layer).Collide(v.GetEffectiveShape(layer), 0):
            n += 1
            break
print(n)
"""


def kicad_drc(pcb: pathlib.Path) -> tuple[collections.Counter, int]:
    out = pcb.with_suffix(".drc.json")
    subprocess.run(["kicad-cli", "pcb", "drc", "--refill-zones", "--severity-error", "--format", "json", "--output", str(out),
                    str(pcb)], capture_output=True, check=False)
    rep = json.loads(out.read_text())
    return collections.Counter(v["type"] for v in rep.get("violations", [])), len(rep.get("unconnected_items", []))


def small_pad_vias(pcb: pathlib.Path, limit_mm: float) -> int | None:
    if not pathlib.Path(KICAD_PYTHON).exists():
        return None
    p = subprocess.run([KICAD_PYTHON, "-c", SMALL_PAD_VIAS, str(pcb), str(limit_mm)], capture_output=True, text=True)
    return int(p.stdout.strip()) if p.returncode == 0 and p.stdout.strip().isdigit() else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("binary")
    ap.add_argument("--work", type=int, default=2_000_000)
    ap.add_argument("--demos", nargs="*", default=DEMOS)
    ap.add_argument("--small-pad-mm", type=float, default=2.0)
    ap.add_argument("--configs", nargs="*", default=CONFIGS, help="NAME=ARGS (args space-separated)")
    ap.add_argument("--out", default=str(ROOT / "build/planes_eval"))
    a = ap.parse_args()
    out = pathlib.Path(a.out)
    rows = []
    for demo in a.demos:
        board = prepare_demo(demo, out)
        base_errors, _ = kicad_drc(board)
        for cfg, args in [(c.split("=", 1)[0], c.split("=", 1)[1].split() if "=" in c else []) for c in a.configs]:
            pcb, summary = out / cfg / board.name, out / cfg / f"{board.stem}.json"
            pcb.parent.mkdir(parents=True, exist_ok=True)
            for ext in (".kicad_pro", ".kicad_dru"):  # KiCad's DRC reads the rules next to the board
                if board.with_suffix(ext).exists():
                    pcb.with_suffix(ext).write_bytes(board.with_suffix(ext).read_bytes())
            subprocess.run([a.binary, "route", str(board), "-o", str(pcb), "--json", str(summary), "--work", str(a.work), "--time",
                            "3600", "--threads", "1", "--variants", "1", "--no-kb", "--no-gpu"] + args, capture_output=True, check=False)
            s = json.loads(summary.read_text())
            errors, unconnected = kicad_drc(pcb)
            added = sum(max(0, n - base_errors[t]) for t, n in errors.items())
            rows.append({"board": board.stem, "config": cfg, "routed": s["routed"], "connections": s["connections"],
                         "vias": s["vias"], "plane_connections": s.get("plane_connections", 0),
                         "zones_needing_refill": s.get("zones_needing_refill", 0), "unconnected_after_refill": unconnected,
                         "added_errors": added, "added_by_type": {t: n - base_errors[t] for t, n in errors.items() if n > base_errors[t]},
                         "small_pad_vias": small_pad_vias(pcb, a.small_pad_mm)})
            r = rows[-1]
            print(f"{r['board']} {cfg}: routed {r['routed']}/{r['connections']}, vias {r['vias']}, unconnected after refill "
                  f"{unconnected}, added errors {added}, small-pad vias {r['small_pad_vias']}", flush=True)
    (out / "summary.json").write_text(json.dumps({"work": a.work, "rows": rows}, indent=1))
    print("\n| Board | Config | Routed | Vias | Plane connections | Zones to refill | Unconnected after refill | Added errors | "
          "Vias on small pads |")
    print("|---|---|--:|--:|--:|--:|--:|--:|--:|")
    for r in rows:
        print(f"| {r['board']} | {r['config']} | {r['routed']}/{r['connections']} | {r['vias']} | {r['plane_connections']} | "
              f"{r['zones_needing_refill']} | {r['unconnected_after_refill']} | {r['added_errors']} | "
              f"{'–' if r['small_pad_vias'] is None else r['small_pad_vias']} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
