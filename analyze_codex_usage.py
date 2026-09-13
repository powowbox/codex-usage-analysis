#!/usr/bin/env python3
"""Read-only Codex rollout audit. Python 3.9+, standard library only.

Never import the tracker, ingest logs, or write to Codex. See README.md for
accounting semantics, matching policy, nulls, and reproducible bounded replay.
"""
import argparse
import collections
import csv
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

KEYS = ('input_tokens', 'cached_input_tokens', 'output_tokens',
        'reasoning_output_tokens', 'total_tokens')
SUMS = ('input_tokens', 'cached_input_tokens', 'uncached_input_tokens',
        'output_tokens', 'reasoning_tokens', 'total_tokens')
LABELS = ('task_type', 'success', 'tests_passed', 'build_passed', 'lint_passed',
          'human_accepted', 'retry_count', 'escalated_from_model', 'escalated_to_model')
WEEKDAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')

def dt(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))

def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

def valid(u):
    assert all(type(u.get(k)) is int and u[k] >= 0 for k in KEYS[:3]), u
    assert u['cached_input_tokens'] <= u['input_tokens'], u
    assert u.get('total_tokens', u['input_tokens'] + u['output_tokens']) == u['input_tokens'] + u['output_tokens'], u
    assert u.get('reasoning_output_tokens', 0) <= u['output_tokens'], u
    return u

class Counter:
    """Only token_count uses this state; never mix the two cumulative streams."""
    def __init__(self):
        self.previous = None

    def consume(self, info):
        if not info:
            return None, 'missing_usage'
        total, last = info.get('total_token_usage'), info.get('last_token_usage')
        # Unknown shapes fail explicitly instead of silently changing accounting.
        if not total:
            raise ValueError('Last-only usage needs an explicit reconciliation policy')
        # Compaction/context placeholders can expose total_tokens alone while
        # every billable component is zero. This is not attributable usage.
        if total['input_tokens'] == total['output_tokens'] == 0:
            total = dict(total, total_tokens=0)
        if last and last['input_tokens'] == last['output_tokens'] == 0:
            last = dict(last, total_tokens=0)
        valid(total)
        if last:
            valid(last)
        old, self.previous = self.previous, total
        if old == total:
            return None, 'duplicate_cumulative'
        if old is None or any(total[k] < old[k] for k in KEYS if k in total and k in old):
            if not last:
                raise ValueError('Initial/reset counter without last usage is ambiguous')
            u, status = last, ('initial_last' if old is None else 'counter_reset')
        else:
            u = {k: total[k] - old[k] for k in KEYS if k in total and k in old}
            status = 'cumulative_verified' if u == {k: last[k] for k in u} else 'cumulative_interval'
        valid(u)
        return (u if u['input_tokens'] + u['output_tokens'] else None), status

def discover(home, extra):
    paths = set()
    for root in [home/'sessions', home/'archived_sessions', *extra]:
        if root.is_file():
            paths.add(root.resolve())
        elif root.exists():
            paths.update(p.resolve() for p in root.rglob('*.jsonl'))
    db = home/'state_5.sqlite'
    metadata = {'state_database': str(db), 'state_read_mode': 'mode=ro; query_only', 'missing_rollouts': []}
    if db.exists():
        c = sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            c.execute('PRAGMA query_only=ON')
            c.execute('BEGIN')
            rows = c.execute('SELECT id, rollout_path FROM threads').fetchall()
            metadata['indexed_threads'] = len(rows)
            for sid, name in rows:
                p = Path(name).resolve()
                if p.exists():
                    paths.add(p)
                else:
                    metadata['missing_rollouts'].append({'thread_id': sid, 'path': name})
        finally:
            c.close()
    return sorted(paths), metadata

def aggregate(rows):
    result = {k: sum(r[k] for r in rows) if all(r.get(k) is not None for r in rows) else None for k in SUMS}
    result['cache_hit_ratio'] = result['cached_input_tokens']/result['input_tokens'] if result['input_tokens'] else None
    result['number_of_sessions'] = len({r['session_id'] for r in rows})
    result['number_of_consumption_units'] = len(rows)
    result['number_of_llm_calls'] = len(rows) if all(r['unit_kind'] == 'llm_call' for r in rows) else None
    result['number_of_explicit_response_ids'] = sum(bool(r.get('response_id')) for r in rows)
    return result

def write_csv(path, rows):
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with path.open('w', newline='') as f:
        w = csv.DictWriter(f, fields)
        w.writeheader()
        w.writerows(rows)

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--codex-home', type=Path, default=Path(os.environ.get('CODEX_HOME', Path.home()/'.codex')))
    ap.add_argument('--extra-root', type=Path, action='append', default=[])
    ap.add_argument('--output-dir', type=Path, default=Path(__file__).resolve().parent/'outputs')
    ap.add_argument('--timezone', default='Europe/Paris')
    ap.add_argument('--exclude-days', nargs='+', type=str.lower, choices=WEEKDAYS, default=[],
                    help='Weekdays to exclude in --timezone, e.g. sat sun (default: none)')
    ap.add_argument('--manifest', type=Path, help='Replay exact file prefixes from a previous run')
    args = ap.parse_args()
    out = args.output_dir.resolve()
    assert not out.is_relative_to(args.codex_home.resolve()), 'Output must be outside Codex home'
    out.mkdir(parents=True, exist_ok=True)
    tz = ZoneInfo(args.timezone)
    started = datetime.now(timezone.utc).isoformat()
    if args.manifest:
        prior = json.loads(args.manifest.read_text())
        sources, discovery = prior['sources'], prior['discovery']
    else:
        paths, discovery = discover(args.codex_home, args.extra_root)
        sources = [{'path': str(p), 'size_bytes': p.stat().st_size} for p in paths]
    diagnostics = collections.Counter()
    rows, sessions_meta, evidence, integrity = [], {}, [], []
    seen_response, seen_event = {}, set()
    for source in sources:
        path = Path(source['path'])
        with path.open('rb') as f:
            data = f.read(source['size_bytes'])
        digest = hashlib.sha256(data).hexdigest()
        if source.get('sha256'):
            assert source['sha256'] == digest, f'Changed source prefix: {path}'
        source['sha256'] = digest
        source['complete_lines'] = data.count(b'\n')
        if data and not data.endswith(b'\n'):
            diagnostics['trailing_partial_line_excluded'] += 1
        events, records, metas = [], [], []
        counter, turn, model, provider, first, last_ts = Counter(), None, None, None, None, None
        for line_no, line in enumerate(data.split(b'\n')[:-1], 1):
            if not line.strip():
                continue
            d = json.loads(line)  # malformed complete lines fail loudly
            p, typ, ts = d.get('payload') or {}, d.get('type'), d.get('timestamp')
            if ts:
                first = min(first, ts) if first else ts
                last_ts = max(last_ts, ts) if last_ts else ts
            if typ == 'session_meta':
                metas.append(p)
                provider = p.get('model_provider', provider)
                model = p.get('model', model)
            if typ == 'turn_context' or typ == 'event_msg' and p.get('type') in ('task_started', 'thread_settings_applied'):
                turn = p.get('turn_id', turn)
                model = p.get('model', model)
                provider = p.get('model_provider', provider)
            if typ == 'token_usage_record':
                u = valid(p['usage'])
                status = 'explicit_response'
                target = records
                diagnostics['raw_token_usage_records'] += 1
            elif typ == 'event_msg' and p.get('type') == 'token_count':
                for key in ('total_token_usage', 'last_token_usage'):
                    raw = (p.get('info') or {}).get(key) or {}
                    if raw.get('total_tokens', 0) != raw.get('input_tokens', 0) + raw.get('output_tokens', 0):
                        diagnostics[key+'_inconsistent_context_placeholder'] += 1
                u, status = counter.consume(p.get('info'))
                diagnostics[status] += 1
                if not u:
                    continue
                target = events
            else:
                continue
            row = {'timestamp': ts, 'date': dt(ts).astimezone(tz).date().isoformat(),
                   'task_id': p.get('turn_id', turn), 'provider': provider, 'model': model,
                   'input_tokens': u['input_tokens'], 'cached_input_tokens': u['cached_input_tokens'],
                   'uncached_input_tokens': max(u['input_tokens']-u['cached_input_tokens'], 0),
                   'output_tokens': u['output_tokens'], 'reasoning_tokens': u.get('reasoning_output_tokens'),
                   'total_tokens': u['input_tokens']+u['output_tokens'],
                   'response_id': p.get('response_id'), 'usage_status': status,
                   'unit_kind': 'cumulative_interval' if status == 'cumulative_interval' else 'llm_call',
                   'source_file': str(path), 'source_line': line_no,
                   'thread_id': p.get('thread_id'), 'root_session_id': p.get('session_id'),
                   'root_task_id': p.get('root_turn_id'),
                   'matching_event_line': None}
            target.append(row)
        assert metas, f'No session metadata: {path}'
        ids = {m['id'] for m in metas}
        assert len(ids) == 1 or metas[0].get('forked_from_id'), f'Multiple thread identities: {path}'
        meta, sid = metas[0], metas[0]['id']
        if meta.get('forked_from_id'):
            first = meta['timestamp']
            diagnostics['fork_with_inherited_metadata'] += 1
        sessions_meta[sid] = {'timestamp_start': first, 'timestamp_end': last_ts,
                              'parent_thread_id': meta.get('parent_thread_id'),
                              'forked_from_id': meta.get('forked_from_id'),
                              'session_kind': meta.get('thread_source') or json.dumps(meta.get('source'))}
        # Match only within the same turn, with exact full usage and one-to-one
        # multiplicity. Never merge records merely because their usage is equal.
        def fingerprint(r):
            return (r['task_id'], *(r[k] for k in SUMS))
        pool = collections.defaultdict(collections.deque)
        for r in records:
            pool[fingerprint(r)].append(r)
        selected = list(records)
        for e in events:
            q = pool[fingerprint(e)]
            if q:
                r = q.popleft()
                r['matching_event_line'] = e['source_line']
                diagnostics['paired_record_event'] += 1
            else:
                selected.append(e)
                diagnostics['legacy_event_only'] += 1
        for r in selected:
            # A child's session_meta.session_id may be its root, not its own id.
            r['session_id'] = r.pop('thread_id') or sid
            r['root_session_id'] = r['root_session_id'] or meta.get('session_id')
            if r['session_id'] != sid:
                diagnostics['inherited_record_excluded'] += 1
                continue
            if meta.get('forked_from_id') and r['timestamp'] < meta['timestamp']:
                diagnostics['inherited_fork_event_excluded'] += 1
                continue
            if r['response_id']:
                rid = r['response_id']
                if rid in seen_response:
                    assert seen_response[rid] == tuple(r[k] for k in SUMS)
                    diagnostics['duplicate_response_id'] += 1
                    continue
                seen_response[rid] = tuple(r[k] for k in SUMS)
            else:
                key = (sid, r['timestamp'], fingerprint(r))
                if key in seen_event:
                    diagnostics['duplicate_legacy_event'] += 1
                    continue
                seen_event.add(key)
            r['parent_thread_id'] = meta.get('parent_thread_id')
            r['session_kind'] = sessions_meta[sid]['session_kind']
            rows.append(r)
        source['session_id'] = sid
    rows.sort(key=lambda r: (r['timestamp'], r['session_id'], r['source_line']))
    assert rows, 'No consumption found'
    assert len(seen_response) == sum(bool(r['response_id']) for r in rows)
    excluded_days = [day for day in WEEKDAYS if day in args.exclude_days]
    # Filter only after reconstructing counters and matching responses so that
    # excluded-day usage cannot leak into the next retained cumulative delta.
    excluded_rows = [r for r in rows if WEEKDAYS[dt(r['date']).weekday()] in excluded_days]
    rows = [r for r in rows if WEEKDAYS[dt(r['date']).weekday()] not in excluded_days]
    if not rows:
        ap.error('No consumption remains after excluding the selected weekdays')
    grouped = {}
    for name, keys in [('sessions', ('session_id',)), ('daily_summary', ('date',)),
                       ('model_summary', ('provider','model')), ('session_model_summary', ('session_id','provider','model'))]:
        buckets = collections.defaultdict(list)
        for r in rows:
            buckets[tuple(r.get(k) for k in keys)].append(r)
        result = []
        for values, group in sorted(buckets.items(), key=lambda kv: str(kv[0])):
            row = dict(zip(keys, values))
            row.update(aggregate(group))
            if name == 'sessions':
                meta = sessions_meta[row['session_id']]
                row.update(meta)
                row['date'] = dt(meta['timestamp_start']).astimezone(tz).date().isoformat()
                row['duration_seconds'] = (dt(meta['timestamp_end'])-dt(meta['timestamp_start'])).total_seconds()
                for k in ('task_id','provider','model'):
                    unique = {r.get(k) for r in group}
                    row[k] = next(iter(unique)) if len(unique) == 1 else None
                row['models_json'] = json.dumps(sorted({r['model'] for r in group if r['model']}))
                row['task_ids_json'] = json.dumps(sorted({r['task_id'] for r in group if r['task_id']}))
                row.update({k: None for k in LABELS})
            result.append(row)
        grouped[name] = result
        write_csv(out/(name+'.csv'), result)
    write_csv(out/'token_usage.csv', rows)
    summary = aggregate(rows)
    summary.update({'timezone': args.timezone, 'analysis_started_at_utc': started,
                    'excluded_days': excluded_days,
                    'excluded_usage': aggregate(excluded_rows),
                    'period_start': rows[0]['date'], 'period_end': rows[-1]['date'],
                    'first_usage_timestamp': rows[0]['timestamp'], 'last_usage_timestamp': rows[-1]['timestamp'],
                    'active_days': len(grouped['daily_summary']),
                    'diagnostics': dict(diagnostics), 'discovery': discovery,
                    'source_files': len(sources), 'sessions_without_usage': len(sessions_meta)-len(grouped['sessions'])})
    # Validate the emitted CSVs, not just the in-memory aggregations.
    validation = {'csv_sums': {}, 'subset_relations_passed': True}
    for name in ['token_usage', *grouped]:
        with (out/(name+'.csv')).open() as f:
            loaded = list(csv.DictReader(f))
        for k in SUMS:
            if summary[k] is not None:
                assert sum(int(r[k]) for r in loaded) == summary[k], (name,k)
        validation['csv_sums'][name] = 'passed'
    retained_response_ids = [r['response_id'] for r in rows if r['response_id']]
    assert len(retained_response_ids) == len(set(retained_response_ids))
    validation['response_ids_unique'] = True
    # Re-read all analyzed prefixes: concurrent appends are allowed, edits are not.
    for source in sources:
        p = Path(source['path'])
        with p.open('rb') as f:
            current = hashlib.sha256(f.read(source['size_bytes'])).hexdigest()
        assert current == source['sha256'], f'Log changed inside analyzed prefix: {p}'
        integrity.append({'path': str(p), 'prefix_unchanged': True,
                          'appended_bytes_during_run': p.stat().st_size-source['size_bytes']})
    validation['all_source_prefixes_sha256_unchanged'] = True
    validation['source_integrity'] = integrity
    validation['known_ambiguities'] = [
        'Initial and reset last usage is counted; inherited cumulative history is not.',
        'task_id means explicit turn_id; a session can contain multiple tasks.',
        'Session duration is observed wall-clock span, not compute latency.',
        'No success, test, acceptance, retry, or escalation labels inferred.',
        'Provider/model reflect logged context; backend billing identity is not proven.',
        'Missing/deleted/unflushed/remote logs and unreported failures are outside coverage.'
    ]
    dump(out/'summary.json', summary)
    dump(out/'validation.json', validation)
    dump(out/'source_manifest.json', {'analysis_started_at_utc': started, 'discovery': discovery, 'sources': sources})
    print(json.dumps({k: summary[k] for k in ['input_tokens','cached_input_tokens','uncached_input_tokens','output_tokens','total_tokens','cache_hit_ratio','number_of_sessions','number_of_llm_calls','period_start','period_end','excluded_days','diagnostics']}, indent=2))

if __name__ == '__main__':
    main()
