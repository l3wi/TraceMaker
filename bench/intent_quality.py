#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Routing-intent metrics (doc 05 §44): does the routed copper keep the electrical intent of a manifest?

  bench/intent_quality.py RUN [RUN ...] --intents DIR [--fixtures DIR] [--json out.json]

RUN is a bench/results/<id> directory (bench/run.py output). DIR holds <board>.intent.json manifests; a board without
one gets the component-rule draft of its input (`tracemaker rules --intent-draft`), written to DIR, so every run is
judged against the same manifest whether or not it routed with it. Measured on track copper only (tracks, arcs as
chords, vias, pads; zone fills are not modelled), from `tracemaker inspect --json` of each output board:

  connections   for each manifest pad pair, the shortest track path from pad centre to pad centre (through tracks,
                vias and pads of the net) and its excess over the straight line; `long` = excess > 2 mm; `missing` =
                no track path (the pair may still meet through a plane fill: that is what the intent forbids)
  plane pads    for each plane pad whose net has a zone: its plane access -- `through` (through-hole pad), `pour`
                (pad inside a same-net zone outline on its layer), `track` (a track path, not through other pads, to a
                via of the net, a through-hole pad of the net or track copper inside a same-net zone outline; lead = its length) or none
                (`plane_no_access`). Zone outlines stand in for fills (refilled fills are smaller by clearances).
  no-branch     for each no-branch net, junctions (a track node with 3 or more distinct track neighbours, off pad and
                via copper; `junctions_far` = more than 1 mm of track from the nearest pad) and free ends (a track end
                touching nothing)

Equal manifests across runs make the counts comparable; completion differs between runs, so `missing` counts must
be read next to each run's completion.
"""
import argparse
import heapq
import json
import math
import pathlib
import statistics
import re
import subprocess
from collections import defaultdict

import plane_metrics

ROOT = pathlib.Path(__file__).resolve().parent.parent
TM = ROOT / "build/release/src/app/tracemaker"
PCBENCH = ROOT / "bench/data/freerouting/scripts/benchmark/fixtures/PCBench"
TOL = 1000  # nm: endpoint coincidence and point-on-segment tolerance
LONG_MM = 2.0
JUNCTION_PAD_MM = 1.0


def inspect(board: pathlib.Path, cache: pathlib.Path) -> dict:
    out = cache / (board.name + ".inspect.json")
    if not out.exists() or out.stat().st_mtime < board.stat().st_mtime:
        subprocess.run([str(TM), "inspect", str(board), "--json", str(out)], check=True, capture_output=True)
    return json.loads(out.read_text())


def in_pad(p, x, y, margin=0.0):
    """Is (x, y) within pad p's rectangle grown by `margin` (nm)? Pad shapes are taken as their rotated rectangle."""
    a = math.radians(p["angle"])
    dx, dy = x - p["x"], y - p["y"]
    u = dx * math.cos(a) - dy * math.sin(a)
    v = dx * math.sin(a) + dy * math.cos(a)
    return abs(u) <= p["w"] / 2 + margin + TOL and abs(v) <= p["h"] / 2 + margin + TOL


def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy)), t


class NetGraph:
    """Track copper of one net. A track end touching another track's copper (centre within the two half widths) joins
    it there; touching points are merged into one node. Pads and vias are nodes joined to the track nodes whose
    copper reaches them. Edge weights in nm along track centrelines (plus centre offsets into pads and vias)."""

    def __init__(self, d, net):
        layers = d["copper_layers"]
        self.pads = [p for p in d["pads"] if p["net"] == net]
        self.vias = [v for v in d["vias"] if v["net"] == net]
        segs = [(t["layer"], t["sx"], t["sy"], t["ex"], t["ey"], t["width"]) for t in d["tracks"] if t["net"] == net]
        for a in d.get("arcs", []):
            if a["net"] == net:
                segs += [(a["layer"], a["sx"], a["sy"], a["mx"], a["my"], a["width"]), (a["layer"], a["mx"], a["my"], a["ex"], a["ey"], a["width"])]
        key = lambda l, x, y: (l, round(x / TOL), round(y / TOL))
        parent, pos, half = {}, {}, {}

        def find(k):
            while parent.setdefault(k, k) != k:
                parent[k] = parent[parent[k]]
                k = parent[k]
            return k

        def point(l, x, y, w):
            k = key(l, x, y)
            pos[k] = (l, x, y)
            half[k] = max(half.get(k, 0), w / 2)
            find(k)
            return k

        ends = defaultdict(list)
        for i, (l, ax, ay, bx, by, w) in enumerate(segs):
            for x, y in ((ax, ay), (bx, by)):
                ends[(l, int(x // 1e6), int(y // 1e6))].append((x, y, w, i))
        edges = []
        for i, (l, ax, ay, bx, by, w) in enumerate(segs):
            pts = [(0.0, point(l, ax, ay, w)), (1.0, point(l, bx, by, w))]
            reach = w + 1e6
            for cx in range(int((min(ax, bx) - reach) // 1e6), int((max(ax, bx) + reach) // 1e6) + 1):
                for cy in range(int((min(ay, by) - reach) // 1e6), int((max(ay, by) + reach) // 1e6) + 1):
                    for x, y, we, j in ends.get((l, cx, cy), ()):
                        if j == i:
                            continue
                        dist, t = seg_dist(x, y, ax, ay, bx, by)
                        if dist <= (w + we) / 2 + TOL:
                            proj = point(l, ax + t * (bx - ax), ay + t * (by - ay), w)
                            pts.append((t, proj))
                            parent[find(key(l, x, y))] = find(proj)
            pts.sort()
            for (t0, k0), (t1, k1) in zip(pts, pts[1:]):
                if k0 != k1:
                    edges.append((k0, k1, math.hypot(bx - ax, by - ay) * (t1 - t0)))
        self.adj = defaultdict(dict)
        self.neigh = defaultdict(set)  # track neighbours of each track node (distinct nodes)
        for k0, k1, wgt in edges:
            n0, n1 = ("t",) + find(k0), ("t",) + find(k1)
            if n0 == n1:
                continue
            self._edge(n0, n1, wgt)
            self.neigh[n0].add(n1)
            self.neigh[n1].add(n0)
        members = defaultdict(list)
        for k in pos:
            members[("t",) + find(k)].append(k)
        self.pos, self.members = pos, members
        self.anchored = set()  # track nodes touching a pad or via
        layer_index = {n: i for i, n in enumerate(layers)}
        for i, v in enumerate(self.vias):
            top, bot = layer_index.get(v["top"], 0), layer_index.get(v["bottom"], len(layers) - 1)
            span = {layers[k] for k in range(min(top, bot), max(top, bot) + 1)}
            for n, ks in members.items():
                for k in ks:
                    l, x, y = pos[k]
                    dv = math.hypot(x - v["x"], y - v["y"])
                    if l in span and dv <= v["size"] / 2 + half[k] + TOL:
                        self._edge(("v", i), n, dv)
                        self.anchored.add(n)
            for j, p in enumerate(self.pads):
                if span & set(p["layers"]) and in_pad(p, v["x"], v["y"]):
                    self._edge(("v", i), ("p", j), math.hypot(p["x"] - v["x"], p["y"] - v["y"]))
        self.pad_touch = set()  # track nodes on pad copper
        for j, p in enumerate(self.pads):
            for n, ks in members.items():
                for k in ks:
                    l, x, y = pos[k]
                    if l in p["layers"] and in_pad(p, x, y, half[k]):
                        self._edge(("p", j), n, math.hypot(x - p["x"], y - p["y"]))
                        self.anchored.add(n)
                        self.pad_touch.add(n)

    def _edge(self, a, b, w):
        self.adj[a][b] = min(w, self.adj[a].get(b, math.inf))
        self.adj[b][a] = self.adj[a][b]

    def pad_index(self, ref, num):
        for j, p in enumerate(self.pads):
            if p["ref"] == ref and p["num"] == num:
                return j
        return None

    def shortest(self, src, goal, block_pads=False):
        """Dijkstra from node src; goal(node) -> bool. With block_pads, other pads are terminals only."""
        dist, heap = {src: 0.0}, [(0.0, 0, src)]
        tick = 1
        while heap:
            d, _, n = heapq.heappop(heap)
            if d > dist.get(n, math.inf):
                continue
            if n != src and goal(n):
                return d
            if block_pads and n != src and n[0] == "p":
                continue
            for m, w in self.adj[n].items():
                nd = d + w
                if nd < dist.get(m, math.inf):
                    dist[m] = nd
                    heapq.heappush(heap, (nd, tick, m))
                    tick += 1
        return None

    def junctions_and_ends(self):
        """Junctions (3+ distinct track neighbours, off pad and via copper) with their track distance to the nearest
        pad copper in mm, and free ends (one track neighbour, touching nothing)."""
        junctions = []
        for n, s in self.neigh.items():
            if len(s) >= 3 and n not in self.anchored:
                d = self.shortest(n, lambda m: m in self.pad_touch)
                junctions.append(None if d is None else d / 1e6)
        ends = sum(1 for n, s in self.neigh.items() if len(s) == 1 and n not in self.anchored)
        return junctions, ends


def split_pad(name, refs):
    for i, ch in enumerate(name):
        if ch == "." and name[:i] in refs:
            return name[:i], name[i + 1:]
    return None, None


def zone_outlines(board: pathlib.Path):
    """{(net, canonical copper layer): [outline polygons in nm]} of the board's copper zones (rule areas excluded).
    Old files name copper layers freely ("Front", "Inner1_GND"); their ordinal gives the canonical name."""
    root = plane_metrics.parse(board.read_text())
    canon = {}
    for e in plane_metrics.child(root, "layers", [])[1:]:
        if not (isinstance(e, list) and e and e[0].isdigit() and len(e) > 1):
            continue
        num, name = int(e[0]), e[1]
        if re.fullmatch(r"(F|B|In\d+)\.Cu", name):
            canon[name] = name
        elif num == 0:
            canon[name] = "F.Cu"
        elif num == 31:
            canon[name] = "B.Cu"
        elif num < 31:
            canon[name] = f"In{num}.Cu"
    nets = {n[1]: n[2] for n in plane_metrics.children(root, "net")}
    out = defaultdict(list)
    for z in plane_metrics.children(root, "zone"):
        if plane_metrics.child(z, "keepout") is not None:
            continue
        name = plane_metrics.child(z, "net_name", ["", nets.get(plane_metrics.child(z, "net", ["", "0"])[1], "")])[1]
        layer, layers = plane_metrics.child(z, "layer"), plane_metrics.child(z, "layers")
        copper = [layer[1]] if layer else (layers[1:] if layers else [])
        copper = sorted(set(canon.values())) if "*.Cu" in copper else [canon[l] for l in copper if l in canon]
        for poly in plane_metrics.children(z, "polygon"):
            pts = [(float(p[1]) * 1e6, float(p[2]) * 1e6) for p in plane_metrics.children(plane_metrics.child(poly, "pts"), "xy")]
            for l in copper:
                out[(name, l)].append(pts)
    return out


def inside(poly, x, y):
    hit = False
    for (x0, y0), (x1, y1) in zip(poly, poly[1:] + poly[:1]):
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
            hit = not hit
    return hit


def measure(d, manifest, zones):
    refs = {p["ref"] for p in d["pads"]}
    pad_net = {(p["ref"], p["num"]): p["net"] for p in d["pads"]}
    zone_nets = {net for net, _ in zones}
    in_zone = lambda net, l, x, y: any(inside(poly, x, y) for poly in zones.get((net, l), ()))
    graphs = {}
    graph = lambda net: graphs.setdefault(net, NetGraph(d, net))
    out = {"connections": [], "plane_pads": [], "nets": []}
    for c in manifest.get("connections", []):
        (ra, na), (rb, nb) = split_pad(c["from"], refs), split_pad(c["to"], refs)
        net = pad_net.get((ra, na))
        if net is None or pad_net.get((rb, nb)) != net:
            continue
        g = graph(net)
        ia, ib = g.pad_index(ra, na), g.pad_index(rb, nb)
        pa, pb = g.pads[ia], g.pads[ib]
        direct = math.hypot(pa["x"] - pb["x"], pa["y"] - pb["y"]) / 1e6
        path = g.shortest(("p", ia), lambda n: n == ("p", ib))
        out["connections"].append({"from": c["from"], "to": c["to"], "direct_mm": round(direct, 4),
                                   "path_mm": None if path is None else round(path / 1e6, 4)})
    for p in manifest.get("plane_pads", []):
        r, n = split_pad(p["pad"], refs)
        net = pad_net.get((r, n))
        if net is None or net not in zone_nets:
            continue
        g = graph(net)
        i = g.pad_index(r, n)
        pad = g.pads[i]
        # Plane access: a through pad, the pad inside a same-net zone outline on one of its layers, or a track path
        # (not through other pads) to a via of the net or to track copper inside such an outline.
        if pad["attr"] == "thru_hole":
            kind, lead = "through", 0.0
        elif any(in_zone(net, l, pad["x"], pad["y"]) for l in pad["layers"]):
            kind, lead = "pour", 0.0
        else:
            def reaches_plane(m):
                if m[0] == "v" or (m[0] == "p" and g.pads[m[1]]["attr"] == "thru_hole"):
                    return True
                return m[0] == "t" and any(in_zone(net, *g.pos[k]) for k in g.members[m])
            lead = g.shortest(("p", i), reaches_plane, block_pads=True)
            kind, lead = ("track", lead / 1e6) if lead is not None else (None, None)
        out["plane_pads"].append({"pad": p["pad"], "access": kind, "lead_mm": None if lead is None else round(lead, 4)})
    for n in manifest.get("no_branch_nets", []):
        if not any(p["net"] == n["net"] for p in d["pads"]):
            continue
        j, e = graph(n["net"]).junctions_and_ends()
        out["nets"].append({"net": n["net"], "junction_pad_mm": [None if x is None else round(x, 3) for x in j], "free_ends": e})
    return out


def summarize(m):
    conns = m["connections"]
    excess = [c["path_mm"] - c["direct_mm"] for c in conns if c["path_mm"] is not None]
    leads = [p["lead_mm"] for p in m["plane_pads"] if p["access"] == "track"]
    q = lambda v, f: round(f(v), 3) if v else None
    return {"connections": len(conns), "conn_missing": sum(c["path_mm"] is None for c in conns),
            "conn_long": sum(x > LONG_MM for x in excess), "conn_excess_median_mm": q(excess, statistics.median),
            "conn_excess_max_mm": q(excess, max),
            "plane_pads": len(m["plane_pads"]), "plane_no_access": sum(p["access"] is None for p in m["plane_pads"]),
            "plane_by_track": sum(p["access"] == "track" for p in m["plane_pads"]),
            "plane_lead_median_mm": q(leads, statistics.median), "plane_lead_max_mm": q(leads, max),
            "no_branch_nets": len(m["nets"]), "junctions": sum(len(n["junction_pad_mm"]) for n in m["nets"]),
            "junctions_far": sum(sum(x is None or x > JUNCTION_PAD_MM for x in n["junction_pad_mm"]) for n in m["nets"]),
            "free_ends": sum(n["free_ends"] for n in m["nets"])}


def input_board(name, fixtures):
    for base in ([fixtures] if fixtures else []) + [PCBENCH]:
        p = pathlib.Path(base) / name / "unrouted.kicad_pcb"
        if p.exists():
            return p
    raise FileNotFoundError(f"no input board for {name}")


def main() -> int:
    global TM
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--intents", required=True, help="directory of <board>.intent.json (missing ones are drafted)")
    ap.add_argument("--fixtures", help="input boards <DIR>/<board>/unrouted.kicad_pcb (default PCBench)")
    ap.add_argument("--tm", help="tracemaker binary (default build/release)")
    ap.add_argument("--json", help="write per-board measurements")
    a = ap.parse_args()
    if a.tm:
        TM = pathlib.Path(a.tm)
    idir = pathlib.Path(a.intents)
    idir.mkdir(parents=True, exist_ok=True)
    report, totals = {}, {}
    for run in a.runs:
        rdir = pathlib.Path(run)
        cache = rdir / "intent_cache"
        cache.mkdir(exist_ok=True)
        names = [json.loads(l)["board"] for l in (rdir / "boards.jsonl").read_text().splitlines() if l.strip()]
        report[run] = {}
        for name in sorted(names):
            man = idir / f"{name}.intent.json"
            if not man.exists():
                subprocess.run([str(TM), "rules", str(input_board(name, a.fixtures)), "--intent-draft", str(man)], check=True, capture_output=True)
            out = rdir / "boards" / f"{name}.kicad_pcb"
            if not out.exists():
                continue
            m = measure(inspect(out, cache), json.loads(man.read_text()), zone_outlines(out))
            report[run][name] = {"summary": summarize(m), "detail": m}
        keys = ["connections", "conn_missing", "conn_long", "plane_pads", "plane_no_access", "plane_by_track", "no_branch_nets", "junctions",
                "junctions_far", "free_ends"]
        totals[run] = {k: sum(b["summary"][k] for b in report[run].values()) for k in keys}
        excess = [c["path_mm"] - c["direct_mm"] for b in report[run].values() for c in b["detail"]["connections"] if c["path_mm"] is not None]
        leads = [p["lead_mm"] for b in report[run].values() for p in b["detail"]["plane_pads"] if p["access"] == "track"]
        totals[run].update({"conn_excess_median_mm": round(statistics.median(excess), 3) if excess else None,
                            "conn_excess_p90_mm": round(sorted(excess)[int(0.9 * (len(excess) - 1))], 3) if excess else None,
                            "plane_lead_median_mm": round(statistics.median(leads), 3) if leads else None,
                            "plane_lead_p90_mm": round(sorted(leads)[int(0.9 * (len(leads) - 1))], 3) if leads else None})
    cols = list(next(iter(totals.values())).keys())
    print("| run | " + " | ".join(cols) + " |")
    print("|---|" + "---|" * len(cols))
    for run, t in totals.items():
        print(f"| {pathlib.Path(run).name} | " + " | ".join(str(t[c]) for c in cols) + " |")
    if a.json:
        pathlib.Path(a.json).write_text(json.dumps({"totals": totals, "boards": report}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
