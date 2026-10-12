# SPDX-License-Identifier: GPL-3.0-or-later
"""KiCad's uncapped ratsnest counts on already-refilled saved boards.

The host needs only the stdlib. Geometry stays in a single, version-matched
KiCad Python subprocess; this module neither refills nor modifies board files.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

CAPS = {"unconnected_items": 499, "clearance": 499, "default": 199}
ROOT = Path(__file__).resolve().parents[1]


class ConnectivityUnavailable(RuntimeError):
    """KiCad Python cannot supply a trustworthy connectivity result."""


class ConnectivityVersionMismatch(ConnectivityUnavailable):
    """The connectivity interpreter and DRC judge use different KiCad versions."""


def _version(value: str) -> str:
    match = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", value)
    if not match:
        raise ConnectivityUnavailable(f"Unrecognised KiCad version: {value!r}")
    return match.group(1)


def _cli(kicad_cli: str | None) -> str:
    return kicad_cli or os.environ.get("KICAD_CLI", "kicad-cli")


def kicad_version(kicad_cli: str | None = None) -> str:
    """Return the selected DRC judge's major.minor.patch version."""
    try:
        result = subprocess.run([_cli(kicad_cli), "version"], capture_output=True,
                                text=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConnectivityUnavailable(f"Cannot query KiCad CLI version: {exc}") from exc
    return _version(result.stdout)


def _interpreter(kicad_cli: str | None) -> str:
    override = os.environ.get("KICAD_PYTHON")
    if override:
        return override
    cli = shutil.which(_cli(kicad_cli))
    if cli:
        resolved = Path(cli).resolve()
        # KiCad.app/Contents/MacOS/kicad-cli; do not accidentally use a Python
        # from another installed KiCad bundle or the host's site-packages.
        if resolved.parent.name == "MacOS":
            versions = resolved.parent.parent / "Frameworks/Python.framework/Versions"
            candidates = [versions / "Current/bin/python3"]
            candidates.extend(sorted(versions.glob("*/bin/python3")))
            for candidate in candidates:
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)
    wrapper = ROOT / "scripts/kicad-python"
    if sys.platform.startswith("linux") and wrapper.is_file() and os.access(wrapper, os.X_OK):
        return str(wrapper)
    raise ConnectivityUnavailable("KiCad Python not found; set KICAD_PYTHON to its interpreter")


# KiCad 10's SWIG exposes RN_NET as an opaque pointer (and cannot accept the
# std::function in RunOnUnconnectedEdges). Removing a complete net from the
# private connectivity graph removes exactly its ratsnest edges. Unlike Add()
# into an empty graph, this preserves Build()'s zone-island/RTree semantics.
_WORKER = r'''
import contextlib
import json
import os
import re
import sys

@contextlib.contextmanager
def quiet_kicad():
    saved = [os.dup(fd) for fd in (1, 2)]
    try:
        with open(os.devnull, "wb") as sink:
            for fd in (1, 2):
                os.dup2(sink.fileno(), fd)
            yield
            sys.stdout.flush()
            sys.stderr.flush()
    finally:
        for fd, original in zip((1, 2), saved):
            os.dup2(original, fd)
            os.close(original)

request = json.loads(sys.argv[1])
results = {}
with quiet_kicad():
    import pcbnew
    raw_version = pcbnew.Version() if hasattr(pcbnew, "Version") else pcbnew.GetBuildVersion()
    match = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", raw_version)
    if not match:
        raise RuntimeError("Unrecognised pcbnew version: " + raw_version)
    version = match.group(1)
    if version == request["kicad_version"]:
        for key, path in request["boards"]:
            board = pcbnew.LoadBoard(path)
            if board is None:
                raise RuntimeError("Cannot load board: " + path)
            board.BuildConnectivity()
            connectivity = board.GetConnectivity()
            total = int(connectivity.GetUnconnectedCount(False))
            groups = {}
            shapes = [item for item in board.GetDrawings()
                      if isinstance(item, pcbnew.PCB_SHAPE) and item.IsOnCopperLayer()]
            for item in list(board.GetPads()) + list(board.GetTracks()) + list(board.Zones()) + shapes:
                code = item.GetNetCode()
                if code > 0:
                    groups.setdefault(code, []).append(item)
            per_net = {}
            remaining = total
            for code, items in sorted(groups.items()):
                for item in items:
                    connectivity.Remove(item)
                connectivity.RecalculateRatsnest()
                after = int(connectivity.GetUnconnectedCount(False))
                count = remaining - after
                if count < 0:
                    raise RuntimeError("Removing a net increased ratsnest edges: " + path)
                remaining = after
                if count:
                    name = str(connectivity.GetNetNameForNetCode(code))
                    if name in per_net:
                        raise RuntimeError("Duplicate net name: " + name)
                    per_net[name] = count
            if remaining != 0 or sum(per_net.values()) != total:
                raise RuntimeError("Per-net connectivity sum disagrees with board total: " + path)
            results[key] = {"unconnected": total, "per_net": per_net,
                            "kicad_version": version, "method": "pcbnew-connectivity"}
print(json.dumps({"kicad_version": version, "boards": results}, sort_keys=True))
'''


def count_unconnected(boards: list[Path], *, kicad_cli: str | None = None) -> dict[str, dict]:
    """Count all ratsnest edges (including hidden ones) in one KiCad process.

    Callers must supply already-refilled boards and compare uncapped DRC lists
    against these totals. Unavailability must remain censored, never estimated.
    """
    if not boards:
        return {}
    version = kicad_version(kicad_cli)
    request = {"kicad_version": version,
               "boards": [(str(path), str(Path(path).resolve())) for path in boards]}
    try:
        result = subprocess.run([_interpreter(kicad_cli), "-c", _WORKER, json.dumps(request)],
                                capture_output=True, text=True, timeout=300, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        diagnostic = getattr(exc, "stderr", None)
        raise ConnectivityUnavailable(f"KiCad connectivity process failed: {diagnostic or exc}") from exc
    try:
        payload = json.loads(result.stdout)
        actual = _version(payload["kicad_version"])
        if actual != version:
            raise ConnectivityVersionMismatch(f"KiCad Python {actual} != DRC judge {version}")
        counts = payload["boards"]
        if not isinstance(counts, dict) or set(counts) != {str(path) for path in boards}:
            raise ValueError("Returned board roster does not match request")
        for path, row in counts.items():
            if row["method"] != "pcbnew-connectivity" or row["kicad_version"] != version:
                raise ValueError(f"Invalid provenance for {path}")
            total, per_net = row["unconnected"], row["per_net"]
            if type(total) is not int or total < 0 or not isinstance(per_net, dict):
                raise ValueError(f"Invalid connectivity counts for {path}")
            if any(not isinstance(name, str) or type(count) is not int or count <= 0
                   for name, count in per_net.items()):
                raise ValueError(f"Invalid per-net counts for {path}")
            if sum(per_net.values()) != total:
                raise ValueError(f"Per-net sum disagrees with total for {path}")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ConnectivityUnavailable(f"Invalid KiCad connectivity output: {exc}") from exc
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("boards", type=Path, nargs="+", help="already-refilled saved .kicad_pcb files")
    parser.add_argument("--kicad-cli", help="DRC judge executable (defaults to KICAD_CLI or kicad-cli)")
    args = parser.parse_args()
    try:
        rows = count_unconnected(args.boards, kicad_cli=args.kicad_cli)
    except ConnectivityUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    width = max(len("Board"), *(len(str(path)) for path in args.boards))
    print(f"{'Board':<{width}}  {'Unconnected':>11}  {'Nets':>5}  KiCad   Method")
    for path in args.boards:
        row = rows[str(path)]
        print(f"{str(path):<{width}}  {row['unconnected']:>11}  {len(row['per_net']):>5}  "
              f"{row['kicad_version']}  {row['method']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
