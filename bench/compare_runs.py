#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Paired comparison of two bench/run.py runs on the same boards (doc 10 §4, gates of §5).

  bench/compare_runs.py BEFORE_RUN AFTER_RUN [--expect-identical] [--markdown FILE]

A run is a directory bench/results/<run-id> or just the run id. Per board: routed boards byte-identical or not,
completion and clean pass before and after, added DRC errors. Exit status 1 when a gate fails:
  1. finished ("complete" or "exhausted") and legacy/unknown-stop results must not regress;
     only "work" is budget-censored, and a finished baseline must not become work-stopped;
     "time"/"deadline" outcomes fail; a human baseline is a reference, not a regression gate;
  2. no added KiCad DRC errors on any board, including unfinished boards;
  3. final runs contain their declared rosters and compatible input/judge provenance;
     everyday/full runs may compare their common cohort only for the exact set's #slow difference;
  with --expect-identical (speed-ups, refactors): every routed board byte-identical.
The style metric (doc 05 §39) is shown per board where it changed; it is informational and never gated.
Wall time and route CPU time are reported, not gated: they are only comparable from the same machine. When both runs
used the same --work budget, boards whose routed output is byte-identical did the same work, so their CPU change is
run-to-run noise or a change in speed that leaves the output alone; the report gives that band and lists the other
boards (and the total) that moved beyond it, at least 10 %. One pair of runs cannot tell the two apart: when every
identical board moved the same way by more than 10 % the report says so, and a second run of one binary settles it.
Equal work is not equal time (the budget counts search expansions and flood cells only, doc 10 §5), so a claim
about speed needs a run at equal wall time.
"""
import argparse
import hashlib
import json
import pathlib
import sys

from drc_judge import completion as count_completion

ROOT = pathlib.Path(__file__).resolve().parent.parent


STYLE_KEYS = ("bends_per_connection", "acute_angles", "stair_steps", "short_segments", "off_angle_segments", "detour_ratio", "long_detours")
CPU_FLAG = 0.10  # smallest relative change in route CPU time at equal work that the report points out
FINISHED_STOPS = {"complete", "exhausted"}
TIME_STOPS = {"time", "deadline"}


def read_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def completion_value(row: dict) -> float | None:
    """Recompute legacy ratios too: a stored percentage cannot undo KiCad's list cap."""
    if row.get("judge") == "failed":
        return None
    exact = row.get("unconnected_uncapped")
    if exact is not None:
        before, after = exact["before"], exact["after"]
        return 1.0 if before == 0 else max(0, 1-after/before)
    if row.get("unconnected_before") is not None and row.get("unconnected_after") is not None:
        return count_completion(row["unconnected_before"], row["unconnected_after"])["completion"]
    if row.get("censored") or row.get("judge_list_censored"):
        return None
    return row.get("completion")


def completion_text(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.1%}"


def validate_run(directory: pathlib.Path, rows: dict, summary: dict) -> tuple[list[str], list[str]]:
    failures, notes = [], []
    manifest_path = directory / "manifest.json"
    manifest = read_json(manifest_path)
    stateful = "state" in manifest or "state" in summary
    if stateful:
        for source, metadata in (("manifest", manifest), ("summary", summary)):
            if metadata.get("state") != "final":
                failures.append(f"{directory.name}: {source} is not final (state {metadata.get('state', 'missing')})")
    declared = []
    for source, metadata in (("manifest", manifest), ("summary", summary)):
        if "roster" not in metadata:
            if stateful:
                failures.append(f"{directory.name}: {source} roster is missing")
            continue
        roster = metadata["roster"]
        if not isinstance(roster, list) or any(not isinstance(name, str) for name in roster):
            failures.append(f"{directory.name}: invalid {source} roster")
            continue
        if stateful and not roster:
            failures.append(f"{directory.name}: {source} roster is empty")
        if stateful:
            judged = sum(row.get("judge") == "ok" for row in rows.values())
            expected = dict(scheduled=len(roster), attempted=len(rows), judged=judged,
                            judge_failures=len(roster) - judged)
            for key, value in expected.items():
                recorded = metadata.get(key)
                if type(recorded) is not int or recorded != value:
                    failures.append(f"{directory.name}: {source} {key} inconsistent with roster/results "
                                    f"(recorded {recorded}, expected {value})")
        if len(roster) != len(set(roster)):
            failures.append(f"{directory.name}: duplicate boards in {source} roster")
        declared.append(set(roster))
        missing, unexpected = sorted(set(roster) - rows.keys()), sorted(rows.keys() - set(roster))
        if missing:
            failures.append(f"{directory.name}: missing declared results ({source}): {', '.join(missing)}")
        if unexpected:
            failures.append(f"{directory.name}: results outside declared roster ({source}): {', '.join(unexpected)}")
    if len(declared) == 2 and declared[0] != declared[1]:
        failures.append(f"{directory.name}: manifest and summary rosters differ")
    if not declared:
        notes.append(f"{directory.name}: legacy roster metadata absent; scheduled-result coverage cannot be established")
    if not summary.get("provenance"):
        notes.append(f"{directory.name}: legacy provenance absent; input/judge compatibility cannot be established")
    return failures, notes


def provenance_mismatches(before: dict, after: dict, boards: list[str]) -> list[str]:
    pb, pf = before.get("provenance"), after.get("provenance")
    if not pb or not pf:
        return []
    mismatches = []
    for key in ("metric_schema_version", "kicad_version", "judge_flags"):
        if key not in pb or key not in pf or pb[key] != pf[key]:
            mismatches.append(f"provenance {key} differs or is missing")
    for name in boards:
        ib, ia = pb.get("inputs", {}).get(name), pf.get("inputs", {}).get(name)
        if not isinstance(ib, dict) or not isinstance(ia, dict):
            mismatches.append(f"{name}: provenance input is missing")
            continue
        for key in ("board_sha256", "rules", "manifest_sha256"):
            if key not in ib or key not in ia or ib[key] != ia[key]:
                mismatches.append(f"{name}: provenance {key} differs or is missing")
    return mismatches


def slow_cohort(before: dict, after: dict, before_names: set[str], after_names: set[str]) -> bool:
    """A declared full/everyday choice is not an arbitrary missing-result exemption."""
    if before.get("set") != after.get("set"):
        return False
    set_name = before.get("set")
    if not isinstance(set_name, str) or pathlib.Path(set_name).name != set_name:
        return False
    path = ROOT / "bench/sets" / f"{set_name}.txt"
    if not path.exists():
        return False
    lines = path.read_text().splitlines()
    slow = {line.split("#slow", 1)[1].strip().split()[0]
            for line in lines if line.startswith("#slow ") and line.split("#slow", 1)[1].strip()}
    known_everyday = {line.strip() for line in lines if line.strip() and not line.startswith("#")}

    def mode(summary, names):
        if summary.get("selection_mode"):
            return summary["selection_mode"]
        if "full" in summary:
            return "full" if summary["full"] else "everyday"
        # Historical summaries omitted selection metadata. Infer only exact named-set rosters,
        # never from a subset or the size of a run (which could also be truncated).
        if names == known_everyday | slow:
            return "full"
        if names == known_everyday:
            return "everyday"
        return None

    bm, am = mode(before, before_names), mode(after, after_names)
    if {bm, am} != {"full", "everyday"}:
        return False
    full, everyday = (before_names, after_names) if bm == "full" else (after_names, before_names)
    return bool(slow) and full - everyday == slow and not everyday - full and not everyday & slow


def load(run: str) -> tuple[pathlib.Path, dict]:
    d = pathlib.Path(run)
    if not d.is_dir():
        d = ROOT / "bench/results" / run
    rows = {}
    for line in (d / "boards.jsonl").read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            if r["board"] in rows:
                raise ValueError(f"{d.name}: duplicate board result {r['board']}")
            rows[r["board"]] = r
    return d, rows


def digest(path: pathlib.Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def stop_reason(run_dir: pathlib.Path, name: str, row: dict) -> str | None:
    """The router's stop reason ("complete", "work", ...); None for runs that never recorded it."""
    if row.get("stop"):
        return row["stop"]
    route = run_dir / "boards" / f"{name}.kicad_pcb.route.json"
    return json.loads(route.read_text()).get("stop") if route.exists() else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--expect-identical", action="store_true", help="fail unless every routed board is byte-identical")
    ap.add_argument("--markdown", help="also write the report here")
    ap.add_argument("--allow-incompatible", action="store_true", help="override unequal input/judge provenance (reported)")
    a = ap.parse_args()
    try:
        bdir, before = load(a.before)
        adir, after = load(a.after)
        bsummary, asummary = read_json(bdir / "summary.json"), read_json(adir / "summary.json")
        bfail, bnotes = validate_run(bdir, before, bsummary)
        afail, anotes = validate_run(adir, after, asummary)
    except (OSError, ValueError, TypeError) as error:
        print(f"Gates: FAIL\n- invalid run: {error}")
        return 1
    boards = sorted(set(before) & set(after))
    missing = sorted(set(before) ^ set(after))
    cohort_allowed = bool(missing) and slow_cohort(bsummary, asummary, set(before), set(after))
    incompatible = provenance_mismatches(bsummary, asummary, boards)
    metadata_notes = bnotes + anotes
    if cohort_allowed:
        for directory, summary in ((bdir, bsummary), (adir, asummary)):
            if not summary.get("selection_mode") and "full" not in summary:
                metadata_notes.append(f"{directory.name}: legacy selection inferred from exact known set roster")
    failures = bfail + afail
    if incompatible:
        if a.allow_incompatible:
            metadata_notes += ["Incompatible provenance explicitly overridden (--allow-incompatible):"] + incompatible
        else:
            failures += incompatible
    if failures:
        text = "\n".join([f"# {bdir.name} → {adir.name}", "", "Comparison refused; run metadata is invalid or incompatible.",
                          *metadata_notes, "", "Gates: FAIL", *[f"- {x}" for x in failures]]) + "\n"
        print(text, end="")
        if a.markdown:
            pathlib.Path(a.markdown).write_text(text)
        return 1
    human_reference = bool(bsummary.get("human"))
    identical, rows, notes, unfinished = 0, [], [], []
    plane_columns = any("plane_unconnected_after" in r for r in list(before.values()) + list(after.values()))
    noise: list[float] = []  # CPU change of byte-identical boards: same work, so run-to-run noise or a uniform speed change
    same_work = bool(bsummary.get("work")) and bsummary.get("work") == asummary.get("work")

    def cpu_change(b: dict, f: dict) -> float | None:
        cb, cf = b.get("cpu_s"), f.get("cpu_s")
        return (cf - cb) / cb if cb and cf is not None else None
    for name in boards:
        b, f = before[name], after[name]
        db, df = digest(bdir / "boards" / f"{name}.kicad_pcb"), digest(adir / "boards" / f"{name}.kicad_pcb")
        same = db is not None and db == df
        identical += same
        cb, cf = completion_value(b), completion_value(f)
        if bool(b.get("unconnected_uncapped")) != bool(f.get("unconnected_uncapped")):
            metadata_notes.append(f"{name}: mixed connectivity methods (uncapped versus legacy DRC-list"
                                  f"{'; legacy censored' if cb is None or cf is None else ''}); "
                                  "completion gated only when both are known")
        sb, sf = stop_reason(bdir, name, b), stop_reason(adir, name, f)
        if sf == "work":
            status = "finished baseline lost; gated" if sb in FINISHED_STOPS and not human_reference else "not gated"
            unfinished.append(f"{name}: completion {completion_text(cb)} -> {completion_text(cf)} (stop {sb} -> {sf}; {status})")
        if "clean" not in b or "clean" not in f or b.get("judge") == "failed" or f.get("judge") == "failed":
            side = 'before' if "clean" not in b or b.get("judge") == "failed" else 'after'
            failures.append(f"{name}: judge failed ({side})")
            continue
        worse = (b["clean"] and not f["clean"]) or (cb is not None and cf is not None and cf < cb)
        added = f.get("added_errors", {})
        for side, stop in (("before", sb), ("after", sf)):
            if stop in TIME_STOPS:
                failures.append(f"{name}: {side} stopped by {stop}; not a deterministic work-budget result")
        if not human_reference and sb not in TIME_STOPS and sf not in TIME_STOPS:
            if sb in FINISHED_STOPS and sf == "work":
                failures.append(f"{name}: no longer finishes (stop {sb} -> {sf})")
            elif worse and sf != "work":
                failures.append(f"{name}: worse (clean {b['clean']} -> {f['clean']}, completion {completion_text(cb)} -> {completion_text(cf)})")
        if added:
            failures.append(f"{name}: added DRC errors: {added}")
        if f.get("error_gate") == "unknown":
            failures.append(f"{name}: DRC error gate unknown; capped types {f.get('capped_types', [])}")
        if not same and a.expect_identical:
            failures.append(f"{name}: routed board differs")
        dc = cpu_change(b, f)
        if same and dc is not None:
            noise.append(dc)
        if not same or worse or added or plane_columns or cb is None or cf is None:
            cpu = f"{b['cpu_s']:.1f} → {f['cpu_s']:.1f}" if dc is not None else "–"
            split = (f"{b.get('plane_unconnected_after', '?')} → {f.get('plane_unconnected_after', '?')} | "
                     f"{b.get('signal_unconnected_after', '?')} → {f.get('signal_unconnected_after', '?')} | ") if plane_columns else ""
            rows.append(f"| {name} | {b.get('routed', '?')} → {f.get('routed', '?')} / {f.get('connections', '?')} | "
                        f"{completion_text(cb)} → {completion_text(cf)} | {'yes' if b['clean'] else 'no'} → {'yes' if f['clean'] else 'no'} | "
                        f"{split}{added or '–'} | {b.get('seconds', 0):.1f} → {f.get('seconds', 0):.1f} | {cpu} | {'identical' if same else 'differs'} |")


    bs, fs = [before[n] for n in boards], [after[n] for n in boards]
    completion_pairs = [(completion_value(b), completion_value(f)) for b, f in zip(bs, fs)]
    known_pairs = [(b, f) for b, f in completion_pairs if b is not None and f is not None]
    mean_b = sum(b for b, _ in known_pairs) / len(known_pairs) if known_pairs else None
    mean_f = sum(f for _, f in known_pairs) / len(known_pairs) if known_pairs else None
    out = [f"# {bdir.name} → {adir.name}", "",
           f"{len(boards)} boards; routed boards byte-identical: {identical}/{len(boards)}.",
           f"Clean pass {sum(r.get('clean', False) for r in bs)} → {sum(r.get('clean', False) for r in fs)}; "
           f"mean completion {completion_text(mean_b)} → {completion_text(mean_f)} "
           f"(paired known cohort {len(known_pairs)}/{len(boards)}; unknown before/after "
           f"{sum(b is None for b, _ in completion_pairs)}/{sum(f is None for _, f in completion_pairs)}); "
           f"router seconds {sum(r.get('seconds', 0) for r in bs):.1f} → {sum(r.get('seconds', 0) for r in fs):.1f}."]
    if human_reference:
        out += ["", "Human baseline: reference comparison, not a regression gate; clean/completion deficits are informational, not human parity."]
    if metadata_notes:
        out += ["", "Metadata notes:"] + [f"- {x}" for x in metadata_notes]
    cpu_b, cpu_f = sum(r.get("cpu_s", 0) for r in bs), sum(r.get("cpu_s", 0) for r in fs)
    if cpu_b and all("cpu_s" in r for r in bs + fs):
        out.append(f"Route CPU time {cpu_b:,.0f} → {cpu_f:,.0f} s ({(cpu_f - cpu_b) / cpu_b:+.1%})" + (" at the same work." if same_work else "."))
        if same_work:
            band = max([CPU_FLAG] + [abs(x) for x in noise])
            if noise:
                out.append(f"Byte-identical boards moved {min(noise):+.0%} to {max(noise):+.0%} in CPU time (the same work: noise, or speed).")
            # Noise scatters around zero. A speed-up or slow-down that leaves the output alone moves every identical
            # board the same way, and the band above would hide it.
            if noise and (min(noise) > CPU_FLAG or max(noise) < -CPU_FLAG):
                notes.append(f"every byte-identical board moved the same way, {min(noise):+.0%} to {max(noise):+.0%}: a change in speed "
                             "(binary or machine), not scatter; run one binary twice to tell which")
            if abs(cpu_f - cpu_b) / cpu_b > band:
                notes.append(f"route CPU time {(cpu_f - cpu_b) / cpu_b:+.1%} in total")
            for name in boards:
                dc = cpu_change(before[name], after[name])
                if dc is not None and abs(dc) > band:
                    notes.append(f"{name}: route CPU time {before[name]['cpu_s']:.1f} → {after[name]['cpu_s']:.1f} s ({dc:+.0%})")
    # A run that lost a board (crash, judge failure) or shares none with the other must not pass by having nothing
    # to compare.
    if missing:
        out.append(f"In one run only (not compared): {', '.join(missing)}.")
        if cohort_allowed:
            out.append("Everyday/full selection differs by exactly the set's #slow boards; comparing the common cohort only.")
        else:
            failures.append(f"{len(missing)} boards are in one run only (not an exact everyday/full #slow selection)")
    if not boards:
        failures.append("no board is in both runs")
    if rows:
        extra_headers = " Plane unconnected | Signal unconnected |" if plane_columns else ""
        extra_rules = "---|---|" if plane_columns else ""
        out += ["", f"| Board | Routed | Completion | Clean |{extra_headers} Added errors (after) | Router s | CPU s | Output |",
                f"|---|---|---|---|{extra_rules}---|---|---|---|"] + rows
    # Style metric (doc 05 §39): informational, never a gate. Runs made before the metric existed have none.
    style_rows = []
    for name in boards:
        sb, sf = before[name].get("style"), after[name].get("style")
        if not sb or not sf or all(sb.get(k) == sf.get(k) for k in STYLE_KEYS):
            continue
        style_rows.append(f"| {name} | " + " | ".join(f"{sb.get(k, '–')} → {sf.get(k, '–')}" for k in STYLE_KEYS) + " |")
    tb, tf = bsummary.get("style"), asummary.get("style")
    if tb and tf:
        out += ["", "Style (informational): " + "; ".join(f"{k.replace('_', ' ')} {tb.get(k, '–')} → {tf.get(k, '–')}" for k in STYLE_KEYS) + "."]
    elif tb or tf:
        out += ["", "Style (informational): only one of the two runs recorded it."]
    if style_rows:
        out += ["", "| Board | " + " | ".join(k.replace("_", " ").capitalize() for k in STYLE_KEYS) + " |", "|---|" + "---|" * len(STYLE_KEYS)] + style_rows
    if notes:
        out += ["", "CPU time at equal work, beyond what the byte-identical boards show as noise (not a gate; check speed at equal wall time, doc 10 §5):"] + [f"- {x}" for x in notes]
    if unfinished:
        out += ["", "Work-budget-stopped observations (listed, not gated unless a finished baseline was lost; doc 10 §5):"] + [f"- {x}" for x in unfinished]
    out += ["", "Gates: " + ("pass" if not failures else "FAIL")] + [f"- {x}" for x in failures]
    text = "\n".join(out) + "\n"
    print(text, end="")
    if a.markdown:
        pathlib.Path(a.markdown).write_text(text)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
