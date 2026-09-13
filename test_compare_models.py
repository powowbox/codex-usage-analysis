"""Comparison uses all supplied rows and the analyzer's local dates."""
import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal


class ComparisonTests(unittest.TestCase):
    def test_sunday_and_analyzer_dates_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'usage.csv'
            with source.open('w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['timestamp', 'date', 'session_id', 'input_tokens',
                                 'cached_input_tokens', 'uncached_input_tokens',
                                 'output_tokens', 'total_tokens'])
                # UTC dates deliberately differ from Europe/Paris dates.
                writer.writerow(['2026-09-13T23:30:00Z', '2026-09-13', 'test',
                                 100, 0, 100, 0, 100])
                writer.writerow(['2026-09-15T23:30:00Z', '2026-09-15', 'test',
                                 200, 0, 200, 0, 200])
            before = source.read_bytes()
            pricing = root / 'pricing.json'
            pricing.write_text(json.dumps({'models': [dict(requested='test',
                resolved='Test model', rates=[1000000, 0, 0], source='synthetic')]}))
            run = subprocess.run([sys.executable, str(Path(__file__).with_name('compare_models.py')),
                '--input', str(source), '--pricing', str(pricing), '--output-dir', str(root)],
                check=True, capture_output=True, text=True)
            summary = json.loads((root / 'comparison_summary.json').read_text())
            self.assertEqual(summary['retained_calls'], 2)
            self.assertEqual(summary['active_days'], 2)
            self.assertEqual(summary['calendar_days'], 3)
            model = summary['models'][0]
            self.assertEqual(Decimal(model['cost_usd']), 300)
            self.assertEqual(Decimal(model['mean_per_active_day_usd']), 150)
            self.assertEqual(Decimal(model['mean_per_calendar_day_usd']), 100)
            self.assertNotIn('Sundays excluded', run.stdout)
            self.assertNotIn('Sundays excluded', (root / 'comparison_models.md').read_text())
            self.assertEqual(source.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
