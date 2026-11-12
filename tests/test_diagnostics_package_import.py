"""Do not let other tests' scripts/ sys.path additions hide API import errors."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class DiagnosticsPackageImportTests(unittest.TestCase):
    def test_api_import_works_in_an_isolated_interpreter(self):
        root = str(Path(__file__).resolve().parents[1])
        command = ('import sys; sys.path.insert(0, ' + repr(root) + '); '
                   'from scripts.pod_diagnostics import collect_pod_diagnostics; '
                   'assert callable(collect_pod_diagnostics)')
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-I', '-c', command], cwd=directory,
                                    capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
