# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure scorecard kernels ported from the PCB quality reviewer prototypes.

Sources: ManufacturabilityReview/analyze.py (metadata and craft geometry) and
PowerThermalReview/metrics.py (rail names, weighted widths and EP candidates).
Inspect coordinates are nanometres; reported lengths and widths are millimetres.
No board mutation, inspection subprocess, cache, refill or run orchestration lives
here. Craft deliberately omits arcs and zone conduction, as the reviewer did.
Thermal candidates and rail names are heuristics, not electrical/thermal sign-off.
"""

import math
import re
import statistics
from collections import defaultdict
from itertools import combinations
from pathlib import Path

import plane_metrics
from intent_quality import in_pad


POWER = re.compile(
    r"^(?:\+?\d+(?:[Vv.]\d+)?[Vv](?:\d+)?[A-Za-z_]*|"
    r"V(?:DD|CC|BAT|BUS|IN|OUT|SYS|PWR)(?:[0-9_].*)?|[0-9]+V[0-9]+)$",
    re.I,
)
SUPPORTED_SHAPES = {"rect", "roundrect", "oval", "circle"}


def metadata(board: Path) -> dict:
    """Read reviewer pad shape/EP hints, keyed by JSON-safe ``ref.num``.

    Explicit EP/PAD/DAP names and pin functions are kept separately from the
    reviewer's large-central-pad QFN/DFN heuristic. Multiple copper primitives
    sharing a pad number merge EP evidence; conflicting shapes are unsupported,
    rather than silently assigning the final primitive's shape to every pad.
    Footprint dimensions and positions used for inference are in millimetres.
    """
    root = plane_metrics.parse(board.read_text())
    result = {}
    for fp in plane_metrics.children(root, "footprint") + plane_metrics.children(root, "module"):
        refs = [p[2] for p in plane_metrics.children(fp, "property") if p[1] == "Reference"]
        refs += [p[2] for p in plane_metrics.children(fp, "fp_text") if p[1] == "reference"]
        if not refs:
            continue
        ref = refs[0]
        pads = plane_metrics.children(fp, "pad")
        sizes = [float(plane_metrics.child(p, "size")[1]) * float(plane_metrics.child(p, "size")[2])
                 for p in pads if p[2] == "smd"]
        median = sorted(sizes)[len(sizes) // 2] if sizes else 0
        for p in pads:
            size, at = plane_metrics.child(p, "size"), plane_metrics.child(p, "at")
            area = float(size[1]) * float(size[2])
            x, y = float(at[1]), float(at[2])
            pinfunction = plane_metrics.child(p, "pinfunction", ["", ""])[1].upper()
            explicit = p[1].upper() in {"EP", "PAD", "DAP"} or pinfunction in {
                "EP", "EXPOSED_PAD", "THERMAL_PAD"
            }
            central = ("_EP" in fp[1] or "QFN" in fp[1] or "DFN" in fp[1]) and (
                area > 4 * median and math.hypot(x, y) < 0.5
            )
            row = {
                "shape": p[3],
                "rr": float(plane_metrics.child(p, "roundrect_rratio", ["", "0"])[1]),
                "exposed": bool(explicit or central),
                "explicit_exposed": explicit,
                "inferred_exposed": bool(central),
                "lib": fp[1],
            }
            key = ref + "." + p[1]
            previous = result.get(key)
            if previous is not None:
                row["exposed"] |= previous["exposed"]
                row["explicit_exposed"] |= previous["explicit_exposed"]
                row["inferred_exposed"] |= previous["inferred_exposed"]
                if (row["shape"], row["rr"]) != (previous["shape"], previous["rr"]):
                    row["shape"], row["rr"] = None, None
            result[key] = row
    return dict(sorted(result.items()))


def local(p, x, y):
    """Reviewer board-to-pad rotation, in inspect nanometres."""
    a = math.radians(p["angle"])
    dx, dy = x - p["x"], y - p["y"]
    return dx * math.cos(a) - dy * math.sin(a), dx * math.sin(a) + dy * math.cos(a)


def sd(p, x, y, meta):
    """Reviewer pad signed distance; custom/trapezoid pads are not exact.

    The rectangle fallback is used only to suppress junctions inside unmodelled
    pad copper, matching the prototype; unsupported entry/VIP pads are counted
    explicitly and excluded from those shape-sensitive measurements.
    """
    u, v = local(p, x, y)
    w, h = p["w"] / 2, p["h"] / 2
    shape = meta.get("shape", "rect")
    if shape == "circle":
        return math.hypot(u, v) - w
    r = min(w, h) if shape == "oval" else (
        min(w, h) * 2 * meta["rr"] if shape == "roundrect" else 0
    )
    qx, qy = abs(u) - (w - r), abs(v) - (h - r)
    return math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - r


def bucket(items, kind):
    """Reviewer 2 mm candidate grid with a 1 mm neighbourhood margin (nm)."""
    cells = defaultdict(list)
    for p in items:
        margin = max(p["w"], p["h"]) / 2 + 1e6 if kind == "pad" else p["size"] / 2 + 1e6
        for x in range(math.floor((p["x"] - margin) / 2e6), math.floor((p["x"] + margin) / 2e6) + 1):
            for y in range(math.floor((p["y"] - margin) / 2e6), math.floor((p["y"] + margin) / 2e6) + 1):
                cells[x, y].append(p)
    return cells


def measure(d, meta):
    """Return every reviewer craft field plus per-net exposed acute counts.

    Same thresholds as ManufacturabilityReview: small non-EP single-layer SMD
    pads, 0.25 mm via/drill neighbourhood, 15 degree entry axis tolerance, 15%
    corner band, 100 nm centreline T-junction tolerance. Vias are tested on their
    endpoint layers only. Arcs are deliberately recorded as omitted. Passive
    neck asymmetry measures track entries only, not plane/thermal conduction.
    Missing shape metadata contributes to unsupported_pads, never known pads.
    """
    pads = [p for p in d["pads"] if p["attr"] == "smd" and len(p["layers"]) == 1
            and min(p["w"], p["h"]) < 2e6
            and not meta.get(p["ref"] + "." + p["num"], {}).get("exposed")]
    known = [p for p in pads if meta.get(p["ref"] + "." + p["num"], {}).get("shape") in SUPPORTED_SHAPES]
    pc, vc = bucket(known, "pad"), bucket(d["vias"], "via")
    allpc = bucket(d["pads"], "pad")
    m = {
        "small_smd_pads": len(pads), "unsupported_pads": len(pads) - len(known),
        "vias": len(d["vias"]), "via_in_pad": 0, "via_near_pad_025": 0,
        "drill_near_pad_025": 0, "entries": 0, "off_axis_entries": 0,
        "corner_entries": 0, "wide_entries": 0, "acute_junctions": 0,
        "eligible_junctions": 0, "bends": 0, "track_mm": 0.0,
        "track_count": len(d["tracks"]), "arcs_omitted": len(d.get("arcs", [])),
    }
    violations = []
    for v in d["vias"]:
        distances = []
        for p in pc[math.floor(v["x"] / 2e6), math.floor(v["y"] / 2e6)]:
            if p["layers"][0] not in (v["top"], v["bottom"]):
                continue
            distances.append((sd(p, v["x"], v["y"], meta[p["ref"] + "." + p["num"]]), p))
        if not distances:
            continue
        dist, p = min(distances, key=lambda q: q[0])
        if dist <= 0:
            m["via_in_pad"] += 1
            violations.append({"ref": p["ref"], "pad": p["num"], "via_net": v["net"],
                               "x_mm": v["x"] / 1e6, "y_mm": v["y"] / 1e6})
        if dist - v["size"] / 2 < 250000:
            m["via_near_pad_025"] += 1
        if dist - v["drill"] / 2 < 250000:
            m["drill_near_pad_025"] += 1
    rays, entry_by_pad, track_cells = defaultdict(set), defaultdict(list), defaultdict(list)
    perpad = {}
    acute_by_net = {t["net"]: 0 for t in d["tracks"]}
    for t in d["tracks"]:
        for cx in range(math.floor((min(t["sx"], t["ex"]) - 100) / 3e6), math.floor((max(t["sx"], t["ex"]) + 100) / 3e6) + 1):
            for cy in range(math.floor((min(t["sy"], t["ey"]) - 100) / 3e6), math.floor((max(t["sy"], t["ey"]) + 100) / 3e6) + 1):
                track_cells[t["net"], t["layer"], cx, cy].append(t)
    for t in d["tracks"]:
        ax, ay, bx, by = t["sx"], t["sy"], t["ex"], t["ey"]
        dx, dy = bx - ax, by - ay
        ln = math.hypot(dx, dy)
        if not ln:
            continue
        m["track_mm"] += ln / 1e6
        for x, y, rx, ry in ((ax, ay, dx, dy), (bx, by, -dx, -dy)):
            rays[t["net"], t["layer"], x, y].add((round(rx / ln, 8), round(ry / ln, 8)))
            for other in track_cells[t["net"], t["layer"], math.floor(x / 3e6), math.floor(y / 3e6)]:
                ox, oy = other["sx"], other["sy"]
                odx, ody = other["ex"] - ox, other["ey"] - oy
                olen = math.hypot(odx, ody)
                if not olen:
                    continue
                fraction = ((x - ox) * odx + (y - oy) * ody) / (olen * olen)
                if 1e-8 < fraction < 1 - 1e-8 and math.hypot(x - ox - fraction * odx, y - oy - fraction * ody) <= 100:
                    rays[t["net"], t["layer"], x, y].update({
                        (round(odx / olen, 8), round(ody / olen, 8)),
                        (round(-odx / olen, 8), round(-ody / olen, 8)),
                    })
            for p in pc[math.floor(x / 2e6), math.floor(y / 2e6)]:
                if p["net"] != t["net"] or p["layers"][0] != t["layer"]:
                    continue
                md = meta[p["ref"] + "." + p["num"]]
                if md["shape"] not in {"rect", "roundrect"}:
                    continue
                if sd(p, x, y, md) > 0 or sd(p, x + rx, y + ry, md) <= 0:
                    continue
                lo, hi = 0.0, 1.0
                for _ in range(35):
                    mid = (lo + hi) / 2
                    if sd(p, x + mid * rx, y + mid * ry, md) <= 0:
                        lo = mid
                    else:
                        hi = mid
                ex, ey = x + lo * rx, y + lo * ry
                u, v = local(p, ex, ey)
                du, dv = local(dict(p, x=0, y=0), rx, ry)
                gx, gy = p["w"] / 2 - abs(u), p["h"] / 2 - abs(v)
                xface = gx < gy
                normal = abs(du if xface else dv) / ln
                off = normal < math.cos(math.radians(15))
                corner = min(gx, gy) < 0.15 * min(p["w"], p["h"]) and max(gx, gy) < 0.15 * min(p["w"], p["h"])
                face_width = p["h"] if xface else p["w"]
                wide = t["width"] > face_width
                m["entries"] += 1
                m["off_axis_entries"] += off
                m["corner_entries"] += corner
                m["wide_entries"] += wide
                entry_by_pad[p["ref"], p["num"]].append(t["width"])
                key = p["ref"] + "." + p["num"]
                flags = perpad.setdefault(key, {"off": False, "corner": False, "wide": False, "width_sum": 0, "count": 0})
                flags["off"] |= off
                flags["corner"] |= corner
                flags["wide"] |= wide
                flags["width_sum"] += t["width"]
                flags["count"] += 1
    for (net, layer, x, y), vs in sorted(rays.items()):
        if len(vs) < 2:
            continue
        # Pad/via copper swallows the junction, so it is not an exposed corner.
        if any(p["net"] == net and layer in p["layers"]
               and sd(p, x, y, meta.get(p["ref"] + "." + p["num"], {})) <= 0
               for p in allpc[math.floor(x / 2e6), math.floor(y / 2e6)]):
            continue
        if any(v["net"] == net and math.hypot(x - v["x"], y - v["y"]) <= v["size"] / 2
               for v in vc[math.floor(x / 2e6), math.floor(y / 2e6)]):
            continue
        m["eligible_junctions"] += 1
        dots = [a[0] * b[0] + a[1] * b[1] for a, b in combinations(sorted(vs), 2)]
        if any(1e-7 < z < 1 - 1e-7 for z in dots):
            m["acute_junctions"] += 1
            acute_by_net[net] += 1
        if len(vs) == 2 and dots[0] > -1 + 1e-7:
            m["bends"] += 1
    passives = defaultdict(list)
    for p in known:
        if p["ref"].startswith(("C", "R", "L")):
            passives[p["ref"]].append(p)
    m["passives_both_track_entries"] = 0
    m["passive_neck_asymmetry_gt2"] = 0
    for pp in passives.values():
        if len(pp) != 2:
            continue
        widths = [sum(entry_by_pad[p["ref"], p["num"]]) for p in pp]
        if min(widths) > 0:
            m["passives_both_track_entries"] += 1
            m["passive_neck_asymmetry_gt2"] += max(widths) / min(widths) > 2
    m["examples"] = violations[:10]
    m["per_pad_entry"] = dict(sorted(perpad.items()))
    m["acute_by_net"] = dict(sorted(acute_by_net.items()))
    return m


def is_power(name):
    """Reviewer supply-name heuristic, applied to the final hierarchical name."""
    return bool(POWER.fullmatch(name.rsplit("/", 1)[-1]))


def is_ground(name):
    """Reviewer ground-name heuristic; not a net-role/schematic classifier."""
    return name.rsplit("/", 1)[-1].upper() in {"GND", "AGND", "DGND", "PGND", "GNDA", "GNDD"}


def weighted_median(rows):
    """Reviewer lower weighted median of (width, length); empty is unknown."""
    rows = sorted(rows)
    half = sum(weight for _, weight in rows) / 2
    total = 0.0
    for value, weight in rows:
        total += weight
        if total >= half:
            return value
    return None


def power(d):
    """Reviewer per-supply widths with exact circular arcs and neck diagnostics.

    resistance_35um_ohm sums rho*length/(width*35 um), rho=1.724e-8 ohm m.
    It treats every track/arc as series copper: branches, parallel paths, planes,
    vias, temperature and actual stackup thickness are not modelled. It is not
    terminal-to-terminal resistance. Nonpositive widths make resistance unknown.
    class_neck_mm is length strictly below inspect's net_track_width in nm;
    absent/nonpositive design width is unknown, not zero. Segments are [mm, mm].
    """
    widths = defaultdict(list)
    for t in d["tracks"]:
        if is_power(t["net"]):
            length = math.hypot(t["ex"] - t["sx"], t["ey"] - t["sy"]) / 1e6
            if length > 0:
                widths[t["net"]].append((t["width"] / 1e6, length))
    for a in d.get("arcs", []):
        if is_power(a["net"]):
            node = ["arc", ["start", a["sx"] / 1e6, a["sy"] / 1e6],
                    ["mid", a["mx"] / 1e6, a["my"] / 1e6],
                    ["end", a["ex"] / 1e6, a["ey"] / 1e6]]
            length = plane_metrics.arc_geometry(node)[3]
            if length > 0:
                widths[a["net"]].append((a["width"] / 1e6, length))
    result = {}
    classes = d.get("net_track_width") or {}
    for net, rows in sorted(widths.items()):
        rows = sorted(rows)
        design_width = classes.get(net)
        result[net] = {
            "median_width": weighted_median(rows),
            "min_width": min(width for width, _ in rows),
            "length_mm": sum(length for _, length in rows),
            "segments": [[width, length] for width, length in rows],
            "resistance_35um_ohm": sum(1.724e-8 * length / (width * 35e-6)
                                       for width, length in rows) if all(width > 0 for width, _ in rows) else None,
            "class_neck_mm": sum(length for width, length in rows if width * 1e6 < design_width)
            if design_width is not None and design_width > 0 else None,
        }
    return result


def thermal(d, meta):
    """Merge reviewer EP candidates and explicit board EP evidence by ref.num.

    Geometry candidates retain the prototype's U-number, >=5 primitive, ground
    net, >=1 mm minimum dimension, >=2 mm2 area and near-centroid tests. Explicit
    EP evidence additionally admits other references, smaller or non-ground pads.
    Once a logical pad is selected, all its SMD primitives contribute to its via
    count. Via centres use the prototype's rotated bounding rectangle (1 um
    tolerance), not custom copper geometry; only via endpoint layers are tested.
    Unique vias are keyed by centre and layer span. Confidence is "explicit" or
    "inferred", never a claim of cooling performance. Conflicting primitive nets
    make net/count unknown rather than attributing vias to an arbitrary net.
    """
    refs, logical = defaultdict(list), defaultdict(list)
    for p in d["pads"]:
        refs[p["ref"]].append(p)
        logical[p["ref"] + "." + p["num"]].append(p)
    candidates = {}
    for ref, pads in sorted(refs.items()):
        geometry_eligible = re.match(r"^U\d", ref) is not None and len(pads) >= 5
        cx = statistics.mean(p["x"] for p in pads)
        cy = statistics.mean(p["y"] for p in pads)
        for p in pads:
            if p["attr"] != "smd":
                continue
            key = ref + "." + p["num"]
            md = meta.get(key, {})
            explicit = md.get("explicit_exposed", False)
            inferred = md.get("exposed", False) or (geometry_eligible and is_ground(p["net"])
                       and min(p["w"], p["h"]) >= 1e6 and p["w"] * p["h"] >= 2e12
                       and math.hypot(p["x"] - cx, p["y"] - cy) <= max(p["w"], p["h"]))
            if explicit or inferred:
                if explicit or key not in candidates:
                    candidates[key] = "explicit" if explicit else "inferred"
    result = {}
    for key, confidence in sorted(candidates.items()):
        pads = [p for p in logical[key] if p["attr"] == "smd"]
        nets = {p["net"] for p in pads}
        net = next(iter(nets)) if len(nets) == 1 and all(nets) else None
        vias = set()
        if net is not None:
            for p in pads:
                for v in d["vias"]:
                    if v["net"] == net and in_pad(p, v["x"], v["y"]) and set(p["layers"]) & {v["top"], v["bottom"]}:
                        vias.add((v["x"], v["y"], v["top"], v["bottom"]))
        result[key] = {"count": len(vias) if net is not None else None, "net": net,
                       "ref": pads[0]["ref"], "num": pads[0]["num"], "confidence": confidence}
    return result
