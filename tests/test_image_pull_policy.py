import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import run_litmus_pod_delete as litmus  # noqa: E402


class ImagePullPolicyTests(unittest.TestCase):
    """Litmus 이미지는 클러스터에 있으면 다시 받지 않는다.

    공식 차트는 Always라 실험마다 레지스트리에 묻는다. Docker Hub가 429를 주거나
    레지스트리가 잠깐 안 닿으면, 이미 받아둔 이미지로 돌 수 있는 실험이 인프라 오류로 끝난다.
    """

    def test_helper_pods_reuse_cached_image(self):
        for scenario in litmus.LITMUS_SCENARIOS:
            with self.subTest(scenario=scenario):
                env = litmus.fault_environment(scenario, 'api', 15)
                self.assertEqual(env['LIB_IMAGE_PULL_POLICY'], 'IfNotPresent')

    @patch('run_litmus_pod_delete.subprocess.run')
    def test_copied_experiment_job_reuses_cached_image(self, run):
        chart = {'apiVersion': 'litmuschaos.io/v1alpha1', 'kind': 'ChaosExperiment',
                 'metadata': {'name': 'pod-delete', 'namespace': 'litmus', 'uid': 'x'},
                 'spec': {'definition': {'image': 'go-runner:3.30.0', 'imagePullPolicy': 'Always'}}}
        run.side_effect = [subprocess.CompletedProcess([], 0, json.dumps(chart), ''),
                           subprocess.CompletedProcess([], 0, '', '')]
        litmus.copy_experiment('target', 'pod-delete')
        applied = json.loads(run.call_args_list[1].kwargs['input'])
        self.assertEqual(applied['spec']['definition']['imagePullPolicy'], 'IfNotPresent')
        self.assertEqual(applied['metadata']['namespace'], 'target')

    @patch('run_litmus_pod_delete.copy_experiment')
    @patch('run_litmus_pod_delete.subprocess.run')
    def test_chaos_runner_reuses_cached_image(self, run, _copy):
        run.return_value = subprocess.CompletedProcess([], 0, '', '')
        litmus.apply('target', 'engine', 'api', 'app=api', 'pod_delete', 'api')
        documents = list(yaml.safe_load_all(run.call_args.kwargs['input']))
        engine = next(d for d in documents if d['kind'] == 'ChaosEngine')
        self.assertEqual(engine['spec']['components']['runner']['imagePullPolicy'], 'IfNotPresent')


class MissingExperimentTests(unittest.TestCase):
    """클러스터에서 ChaosExperiment 정의가 빠져 있으면 받아둔 차트로 다시 설치한다.

    부트스트랩이 operator rollout에서 멈추면 실험 정의가 하나도 설치되지 않는다. 그러면
    묶음의 모든 시나리오가 `kubectl get chaosexperiment` NotFound로 인프라 오류가 된다.
    차트 파일은 디스크에 있으므로 샌드박스가 스스로 메울 수 있다.
    """

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / 'pod-delete-v3.30.0.yaml').write_text('kind: ChaosExperiment\n', encoding='utf-8')
        self.chart = {'apiVersion': 'litmuschaos.io/v1alpha1', 'kind': 'ChaosExperiment',
                      'metadata': {'name': 'pod-delete', 'namespace': 'litmus'},
                      'spec': {'definition': {'image': 'go-runner:3.30.0'}}}

    @patch('run_litmus_pod_delete.subprocess.run')
    def test_reinstalls_missing_definition_from_cached_chart(self, run):
        not_found = subprocess.CompletedProcess([], 1, '', 'Error from server (NotFound): chaosexperiments "pod-delete" not found')
        ok = subprocess.CompletedProcess([], 0, '', '')
        run.side_effect = [not_found, ok, subprocess.CompletedProcess([], 0, json.dumps(self.chart), ''), ok]
        with patch.object(litmus, 'MANIFEST_DIR', self.tmp):
            litmus.copy_experiment('target', 'pod-delete')
        install = run.call_args_list[1].args[0]
        self.assertIn(str(self.tmp / 'pod-delete-v3.30.0.yaml'), install)
        self.assertIn('litmus', install)
        self.assertEqual(json.loads(run.call_args_list[3].kwargs['input'])['metadata']['namespace'], 'target')

    @patch('run_litmus_pod_delete.subprocess.run')
    def test_missing_chart_file_reports_what_is_missing(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, '', 'Error from server (NotFound): not found')
        with patch.object(litmus, 'MANIFEST_DIR', self.tmp), self.assertRaises(SystemExit) as raised:
            litmus.copy_experiment('target', 'container-kill')
        self.assertIn('container-kill', str(raised.exception.code))
        self.assertEqual(run.call_count, 1)

    @patch('run_litmus_pod_delete.subprocess.run')
    def test_other_kubectl_errors_are_not_masked(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, '', 'Unable to connect to the server')
        with patch.object(litmus, 'MANIFEST_DIR', self.tmp), self.assertRaises(SystemExit) as raised:
            litmus.copy_experiment('target', 'pod-delete')
        self.assertIn('Unable to connect', str(raised.exception.code))
        self.assertEqual(run.call_count, 1)


if __name__ == '__main__':
    unittest.main()
