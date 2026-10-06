#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Plane-aware routing evaluation (doc 05 §18-19): KiCad demo projects routed without and with --soft-zones and
--keep-vias-off-pads, judged by KiCad after a zone refill and scored against the demos' own hand routing.

  bench/planes_eval.py TRACEMAKER_BINARY [--work 2000000] [--demos DIR/NAME ...] [--pcbench NAME ...]
                       [--configs NAME=ARGS ...] [--no-original] [--out build/planes_eval]

Demo boards are the demos' own projects with tracks and vias removed (bench/speed_ab.py prepare_demo), so their
net classes, custom rules and zones apply. PCBench boards route the fixture's unrouted.kicad_pcb (zones already
removed by PCBench, so they test signal routing only) against the human raw.kicad_pcb. The hand-routed original
is scored the same way, labelled "original", as a guide: it is judged by the same rules and can lose. Each board
becomes a metric record (bench/quality.py record(): KiCad DRC after a zone refill, per-net geometry, plane
metrics before and after routing), written to OUT/records/BOARD.json and scored by bench/score.py. KiCad's
Python (pcbnew) is needed for the plane and small-pad metrics (TM_KICAD_PYTHON, or the macOS KiCad.app
default); without it those are null.
"""
import argparse
import json
import pathlib
import shutil
import subprocess

import quality
import score
from speed_ab import prepare_demo

ROOT = pathlib.Path(__file__).resolve().parent.parent
PCBENCH = ROOT / "bench/data/freerouting/scripts/benchmark/fixtures/PCBench"
DEMOS = ["stickhub/StickHub", "multichannel/multichannel_mixer", "interf_u/interf_u", "pic_programmer/pic_programmer",
         "complex_hierarchy/complex_hierarchy", "royalblue54L_feather/RoyalBlue54L-Feather", "cm5_minima/CM5_MINIMA_3",
         "kit-dev-coldfire-xilinx_5213/kit-dev-coldfire-xilinx_5213"]
CONFIGS = ["base=", "soft=--soft-zones", "soft+vop=--soft-zones --keep-vias-off-pads"]


def copy_project(src: pathlib.Path, dst: pathlib.Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    for ext in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):  # KiCad's DRC reads the rules next to the board
        if src.with_suffix(ext).exists() and (ext != ".kicad_pcb" or src != dst):
            shutil.copyfile(src.with_suffix(ext), dst.with_suffix(ext))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("binary")
    ap.add_argument("--work", type=int, default=2_000_000)
    ap.add_argument("--demos", nargs="*", default=DEMOS)
    ap.add_argument("--pcbench", nargs="*", default=[], help="PCBench fixture names")
    ap.add_argument("--small-pad-mm", type=float, default=2.0)
    ap.add_argument("--configs", nargs="*", default=CONFIGS, help="NAME=ARGS (args space-separated)")
    ap.add_argument("--no-original", action="store_true", help="do not score the hand-routed originals")
    ap.add_argument("--out", default=str(ROOT / "build/planes_eval"))
    a = ap.parse_args()
    quality.TM = pathlib.Path(a.binary)  # `tracemaker inspect` for the geometry metrics
    out = pathlib.Path(a.out)
    (out / "records").mkdir(parents=True, exist_ok=True)
    records = []
    boards = [(prepare_demo(d, out), ROOT / "bench/data/kicad/demos" / f"{d}.kicad_pcb") for d in a.demos]
    for name in a.pcbench:
        inp = out / "pcbench" / f"{name}.kicad_pcb"
        inp.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PCBENCH / name / "unrouted.kicad_pcb", inp)
        boards.append((inp, PCBENCH / name / "raw.kicad_pcb"))
    for board, hand in boards:
        recs = []
        if not a.no_original:
            orig = out / "original" / board.name
            copy_project(hand, orig)
            recs.append(quality.record(board, orig, "original", hand=True, small_pad_mm=a.small_pad_mm))
        for cfg, args in [(c.split("=", 1)[0], c.split("=", 1)[1].split() if "=" in c else []) for c in a.configs]:
            pcb, summary = out / cfg / board.name, out / cfg / f"{board.stem}.json"
            copy_project(board, pcb)
            subprocess.run([a.binary, "route", str(board), "-o", str(pcb), "--json", str(summary), "--work", str(a.work), "--time",
                            "3600", "--threads", "1", "--variants", "1", "--no-kb", "--no-gpu"] + args, capture_output=True, check=False)
            s = json.loads(summary.read_text())
            rec = quality.record(board, pcb, cfg, small_pad_mm=a.small_pad_mm)
            rec["router"] = {k: s.get(k) for k in ("routed", "connections", "plane_connections", "zones_needing_refill", "seconds")}
            rec["router"]["args"] = args
            recs.append(rec)
        for r in recs:
            print(f"{r['board']} {r['label']}: unconnected {r.get('unconnected')}, added errors {r.get('added_errors')}", flush=True)
        (out / "records" / f"{board.stem}.json").write_text(json.dumps(recs, indent=1))
        records += recs
    (out / "summary.json").write_text(json.dumps({"work": a.work, "configs": a.configs, "records": [r["file"] for r in records]}, indent=1))
    print()
    print(score.markdown(records, hand_label=None if a.no_original else "original"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
