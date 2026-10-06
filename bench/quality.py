#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Routing quality metrics for a routed .kicad_pcb, judged against its unrouted input.

  bench/quality.py unrouted.kicad_pcb routed_a.kicad_pcb [routed_b.kicad_pcb ...] [--labels A B ...] [--json out.json]

Per routed board:
  completion, unconnected       from kicad-cli DRC (connections left unrouted)
  router_errors                 KiCad DRC errors involving routed copper that the input did not have
  length_mm (and per layer)     total track length
  vias                          via count
  detour                        signal track length / pad-centre minimum spanning tree, over nets without a conductive
                                zone (a zone carries part of a plane net, so its MST is no reference); < 1 is still
                                possible with shared trunks
  nets                          per net: length, vias, pad MST, pads, whether it owns a zone, and (record()) complete
  bends, sharp_bends            direction changes at joints; sharp = interior angle under 90 degrees at a joint outside
                                pad copper (an acid trap); sharp_at_pads counts those inside a pad, where pad copper
                                fills the angle
  narrowed_mm, narrowed_share   track length below the net's design width (net class or board minimum)
"""
import argparse
import hashlib
import importlib.util
import json
import math
import os
import pathlib
import subprocess
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parent.parent
TM = ROOT / "build/release/src/app/tracemaker"
_spec = importlib.util.spec_from_file_location("bench_run", ROOT / "bench/run.py")
bench_run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench_run)
_MAC_KICAD_PYTHON = "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3"
# A Python that can import pcbnew (KiCad's own on macOS; the system one where KiCad installs its module, e.g. Linux).
KICAD_PYTHON = os.environ.get("TM_KICAD_PYTHON", _MAC_KICAD_PYTHON if pathlib.Path(_MAC_KICAD_PYTHON).exists() else "python3")


def board_json(path: pathlib.Path) -> dict:
    out = ROOT / "build/quality" / (path.resolve().as_posix().replace("/", "_")[-150:] + ".json")
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists() or out.stat().st_mtime < path.stat().st_mtime:
        subprocess.run([str(TM), "inspect", str(path), "--json", str(out)], check=True, capture_output=True)
    return json.loads(out.read_text())


def mst_length(pts):
    pts = list(dict.fromkeys(pts))
    n = len(pts)
    if n < 2:
        return 0.0
    inn, best, total = [False] * n, [math.inf] * n, 0.0
    best[0] = 0.0
    for _ in range(n):
        u = min((i for i in range(n) if not inn[i]), key=lambda i: best[i])
        inn[u] = True
        total += best[u]
        for v in range(n):
            if not inn[v]:
                d = math.dist(pts[u], pts[v])
                if d < best[v]:
                    best[v] = d
    return total


def geometry(d: dict, zone_nets: set | None = None) -> dict:
    """zone_nets: nets carried partly by a pour; default every net with a conductive zone in `d` (teardrops
    included, which inspect cannot tell apart: record() passes KiCad's list without them)."""
    mm = 1e-6
    by_layer = defaultdict(float)
    net_len, net_vias = defaultdict(float), defaultdict(int)
    narrowed = 0.0
    widths = d.get("net_track_width", {})
    if zone_nets is None:
        zone_nets = {z["net"] for z in d.get("zones", []) if z.get("net") and not z.get("rule_area")}
    # Joints: endpoints shared by exactly two segments of the same net and layer.
    ends = defaultdict(list)
    for t in d["tracks"]:
        a, b = (t["sx"], t["sy"]), (t["ex"], t["ey"])
        L = math.dist(a, b) * mm
        by_layer[t["layer"]] += L
        net_len[t["net"]] += L
        if t["net"] in widths and t["width"] < widths[t["net"]] - 1000:
            narrowed += L
        ends[(t["net"], t["layer"], a)].append(b)
        ends[(t["net"], t["layer"], b)].append(a)
    for v in d["vias"]:
        net_vias[v.get("net", "")] += 1
    # Pad copper per layer (rotated rectangles; round pads are covered by their bounding square, conservative).
    pad_rects = defaultdict(list)
    for pd in d["pads"]:
        a = math.radians(-pd.get("angle", 0.0))
        for l in pd.get("layers", []):
            pad_rects[l].append((pd["x"], pd["y"], pd["w"] / 2, pd["h"] / 2, math.cos(a), math.sin(a)))

    def on_pad(layer, p):
        for x, y, hw, hh, c, s_ in pad_rects.get(layer, []) + pad_rects.get("*.Cu", []):
            dx, dy = p[0] - x, p[1] - y
            u, v = dx * c + dy * s_, -dx * s_ + dy * c
            if abs(u) <= hw + 1 and abs(v) <= hh + 1:
                return True
        return False

    bends = sharp = sharp_pad = 0
    for (net, layer, p), others in ends.items():
        if len(others) != 2:
            continue
        v1 = (others[0][0] - p[0], others[0][1] - p[1])
        v2 = (others[1][0] - p[0], others[1][1] - p[1])
        n1, n2 = math.hypot(*v1), math.hypot(*v2)
        if n1 == 0 or n2 == 0:
            continue
        # Interior angle between the two legs; 180 degrees = straight through.
        cosang = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
        interior = math.degrees(math.acos(cosang))
        if interior < 179.0:
            bends += 1
            if interior < 90.0 - 0.5:
                if on_pad(layer, p):
                    sharp_pad += 1
                else:
                    sharp += 1
    pads_by_net = defaultdict(list)
    for p in d["pads"]:
        if p.get("net"):
            pads_by_net[p["net"]].append((p["x"] * mm, p["y"] * mm))
    nets = {}
    for net in sorted(set(pads_by_net) | set(net_len) | {n for n in net_vias if n}):
        pts = pads_by_net.get(net, [])
        nets[net] = {"plane": net in zone_nets, "length_mm": round(net_len[net], 3), "vias": net_vias[net], "pads": len(pts),
                     "mst_mm": round(mst_length(pts), 3) if 1 < len(pts) <= 2000 else 0.0}
    signal = [n for n in nets.values() if not n["plane"] and n["mst_mm"] > 0]
    sig_len, sig_ref = sum(n["length_mm"] for n in signal), sum(n["mst_mm"] for n in signal)
    total = sum(by_layer.values())
    return {"length_mm": round(total, 1), "length_by_layer_mm": {k: round(v, 1) for k, v in sorted(by_layer.items())},
            "vias": len(d["vias"]), "segments": len(d["tracks"]), "bends": bends, "sharp_bends": sharp, "sharp_at_pads": sharp_pad,
            "narrowed_mm": round(narrowed, 1), "narrowed_share": round(narrowed / total, 4) if total else 0.0,
            "signal_length_mm": round(sig_len, 1), "signal_mst_mm": round(sig_ref, 1),
            "detour": round(sig_len / sig_ref, 3) if sig_ref else None,
            "plane_net_length_mm": round(sum(n["length_mm"] for n in nets.values() if n["plane"]), 1), "nets": nets}


def pair_partner(name: str, names: set):
    """KiCad's diff-pair naming: same name with a final P/N or +/- swapped."""
    if len(name) < 2:
        return None
    swap = {"P": "N", "N": "P", "+": "-", "-": "+"}.get(name[-1])
    other = name[:-1] + swap if swap else None
    return other if other in names else None


def coupling(d: dict) -> dict:
    """Share of differential-pair track length that runs beside its partner (same layer, gap <= 0.3 mm)."""
    by_net = defaultdict(list)
    for t in d["tracks"]:
        by_net[t["net"]].append(t)
    names = set(by_net)
    total = coupled = 0.0
    pairs = 0
    for n in sorted(names):
        o = pair_partner(n, names)
        if not o or n > o:
            continue
        pairs += 1
        for a_net, b_net in ((n, o), (o, n)):
            bs = by_net[b_net]
            for t in by_net[a_net]:
                ax, ay, bx, by_ = t["sx"], t["sy"], t["ex"], t["ey"]
                L = math.hypot(bx - ax, by_ - ay)
                k = max(1, int(L / 100000))  # 0.1 mm samples
                for i in range(k):
                    px, py = ax + (bx - ax) * (i + 0.5) / k, ay + (by_ - ay) * (i + 0.5) / k
                    best = math.inf
                    for u in bs:
                        if u["layer"] != t["layer"]:
                            continue
                        ux, uy, vx, vy = u["sx"], u["sy"], u["ex"], u["ey"]
                        dx, dy = vx - ux, vy - uy
                        LL = dx * dx + dy * dy
                        tt = 0.0 if LL == 0 else max(0.0, min(1.0, ((px - ux) * dx + (py - uy) * dy) / LL))
                        dist = math.hypot(px - ux - tt * dx, py - uy - tt * dy) - (t["width"] + u["width"]) / 2
                        best = min(best, dist)
                    total += L / k
                    if best <= 300000:
                        coupled += L / k
    return {"diff_pairs": pairs, "pair_length_mm": round(total / 1e6, 1),
            "pair_coupled_share": round(coupled / total, 3) if total else None}


def judge(before: dict, after: dict) -> dict:
    """Completion and router-added errors of `after` against its unrouted input `before` (bench/run.py drc())."""
    added = {t: n - before["routed_errors"].get(t, 0) for t, n in after["routed_errors"].items()
             if t not in bench_run.NOT_ROUTING and n - before["routed_errors"].get(t, 0) > 0}
    return {"connections": before["unconnected"], "unconnected": after["unconnected"], "router_errors": added,
            "completion": 1.0 if before["unconnected"] == 0 else round(1 - after["unconnected"] / before["unconnected"], 4),
            "clean": after["unconnected"] == 0 and not added}


def metrics(unrouted: pathlib.Path, routed: pathlib.Path) -> dict:
    before, after = bench_run.drc(unrouted), bench_run.drc(routed)
    res = {"board": str(routed)}
    if before and after:
        res.update(judge(before, after))
    bj = board_json(routed)
    geo = geometry(bj)
    geo.pop("nets")  # per-net detail is for record(); compare.py and the CLI keep totals
    res.update(geo)
    res.update(coupling(bj))
    return res


def plane_metrics(pcb: pathlib.Path, small_pad_mm: float = 2.0) -> dict | None:
    """bench/plane_metrics_kicad.py (KiCad's refill, plane coverage/islands/gaps, small-pad vias); None without pcbnew."""
    st, probe = pcb.stat(), ROOT / "bench/plane_metrics_kicad.py"
    key = hashlib.sha1(f"{pcb.resolve()}:{st.st_mtime_ns}:{st.st_size}:{small_pad_mm}:{probe.stat().st_mtime_ns}".encode()).hexdigest()[:16]
    out = ROOT / "build/quality" / f"planes-{key}.json"
    if not out.exists():
        p = subprocess.run([KICAD_PYTHON, str(ROOT / "bench/plane_metrics_kicad.py"), str(pcb), str(small_pad_mm)],
                           capture_output=True, text=True)
        if p.returncode != 0 or not p.stdout.strip():
            return None
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(p.stdout.strip().splitlines()[-1])
    return json.loads(out.read_text())


def record(unrouted: pathlib.Path, pcb: pathlib.Path, label: str, hand: bool = False, small_pad_mm: float = 2.0) -> dict:
    """Everything bench/score.py needs about one routed (or hand-routed) board, judged against its unrouted input
    after a zone refill. Nets carry per-net length and vias, so two boards can be compared on the nets both
    completed rather than on totals that reward routing less."""
    before, after = bench_run.drc(unrouted), bench_run.drc(pcb)
    if before is None or after is None:
        return {"label": label, "board": unrouted.stem, "file": str(pcb), "hand": hand, "judge": "failed"}
    j = judge(before, after)
    bj = board_json(pcb)
    planes, planes_input = plane_metrics(pcb, small_pad_mm), plane_metrics(unrouted, small_pad_mm)
    geo = geometry(bj, set(planes["zone_nets"]) if planes else None)
    geo.update(coupling(bj))
    nets = geo.pop("nets")
    open_now, open_before = set(after["unconnected_nets"]), set(before["unconnected_nets"])
    for name, n in nets.items():
        n["complete"] = name not in open_now
        n["open_in_input"] = name in open_before
    return {"label": label, "board": unrouted.stem, "file": str(pcb), "hand": hand,
            "connections": j["connections"], "unconnected": j["unconnected"], "completion": j["completion"],
            "added_errors": j["router_errors"], "nets": nets, "geometry": geo, "planes": planes, "planes_input": planes_input}


def rescore(quality_json: pathlib.Path) -> None:
    """Recompute the geometry metrics of a stored bench/compare.py result (after a metric definition changes)."""
    d = json.loads(quality_json.read_text())
    for r in d["runs"]:
        if r.get("file") and pathlib.Path(r["file"]).exists():
            geo = geometry(board_json(pathlib.Path(r["file"])))
            geo.pop("nets")
            r.update(geo)
    quality_json.write_text(json.dumps(d, indent=1))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("unrouted")
    ap.add_argument("routed", nargs="+")
    ap.add_argument("--labels", nargs="*")
    ap.add_argument("--json")
    a = ap.parse_args()
    labels = a.labels or [pathlib.Path(r).stem for r in a.routed]
    rows = [dict(metrics(pathlib.Path(a.unrouted), pathlib.Path(r)), label=l) for l, r in zip(labels, a.routed)]
    cols = ["label", "completion", "clean", "router_errors", "length_mm", "detour", "vias", "bends", "sharp_bends", "narrowed_mm"]
    print(" | ".join(cols))
    for r in rows:
        print(" | ".join(str(r.get(c, "")) for c in cols))
    if a.json:
        pathlib.Path(a.json).write_text(json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
