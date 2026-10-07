#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Score refilled bench/quality.py metric records, not board files.

Rank lexicographically by (added_errors > 0, added_errors, unconnected), then
quality within an equal tier. Costs are dimensionless and lower is better:

  cost = sum(weight * term / scale) / sum(available weights)
  quality = 100 / (1 + cost)

Term                         weight  scale  group
signal_detour                    25      1  signal
vias_per_connection              10      1  signal
unsupported_share_added          20    0.1  planes
coverage_lost                    15    0.1  planes
islands_added_per_connection     10    0.1  planes
plane_crossings_per_connection    5      1  planes
small_pad_vias_per_connection    10    0.1  dfm
sharp_per_connection              2      1  dfm
narrowed_share                    2    0.1  dfm
pair_uncoupled_share              1    0.1  signal

These weights/scales are TraceMaker policy, not IPC limits or contest weights.
See docs/10-benchmarking.md. Geometry counts use completed input connections
(connections - unconnected); signal detour uses complete, non-plane nets that
were open in the input and have an available positive MST. Nets with > 2000 pads
have no MST and contribute neither length nor MST to detour. Pair and narrowed shares are length-normalised. Plane
terms measure positive degradation from this record's own planes_input. No
credit is given for repairing pre-existing plane damage. Missing evidence is
null, excluded from cost, and exposed as missing_terms/available_weight. A
quality number is descriptive, never a replacement for legality/completion or
comparable across different sets of available terms.

compare() restricts length, vias and detour to nets open in both inputs and
complete in both outputs, excluding plane nets in either record. Its quality
uses only mutually available terms. Equal-tier quality differences below 2% of
the larger cost are ties: a policy noise floor, not a statistical confidence claim.
Unsupported length includes all samples inside the board without filled reference
copper in the union of planes on adjacent layers, including outside plane outlines.
For matched vias the denominator is sum(pads - 1), a pad-tree connection proxy,
not the KiCad missing-item count. Other terms remain board-level costs per
completed connection. Hand routing is scored identically, never a target to
imitate. Ratios with a zero hand denominator are null with an explicit status.

All arithmetic affecting verdicts is rational; reports round half-up to six
places and quality_milli is a deterministic integer (0..100000). Pad-centre
MST detour is a topology proxy, not a proven geometric lower bound: Steiner
sharing/pad edges may legitimately give detour < 1. No routing or KiCad is run.

  python3 bench/score.py RECORD.json [RECORD.json ...] [--hand LABEL] [--md]
"""
import argparse
from fractions import Fraction
import json
from statistics import median
import pathlib


TERMS = {
    "signal_detour": (25, Fraction(1), "signal"),
    "vias_per_connection": (10, Fraction(1), "signal"),
    "unsupported_share_added": (20, Fraction(1, 10), "planes"),
    "coverage_lost": (15, Fraction(1, 10), "planes"),
    "islands_added_per_connection": (10, Fraction(1, 10), "planes"),
    "plane_crossings_per_connection": (5, Fraction(1), "planes"),
    "small_pad_vias_per_connection": (10, Fraction(1, 10), "dfm"),
    "sharp_per_connection": (2, Fraction(1), "dfm"),
    "narrowed_share": (2, Fraction(1, 10), "dfm"),
    "pair_uncoupled_share": (1, Fraction(1, 10), "signal"),
}

# Small cost changes are not meaningful evidence of a better routing configuration.
QUALITY_NOISE = Fraction(1, 50)


def _number(value):
    return None if value is None else Fraction(str(value))


def _divide(numerator, denominator):
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def _rounded(value, scale=1_000_000):
    if value is None:
        return None
    scaled = value * scale
    return (2 * scaled.numerator + scaled.denominator) // (2 * scaled.denominator)


def _display(value):
    integer = _rounded(value)
    return None if integer is None else integer / 1_000_000


def _positive_delta(value, baseline):
    if value is None or baseline is None:
        return None
    return max(Fraction(0), value - baseline)


def _tier(rec):
    errors = sum(int(rec["added_errors"][key]) for key in sorted(rec["added_errors"]))
    return (int(errors > 0), errors, int(rec["unconnected"]))


def _complete_nets(rec):
    return [name for name in sorted(rec["nets"])
            if rec["nets"][name]["complete"] and rec["nets"][name]["open_in_input"]]


def _net_totals(rec, names):
    nets = [rec["nets"][name] for name in names]
    signals = [net for net in nets if not net["plane"] and net["mst_mm"] is not None and net["mst_mm"] > 0]
    length = sum((_number(net["length_mm"]) for net in nets), Fraction(0))
    vias = sum(int(net["vias"]) for net in nets)
    signal_length = sum((_number(net["length_mm"]) for net in signals), Fraction(0))
    mst = sum((_number(net["mst_mm"]) for net in signals), Fraction(0))
    return {"length_mm": length, "vias": Fraction(vias),
            "signal_length_mm": signal_length, "signal_mst_mm": mst,
            "detour": _divide(signal_length, mst),
            "pad_tree_connections": Fraction(sum(max(0, int(net["pads"]) - 1) for net in nets))}


def _planes_by_key(planes):
    groups = {}
    for plane in planes["planes"]:
        key = (plane["net"], plane["layer"])
        groups.setdefault(key, []).append(plane)
    result = {}
    for key in sorted(groups):
        entries = groups[key]
        coverages = [_number(entry["coverage"]) for entry in entries]
        coverage = None if any(c is None for c in coverages) else sum(coverages) / len(entries)
        result[key] = (coverage, sum(int(entry["islands"]) for entry in entries))
    return result


def _values(rec):
    geometry = rec["geometry"]
    completed = max(0, int(rec["connections"]) - int(rec["unconnected"]))
    denominator = Fraction(completed)
    nets = _net_totals(rec, _complete_nets(rec))
    values = {name: None for name in TERMS}
    values["signal_detour"] = nets["detour"] if completed else None
    values["vias_per_connection"] = _divide(_number(geometry.get("vias")), denominator)
    sharp = (_number(geometry.get("sharp_bends")), _number(geometry.get("sharp_at_pads")))
    values["sharp_per_connection"] = _divide(sum(sharp), denominator) if all(x is not None for x in sharp) else None
    values["narrowed_share"] = _divide(_number(geometry.get("narrowed_mm")), _number(geometry.get("length_mm")))
    coupled = _number(geometry.get("pair_coupled_share"))
    values["pair_uncoupled_share"] = None if coupled is None else max(Fraction(0), 1 - coupled)
    planes, baseline = rec.get("planes"), rec.get("planes_input")
    if planes is not None:
        values["small_pad_vias_per_connection"] = _divide(_number(planes.get("small_pad_vias")), denominator)
    if planes is not None and baseline is not None and baseline["planes"]:
        before, after = _planes_by_key(baseline), _planes_by_key(planes)
        lost, original, added_islands = Fraction(0), Fraction(0), 0
        known_coverage = True
        for key in sorted(before):
            coverage, islands = before[key]
            new_coverage, new_islands = after.get(key, (Fraction(0), 0))
            if coverage is None or new_coverage is None:
                known_coverage = False
            else:
                lost += max(Fraction(0), coverage - new_coverage)
                original += coverage
            added_islands += max(0, new_islands - islands)
        values["coverage_lost"] = _divide(lost, original) if known_coverage else None
        values["islands_added_per_connection"] = _divide(Fraction(added_islands), denominator)
        unsupported = _divide(_number(planes.get("unsupported_mm")), _number(planes.get("plane_adjacent_mm")))
        old_unsupported = _divide(_number(baseline.get("unsupported_mm")), _number(baseline.get("plane_adjacent_mm")))
        if _number(baseline.get("plane_adjacent_mm")) == 0 and _number(baseline.get("unsupported_mm")) == 0:
            old_unsupported = Fraction(0)
        values["unsupported_share_added"] = _positive_delta(unsupported, old_unsupported)
        crossings = _positive_delta(_number(planes.get("plane_vias")), _number(baseline.get("plane_vias")))
        values["plane_crossings_per_connection"] = _divide(crossings, denominator)
    if not completed:
        values = {name: None for name in TERMS}
    values["length_mm_per_connection"] = _divide(_number(geometry.get("length_mm")), denominator)
    return values


def _cost(values, names):
    available = [name for name in sorted(names) if values[name] is not None]
    weight = sum(TERMS[name][0] for name in available)
    total = sum((TERMS[name][0] * values[name] / TERMS[name][1] for name in available), Fraction(0))
    return _divide(total, Fraction(weight)), weight


def score(rec: dict) -> dict:
    """Return legality/completion tiers, dimensionless costs and diagnostic quality."""
    tier = _tier(rec)
    values = _values(rec)
    cost, weight = _cost(values, TERMS)
    quality = None if cost is None else 100 / (1 + cost)
    sub_scores = {}
    for group in ("signal", "planes", "dfm"):
        subtotal, subweight = _cost(values, [name for name in TERMS if TERMS[name][2] == group])
        sub_scores[group] = {"cost": _display(subtotal), "available_weight": subweight,
                             "quality": _display(None if subtotal is None else 100 / (1 + subtotal))}
    connections = Fraction(int(rec["connections"]))
    completed = max(0, int(rec["connections"]) - int(rec["unconnected"]))
    completion = _divide(Fraction(completed), connections) if connections else Fraction(int(rec["unconnected"] == 0))
    return {"tier": list(tier), "legal": tier[0] == 0, "added_errors": tier[1],
            "unconnected": tier[2], "completed_connections": completed,
            "completion": _display(completion), "clean_pass": tier[0] == 0 and tier[2] == 0,
            "terms": {name: _display(values[name]) for name in sorted(values)},
            "sub_scores": sub_scores, "cost": _display(cost), "quality": _display(quality),
            "quality_milli": _rounded(quality, 1000), "available_weight": weight,
            "missing_terms": [name for name in sorted(TERMS) if values[name] is None]}


def _ratio(a, b):
    if a is None or b is None:
        return {"ratio": None, "status": "unavailable"}
    if b == 0:
        return {"ratio": None, "status": "both_zero" if a == 0 else "zero_reference"}
    return {"ratio": _display(a / b), "status": "ok"}


def compare(a: dict, b: dict) -> dict:
    """Compare same-board records; winner is 'a', 'b', or 'tie', never hand bias."""
    if a["board"] != b["board"] or a["connections"] != b["connections"]:
        raise ValueError("paired comparison requires the same board and input connection count")
    names = [name for name in sorted(set(_complete_nets(a)) & set(_complete_nets(b)))
             if not a["nets"][name]["plane"] and not b["nets"][name]["plane"]]
    totals_a, totals_b = _net_totals(a, names), _net_totals(b, names)
    values_a, values_b = _values(a), _values(b)
    detour_names = [name for name in names if all(rec["nets"][name]["mst_mm"] is not None
                                                and rec["nets"][name]["mst_mm"] > 0 for rec in (a, b))]
    detour_a, detour_b = _net_totals(a, detour_names), _net_totals(b, detour_names)
    for totals, detour in ((totals_a, detour_a), (totals_b, detour_b)):
        for key in ("signal_length_mm", "signal_mst_mm", "detour"):
            totals[key] = detour[key]
    values_a["signal_detour"], values_b["signal_detour"] = detour_a["detour"], detour_b["detour"]
    denominator = min(totals_a["pad_tree_connections"], totals_b["pad_tree_connections"])
    values_a["vias_per_connection"] = _divide(totals_a["vias"], denominator)
    values_b["vias_per_connection"] = _divide(totals_b["vias"], denominator)
    available = [name for name in sorted(TERMS) if values_a[name] is not None and values_b[name] is not None]
    cost_a, _ = _cost(values_a, available)
    cost_b, _ = _cost(values_b, available)
    tier_a, tier_b = _tier(a), _tier(b)
    winner, reason = "tie", "equal tiers and quality"
    if tier_a[:2] != tier_b[:2]:
        winner, reason = ("a" if tier_a[:2] < tier_b[:2] else "b"), "legality: fewer added DRC errors; zero always wins"
    elif tier_a[2] != tier_b[2]:
        winner, reason = ("a" if tier_a[2] < tier_b[2] else "b"), "completion: fewer unconnected items"
    elif cost_a is None or cost_b is None:
        reason = "equal tiers; no mutually available quality terms"
    elif cost_a != cost_b:
        relative = abs(cost_a - cost_b) / max(cost_a, cost_b)
        if relative < QUALITY_NOISE:
            reason = "equal tiers; matched quality costs differ by less than 2% (noise floor)"
        else:
            winner, reason = ("a" if cost_a < cost_b else "b"), "quality: lower matched-net/normalised cost"
    return {"winner": winner, "reason": reason, "tier_a": list(tier_a), "tier_b": list(tier_b),
            "matched_nets": names, "matched_signal_nets": names,
            "matched_a": {key: _display(totals_a[key]) for key in sorted(totals_a)},
            "matched_b": {key: _display(totals_b[key]) for key in sorted(totals_b)},
            "ratios_a_to_b": {key: _ratio(totals_a[key], totals_b[key]) for key in ("length_mm", "vias", "detour")},
            "quality_terms": available, "cost_a": _display(cost_a), "cost_b": _display(cost_b)}


def versus_hand(rec, hand) -> dict:
    """Expose per-term candidate/hand ratios without rewarding similarity to hand."""
    if rec["board"] != hand["board"]:
        raise ValueError("hand reference must belong to the same board")
    values, reference = _values(rec), _values(hand)
    return {"terms": {name: _ratio(values[name], reference[name]) for name in sorted(values)},
            "matched": compare(rec, hand), "candidate_score": score(rec), "hand_score": score(hand)}


def _format(value):
    return "—" if value is None else f"{value:.3f}"


def _cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def _seed_label(rec):
    label = str(rec.get("label", "unknown"))
    return label if rec.get("seed") is None else f"{label}@s{rec['seed']}"


def _aggregate(rows, hand):
    groups = {}
    for rec in rows:
        if rec is not hand:
            groups.setdefault(rec.get("label", "unknown"), []).append(rec)
    lines = ["", "Seed aggregates (median tiers then median Q; spread = min–max; failed judges excluded):", "",
             "| Config | Seeds / failed | Median errors | Median open (spread) | Median Q (spread) | vs hand W/T/L |",
             "|---|--:|--:|--:|--:|---|"]
    ranks = []
    for label in sorted(groups):
        group = groups[label]
        good = [rec for rec in group if rec.get("judge") != "failed"]
        results = [score(rec) for rec in good]
        quality = [_number(result["quality"]) for result in results if result["quality"] is not None]
        opens = [Fraction(result["unconnected"]) for result in results]
        errors = [Fraction(result["added_errors"]) for result in results]
        wins = ties = losses = 0
        if hand:
            verdicts = [compare(rec, hand)["winner"] for rec in good]
            wins, ties, losses = (verdicts.count(v) for v in ("a", "tie", "b"))
        q = median(quality) if quality else None
        u, e = (median(opens), median(errors)) if good else (None, None)
        open_cell = "—" if u is None else f"{_display(u):g} ({_display(min(opens)):g}–{_display(max(opens)):g})"
        q_cell = "—" if q is None else f"{_format(_display(q))} ({_format(_display(min(quality)))}–{_format(_display(max(quality)))})"
        verdict_cell = f"{wins}/{ties}/{losses}" if hand else "—"
        lines.append(f"| {_cell(label)} | {len(good)} / {len(group) - len(good)} | {_format(_display(e))}"
                     f" | {open_cell} | {q_cell} | {verdict_cell} |")
        if good:
            ranks.append(((int(e > 0), e, u, (q is None, Fraction(0) if q is None else -q)), label))
    if ranks:
        best = min(key for key, _ in ranks)
        labels = sorted(label for key, label in ranks if key == best)
        lines.extend(["", f"Best configuration by median (legality, added errors, open), then Q: **{', '.join(_cell(label) for label in labels)}**."])
    return lines


def markdown(records: list[dict], hand_label: str | None = "original") -> str:
    """Render grouped diagnostics, including failed judges without metric fields."""
    lines = []
    boards = sorted({rec.get("board", "unknown board") for rec in records})
    for board in boards:
        rows = sorted((rec for rec in records if rec.get("board", "unknown board") == board),
                      key=lambda rec: ((1, 0, 0) if rec.get("judge") == "failed" else _tier(rec),
                                       (True, Fraction(0)) if rec.get("judge") == "failed" or score(rec)["quality"] is None
                                       else (False, -_number(score(rec)["quality"])),
                                       _seed_label(rec), str(rec.get("file", ""))))
        hands = [rec for rec in rows if rec.get("judge") != "failed"
                 and (rec.get("label") == hand_label if hand_label else rec.get("hand", False))]
        if len(hands) > 1:
            raise ValueError(f"ambiguous hand reference for {board}; use unique --hand LABEL")
        hand = hands[0] if hands else None
        lines.extend([f"### {_cell(board)}", "",
                      "| Label | Legal | Open | Completion | Quality ↑ | Weight / 100 | Detour | Vias / conn | Plane cost | DFM cost | Matched length / hand | Matched vias / hand | vs hand |",
                      "|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|"])
        for rec in rows:
            if rec.get("judge") == "failed":
                lines.append(f"| {_cell(_seed_label(rec))} | judge failed | — | — | — | — | — | — | — | — | — | — | failed |")
                continue
            result = score(rec)
            paired = compare(rec, hand) if hand else None
            ratios = paired["ratios_a_to_b"] if paired else {}
            length = ratios.get("length_mm", {}).get("ratio")
            vias = ratios.get("vias", {}).get("ratio")
            verdict = "—" if paired is None else paired["winner"]
            if rec is hand:
                verdict = "reference"
            elif paired:
                verdict = {"a": "candidate", "b": "hand", "tie": "tie"}[verdict]
            lines.append(f"| {_cell(_seed_label(rec))} | {'yes' if result['legal'] else 'FAIL (' + str(result['added_errors']) + ')'}"
                         f" | {result['unconnected']} | {100 * result['completion']:.1f}% | {_format(result['quality'])}"
                         f" | {result['available_weight']} | {_format(result['terms']['signal_detour'])}"
                         f" | {_format(result['terms']['vias_per_connection'])} | {_format(result['sub_scores']['planes']['cost'])}"
                         f" | {_format(result['sub_scores']['dfm']['cost'])} | {_format(length)} | {_format(vias)} | {verdict} |")
        lines.extend(_aggregate(rows, hand))
        lines.extend(["", "Quality is diagnostic within equal legality/completion tiers; missing terms are not zero. "
                      "Hand ratios use matched input-open, output-complete nets; zero denominators display —. "
                      "Equal-tier matched-cost differences below 2% are ties.", ""])
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("records", nargs="+", type=pathlib.Path)
    ap.add_argument("--hand", help="reference label for each board; otherwise use hand=true")
    ap.add_argument("--md", action="store_true", help="print Markdown (also the default)")
    args = ap.parse_args()
    records = []
    for path in args.records:
        data = json.loads(path.read_text())
        records.extend(data if isinstance(data, list) else [data])
    print(markdown(records, args.hand))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
