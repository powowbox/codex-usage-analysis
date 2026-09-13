#!/usr/bin/env python3
"""Reprice every row in audited token_usage.csv; exclusions belong to the analyzer.
Run from any directory: python3 /path/to/compare_models.py
Writes comparison_* files to outputs/ by default; original audit preserved.
"""
import csv
import argparse
import json
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal as D

BASE = Path(__file__).resolve().parent
KEYS = ['input_tokens', 'cached_input_tokens', 'uncached_input_tokens', 'output_tokens', 'total_tokens']

def write_csv(path, rows):
    with path.open('w', newline='', encoding='utf-8') as f:
        w=csv.DictWriter(f, list(rows[0]));w.writeheader();w.writerows(rows)

def comparison_rows(results):
    for result in sorted(results, key=lambda r: D(r['mean_per_active_day_usd'])):
        name = result['resolved_model'].replace('\n', ' ').replace('\r', ' ')
        daily_cost = D(result['mean_per_active_day_usd'])
        yield name, f'{daily_cost:.2f}', f'{daily_cost * 30:.2f}'

def print_comparison(results, out):
    headers = ('Model', 'Daily (USD)', 'Monthly (USD)')
    rows = list(comparison_rows(results))
    widths = [max(len(row[i]) for row in [headers] + rows) for i in range(3)]
    def format_row(row):
        return f'{row[0]:<{widths[0]}}  {row[1]:>{widths[1]}}  {row[2]:>{widths[2]}}'
    print(format_row(headers))
    print('  '.join('-' * width for width in widths))
    for row in rows:
        print(format_row(row))
    print('\nDaily = active-day average; monthly = daily × 30 active days.')
    print('Optional cache-write surcharge excluded.')
    print(f'Files: {out.resolve()}/')
    print('  comparison_models.md: table; comparison_models.csv: model details;')
    print('  comparison_daily.csv: daily breakdown; comparison_summary.json: full totals and pricing.')

def write_markdown(path, results, summary):
    lines = [
        '# Model Cost Comparison',
        '',
        f"Period: {summary['period_start']} to {summary['period_end']} "
        f"({summary['active_days']} active days).",
        '',
        'Daily cost is the average per active day. Monthly cost assumes 30 active',
        'days at the same usage level (daily cost × 30), not a calendar-month forecast.',
        'Amounts are in USD, sorted from least to most expensive before rounding.',
        'Estimates exclude the optional cache-write surcharge and unobserved costs.',
        '',
        '| Model | Daily cost (USD) | Monthly cost (USD, 30 active days) |',
        '| --- | ---: | ---: |',
    ]
    for name, daily_cost, monthly_cost in comparison_rows(results):
        name = name.replace('|', '&#124;')
        lines.append(f'| {name} | {daily_cost} | {monthly_cost} |')
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', type=Path, default=BASE/'outputs'/'token_usage.csv')
    ap.add_argument('--output-dir', type=Path, default=BASE/'outputs')
    ap.add_argument('--pricing', type=Path, default=BASE/'comparison_pricing.json')
    args=ap.parse_args()
    source=args.input
    out=args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    config=json.loads(args.pricing.read_text(encoding='utf-8'))
    with source.open() as f: rows=list(csv.DictReader(f))
    if not rows:
        ap.error('No consumption found in input CSV')
    for r in rows:
        # Dates already reflect the analyzer's selected timezone.
        datetime.fromisoformat(r['date'])
        r.update({k:int(r[k]) for k in KEYS})
        assert r['input_tokens']==r['uncached_input_tokens']+r['cached_input_tokens']
    active_days=len({r['date'] for r in rows})
    start=min(datetime.fromisoformat(r['date']).date() for r in rows)
    end=max(datetime.fromisoformat(r['date']).date() for r in rows)
    calendar_days=(end-start).days+1
    totals={k:sum(r[k] for r in rows) for k in KEYS}
    summary={'source_sha256':before,'date_basis':'Input CSV dates from analyzer',
             'period_start':str(start),'period_end':str(end),
             'active_days':active_days,'calendar_days':calendar_days,
             'retained_calls':len(rows),'retained_sessions':len({r['session_id'] for r in rows}),
             'usage':totals,'cache_hit_ratio':totals['cached_input_tokens']/totals['input_tokens'],
             'max_input_per_call':max(r['input_tokens'] for r in rows),
             'pricing':config}
    results=[]; daily=[]
    for model in config['models']:
        amounts=[]; upper=[]; peak=[]; off=[]; by_day={}; long_count=0; peak_count=0
        for r in rows:
            is_long=r['input_tokens']>model.get('threshold',float('inf'))
            rates=[D(str(x)) for x in model['long_rates' if is_long else 'rates']]
            long_count+=is_long
            value=sum(D(r[k])*rate for k,rate in zip(['uncached_input_tokens','cached_input_tokens','output_tokens'],rates))/D(1000000)
            high=value
            if model.get('cache_write_multiplier'):
                high+=D(r['uncached_input_tokens'])*rates[0]*(D(str(model['cache_write_multiplier']))-1)/D(1000000)
            if model.get('deepseek_schedule'):
                stamp=datetime.fromisoformat(r['timestamp'].replace('Z','+00:00')).astimezone(timezone.utc)
                schedule=config['deepseek_peak_utc'];minute=stamp.hour*60+stamp.minute
                is_peak=stamp.weekday() in schedule['weekdays'] and any(a<=minute<b for a,b in schedule['intervals_minutes'])
                peak_count+=is_peak
                peak.append(value);off.append(value*D(str(schedule['off_peak_multiplier'])))
                value*=D(1) if is_peak else D(str(schedule['off_peak_multiplier']))
                high=value
            amounts.append(value);upper.append(high)
            by_day[r['date']]=by_day.get(r['date'],D(0))+value
        total=sum(amounts); hi=sum(upper)
        assert sum(by_day.values())==total
        result={'requested_model':model['requested'],'resolved_model':model['resolved'],
                'cost_usd':str(total),'mean_per_active_day_usd':str(total/D(active_days)),
                'mean_per_calendar_day_usd':str(total/D(calendar_days)),
                'cost_with_max_uncached_write_surcharge_usd':str(hi) if model.get('cache_write_multiplier') else None,
                'mean_with_max_uncached_write_surcharge_usd':str(hi/D(active_days)) if model.get('cache_write_multiplier') else None,
                'peak_only_usd':str(sum(peak)) if peak else None,'off_peak_only_usd':str(sum(off)) if off else None,
                'long_context_calls':long_count,'deepseek_peak_calls':peak_count if peak else None,
                'source':model['source']}
        results.append(result)
        daily.extend({'date':day,'requested_model':model['requested'],'cost_usd':str(value)} for day,value in sorted(by_day.items()))
    summary['models']=results
    write_csv(out/'comparison_models.csv',results)
    write_csv(out/'comparison_daily.csv',daily)
    write_markdown(out/'comparison_models.md',results,summary)
    (out/'comparison_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before
    print_comparison(results, out)

if __name__=='__main__':main()
