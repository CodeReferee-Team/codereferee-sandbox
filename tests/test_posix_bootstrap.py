"""Execute Bash control flow without downloads or touching a Kubernetes cluster."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which('bash')
FAULTS = ['pod-delete', 'container-kill', 'pod-cpu-hog', 'pod-network-latency', 'pod-network-loss', 'pod-memory-hog']


@unittest.skipUnless(BASH, 'Bash is needed for POSIX bootstrap control-flow tests')
class PosixBootstrapTests(unittest.TestCase):
    def prepare(self, directory, corrupt=None):
        root = Path(directory)
        scripts = root / 'scripts'
        scripts.mkdir()
        for path in (ROOT / 'scripts').glob('*.sh'):
            (scripts / path.name).write_text(path.read_text(encoding='utf-8'), encoding='utf-8', newline='\n')
        manifests = root / '.runtime' / 'litmus'
        manifests.mkdir(parents=True)
        (manifests / 'litmus-operator-v3.29.0.yaml').write_text('name: chaos-operator-ce\nimage: chaos-operator:3.29.0\n', encoding='utf-8')
        for fault in FAULTS:
            version = '3.30.1' if corrupt == fault else '3.30.0'
            (manifests / f'{fault}-v3.30.0.yaml').write_text(
                f'kind: ChaosExperiment\nmetadata:\n  name: {fault}\nspec:\n  image: "go-runner:{version}"\n', encoding='utf-8', newline='\n')
        return scripts / 'bootstrap_litmus.sh'

    def run_script(self, script, fail=''):
        shell = '''kubectl() {
          printf 'KUBECTL %s\\n' "$*"
          if [[ -n "$FAIL_MATCH" && "$*" == *"$FAIL_MATCH"* ]]; then return 17; fi
        }
        curl() { echo UNEXPECTED_DOWNLOAD >&2; return 99; }
        source "$1" kind-test'''
        environment = dict(os.environ, FAIL_MATCH=fail)
        environment.pop('BASH_ENV', None)
        return subprocess.run([BASH, '-c', shell, '--', script.as_posix()],
                              env=environment, capture_output=True, text=True, timeout=20)

    def test_all_faults_installed_with_explicit_context(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_script(self.prepare(directory))
        self.assertEqual(result.returncode, 0, result.stderr)
        for fault in FAULTS:
            self.assertIn(f'{fault}-v3.30.0.yaml', result.stdout)
        self.assertEqual(result.stdout.count('-n litmus apply -f'), 6)
        self.assertNotIn('UNEXPECTED_DOWNLOAD', result.stderr)
        self.assertTrue(all('--context kind-test' in line for line in result.stdout.splitlines()))

    def test_invalid_cached_fault_blocks_all_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_script(self.prepare(directory, corrupt='pod-memory-hog'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('pod-memory-hog', result.stderr)
        self.assertNotIn('apply -f', result.stdout)

    def test_apply_failure_stops_before_later_faults(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_script(self.prepare(directory), fail='pod-cpu-hog-v3.30.0.yaml')
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertNotIn('pod-network-latency-v3.30.0.yaml', result.stdout)
        self.assertNotIn('get chaosexperiments', result.stdout)

    def test_syntax_and_windows_fault_parity(self):
        for script in (ROOT / 'scripts').glob('*.sh'):
            result = subprocess.run([BASH, '-n'], input=script.read_text(encoding='utf-8'),
                                    text=True, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
        powershell = (ROOT / 'scripts' / 'bootstrap_litmus.ps1').read_text(encoding='utf-8')
        additional = re.search(r'\$runtimeFaults = @\(([^)]+)\)', powershell).group(1)
        self.assertEqual(FAULTS[2:], re.findall(r"'([^']+)'", additional))


if __name__ == '__main__':
    unittest.main()
