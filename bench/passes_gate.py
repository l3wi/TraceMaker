#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Gate of the after-route passes (doc 05 §39): every board is routed twice with the same work budget, once
without and once with the pass options, and KiCad judges both outputs.

  bench/passes_gate.py --name RUN --fixtures DIR --boards A B ... [--refill] [--work N] [--route-args "..."]
                       --pass-args="--widen-power --widen-net GND=0.5"     (write --pass-args=... : the value starts with a dash)

The routing itself is the same in both runs (the passes work on the finished result), so every difference in
KiCad's report is the pass's doing. Per board: unconnected items, DRC violations by type (errors and warnings),
`via_dangling` items (KiCad's "via is not connected or connected on only one layer"), and what the pass reports.
Exit status 1 if a board gains a violation of any type or severity, or an unconnected item. Boards and reports are
written to build/passes-gate/<RUN> (kicad-cli cannot read /tmp).
"""
import argparse
import concurrent.futures as cf
import json
import pathlib
import shutil
import subprocess
import sys
from drc_judge import capped_types, instance_counts, violations
import run as bench_run

ROOT = pathlib.Path(__file__).resolve().parent.parent
PCBENCH = ROOT / "bench/data/freerouting/scripts/benchmark/fixtures/PCBench"


def run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def judge(board: pathlib.Path, refill: bool) -> dict | None:
    bench_run.REFILL = refill
    saved = board.with_name(board.stem + ".refilled.kicad_pcb") if refill else None
    result = bench_run.drc(board, saved=saved)
    if result is None:
        return None
    d = result["report"]
    result.update(violations=instance_counts(violations(d, severity=None), severity_labels=True),
                  capped=capped_types(d),
                  via_dangling=sum(1 for v in d.get("violations", []) if v["type"] == "via_dangling"))
    return result


def one(name: str, a: argparse.Namespace, outdir: pathlib.Path) -> dict:
    src = pathlib.Path(a.fixtures) / name / "unrouted.kicad_pcb"
    res: dict = {"board": name}
    for tag, extra in (("off", []), ("on", a.pass_args.split())):
        out = outdir / f"{name}.{tag}.kicad_pcb"
        for ext in (".kicad_pro", ".kicad_dru"):
            if src.with_suffix(ext).exists():
                shutil.copyfile(src.with_suffix(ext), out.with_suffix(ext))
        js = outdir / f"{name}.{tag}.route.json"
        p = run([str(a.binary), "route", str(src), "-o", str(out), "--work", str(a.work), "--no-kb", "--time", "3600", "--threads", "1",
                 "--json", str(js)] + a.route_args.split() + extra, 7200)
        if p.returncode not in (0, 3) or not out.exists():
            res["error"] = f"route {tag}: exit {p.returncode}: {(p.stderr or p.stdout)[-300:]}"
            return res
        r = json.loads(js.read_text())
        res[tag] = {"routed": r["routed"], "connections": r["connections"], "tracks": r["tracks"], "vias": r["vias"],
                    "judge": judge(out, a.refill), **{k: r[k] for k in ("widen", "stitch", "style") if k in r}}
        if res[tag]["judge"] is None:
            res["error"] = f"kicad-cli failed on the {tag} board"
            return res
    off, on = res["off"]["judge"], res["on"]["judge"]
    metrics = {"exit": 0}
    bench_run.apply_judgment(metrics, off, on)
    res.update({key: value for key, value in metrics.items() if key != "exit"})
    if metrics["judge"] != "ok":
        return res
    excluded = set(capped_types(off["report"])) | set(capped_types(on["report"]))
    off_instances, on_instances = violations(off["report"], severity=None), violations(on["report"], severity=None)
    res["added"] = instance_counts({key: n for key, n in (on_instances-off_instances).items()
                                   if key[0] not in excluded}, severity_labels=True)
    res["removed"] = instance_counts({key: n for key, n in (off_instances-on_instances).items()
                                     if key[0] not in excluded}, severity_labels=True)
    for stage, judgment in (("before", off), ("after", on)):
        judgment["unconnected"] = metrics.get("unconnected_uncapped", {}).get(stage, judgment["unconnected"])
    unknown_connectivity = metrics["censored"]
    res["pass"] = (not res["added"] and not (excluded - {"unconnected_items"}) and not unknown_connectivity
                   and on["unconnected"] <= off["unconnected"] and res["on"]["routed"] == res["off"]["routed"])
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True)
    ap.add_argument("--fixtures", default=str(PCBENCH), help="directory with <board>/unrouted.kicad_pcb (default: PCBench)")
    ap.add_argument("--boards", nargs="+", required=True)
    ap.add_argument("--work", type=int, default=1_000_000)
    ap.add_argument("--refill", action="store_true", help="KiCad refills zones before judging (boards with planes)")
    ap.add_argument("--route-args", default="--variants 1 --no-gpu", help="route options of both runs")
    ap.add_argument("--pass-args", required=True, help="the pass options, added in the second run only")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--binary", default=str(ROOT / "build/release/src/app/tracemaker"))
    a = ap.parse_args()
    outdir = ROOT / "build/passes-gate" / a.name
    outdir.mkdir(parents=True, exist_ok=True)
    with cf.ThreadPoolExecutor(a.jobs) as ex:
        rows = list(ex.map(lambda n: one(n, a, outdir), a.boards))
    (outdir / "gate.json").write_text(json.dumps({"pass_args": a.pass_args, "route_args": a.route_args, "work": a.work, "refill": a.refill, "boards": rows}, indent=1))
    ok = True
    for r in rows:
        if "error" in r:
            print(f"ERROR {r['board']}: {r['error']}")
            ok = False
            continue
        off, on = r["off"], r["on"]
        ok = ok and r["pass"]
        line = (f"{'pass' if r['pass'] else 'FAIL'} {r['board'][:40]:40} routed {off['routed']}/{off['connections']} -> {on['routed']}/{on['connections']}  "
                f"unconnected {off['judge']['unconnected']} -> {on['judge']['unconnected']}  via_dangling {off['judge']['via_dangling']} -> "
                f"{on['judge']['via_dangling']}  added {r['added'] or '-'}  removed {r['removed'] or '-'} "
                f"capped {r['capped_types']} error gate {r['error_gate']} connectivity {'unknown' if r['censored'] else 'known'}")
        if "widen" in on:
            w = on["widen"]
            line += f"  | widened {w['tracks_widened']} tracks, {w['widened_mm']:.1f} mm, {sum(1 for n in w['nets'] if n['tracks_widened'])} nets"
        if "stitch" in on:
            s = on["stitch"]
            line += f"  | stitch vias {s['plane_vias']} plane + {s['return_path_vias']} return ({on['vias'] - off['vias']} more vias)"
            for x in s["return_paths"]:
                line += f"; {x['net']}: unstitched {x['unstitched_before']} -> {x['unstitched_after']} of {x['signal_vias']}"
        print(line)
    print("Gate: " + ("pass" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
