"""요청 기한(deadlineSeconds) 전달.

AI는 샌드박스 응답을 600초까지 기다리는데 샌드박스는 배포에만 1,200초를 쓸 수 있었다.
AI가 먼저 포기하면 샌드박스는 계속 돌면서 락을 쥐고, AI는 판정 불가를 낸다. 그 사이 온
다음 요청은 409를 받는다. 기한을 받으면 샌드박스가 그 안에서 끝내고, 못 돌릴 시나리오는
시작하지 않고 이유를 남긴다.
"""
import json
import subprocess
import unittest
from unittest.mock import patch

from app import main
from app.main import RepositoryValidationRequest, StageTimeout, validate_repository


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def observed(exit_code=0, recovery=1.0):
    return {'observationStatus': 'observed', 'exitCode': exit_code,
            'metrics': {'probe_count': 10, 'success_count': 10},
            'chaos_observation': {'recovered': exit_code == 0, 'recovery_seconds': recovery,
                                  'target_configuration': {'replicas': 2}},
            'baseline': {'metrics': {'availability': 1.0}}}


@patch('app.main.cleanup_namespace', return_value=True)
@patch('app.main.deploy_repository')
class RequestDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        patcher = patch('app.main.time.monotonic', self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, mode='suite_standard', deadline=None):
        extra = {} if deadline is None else {'deadlineSeconds': deadline}
        return RepositoryValidationRequest(repositoryUrl='https://github.com/test/repo', requestId='test',
                                           deploymentProfile='test', chaosMode=mode, **extra)

    def deploy_after(self, deploy, seconds):
        def fake(*_args, **_kwargs):
            self.clock.now += seconds
            return {'target': {'namespace': 'test', 'deployment': 'api', 'service': 'api',
                               'servicePort': 8080, 'labelSelector': 'app=api'}}
        deploy.side_effect = fake

    def scenarios_taking(self, run, seconds, outcome=None):
        def fake(*_args, **_kwargs):
            self.clock.now += seconds
            return subprocess.CompletedProcess([], 0, json.dumps(outcome or observed()), '')
        run.side_effect = fake

    def test_without_a_deadline_every_stage_keeps_its_own_ceiling(self, deploy, _cleanup):
        self.deploy_after(deploy, 0)
        with patch('app.main.run_bounded') as run:
            self.scenarios_taking(run, 100)
            result = validate_repository(self.request())
        self.assertEqual(deploy.call_args.args[1].remaining(), None)
        self.assertEqual({c.kwargs['seconds'] for c in run.call_args_list}, {main.EXPERIMENT_TIMEOUT_SECONDS})
        self.assertFalse(result['chaos_observation']['partial'])

    def test_scenario_that_cannot_finish_in_time_is_not_started(self, deploy, _cleanup):
        # 600초 기한, 정리 몫 90초를 빼면 510초. 배포 200초 + 시나리오 130초 두 개면 460초가 남고,
        # 세 번째(지연, 실측 없음 → 상한 300초)는 50초 안에 끝날 수 없다.
        self.deploy_after(deploy, 200)
        with patch('app.main.run_bounded') as run:
            self.scenarios_taking(run, 130)
            result = validate_repository(self.request(deadline=600))
        self.assertEqual(run.call_count, 2)
        observation = result['chaos_observation']
        self.assertEqual(result['observationStatus'], 'observed')
        self.assertTrue(observation['partial'])
        self.assertEqual(observation['executed'], ['litmus_container_kill', 'litmus_pod_cpu_hog'])
        stopped = observation['stopped'][0]
        self.assertEqual(stopped['chaosMode'], 'litmus_pod_network_latency')
        self.assertEqual(stopped['infraError'], 'sandbox_budget_exceeded')
        self.assertIn('50s left', stopped['reason'])

    def test_stage_timeout_shrinks_to_what_is_left(self, deploy, _cleanup):
        self.deploy_after(deploy, 400)
        with patch('app.main.run_bounded') as run:
            self.scenarios_taking(run, 10)
            validate_repository(self.request(mode='litmus_container_kill', deadline=600))
        # 510 - 400 = 110초가 남았다. 시나리오 상한 300초보다 작다.
        self.assertAlmostEqual(run.call_args.kwargs['seconds'], 110)

    def test_single_scenario_bound_by_the_deadline_is_reported_as_budget_exceeded(self, deploy, _cleanup):
        self.deploy_after(deploy, 400)
        with patch('app.main.run_bounded', side_effect=StageTimeout('scenario:litmus_container_kill', 110, True)):
            result = validate_repository(self.request(mode='litmus_container_kill', deadline=600))
        self.assertEqual(result['observationStatus'], 'infrastructure_error')
        self.assertEqual(result['infraError'], 'sandbox_budget_exceeded')
        self.assertEqual(result['stoppedAt'], 'scenario:litmus_container_kill')

    def test_deploy_gets_the_request_budget(self, deploy, _cleanup):
        self.deploy_after(deploy, 0)
        with patch('app.main.run_bounded') as run:
            self.scenarios_taking(run, 0)
            validate_repository(self.request(mode='litmus_container_kill', deadline=600))
        budget = deploy.call_args.args[1]
        self.assertEqual(budget.timeout_for(main.DEPLOY_TIMEOUT_SECONDS), (510, True))

    def test_deadline_below_one_minute_is_rejected(self, _deploy, _cleanup):
        with self.assertRaises(ValueError):
            self.request(deadline=30)


class BudgetTests(unittest.TestCase):
    def test_stage_limit_wins_when_more_time_is_left(self):
        budget = main.Budget(3600, main.time.monotonic())
        seconds, bound = budget.timeout_for(300)
        self.assertEqual((seconds, bound), (300, False))

    def test_never_returns_zero_seconds(self):
        budget = main.Budget(60, main.time.monotonic() - 1000)
        seconds, bound = budget.timeout_for(300)
        self.assertEqual((seconds, bound), (1.0, True))


if __name__ == '__main__':
    unittest.main()
