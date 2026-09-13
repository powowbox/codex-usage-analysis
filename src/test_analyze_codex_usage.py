"""Accounting regression tests; run: python3 -m unittest discover -s PATH."""
import unittest
import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from analyze_codex_usage import Counter, aggregate

def u(i, c, o, r=0):
    return dict(input_tokens=i, cached_input_tokens=c, output_tokens=o,
                reasoning_output_tokens=r, total_tokens=i+o)

class AccountingTests(unittest.TestCase):
    def test_cumulative_repeat_and_resume(self):
        c=Counter()
        a,s=c.consume(dict(total_token_usage=u(100,60,10),last_token_usage=u(100,60,10)))
        self.assertEqual(a['input_tokens'],100)
        self.assertIsNone(c.consume(dict(total_token_usage=u(100,60,10),last_token_usage=u(100,60,10)))[0])
        a,s=c.consume(dict(total_token_usage=u(140,90,15),last_token_usage=u(40,30,5)))
        self.assertEqual((a['input_tokens'],s),(40,'cumulative_verified'))

    def test_initial_inherited_total_not_charged(self):
        a,s=Counter().consume(dict(total_token_usage=u(1000,900,100),last_token_usage=u(50,40,5)))
        self.assertEqual(a['input_tokens'],50)

    def test_reset_only_last(self):
        c=Counter(); c.consume(dict(total_token_usage=u(1000,900,100),last_token_usage=u(1000,900,100)))
        a,s=c.consume(dict(total_token_usage=u(100,70,10),last_token_usage=u(30,20,3)))
        self.assertEqual((a['input_tokens'],s),(30,'counter_reset'))

    def test_context_placeholder_not_billable(self):
        c=Counter();a=u(0,0,0);a['total_tokens']=19396
        self.assertIsNone(c.consume(dict(total_token_usage=a,last_token_usage=a))[0])

    def test_cache_and_reasoning_not_added_twice(self):
        row={'input_tokens':1000000,'uncached_input_tokens':400000,
             'cached_input_tokens':600000,'output_tokens':10000,'reasoning_tokens':3000,
             'total_tokens':1010000,'session_id':'synthetic','unit_kind':'llm_call'}
        result=aggregate([row])
        self.assertEqual(result['input_tokens'],1000000)
        self.assertEqual(result['output_tokens'],10000)
        self.assertEqual(result['total_tokens'],1010000)
        self.assertEqual(result['cache_hit_ratio'],.6)
        self.assertFalse(any('cost' in key or 'pricing' in key for key in result))

    def test_invalid_subset_fails(self):
        with self.assertRaises(AssertionError):
            Counter().consume(dict(total_token_usage=u(10,11,1),last_token_usage=u(10,11,1)))

    def test_excluded_days_preserve_counter_deltas_and_timezone(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            sessions=root/'codex'/'sessions'
            sessions.mkdir(parents=True)
            events=[dict(type='session_meta',timestamp='2026-09-11T22:30:00Z',
                         payload=dict(id='synthetic'))]
            for timestamp,total,last in [('2026-09-11T22:30:00Z',100,100),
                                         ('2026-09-12T22:30:00Z',300,200),
                                         ('2026-09-13T22:30:00Z',350,50)]:
                events.append(dict(type='event_msg',timestamp=timestamp,
                    payload=dict(type='token_count',info=dict(
                        total_token_usage=u(total,0,0),last_token_usage=u(last,0,0)))))
            (sessions/'test.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
            command=[sys.executable,str(Path(__file__).with_name('analyze_codex_usage.py')),
                     '--codex-home',str(root/'codex'),'--output-dir',str(root/'out')]
            run=subprocess.run(command+['--exclude-days','SUN'],capture_output=True,text=True,check=True)
            self.assertEqual(json.loads(run.stdout)['input_tokens'],150)
            summary=json.loads((root/'out'/'summary.json').read_text())
            self.assertEqual(summary['excluded_days'],['sun'])
            self.assertEqual(summary['excluded_usage']['input_tokens'],200)
            with (root/'out'/'token_usage.csv').open() as f:
                rows=list(csv.DictReader(f))
            self.assertEqual([r['date'] for r in rows],['2026-09-12','2026-09-14'])
            self.assertEqual([r['input_tokens'] for r in rows],['100','50'])
            run=subprocess.run(command,capture_output=True,text=True,check=True)
            self.assertEqual(json.loads(run.stdout)['input_tokens'],350)
            run=subprocess.run(command+['--exclude-days','sat','sun','mon'],capture_output=True,text=True)
            self.assertEqual(run.returncode,2)
            self.assertIn('No consumption remains',run.stderr)

if __name__=='__main__':
    unittest.main()
