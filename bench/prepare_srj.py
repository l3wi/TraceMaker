#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Convert tscircuit's Simple Route JSON BGA breakout problems (doc 10 §7) into bench/run.py fixtures:
<out>/<set>/<sample>/unrouted.kicad_pcb with its project, a link to the problem and the conversion summary.

  bench/prepare_srj.py [--set srj19 --set srj20] [--clearance-mm 0.1] [--hole-to-hole-mm 0.1] [--out bench/data/tscircuit/boards]

The problems state no clearance. The benchmark sets use the two distances of tscircuit's published pass criterion
("relaxed DRC"): 0.1 mm between copper of different nets and 0.1 mm between via holes, so KiCad's judge and theirs
test the same distances. `--clearance-mm default --hole-to-hole-mm default` uses `tracemaker srj-import`'s own
defaults instead (the problem's defaultObstacleMargin, else 0.15 mm; no hole-to-hole rule).

Every board is checked: the connections the problem asks for must equal the ratsnest the board implies and the
connections `tracemaker route` reports for it. Exit code 1 if any board fails to convert or to pass the check.
"""
import argparse
import concurrent.futures as cf
import json
import hashlib
import os
import pathlib
import subprocess
import sys

from prepare_inputs import fixture_transaction

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "bench/data/tscircuit"
TM = pathlib.Path(os.environ.get("TM_BINARY", ROOT / "build/release/src/app/tracemaker"))
SETS = {"srj19": "dataset-srj19", "srj20": "dataset-srj20"}


def convert(job: tuple[pathlib.Path, pathlib.Path, str, str, bool]) -> dict:
    problem, dst, clearance, hole, verify = job
    res = {"board": dst.name}
    metadata = {"board": dst.name, "human_available": False, "refilled": False, "report": "import.json"}
    try:
        with fixture_transaction(dst, metadata) as stage:
            board = stage / "unrouted.kicad_pcb"
            # Import exactly the frozen problem bytes recorded in the manifest.
            (stage / "problem.json").write_bytes(problem.read_bytes())
            cmd = [str(TM), "srj-import", str(stage / "problem.json"), "-o", str(board), "--json", str(stage / "import.json")]
            if clearance != "default":
                cmd += ["--clearance-mm", clearance]
            if hole != "default":
                cmd += ["--hole-to-hole-mm", hole]
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if p.returncode != 0:
                raise RuntimeError((p.stderr or p.stdout).strip()[-300:])
            imp = json.loads((stage / "import.json").read_text())
            res.update(srj_pairs=imp["srj_pairs"], board_pairs=imp["board_pairs"], warnings=imp["warnings"])
            if res["srj_pairs"] != res["board_pairs"]:
                raise ValueError(f'connection count mismatch: problem {res["srj_pairs"]}, board {res["board_pairs"]}')
            if verify:
                # One expansion counts the requested connections; no routed output is retained.
                rj = stage / "count.route.json"
                counted = stage / "count.kicad_pcb"
                p = subprocess.run([str(TM), "route", str(board), "-o", str(counted), "--work", "1",
                                    "--variants", "1", "--no-gpu", "--no-kb", "--json", str(rj)],
                                   capture_output=True, text=True, timeout=600)
                if p.returncode not in (0, 3) or not rj.exists():
                    raise RuntimeError(f'connection-count route failed: {p.stderr or p.stdout}')
                res["route_connections"] = json.loads(rj.read_text())["connections"]
                if res["route_connections"] != res["srj_pairs"]:
                    raise ValueError(f'connection count mismatch: problem {res["srj_pairs"]}, route {res["route_connections"]}')
                for path in (rj, counted, counted.with_suffix(".kicad_pro"), counted.with_suffix(".kicad_dru")):
                    path.unlink(missing_ok=True)
                imp["route_connections"] = res["route_connections"]
                (stage / "import.json").write_text(json.dumps(imp, indent=1) + "\n")
            metadata.update(res, source=str(problem),
                            source_sha256=hashlib.sha256((stage / "problem.json").read_bytes()).hexdigest(),
                            connections_verified=verify)
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
        res["error"] = str(error)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", action="append", choices=sorted(SETS), help="default: both")
    ap.add_argument("--clearance-mm", default="0.1", help="copper clearance in mm, or 'default' for srj-import's own")
    ap.add_argument("--hole-to-hole-mm", default="0.1", help="drill-to-drill clearance in mm, or 'default' for srj-import's own")
    ap.add_argument("--out", default=str(DATA / "boards"))
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--no-verify", action="store_true", help="skip the connection count reported by `tracemaker route`")
    a = ap.parse_args()
    bad = 0
    for name in a.set or sorted(SETS):
        src = DATA / SETS[name] / "circuits"
        problems = sorted(src.glob("*/*.simple-route.json"))
        if not problems:
            print(f"{name}: no problems in {src} (run scripts/fetch_fixtures.sh tscircuit)")
            bad += 1
            continue
        out = pathlib.Path(a.out).resolve() / name
        jobs = [(p, out / p.parent.name, a.clearance_mm, a.hole_to_hole_mm, not a.no_verify) for p in problems]
        with cf.ThreadPoolExecutor(a.jobs) as ex:
            rows = list(ex.map(convert, jobs))
        failed = [r for r in rows if "error" in r]
        mismatch = [r for r in rows if "error" not in r and (r["srj_pairs"] != r["board_pairs"] or
                    (not a.no_verify and r["route_connections"] != r["srj_pairs"]))]
        for r in failed:
            print(f"  FAILED {r['board']}: {r['error']}")
        for r in mismatch:
            print(f"  COUNT  {r['board']}: problem {r['srj_pairs']}, board {r['board_pairs']}, route {r.get('route_connections')}")
        ok = len(rows) - len(failed) - len(mismatch)
        print(f"{name}: {len(rows)} problems, {len(rows) - len(failed)} converted, {ok} pass the connection count check, "
              f"{sum(r.get('srj_pairs', 0) for r in rows)} connections -> {out}")
        bad += len(failed) + len(mismatch)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
