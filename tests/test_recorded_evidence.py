"""Validate curated observations without assigning AI/SLO training labels."""
import json
from pathlib import Path
import unittest


class RecordedEvidenceTests(unittest.TestCase):
    def test_real_samples_have_consistent_counts_and_no_credentials(self):
        folder = Path(__file__).resolve().parents[1] / 'data/chaos/runtime-kind-2026-10-07'
        files = sorted(folder.glob('*.json'))
        self.assertEqual(len(files), 12)
        for path in files:
            with self.subTest(sample=path.name):
                sample = json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual(sample['observationStatus'], 'observed')
                self.assertEqual(sample['exitCode'], 0)
                self.assertTrue(sample['observation']['recovered'])
                self.assertTrue(sample['source']['real_execution_observed'])
                metrics = sample['metrics']
                total = metrics['probe_count']
                self.assertGreater(total, 0)
                self.assertEqual(total, metrics['success_count'] + metrics['failure_count'])
                self.assertAlmostEqual(metrics['availability'], metrics['success_count'] / total)
                self.assertAlmostEqual(metrics['error_rate'], metrics['failure_count'] / total)
                self.assertIsNone(metrics['cpu_usage_percent'])
                self.assertIsNone(metrics['memory_usage_mb'])
                self.assertNotIn('training_label', sample)
                self.check_keys(sample)

    def check_keys(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                self.assertNotIn(key.lower(), {'password', 'authorization', 'access_token', 'cookie'})
                self.check_keys(item)
        elif isinstance(value, list):
            for item in value:
                self.check_keys(item)
