# SPDX-License-Identifier: GPL-3.0-or-later
"""Content-addressed KiCad analysis artifacts, including project/custom-rule inputs."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

import kicad_connectivity as connectivity

ROOT = Path(__file__).resolve().parent.parent
PCBENCH = ROOT / 'bench/data/freerouting/scripts/benchmark/fixtures/PCBench'
TM = Path(os.environ.get('TM_BINARY', ROOT / 'build/macos-metal/src/app/tracemaker'))
KICAD = os.environ.get('KICAD_CLI', 'kicad-cli')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(board, run):
    summary = run / 'summary.json'
    meta = json.loads(summary.read_text()) if summary.exists() else {}
    explicit = meta.get('fixtures')
    roster = run / 'boards.jsonl'
    prepared = False
    if roster.exists():
        prepared = any(row.get('board') == board and row.get('preparation') for row in
                       (json.loads(line) for line in roster.read_text().splitlines() if line.strip()))
    pcbench = ROOT/'build/pcbench' if prepared else PCBENCH
    roots = ([Path(explicit)] if explicit else []) + [ROOT/'build/modern-planes', ROOT/'build/demos', pcbench]
    return next((p/board for p in roots if (p/board/'unrouted.kicad_pcb').exists()), None)


def sidecars(board, run, name):
    found = {}
    fx = fixture(name, run)
    for ext in ('.kicad_pro', '.kicad_dru'):
        direct = board.with_suffix(ext)
        if direct.exists():
            found[ext] = direct
        elif fx:
            options = [fx/('unrouted'+ext), fx/('routed'+ext)] + sorted(fx.glob('*'+ext))
            match = next((p for p in options if p.exists()), None)
            if match:
                found[ext] = match
    return found


def prepare(board, run, name, version, *, supplied=True):
    """Refill and SAVE a private copy; the original artifact is never modified."""
    rules = sidecars(board, run, name)
    given = board.with_name(name+'.refilled.kicad_pcb')
    use_given = supplied and given.exists() and given != board
    source_bytes=board.read_bytes()
    source_hash=hashlib.sha256(source_bytes).hexdigest()
    analysis_bytes=given.read_bytes() if use_given else source_bytes
    refill_identity=hashlib.sha256(analysis_bytes).hexdigest() if use_given else ''
    rule_bytes={ext:p.read_bytes() for ext,p in rules.items()}
    rule_hashes={ext:hashlib.sha256(data).hexdigest() for ext,data in rule_bytes.items()}
    key = hashlib.sha256((source_hash+version+refill_identity+''.join(ext+rule_hashes[ext] for ext in sorted(rules))).encode()).hexdigest()
    cache = run/'quality_cache'/key
    cache.mkdir(parents=True, exist_ok=True)
    target = cache/board.name
    report = cache/'drc.json'
    provenance = cache/'provenance.json'
    if report.exists() and not target.exists():
        # Identical input/human bytes can share a cache despite different filenames.
        cached = json.loads(provenance.read_text())
        target = Path(cached.get('analysis_board', cache/Path(cached['source']).name))
    from prepare_inputs import repair_design_ids
    repair_code=digest(ROOT/'bench/prepare_inputs.py')
    if not report.exists():
        target.write_bytes(analysis_bytes)
        for ext,data in rule_bytes.items():
            target.with_suffix(ext).write_bytes(data)
        args = [KICAD,'pcb','drc','--format','json','--severity-all','--all-track-errors']
        if not use_given:
            args += ['--refill-zones','--save-board']
        completed = subprocess.run(args+['-o',str(report),str(target)], capture_output=True, text=True, timeout=900)
        if not report.exists():
            raise RuntimeError('KiCad judgment failed: '+completed.stderr[-1000:])
        provenance.write_text(json.dumps(dict(source=str(board), source_sha256=source_hash,
                                              analysis_sha256=digest(target), supplied_refill=use_given,
                                              filler_version=version, rules={ext:dict(path=str(p),sha256=rule_hashes[ext]) for ext,p in rules.items()}), indent=2))
    provenance_data=json.loads(provenance.read_text())
    if provenance_data.get('identity_repair_code_sha256') != repair_code:
        corrected,repairs=repair_design_ids(target.read_text(),name)
        if repairs:
            target.write_text(corrected)
            part=cache/'normalized.drc.json'
            completed=subprocess.run([KICAD,'pcb','drc','--format','json','--severity-all','--all-track-errors',
                                      '--refill-zones','--save-board','-o',str(part),str(target)],
                                     capture_output=True,text=True,timeout=900)
            if not part.exists():
                raise RuntimeError('KiCad judgment after identity repair failed: '+completed.stderr[-1000:])
            part.replace(report)
        provenance_data.update(identity_repairs=repairs,identity_repair_code_sha256=repair_code,
                               analysis_sha256=digest(target),analysis_board=str(target),drc_report=str(report))
        provenance.write_text(json.dumps(provenance_data,indent=2))
    report_data = json.loads(report.read_text())
    connection_key = hashlib.sha256(json.dumps(dict(
        analysis_sha256=digest(target), rules=rule_hashes, kicad_version=version,
        connectivity_code_sha256=digest(Path(connectivity.__file__))),
        sort_keys=True).encode()).hexdigest()
    connection_cache = run/'quality_cache'/('connectivity-'+connection_key+'.json')
    try:
        if connection_cache.exists():
            connection_data = json.loads(connection_cache.read_text())
        else:
            connection_data = connectivity.count_unconnected([target], kicad_cli=KICAD)[str(target)]
            connection_cache.write_text(json.dumps(connection_data, sort_keys=True))
        report_data['connectivity'] = connection_data
        provenance_data['connectivity'] = dict(connection_data, cache=str(connection_cache))
    except connectivity.ConnectivityUnavailable as error:
        report_data['connectivity_unavailable'] = str(error)
        provenance_data['connectivity_unavailable'] = str(error)
    return target, report_data, dict(provenance_data, source=str(board), source_sha256=source_hash)


def inspect(board, run, name):
    """Use content and binary identities, never same-name/mtime-only cache keys."""
    import intent_quality as iq
    cache = run/'quality_cache'/('inspect-'+digest(board)+'-'+digest(TM)[:16])
    cache.mkdir(parents=True, exist_ok=True)
    iq.TM = TM
    return iq.inspect(board, cache)


from drc_judge import bracket_nets, judge as _judge, violations


def judge(before, after):
    """Validate output connectivity even when common-input evaluation failed."""
    result = _judge(after if before is None else before, after)
    if before is None:
        result.update(common_denominator=None, completion=None, already_connected=False,
                      drc_pass=None, error_gate='unknown')
    elif result['common_denominator'] == 0:
        result['completion'] = None  # Already-connected inputs are outside the ratio cohort.
    return result


