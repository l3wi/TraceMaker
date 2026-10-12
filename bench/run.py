#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""TraceMaker benchmark harness (design doc 10).

Routes PCBench fixtures with TraceMaker, judges input and output with `kicad-cli pcb drc`, and compares with
Freerouting's own published per-board results (scripts/benchmark/results/benchmarks.json in the Freerouting
repository, judged by KiCad DRC on Freerouting's side).

  bench/run.py --tier A --limit 60 --time 60 --jobs 12 [--name run-id]
  bench/run.py --set quick --work 1000000 --refill --route-args="--variants 1 --soft-zones --keep-vias-off-pads --diff-pairs"
  bench/run.py --set modern-planes --human --refill --name human-modern
  bench/run.py --set srj19 --work 1000000 --route-args "--variants 1 --no-gpu"   # tscircuit BGA breakouts (doc 10 §7)

--work routes with a deterministic work budget and no knowledge base. --refill
judges and saves a fresh analysis copy as boards/<name>.refilled.kicad_pcb,
leaving the raw router output unchanged. --human uses the prepared human original.

Writes bench/results/<run-id>/{boards.jsonl, summary.json, report.md}; summary.json is shown on the progress site.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime
import hashlib
import functools
import json
import os
import pathlib
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter

from plane_metrics import copper_totals, plane_nets, unconnected_split
from prepare_inputs import copy_rules, refill_board, set_boards
from drc_judge import (JUDGE_CODE_VERSION, JUDGE_FLAGS, METRIC_SCHEMA_VERSION,
                       added_errors as instance_added_errors, completion, instance_counts, violations)
import kicad_connectivity

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIX = ROOT / "bench/data/freerouting/scripts/benchmark/fixtures/PCBench"
SRC_FIX = FIX  # --fixtures DIR: boards to place and route (e.g. set S); FIX stays the human reference for placement errors
FR_RESULTS = ROOT / "bench/data/freerouting/scripts/benchmark/results/benchmarks.json"
TM = pathlib.Path(os.environ.get("TM_BINARY", ROOT / "build/release/src/app/tracemaker"))
EXTRA = os.environ.get("TM_ROUTE_ARGS", "").split()  # extra route options for experiments, e.g. "--via-cost-mm 3"
THREADS = 1
PLACE_THREADS = 1
WORK = 0         # --work N: deterministic work budget per board (0: wall-clock --time only)
REFILL = False   # --refill: KiCad refills zones before judging
HUMAN = False    # --human: judge the prepared routed original without running the router
INTENTS = None   # --intents DIR: route each board with DIR/<board>.intent.json (drafted from its input when missing)
DRC_CACHE = ROOT / "build/drc/kicad"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@functools.cache
def kicad_version():
    result = subprocess.run(["kicad-cli", "--version"], capture_output=True, text=True, timeout=30, check=True)
    return result.stdout.strip()


def atomic_json(path, data):
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.part")
    temporary.write_text(json.dumps(data, indent=1) + "\n")
    os.replace(temporary, path)


def added_errors(before, after):
    return instance_added_errors(before["report"], after["report"])


def attach_connectivity(judgments):
    """Count exactly the saved geometry KiCad judged, once for each board pair."""
    judgments = [j for j in judgments if not
                 (j["report"].get("connectivity") is not None or j["report"].get("connectivity_unavailable"))]
    if not judgments:
        return
    paths = [pathlib.Path(j["analysis_board"]) for j in judgments if j.get("analysis_board")]
    try:
        counts = kicad_connectivity.count_unconnected(paths) if paths else {}
    except kicad_connectivity.ConnectivityUnavailable as error:
        counts = {}
        reason = str(error)
    else:
        reason = "connectivity requires a saved refilled analysis board (--refill)"
    for judgment in judgments:
        path = judgment.get("analysis_board")
        if path is not None and path in counts:
            judgment["report"]["connectivity"] = counts[path]
        else:
            judgment["report"]["connectivity_unavailable"] = reason


def apply_judgment(res, before, after):
    """Shared by routing and non-mutating rejudgment; mismatch is a judge failure."""
    attach_connectivity((before, after))
    res.update(added_errors(before, after))
    res["judge_cache_keys"] = {"before": before.get("cache_key"), "after": after.get("cache_key")}
    res.update(unconnected_before=before["unconnected"], unconnected_after=after["unconnected"])
    res["judge_reports"] = {"before": before.get("drc_report"), "after": after.get("drc_report")}
    try:
        res.update(completion(before["unconnected"], after["unconnected"],
                              before_connectivity=before["report"].get("connectivity"),
                              after_connectivity=after["report"].get("connectivity")))
    except ValueError as error:
        res.update(judge="failed", clean=False, completion=None, error=str(error))
        return
    res["connectivity_unavailable"] = {
        stage: j["report"]["connectivity_unavailable"] for stage, j in (("before", before), ("after", after))
        if j["report"].get("connectivity_unavailable")}
    res["judge"] = "ok"
    remaining = res.get("unconnected_uncapped", {}).get("after", after["unconnected"])
    res["clean"] = (res.get("exit") in (0, 3) and remaining == 0
                    and res["error_gate"] == "pass" and not res.get("place_added")
                    and res.get("place_judge") not in ("failed", "unknown"))


def connectivity_provenance(rows):
    exact = [r for r in rows if r.get("connectivity_method")]
    return dict(connectivity_method=sorted({r["connectivity_method"] for r in exact}),
                kicad_python_version=sorted({r["kicad_python_version"] for r in exact}),
                connectivity_unavailable={r["board"]: r["connectivity_unavailable"]
                                          for r in rows if r.get("connectivity_unavailable")})


def fixture_provenance(name, *, legacy=False):
    fixture = SRC_FIX / name
    manifest = fixture / "preparation.json"
    data = json.loads(manifest.read_text())
    if data.get("state") == "valid" and data.get("files"):
        required = {"unrouted.kicad_pcb"}
        if HUMAN:
            required.add("routed.kicad_pcb")
        required.update(path.name for stem in ("unrouted", "routed") for ext in (".kicad_pro", ".kicad_dru")
                        if (path := fixture / (stem + ext)).exists())
        if not required <= data["files"].keys():
            raise ValueError(f"{name}: preparation manifest omits board/rule hashes")
        for filename, expected in data["files"].items():
            path = fixture / filename
            if pathlib.Path(filename).is_absolute() or ".." in pathlib.Path(filename).parts:
                raise ValueError(f"{name}: invalid preparation filename {filename}")
            if not path.is_file() or digest(path) != expected:
                raise ValueError(f"{name}: preparation hash mismatch: {filename}")
    elif legacy and data.get("refilled") and data.get("input_sha256") == digest(fixture / "unrouted.kicad_pcb"):
        # Explicit historical human rejudgment only: prove the preserved human is
        # the source that the successful legacy preparation actually recorded.
        if digest(fixture / "routed.kicad_pcb") != data.get("source_sha256"):
            raise ValueError(f"{name}: legacy human source hash mismatch")
    else:
        raise ValueError(f"{name}: preparation is not validated; prepare the set first")
    board = fixture / "unrouted.kicad_pcb"
    return dict(board_sha256=digest(board), manifest_sha256=digest(manifest),
                rules={ext: digest(board.with_suffix(ext)) for ext in (".kicad_pro", ".kicad_dru")
                       if board.with_suffix(ext).exists()})


def headline(rows, scheduled):
    judged = [r for r in rows if r.get("judge") == "ok"]
    known = [r["completion"] for r in judged if r.get("completion") is not None]
    return dict(boards=scheduled, scheduled=scheduled, attempted=len(rows), judged=len(judged),
                clean_pass=round(sum(bool(r.get("clean")) for r in rows) / scheduled, 4) if scheduled else None,
                completion=round(sum(known) / len(known), 4) if known else None,
                pooled_completion=(1 - sum(r["unconnected_uncapped"]["after"] for r in judged
                                          if "unconnected_uncapped" in r)
                                   / sum(r["unconnected_uncapped"]["before"] for r in judged
                                         if "unconnected_uncapped" in r))
                    if sum(r.get("unconnected_uncapped", {}).get("before", 0) for r in judged) else None,
                completion_boards=len(known), censored_boards=sum(bool(r.get("censored")) for r in rows),
                judge_list_censored_boards=sum(bool(r.get("judge_list_censored")) for r in rows),
                error_gate_unknown_boards=sum(r.get("error_gate") == "unknown" for r in rows),
                judge_failures=scheduled-len(judged),
                infrastructure_failures=sum(r.get("judge") != "ok" or r.get("exit") not in (0, 3)
                                            or r.get("place_judge") == "failed" for r in rows),
                seconds=round(sum(r.get("seconds", r.get("wall_s", 0)) for r in rows), 1),
                cpu_seconds=round(sum(r.get("cpu_s", 0) for r in rows), 1))


def attempt_board(name, outdir, time_limit):
    try:
        return run_board(name, outdir, time_limit)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        return dict(board=name, judge="failed", error=str(error), exit=-1)


def fr_baseline() -> dict[str, dict]:
    """Per-board Freerouting outcome by version label."""
    if not FR_RESULTS.exists():
        return {}
    d = json.loads(FR_RESULTS.read_text())
    out: dict[str, dict] = {}
    for r in d["runs"]:
        fx = r.get("fixture", {})
        if fx.get("group") != "PCBench":
            continue
        name = fx["relative_path"].split("/")[1]
        q = r.get("quality", {})
        ver = r["binary"]["version_label"]
        out.setdefault(name, {"tier": fx.get("tier")})[ver] = {
            "unrouted": q.get("unrouted_connections"), "violations": q.get("clearance_violations"),
            "clean": q.get("unrouted_connections") == 0 and q.get("clearance_violations") == 0,
            "seconds": q.get("wall_clock_seconds"), "timed_out": r.get("exit", {}).get("timed_out")}
    return out


def drc(path: pathlib.Path, timeout: int = 600, planes: dict | None = None,
        saved: pathlib.Path | None = None) -> dict | None:
    DRC_CACHE.mkdir(parents=True, exist_ok=True)
    rules = {ext: digest(path.with_suffix(ext)) for ext in (".kicad_pro", ".kicad_dru")
             if path.with_suffix(ext).exists()}
    identity = dict(board_sha256=digest(path), rules=rules, kicad_version=kicad_version(),
                    judge_flags=JUDGE_FLAGS, refill=REFILL, judge_code_version=JUDGE_CODE_VERSION,
                    repair_code_sha256=digest(ROOT / "bench/prepare_inputs.py"),
                    judge_code_sha256=digest(ROOT / "bench/drc_judge.py"))
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    out = DRC_CACHE / f"{key}.json"
    filled = DRC_CACHE / f"{key}.refilled.kicad_pcb"
    if not out.exists() or (REFILL and not filled.exists()):
        # A private report is validated before atomic rename. Never modify the input
        # or raw output: KiCad saves only its private analysis copy.
        part = DRC_CACHE / f"{key}.{os.getpid()}.{threading.get_ident()}.part.json"
        try:
            repair_metadata = {}
            if REFILL:
                repair_metadata = refill_board(path, filled, part, timeout) or {}
            else:
                result = subprocess.run(["kicad-cli", "pcb", "drc", "--format", "json", "--severity-all",
                                         "--all-track-errors", "-o", str(part), str(path)],
                                        capture_output=True, timeout=timeout)
                if result.returncode != 0:
                    return None
            if not part.exists():
                return None
            json.loads(part.read_text())
            os.replace(part, out)
            atomic_json(DRC_CACHE / f"{key}.provenance.json", dict(identity, **repair_metadata))
        except (subprocess.TimeoutExpired, RuntimeError, OSError, ValueError):
            return None
        finally:
            part.unlink(missing_ok=True)
    if REFILL and saved is not None:
        copy_rules(path, saved)
        temporary = saved.with_name(f".{saved.name}.{os.getpid()}.{threading.get_ident()}.part")
        shutil.copyfile(filled, temporary)
        os.replace(temporary, saved)
    d = json.loads(out.read_text())
    c = Counter(v["type"] for v in d.get("violations", []) if v.get("severity") == "error")
    # Router-introduced = violations involving a track, arc or via. Fixed design
    # vias can already occur in the input; their baseline errors are subtracted.
    # Pad/footprint-only violations are retained as diagnostics below.
    def routed_copper(v):
        return any(i.get("description", "").startswith(("Track", "Via", "Arc")) for i in v.get("items", []))
    r = Counter(v["type"] for v in d.get("violations", []) if v.get("severity") == "error" and routed_copper(v))
    # Violations between items of one footprint (pads, its copper graphics) cannot be caused by moving it: the
    # footprint is rigid. KiCad still re-classifies some of them (short <-> clearance) after a rotation, so
    # placement-added errors count only violations that involve two footprints or board items.
    def own_footprint(v):
        refs = [re.search(r" of ([^ ]+)(?: on [^ ]+)?$", i.get("description", "")) for i in v.get("items", [])]
        return bool(refs) and all(refs) and len({m.group(1) for m in refs}) == 1
    x = Counter(v["type"] for v in d.get("violations", []) if v.get("severity") == "error" and not own_footprint(v))
    result = {"errors": dict(c), "routed_errors": dict(r), "cross_errors": dict(x),
              "unconnected": len(d.get("unconnected_items", [])), "report": d, "cache_key": key,
              "drc_report": str(out), "provenance": identity,
              "cross_report": {"violations": [v for v in d.get("violations", []) if not own_footprint(v)]}}
    if REFILL:
        result["analysis_board"] = str(saved if saved is not None else filled)
    if planes is not None:
        result.update(unconnected_split(d.get("unconnected_items", []), planes))
    return result


PLACE = ROOT / "build/release/src/place/tracemaker-place"
PLACE_MODE = None        # --place MODE: TraceMaker may move components before routing
PLACE_TIMEOUT = 900      # seconds; on timeout the human placement is routed (recorded)
PLACE_WORK = 3_000_000   # router budget per placement evaluation (deterministic)
PLACE_ARGS: list[str] = []  # --place-args: extra tracemaker-place options (e.g. --scratch for set S)
PLACE_CRULES = None      # --place-crules MODE: tracemaker-place --component-rules (doc 15); None = its default
# Errors placement can introduce (counted against the human board when parts move).
PLACEMENT_ERRORS = {"courtyards_overlap", "pth_inside_courtyard", "npth_inside_courtyard", "copper_edge_clearance", "clearance",
                    "shorting_items", "solder_mask_bridge", "hole_clearance", "hole_to_hole", "malformed_courtyard"}


def place_board(name: str, src: pathlib.Path, outdir: pathlib.Path, res: dict) -> pathlib.Path:
    """Runs tracemaker-place; returns the board to route (the placed one, or the input if placement failed)."""
    placed = outdir / "boards" / f"{name}.placed.kicad_pcb"
    js = outdir / "boards" / f"{name}.placed.json"
    t0 = time.time()
    try:
        p = subprocess.run([str(PLACE), str(src), "-o", str(placed), "--mode", PLACE_MODE, "--route-check", str(PLACE_WORK),
                            "--threads", str(PLACE_THREADS), "--json", str(js)]
                           + (["--component-rules", PLACE_CRULES] if PLACE_CRULES else []) + PLACE_ARGS
                           + (["--loop-time", str(int(PLACE_TIMEOUT * 0.6))] if PLACE_MODE == "routable" else []), capture_output=True, text=True, timeout=PLACE_TIMEOUT)
        res["place_exit"] = p.returncode
    except subprocess.TimeoutExpired:
        res["place_exit"] = -1
    res["place_s"] = round(time.time() - t0, 1)
    if res["place_exit"] not in (0, 2) or not placed.exists():
        res["place"] = "failed or timed out: human placement routed"
        return src
    try:
        j = json.loads(js.read_text())
        res["place"] = "ok"
        res["moved"] = j.get("moved")
        res["hpwl_before"] = j.get("before", {}).get("hpwl_mm")
        res["hpwl_after"] = j.get("after", {}).get("hpwl_mm")
    except (OSError, ValueError):
        res["place"] = "ok (no report)"
    copy_rules(src, placed)
    # Errors the placement itself introduced (any items), against the human board.
    ref = FIX / name / "unrouted.kicad_pcb"
    human, moved = drc(ref if ref.exists() else src), drc(placed)
    if human and moved:
        caps = instance_added_errors(human["report"], moved["report"])["capped_types"]
        if set(caps) & PLACEMENT_ERRORS:
            res["place_judge"] = "unknown"
        res["place_added"] = place_added(human, moved)
    else:
        res["place_judge"] = "failed"
    return placed


def place_added(human: dict, moved: dict) -> dict:
    capped = set(instance_added_errors(human["report"], moved["report"])["capped_types"])
    added = violations(moved["cross_report"]) - violations(human["cross_report"])
    return {t: n for t, n in instance_counts(added).items() if t in PLACEMENT_ERRORS and t not in capped}


def dead_pins(board: pathlib.Path, out_json: pathlib.Path) -> int | None:
    """Pins of dense packages that cannot escape under the board's own rules (`tracemaker escape`, doc 05 §12):
    a board with any is not routable clean by any router."""
    try:
        subprocess.run([str(TM), "escape", str(board), "--json", str(out_json)], capture_output=True, timeout=300)
        return int(json.loads(out_json.read_text())["dead"])
    except (subprocess.TimeoutExpired, OSError, ValueError, KeyError):
        return None


STYLE_COUNTS = ("connections", "segments", "vias", "bends", "acute_angles", "stair_steps", "short_segments", "off_angle_segments",
                "long_detours", "length_mm", "detour_length_mm", "detour_octile_mm")


def style_of(board: pathlib.Path, out_json: pathlib.Path) -> dict | None:
    """Style metric of every track of a board (`tracemaker style`, doc 05 §39), e.g. a fixture's reference routing."""
    try:
        subprocess.run([str(TM), "style", str(board), "--json", str(out_json)], capture_output=True, timeout=300)
        return json.loads(out_json.read_text())
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None


def style_total(styles: list[dict]) -> dict | None:
    """Sums the per-board style metrics; the two ratios are recomputed from the sums."""
    if not styles:
        return None
    t = {k: round(sum(s.get(k, 0) for s in styles), 6) for k in STYLE_COUNTS}
    t["boards"] = len(styles)
    t["bends_per_connection"] = round(t["bends"] / t["connections"], 4) if t["connections"] else 0.0
    t["detour_ratio"] = round(t["detour_length_mm"] / t["detour_octile_mm"], 4) if t["detour_octile_mm"] else 1.0
    return t


def run_measured(cmd: list[str], timeout: float) -> tuple[int, str, str, float]:
    """Runs cmd and returns (exit code, stdout, stderr, CPU seconds). CPU time is the child's own user + system time
    from os.wait4, so other boards routing at the same time do not count. Raises subprocess.TimeoutExpired."""
    with tempfile.TemporaryFile("w+") as fo, tempfile.TemporaryFile("w+") as fe:
        p = subprocess.Popen(cmd, stdout=fo, stderr=fe, text=True)
        killed = threading.Event()

        def kill() -> None:
            killed.set()
            p.kill()

        timer = threading.Timer(timeout, kill)
        timer.start()
        try:
            _, status, usage = os.wait4(p.pid, 0)
        finally:
            timer.cancel()
        p.returncode = os.waitstatus_to_exitcode(status)  # reaped here: Popen must not wait again
        if killed.is_set():
            raise subprocess.TimeoutExpired(cmd, timeout)
        fo.seek(0)
        fe.seek(0)
        return p.returncode, fo.read(), fe.read(), usage.ru_utime + usage.ru_stime


def run_board(name: str, outdir: pathlib.Path, time_limit: float) -> dict:
    src0 = SRC_FIX / name / "unrouted.kicad_pcb"
    out = outdir / "boards" / f"{name}.kicad_pcb"
    out.parent.mkdir(parents=True, exist_ok=True)
    # Route and judge use the fixture's original net classes/custom rules.
    copy_rules(src0, out)
    res = {"board": name, "metric_schema_version": METRIC_SCHEMA_VERSION,
           "input_sha256": digest(src0)}
    preparation = src0.with_name("preparation.json")
    if preparation.exists():
        res["preparation"] = json.loads(preparation.read_text())
    if HUMAN:
        res["human"] = True
    src = place_board(name, src0, outdir, res) if PLACE_MODE and not HUMAN else src0
    try:
        planes = plane_nets(src0)
        res["plane_nets"] = planes
    except ValueError as error:
        # A malformed outline must not change routing or suppress the KiCad judge.
        planes = None
        res["plane_metrics_error"] = str(error)
    if not HUMAN:
        res["dead_pins"] = dead_pins(src, outdir / "boards" / f"{name}.escape.json")
    t0 = time.time()
    if HUMAN:
        human = SRC_FIX / name / "routed.kicad_pcb"
        shutil.copyfile(human, out)
        copy_rules(human, out)
        res["exit"] = 0
    else:
        try:
            # --work: deterministic; --time stays a safety stop.
            budget = ["--work", str(WORK), "--no-kb"] if WORK else ["--kb", str(outdir / "kb.sqlite")]
            intent = []
            if INTENTS is not None:
                man = INTENTS / f"{name}.intent.json"
                if not man.exists():
                    subprocess.run([str(TM), "rules", str(src), "--intent-draft", str(man)], check=True, capture_output=True)
                intent = ["--intent", str(man)]
                res["intent"] = str(man)
            cmd = [str(TM), "route", str(src), "-o", str(out), "--time", str(time_limit), "--threads", str(THREADS),
                   "--json", str(out) + ".route.json"] + budget + intent + EXTRA
            res["effective_args"] = cmd
            res["argument_file_sha256"] = {arg: digest(pathlib.Path(arg)) for arg in cmd[2:]
                                           if pathlib.Path(arg).is_file()}
            if intent:
                res["intent_sha256"] = digest(man)
            code, stdout, stderr, cpu = run_measured(cmd, time_limit * 3 + 60)
            res["exit"] = code
            res["cpu_s"] = round(cpu, 2)
            if code not in (0, 3):
                res["error"] = (stderr or stdout)[-500:]
        except subprocess.TimeoutExpired:
            res["exit"] = -1
            res["error"] = "timeout"
    res["wall_s"] = round(time.time() - t0, 2)
    rj = pathlib.Path(str(out) + ".route.json")
    if rj.exists() and not HUMAN:
        r = json.loads(rj.read_text())
        res.update({k: r[k] for k in ("routed", "connections", "tracks", "vias", "seconds")})
        # Preserve whole-job and winning-variant timing/work, style and post-route pass reports.
        res.update({k: r[k] for k in ("variant_seconds", "variants", "expansions", "work", "work_all_variants",
                                     "stop", "style", "widen", "stitch") if k in r})
    # Prepared fixtures keep the byte-original human; legacy PCBench names it processed.kicad_pcb.
    ref = SRC_FIX / name / "routed.kicad_pcb"
    if not ref.exists():
        ref = SRC_FIX / name / "processed.kicad_pcb"
    if ref.exists() and "style" in res:
        res["style_ref"] = style_of(ref, outdir / "boards" / f"{name}.style_ref.json")
    input_saved = out.with_name(f"{name}.input.refilled.kicad_pcb") if REFILL else None
    before = drc(src, planes=planes, saved=input_saved)
    saved = out.with_name(f"{name}.refilled.kicad_pcb") if REFILL else None
    after = drc(out, planes=planes, saved=saved) if out.exists() else None
    if saved is not None and saved.exists():
        res["refilled_board"] = str(saved.relative_to(outdir))
    if input_saved is not None and input_saved.exists():
        res["input_refilled_board"] = str(input_saved.relative_to(outdir))
    res["judge_input"] = str(src)
    res["judge_input_sha256"] = digest(src)
    if before is None or after is None:
        res["judge"] = "failed"
        return res
    apply_judgment(res, before, after)
    if planes is not None:
        res.update({f"{group}_unconnected_{stage}": judge[f"{group}_unconnected"]
                    for group in ("plane", "signal") for stage, judge in (("before", before), ("after", after))})
        initial, final = copper_totals(src, planes), copper_totals(out, planes)
        if "unconnected_per_net" in res:
            for stage, counts in res["unconnected_per_net"].items():
                res[f"plane_unconnected_{stage}"] = sum(n for net, n in counts.items() if net in planes)
                res[f"signal_unconnected_{stage}"] = sum(n for net, n in counts.items() if net not in planes)
        res.update({f"{group}_vias_added": final[f"{group}_vias"] - initial[f"{group}_vias"]
                    for group in ("plane", "signal")})
        res.update({f"{group}_track_mm_added": round(final[f"{group}_track_mm"] - initial[f"{group}_track_mm"], 4)
                    for group in ("plane", "signal")})
    # Completion stays full precision; presentation rounds, never the regression gate.
    return res


def rejudge_run(source, run_id=None):
    """Publish fresh judgments without routing or modifying source artifacts."""
    global REFILL
    directory = pathlib.Path(source)
    if not directory.is_dir():
        directory = ROOT / "bench/results" / source
    summary = json.loads((directory / "summary.json").read_text())
    manifest_path = directory / "manifest.json"
    prior_manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    if summary.get("state", "final") != "final" or prior_manifest.get("state", "final") != "final":
        raise ValueError("rejudgment requires a final run")
    rows = [json.loads(line) for line in (directory / "boards.jsonl").read_text().splitlines() if line.strip()]
    names = [row["board"] for row in rows]
    if not names or len(names) != len(set(names)):
        raise ValueError("rejudgment roster is empty or duplicated")
    for metadata in (summary, prior_manifest):
        if "roster" in metadata and sorted(metadata["roster"]) != sorted(names):
            raise ValueError("rejudgment roster does not match final run")
    run_id = run_id or directory.name + "-rejudged"
    target = ROOT / "bench/results" / run_id
    if target.exists() and any(target.iterdir()):
        raise ValueError(f"run directory is not empty: {target}; use a new run name")
    version = kicad_version()
    inputs = summary.get("provenance", {}).get("inputs", {})
    fixtures = pathlib.Path(summary.get("fixtures", SRC_FIX))
    prepared = []
    for row in rows:
        name = row["board"]
        output = directory / "boards" / f"{name}.kicad_pcb"
        if row.get("judge_input"):
            src = pathlib.Path(row["judge_input"])
        elif (row.get("place") or "").startswith("ok"):
            src = directory / "boards" / f"{name}.placed.kicad_pcb"
        else:
            src = fixtures / name / "unrouted.kicad_pcb"
        expected = (row.get("judge_input_sha256") or row.get("input_sha256")
                    or inputs.get(name, {}).get("board_sha256")
                    or row.get("preparation", {}).get("input_sha256"))
        if not expected or digest(src) != expected:
            raise ValueError(f"{name}: cannot prove preserved judge input hash")
        expected_rules = inputs.get(name, {}).get("rules")
        actual_rules = {ext: digest(src.with_suffix(ext)) for ext in (".kicad_pro", ".kicad_dru")
                        if src.with_suffix(ext).exists()}
        if expected_rules is not None and actual_rules != expected_rules:
            raise ValueError(f"{name}: preserved input rules differ")
        if not output.exists():
            raise ValueError(f"{name}: saved raw output is missing")
        prepared.append((row, src, output))
        preparation = src.with_name("preparation.json")
        inputs[name] = dict(board_sha256=digest(src), rules=actual_rules,
                            manifest_sha256=digest(preparation) if preparation.exists() else None)
    REFILL = True
    (target / "boards").mkdir(parents=True, exist_ok=True)
    provenance = dict(summary.get("provenance", {}), metric_schema_version=METRIC_SCHEMA_VERSION,
                      judge_code_version=JUDGE_CODE_VERSION, kicad_version=version,
                      judge_flags=JUDGE_FLAGS + ["--refill-zones", "--save-board"],
                      inputs=inputs,
                      rejudged_from=directory.name,
                      source_boards_sha256=digest(directory / "boards.jsonl"),
                      source_outputs={row["board"]: digest(output) for row, _, output in prepared})
    manifest = dict(prior_manifest, run=run_id, state="running", roster=names, scheduled=len(names),
                    provenance=provenance, rejudged_from=directory.name)
    atomic_json(target / "manifest.json", manifest)
    judged_rows = []
    for old, src, output in prepared:
        name = old["board"]
        raw = target / "boards" / output.name
        shutil.copyfile(output, raw)
        copy_rules(output, raw)
        route_json = pathlib.Path(str(output) + ".route.json")
        if route_json.exists():
            shutil.copyfile(route_json, pathlib.Path(str(raw) + ".route.json"))
        row = {key: value for key, value in old.items() if key not in {
            "judge", "clean", "completion", "censored", "judge_list_censored", "judge_cache_keys",
            "judge_reports", "unconnected_before", "unconnected_after", "unconnected_uncapped",
            "unconnected_per_net", "connectivity_method", "kicad_python_version",
            "connectivity_unavailable", "added_errors", "added_nonrouting", "added_any",
            "capped", "capped_types", "error_gate", "error"}}
        row.update(metric_schema_version=METRIC_SCHEMA_VERSION, judge_input=str(src), judge_input_sha256=digest(src))
        saved_input = target / "boards" / f"{name}.input.refilled.kicad_pcb"
        saved_output = target / "boards" / f"{name}.refilled.kicad_pcb"
        planes = row.get("plane_nets")
        before, after = drc(src, planes=planes, saved=saved_input), drc(raw, planes=planes, saved=saved_output)
        row.update(input_refilled_board=str(saved_input.relative_to(target)),
                   refilled_board=str(saved_output.relative_to(target)))
        if before is None or after is None:
            row.update(judge="failed", clean=False, completion=None, error="KiCad rejudgment failed")
        else:
            apply_judgment(row, before, after)
            if planes is not None:
                for stage, j in (("before", before), ("after", after)):
                    counts = row.get("unconnected_per_net", {}).get(stage)
                    for group in ("plane", "signal"):
                        row[f"{group}_unconnected_{stage}"] = (
                            sum(n for net, n in counts.items() if (net in planes) == (group == "plane"))
                            if counts is not None else j[f"{group}_unconnected"])
        judged_rows.append(row)
        ratio = f'{row["completion"]:.2%}' if row.get("completion") is not None else "unknown"
        print(f'{name}: {row.get("unconnected_uncapped", {})}; completion {ratio}; gate {row.get("error_gate", "failed")}')
    (target / "boards.jsonl").write_text("".join(json.dumps(row) + "\n" for row in judged_rows))
    provenance.update(connectivity_provenance(judged_rows))
    summary.update(headline(judged_rows, len(names)))
    summary.update(run=run_id, state="final", roster=names, provenance=provenance, refill=True,
                   rejudged_from=directory.name,
                   boards_with_added_errors=sum(bool(row.get("added_errors")) for row in judged_rows),
                   added_error_types=dict(Counter(kind for row in judged_rows for kind in row.get("added_errors", {}))))
    valid = [row for row in judged_rows if row["judge"] == "ok"]
    fr_rows = [row for row in valid if row.get("fr_rc12")]
    fr241 = [row for row in valid if row.get("fr_241")]
    summary.update(fr_rc12_clean_pass=sum(row["fr_rc12"]["clean"] for row in fr_rows) / len(fr_rows) if fr_rows else None,
                   fr_241_clean_pass=sum(row["fr_241"]["clean"] for row in fr241) / len(fr241) if fr241 else None,
                   both_clean=sum(row["clean"] and row["fr_rc12"]["clean"] for row in fr_rows),
                   only_tracemaker_clean=sum(row["clean"] and not row["fr_rc12"]["clean"] for row in fr_rows),
                   only_freerouting_clean=sum(not row["clean"] and row["fr_rc12"]["clean"] for row in fr_rows))
    for key in ("plane_unconnected_before", "plane_unconnected_after", "signal_unconnected_before", "signal_unconnected_after"):
        summary[key] = sum(row.get(key, 0) for row in judged_rows if row["judge"] == "ok")
    feasible = [row for row in judged_rows if row["judge"] == "ok" and row.get("dead_pins") == 0]
    summary.update(feasible_boards=len(feasible),
                   clean_pass_feasible=sum(row["clean"] for row in feasible) / len(feasible) if feasible else None,
                   plane_metrics_boards=sum("plane_unconnected_after" in row for row in judged_rows if row["judge"] == "ok"))
    atomic_json(target / "summary.json", summary)
    atomic_json(target / "manifest.json", dict(manifest, state="final", attempted=len(names),
                                               judged=summary["judged"], judge_failures=summary["judge_failures"]))
    report = ["# Rejudgment " + run_id, "", "Source: `" + directory.name + "`; raw outputs unchanged; no routing.",
              "", "| Board | Before | After | Completion | Error gate |", "|---|---:|---:|---:|---|"]
    for row in judged_rows:
        counts = row.get("unconnected_uncapped", {})
        ratio = f'{row["completion"]:.2%}' if row.get("completion") is not None else "unknown"
        report.append(f'| {row["board"]} | {counts.get("before", "?")} | {counts.get("after", "?")} | {ratio} | {row.get("error_gate", "judge failed")} |')
    (target / "report.md").write_text("\n".join(report) + "\n")
    print(f"Saved {target}; mean={summary['completion']}, pooled={summary['pooled_completion']}")
    return 1 if summary["infrastructure_failures"] else 0


def main() -> int:
    global TM, PLACE, PLACE_MODE, PLACE_TIMEOUT, PLACE_WORK, PLACE_CRULES
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="A", help="Freerouting tier: A, B, C, D or all")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--time", type=float, help="wall-clock limit per board (default 60 s; 3600 s as a safety stop with --work)")
    ap.add_argument("--work", type=int, default=0, help="deterministic work budget per board (no knowledge base)")
    ap.add_argument("--refill", action="store_true", help="KiCad refills and saves private analysis copies before judging input and output")
    ap.add_argument("--human", action="store_true", help="judge fixtures' routed.kicad_pcb human originals without routing")
    ap.add_argument("--route-args", help="extra route options, space-separated; adds to TM_ROUTE_ARGS (write --route-args=--opt for one option)")
    ap.add_argument("--set", help="board set in bench/sets/<SET>.txt (quick, mid, planes, modern-planes, srj19, srj20); also names the set in the summary")
    ap.add_argument("--jobs", type=int, default=12)
    ap.add_argument("--seed", type=int, default=1, help="board sampling seed")
    ap.add_argument("--name")
    ap.add_argument("--boards", nargs="*", help="explicit board folder names")
    ap.add_argument("--threads", type=int, help="router threads per board (default with --work: the cores per job, at most 8, "
                    "since the output does not depend on it, but 1 with --halving in the route options, where it does; "
                    "without --work it is also the portfolio size, default 1)")
    ap.add_argument("--place", choices=["auto", "routable", "eco", "refine", "full"], help="let TraceMaker move components first")
    ap.add_argument("--place-timeout", type=int, default=900)
    ap.add_argument("--place-work", type=int, default=3_000_000)
    ap.add_argument("--place-crules", choices=["off", "report", "soft", "on"], help="component rules during placement (doc 15)")
    ap.add_argument("--fixtures", help="directory with <board>/unrouted.kicad_pcb to use instead of PCBench (with --boards or --board-list)")
    ap.add_argument("--place-args", help="extra tracemaker-place options, space-separated (quote the whole string)")
    ap.add_argument("--board-list", help="file with one board name per line (# comments)")
    ap.add_argument("--full", action="store_true", help="also route the set's `#slow` boards (the largest; run before committing, doc 10 §4)")
    ap.add_argument("--set-name", help="name of the board set for the summary (default: the tier)")
    ap.add_argument("--intents", help="route with DIR/<board>.intent.json routing intent manifests (doc 05 §45); missing ones are drafted from the input")
    ap.add_argument("--rejudge", metavar="RUN",
                    help="rejudge a final saved run without routing; publish --name or RUN-rejudged")
    a = ap.parse_args()
    explicit_boards = a.boards is not None
    if a.rejudge:
        try:
            return rejudge_run(a.rejudge, a.name)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            ap.error(str(error))
    global THREADS, PLACE_THREADS, SRC_FIX, PLACE_ARGS, WORK, REFILL, EXTRA, INTENTS, HUMAN
    PLACE_ARGS = (a.place_args or "").split()
    # With --work every board runs the whole portfolio whatever the thread count (D47); one thread runs its eight
    # variants one after another. Placement keeps one thread unless asked: its annealing runs follow --threads.
    # Successive halving splits the budget by the thread count, so there the default must not follow the machine.
    EXTRA = EXTRA + (a.route_args or "").split()
    THREADS = a.threads or (max(1, min(8, (os.cpu_count() or 1) // a.jobs)) if a.work and "--halving" not in EXTRA else 1)
    PLACE_THREADS = a.threads or 1
    a.threads = THREADS
    WORK, REFILL, HUMAN = a.work, a.refill, a.human
    if HUMAN and (a.place or a.intents):
        ap.error("--human cannot be combined with --place or --intents")
    if a.intents:
        INTENTS = pathlib.Path(a.intents).resolve()
        INTENTS.mkdir(parents=True, exist_ok=True)
    if a.time is None:
        a.time = 3600.0 if a.work else 60.0
    if a.set:
        a.board_list = a.board_list or str(ROOT / "bench/sets" / f"{a.set}.txt")
        a.set_name = a.set_name or a.set
        # Set metadata overrides the conventional directory, never an explicit --fixtures.
        for line in pathlib.Path(a.board_list).read_text().splitlines():
            if line.startswith("#fixtures:") and not a.fixtures:
                a.fixtures = str(ROOT / line.split(":", 1)[1].strip())
        if not a.fixtures:
            SRC_FIX = ROOT / {"quick": "build/pcbench", "mid": "build/pcbench",
                              "modern-planes": "build/modern-planes"}.get(a.set, str(FIX))
    if a.fixtures:
        SRC_FIX = pathlib.Path(a.fixtures).resolve()
    if a.board_list:
        if not a.boards:
            a.boards = set_boards(a.board_list, full=a.full)
    base = fr_baseline()
    if a.boards:
        names = a.boards
    else:
        names = sorted(n for n, v in base.items() if (a.tier == "all" or v.get("tier") == a.tier) and (FIX / n / "unrouted.kicad_pcb").exists())
        random.Random(a.seed).shuffle(names)
        names = sorted(names[: a.limit])
    if not names or len(names) != len(set(names)):
        ap.error("run roster must be non-empty and contain no duplicate boards")
    missing = [n for n in names if not (SRC_FIX / n / "unrouted.kicad_pcb").is_file()
               or (HUMAN and not (SRC_FIX / n / "routed.kicad_pcb").is_file())]
    if missing:
        ap.error(f"missing prepared fixtures in {SRC_FIX}: {', '.join(missing)}; prepare the set first (see doc 10); check exclusions.json")
    run_id = a.name or datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + f"-tier{a.tier}"
    outdir = ROOT / "bench/results" / run_id
    if outdir.exists() and any(outdir.iterdir()):
        ap.error(f"run directory is not empty: {outdir}; use a new run name")
    try:
        inputs = {name: fixture_provenance(name) for name in names}
        version = kicad_version()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        ap.error(str(error))
    outdir.mkdir(parents=True, exist_ok=True)
    selection_mode = "boards" if explicit_boards else ("full" if a.full else "everyday") if a.board_list else "sample"
    provenance = dict(metric_schema_version=METRIC_SCHEMA_VERSION, kicad_version=version,
                      judge_flags=JUDGE_FLAGS + (["--refill-zones", "--save-board"] if REFILL else []),
                      judge_code_version=JUDGE_CODE_VERSION, inputs=inputs,
                      binary_sha256=None if HUMAN else digest(TM), effective_args=vars(a),
                      set_roster=set_boards(a.board_list, full=True) if a.board_list else names,
                      selection_mode=selection_mode)
    manifest = dict(run=run_id, state="running", roster=names, full=a.full,
                    selection_mode=selection_mode, scheduled=len(names), provenance=provenance,
                    rejudged_from=None)
    atomic_json(outdir / "manifest.json", manifest)
    # Snapshot the engine binary so rebuilding during a run cannot mix versions.
    if not HUMAN:
        snap = outdir / "tracemaker"
        shutil.copy2(TM, snap)
        TM = snap
        provenance["binary_sha256"] = digest(TM)
        atomic_json(outdir / "manifest.json", manifest)
    PLACE_MODE, PLACE_TIMEOUT, PLACE_WORK, PLACE_CRULES = a.place, a.place_timeout, a.place_work, a.place_crules
    if PLACE_MODE:
        psnap = outdir / "tracemaker-place"
        shutil.copy2(PLACE, psnap)
        PLACE = psnap
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    budget = f"{a.work:,} work units" if a.work else f"{a.time:.0f} s"
    print(f"run {run_id}: {len(names)} boards, {a.set_name or 'tier ' + a.tier}, {budget} per board, {a.jobs} jobs", flush=True)
    rows = []
    with cf.ThreadPoolExecutor(a.jobs) as ex, (outdir / "boards.jsonl").open("w") as f:
        for r in ex.map(lambda n: attempt_board(n, outdir, a.time), names):
            fr = base.get(r["board"], {})
            r["fr_rc12"] = fr.get("2.5.0-RC12")
            r["fr_241"] = fr.get("2.4.1")
            r["tier"] = fr.get("tier")
            rows.append(r)
            f.write(json.dumps(r) + "\n")
            f.flush()
            tag = "CLEAN" if r.get("clean") else ("ERR  " if r.get("error") or r.get("judge") == "failed" else "     ")
            frc = r["fr_rc12"]["clean"] if r.get("fr_rc12") else None
            print(f"{tag} {r['board'][:44]:44} routed {r.get('routed', '?')}/{r.get('connections', '?')} unconn {r.get('unconnected_after', '?')} "
                  f"added {r.get('added_errors', {})} {r.get('seconds', 0):.1f}s  FR-RC12 clean={frc}", flush=True)
            if r.get("judge") == "failed" and r.get("error"):
                print(f"JUDGE FAILURE {r['board']}: {r['error']}", file=sys.stderr, flush=True)
    judged = [r for r in rows if r.get("judge") == "ok"]
    provenance.update(connectivity_provenance(rows))
    n = len(names)
    fr_rows = [r for r in judged if r.get("fr_rc12")]
    fr_clean = sum(r["fr_rc12"]["clean"] for r in fr_rows)
    fr241 = [r for r in judged if r.get("fr_241")]
    both = sum(1 for r in fr_rows if r["clean"] and r["fr_rc12"]["clean"])
    only_tm = sum(1 for r in fr_rows if r["clean"] and not r["fr_rc12"]["clean"])
    only_fr = sum(1 for r in fr_rows if not r["clean"] and r["fr_rc12"]["clean"])
    added_types = Counter()
    for r in judged:
        for t in r["added_errors"]:
            added_types[t] += 1
    summary = {
        "run": run_id, "set": (a.set_name or f"PCBench tier {a.tier}") + (f" + placement ({a.place}" + (f", component rules {a.place_crules}" if a.place_crules else "") + ")" if a.place else ""), "boards": n, "commit": commit,
        "place_mode": a.place,
        "human": HUMAN,
        "fixtures": str(SRC_FIX),
        "place_crules": a.place_crules,
        **headline(rows, n),
        "time_limit_s": a.time, "threads": a.threads, "work": a.work, "refill": a.refill, "full": a.full, "route_args": " ".join(EXTRA),
        "intents": str(INTENTS) if INTENTS else None,
        "fr_rc12_clean_pass": round(fr_clean / len(fr_rows), 4) if fr_rows else None,
        "fr_241_clean_pass": round(sum(r["fr_241"]["clean"] for r in fr241) / len(fr241), 4) if fr241 else None,
        "both_clean": both, "only_tracemaker_clean": only_tm, "only_freerouting_clean": only_fr,
        "boards_with_added_errors": sum(1 for r in judged if r["added_errors"]),
        "added_error_types": dict(added_types),
        "state": "final", "roster": names, "selection_mode": selection_mode, "provenance": provenance,
    }
    feasible = [r for r in judged if r.get("dead_pins") == 0]
    summary["feasible_boards"] = len(feasible)
    summary["clean_pass_feasible"] = round(sum(r["clean"] for r in feasible) / len(feasible), 4) if feasible else None
    for key in ("plane_unconnected_before", "plane_unconnected_after", "signal_unconnected_before", "signal_unconnected_after",
                "plane_vias_added", "signal_vias_added", "plane_track_mm_added", "signal_track_mm_added"):
        summary[key] = round(sum(r[key] for r in judged if key in r), 4)
    summary["plane_metrics_boards"] = sum("plane_unconnected_after" in r for r in judged)
    # Style metric (informational, never a gate): totals over the boards that have it, and over the reference
    # routings of the same boards.
    with_style = [r for r in rows if r.get("style")]
    with_ref = [r for r in with_style if r.get("style_ref")]
    summary["style"] = style_total([r["style"] for r in with_style])
    summary["style_ref"] = style_total([r["style_ref"] for r in with_ref])
    summary["style_same_boards"] = style_total([r["style"] for r in with_ref])
    atomic_json(outdir / "summary.json", summary)
    label = "Human" if HUMAN else "TraceMaker"
    notes = (["human originals (no routing)"] if HUMAN else [f"{budget} per board"]) + (["zones refilled and saved before judging"] if a.refill else []) + ([f"route options `{' '.join(EXTRA)}`"] if EXTRA and not HUMAN else [])
    lines = [f"# Benchmark {run_id}", "", f"{a.set_name or 'PCBench tier ' + a.tier}, {n} boards, {', '.join(notes)}.", "",
             f"| Metric | {label} | Freerouting 2.5.0-RC12 | Freerouting 2.4.1 |", "|---|--:|--:|--:|",
             f"| Clean pass (KiCad DRC) | {format(summary['clean_pass'], '.1%') if n else 'unknown'} | {summary['fr_rc12_clean_pass'] or 0:.1%} | {summary['fr_241_clean_pass'] or 0:.1%} |",
             f"| Mean completion (known boards only) | {format(summary['completion'], '.1%') if summary['completion'] is not None else 'unknown'} | | |",
             f"| Route CPU time, all boards | {summary['cpu_seconds']:,.0f} s | | |", "",
             f"Both clean: {both}; only {label}: {only_tm}; only Freerouting: {only_fr}.",
             f"Boards with added DRC errors: {summary['boards_with_added_errors']} ({dict(added_types)})."]
    lines += [f"Scheduled / attempted / judged: {n} / {summary['attempted']} / {summary['judged']}; "
              f"judge failures: {summary['judge_failures']}; censored boards: {summary['censored_boards']}.",
              f"New non-routing error instances (diagnostic): "
              f"{dict(sum((Counter(r.get('added_nonrouting', {})) for r in rows), Counter()))}."]
    lines += ["", "| Board | Routed / connections | KiCad completion | Plane unconnected | Signal unconnected | Vias | Plane / signal vias added | Plane / signal track mm added |",
              "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for row in rows:
        completion_label = f"{row['completion']:.1%}" if row.get("completion") is not None else ("censored" if row.get("censored") else "judge failed")
        lines.append(f"| {row['board']} | {row.get('routed', '?')} / {row.get('connections', '?')} | {completion_label} | "
                     f"{row.get('plane_unconnected_after', '?')} | {row.get('signal_unconnected_after', '?')} | {row.get('vias', '?')} | "
                     f"{row.get('plane_vias_added', '?')} / {row.get('signal_vias_added', '?')} | "
                     f"{row.get('plane_track_mm_added', '?')} / {row.get('signal_track_mm_added', '?')} |")
    if summary["style"]:
        def style_row(label: str, t: dict | None) -> str:
            if not t:
                return f"| {label} | – | | | | | | | |"
            return (f"| {label} | {t['boards']} | {t['bends_per_connection']:.2f} | {t['acute_angles']} | {t['stair_steps']} | {t['short_segments']} | "
                    f"{t['off_angle_segments']} | {t['detour_ratio']:.3f} | {t['long_detours']} |")
        lines += ["", "Style of the routed copper (doc 05 §39, informational):", "",
                  "| Routing | Boards | Bends per connection | Acute angles | Stair-steps | Short segments | Off-angle segments | Detour ratio | Long detours |",
                  "|---|--:|--:|--:|--:|--:|--:|--:|--:|", style_row(label, summary["style"])]
        if summary["style_ref"]:
            lines += [style_row(f"{label}, boards with a reference", summary["style_same_boards"]), style_row("Reference routing", summary["style_ref"])]
    (outdir / "report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    atomic_json(outdir / "manifest.json", dict(manifest, state="final", attempted=len(rows),
                                               judged=summary["judged"], judge_failures=summary["judge_failures"]))
    return 1 if summary["infrastructure_failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
