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


def make_workspace(directory, tools=('docker', 'kubectl', 'kind')):
    """스크립트 사본과 도구 대역만 있는 작업 공간. 클러스터도 네트워크도 쓰지 않는다."""
    root = Path(directory)
    scripts = root / 'scripts'
    scripts.mkdir()
    for path in (ROOT / 'scripts').glob('*.sh'):
        (scripts / path.name).write_text(path.read_text(encoding='utf-8'), encoding='utf-8', newline='\n')
    # Litmus 설치는 이 테스트의 관심사가 아니므로 성공만 돌려주는 대역으로 바꾼다.
    (scripts / 'bootstrap_litmus.sh').write_text('#!/usr/bin/env bash\nexit 0\n', encoding='utf-8', newline='\n')
    stubs = root / 'stubs'
    stubs.mkdir()
    for tool in tools:
        target = stubs / tool
        target.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8', newline='\n')
        target.chmod(0o755)
    for path in scripts.glob('*.sh'):
        path.chmod(0o755)
    return root, stubs


@unittest.skipUnless(BASH, 'Bash is needed for POSIX bootstrap control-flow tests')
class SourcedBootstrapTests(unittest.TestCase):
    """source 로 실행되는 스크립트가 호출한 셸을 망가뜨리지 않는지 본다.

    user-data 와 SSM 세션이 이 경로를 그대로 쓴다. 호출한 셸이 죽으면 뒤 단계가
    통째로 실행되지 않고, set 옵션이 남으면 같은 셸에서 띄우는 uvicorn 이 죽는다.
    """

    def run_shell(self, body, stubs, extra_path=None):
        environment = dict(os.environ, PATH=f'{stubs}{os.pathsep}' + (extra_path or os.environ['PATH']))
        environment.pop('BASH_ENV', None)
        environment.pop('CODEREFEREE_PYTHON', None)
        return subprocess.run([BASH, '-c', body], env=environment, capture_output=True, text=True, timeout=120)

    def test_missing_prerequisite_does_not_kill_the_calling_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            root, stubs = make_workspace(directory, tools=())
            result = self.run_shell(
                f'cd {root}; source scripts/bootstrap_kind.sh codereferee >/dev/null 2>&1; '
                'echo "rc=$?"; echo STILL_ALIVE', stubs)
        self.assertIn('rc=1', result.stdout)
        self.assertIn('STILL_ALIVE', result.stdout, 'sourced exit must not terminate the caller')

    def test_shell_options_and_helpers_do_not_leak_into_the_caller(self):
        with tempfile.TemporaryDirectory() as directory:
            root, stubs = make_workspace(directory)
            result = self.run_shell(
                f'cd {root}; before="$-"; source scripts/bootstrap_kind.sh codereferee >/dev/null 2>&1; '
                'echo "same_flags=$([ "$before" = "$-" ] && echo yes || echo no)"; '
                'echo "leftovers=${_codereferee_opts_kind-none}${_codereferee_rc_kind-none}"; '
                'echo "unset_ref=${NEVER_DEFINED-fallback}"; echo STILL_ALIVE', stubs)
        self.assertIn('same_flags=yes', result.stdout, 'set -euo pipefail must not persist in the caller')
        self.assertIn('leftovers=nonenone', result.stdout)
        self.assertIn('unset_ref=fallback', result.stdout, 'nounset must not break later commands')
        self.assertIn('STILL_ALIVE', result.stdout)

    def test_cluster_variables_reach_the_calling_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            root, stubs = make_workspace(directory)
            result = self.run_shell(
                f'cd {root}; source scripts/bootstrap_kind.sh codereferee >/dev/null 2>&1; '
                'echo "context=$CODEREFEREE_KUBECTL_CONTEXT provider=$CODEREFEREE_CLUSTER_PROVIDER"', stubs)
        self.assertIn('context=kind-codereferee provider=kind', result.stdout)

    def test_callers_existing_errexit_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root, stubs = make_workspace(directory)
            for script in ('bootstrap_kind.sh', 'bootstrap_local.sh'):
                result = self.run_shell(
                    f'cd {root}; set -e; before="$-"; source scripts/{script} codereferee >/dev/null 2>&1; '
                    'after="$-"; echo "same_flags=$([ "$before" = "$after" ] && echo yes || echo no)"', stubs)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('same_flags=yes', result.stdout, script)

    def test_kubectl_is_installed_when_the_host_does_not_have_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root, stubs = make_workspace(directory, tools=('docker', 'kind'))
            installer = root / 'scripts' / 'install_kubectl.sh'
            marker = root / 'installed'
            installer.write_text(f'#!/usr/bin/env bash\ntouch {marker}\nexit 0\n', encoding='utf-8', newline='\n')
            installer.chmod(0o755)
            self.run_shell(f'cd {root}; source scripts/bootstrap_local.sh codereferee >/dev/null 2>&1',
                           stubs, extra_path='/usr/bin:/bin')
            self.assertTrue(marker.exists(), 'bootstrap must install kubectl on a host that lacks it')


@unittest.skipUnless(BASH, 'Bash is needed for POSIX bootstrap control-flow tests')
class PythonInterpreterTests(unittest.TestCase):
    """Amazon Linux 2023 의 기본 python3 은 3.9 이고, app 은 PEP 604 union 을 쓴다.

    uvicorn 을 띄운 뒤 import 에서 TypeError 로 알게 되는 대신 부트스트랩에서 먼저 거른다.
    """

    #: bootstrap_local.sh 가 순서대로 찾아보는 이름들. 전부 대역으로 덮어야 호스트에
    #: 실제로 설치된 인터프리터가 끼어들지 않는다.
    CANDIDATES = ('python3.13', 'python3.12', 'python3.11', 'python3.10', 'python3', 'python')

    def workspace(self, directory, version):
        root, stubs = make_workspace(directory)
        for name in self.CANDIDATES:
            fake = stubs / name
            fake.write_text('#!/bin/sh\n'
                            f'if [ "$1" = "-c" ]; then exit {0 if version >= (3, 10) else 1}; fi\nexit 0\n',
                            encoding='utf-8', newline='\n')
            fake.chmod(0o755)
        return root, stubs

    def run_only_with(self, root, stubs):
        # 호스트에 설치된 다른 파이썬이 끼어들지 않도록 PATH 를 대역과 coreutils 로 한정한다.
        environment = dict(os.environ, PATH=f'{stubs}:/usr/bin:/bin')
        environment.pop('CODEREFEREE_PYTHON', None)
        return subprocess.run(
            [BASH, '-c', f'cd {root}; source scripts/bootstrap_local.sh codereferee 2>&1; '
                         'echo "rc=$? python=${CODEREFEREE_PYTHON-none}"'],
            env=environment, capture_output=True, text=True, timeout=120)

    def test_python_39_is_rejected_with_an_actionable_message(self):
        with tempfile.TemporaryDirectory() as directory:
            root, stubs = self.workspace(directory, (3, 9))
            result = self.run_only_with(root, stubs)
        self.assertIn('python=none', result.stdout)
        self.assertIn('Python 3.10 or newer', result.stdout)
        self.assertIn('dnf install', result.stdout, 'the message should say how to fix it')

    def test_candidate_list_matches_the_script(self):
        script = (ROOT / 'scripts' / 'bootstrap_local.sh').read_text(encoding='utf-8')
        line = next(l for l in script.splitlines() if 'for candidate in' in l)
        self.assertEqual(list(self.CANDIDATES), re.findall(r'python3?(?:\.\d+)?', line))

    def test_python_310_or_newer_is_exported(self):
        with tempfile.TemporaryDirectory() as directory:
            root, stubs = self.workspace(directory, (3, 11))
            result = self.run_only_with(root, stubs)
        self.assertIn('rc=0', result.stdout)
        self.assertIn('python3', result.stdout)
        self.assertNotIn('python=none', result.stdout)


if __name__ == '__main__':
    unittest.main()
