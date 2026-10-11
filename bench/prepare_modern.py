#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fetch and prepare the `modern-planes` set (doc 10 §4, D105): open-hardware KiCad 7-10 boards with inner planes,
pinned to commits in bench/sets/modern-planes.json.

  bench/prepare_modern.py [--data bench/data/modern] [--out build/modern-planes] [NAME ...]

For each board: a shallow, sparse git fetch of its commit into <data>/<repo>/<commit> (only KiCad project files and
licence texts: the board, project, custom rules and schematics for review), then <out>/<name>/unrouted.kicad_pcb.
Track/arcs and track-contact vias are removed, no-track vias and zones kept, stale fills discarded and the input
refilled/saved by KiCad. The human original and both boards' project/custom rules are copied beside them.
"""
import argparse
import json
import pathlib
import subprocess
import sys

from prepare_inputs import prepare_board, set_boards

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCES = ROOT / "bench/sets/modern-planes.json"


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def fetch(board: dict, data: pathlib.Path) -> pathlib.Path:
    """Checkout of the board's commit with only KiCad files and licences; returns the checkout root."""
    repo = board["repo"].rstrip("/").split("/")[-1]
    dst = data / repo / board["commit"][:12]
    if (dst / board["pcb"]).is_file():
        return dst
    dst.mkdir(parents=True, exist_ok=True)
    if not (dst / ".git").exists():
        git("init", "--quiet", cwd=dst)
        git("remote", "add", "origin", board["repo"], cwd=dst)
    pcb_dir = str(pathlib.PurePosixPath(board["pcb"]).parent)
    base = "" if pcb_dir == "." else pcb_dir + "/"
    git("sparse-checkout", "set", "--no-cone", f"/{base}*.kicad_pcb", f"/{base}*.kicad_pro", f"/{base}*.kicad_dru",
        f"/{base}**/*.kicad_sch", "/LICENSE*", "/COPYING*", "/LICENCE*", cwd=dst)
    git("fetch", "--quiet", "--depth", "1", "--filter=blob:none", "origin", board["commit"], cwd=dst)
    git("checkout", "--quiet", "--detach", board["commit"], cwd=dst)
    if not (dst / board["pcb"]).is_file():
        raise SystemExit(f"{board['name']}: {board['pcb']} missing at {board['commit']}")
    return dst


def prepare(board: dict, src_root: pathlib.Path, out: pathlib.Path) -> pathlib.Path:
    src = src_root / board["pcb"]
    dst = out / board["name"] / "unrouted.kicad_pcb"
    prepare_board(src, dst)
    return dst


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(ROOT / "bench/data/modern"))
    ap.add_argument("--out", default=str(ROOT / "build/modern-planes"))
    ap.add_argument("names", nargs="*")
    a = ap.parse_args()
    boards = json.loads(SOURCES.read_text())
    listed = set_boards(ROOT / "bench/sets/modern-planes.txt")
    if sorted(listed) != sorted(b["name"] for b in boards):
        raise SystemExit("bench/sets/modern-planes.txt and modern-planes.json list different boards")
    if a.names:
        unknown = set(a.names) - {b["name"] for b in boards}
        if unknown:
            raise SystemExit(f"unknown board(s): {', '.join(sorted(unknown))}")
        boards = [b for b in boards if b["name"] in a.names]
    data, out = pathlib.Path(a.data).resolve(), pathlib.Path(a.out).resolve()
    for b in boards:
        prepare(b, fetch(b, data), out)
        print(f"  {b['name']}", file=sys.stderr)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
