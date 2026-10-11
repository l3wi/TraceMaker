# SPDX-License-Identifier: GPL-3.0-or-later
"""Content-addressed KiCad analysis artifacts, including project/custom-rule inputs."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

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
    return target, json.loads(report.read_text()), dict(provenance_data, source=str(board), source_sha256=source_hash)


def inspect(board, run, name):
    """Use content and binary identities, never same-name/mtime-only cache keys."""
    import intent_quality as iq
    cache = run/'quality_cache'/('inspect-'+digest(board)+'-'+digest(TM)[:16])
    cache.mkdir(parents=True, exist_ok=True)
    iq.TM = TM
    return iq.inspect(board, cache)


def violations(report):
    """Canonical violation-instance multiset; no cancellation across locations/items."""
    from collections import Counter
    import re
    def signature(v):
        items = []
        for item in v.get('items', []):
            # Native legacy conversion regenerates UUIDs and PCBench renames
            # references. Physical item kind/net/layer/location is the identity.
            description=re.sub(r'\s+of\s+\S+', '', item.get('description',''))
            position=item.get('pos',{})
            identity=item.get('uuid','') if not position else ''
            items.append((identity,description,json.dumps(position,sort_keys=True)))
        return (v.get('type'), v.get('severity'), tuple(sorted(items)), v.get('description','') if not items else '')
    return Counter(signature(v) for v in report.get('violations',[]) if v.get('severity') == 'error')


def judge(before, after):
    from collections import Counter
    ub, ua = len(before.get('unconnected_items',[])), len(after.get('unconnected_items',[]))
    added = violations(after)-violations(before)
    counts = Counter()
    for signature,n in added.items():
        counts[signature[0]] += n
    censored = ub >= 499 or ua >= 499
    return dict(judge='KiCad', common_denominator=ub, unconnected=ua, censored=censored,
                completion=None if censored or ub==0 else max(0,1-ua/ub), already_connected=ub==0,
                electrical_pass=ua==0, added_errors=dict(sorted(counts.items())), drc_pass=not added,
                denominator_method='uncapped KiCad unconnected-list ratio; unknown at 499 saturation',
                engine_denominator_used=False,
                unconnected_nets=sorted({net for v in after.get('unconnected_items',[]) for item in v.get('items',[]) for net in bracket_nets(item.get('description',''))}))


def bracket_nets(text):
    import re
    return re.findall(r'\[([^\]]+)\]',text)
