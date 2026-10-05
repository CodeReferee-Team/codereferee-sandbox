from __future__ import annotations

import json
import subprocess
import unittest

from app.main import parse_experiment_result


class HttpAdapterTests(unittest.TestCase):
    def test_observed_result_includes_process_exit_code(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["python", "experiment.py"],
            returncode=0,
            stdout=json.dumps({"observationStatus": "observed", "scenario": "pod_delete"}),
            stderr="",
        )

        result = parse_experiment_result(completed)

        self.assertEqual(result["exitCode"], 0)
        self.assertFalse(result["timedOut"])

    def test_observed_fault_failure_keeps_structured_result_and_exit_code(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["python", "experiment.py"],
            returncode=1,
            stdout=json.dumps({"observationStatus": "observed", "chaos_observation": {"recovered": False}}),
            stderr="fault did not recover",
        )

        result = parse_experiment_result(completed)

        self.assertEqual(result["exitCode"], 1)
        self.assertEqual(result["observationStatus"], "observed")
        self.assertEqual(result["stderr"], "fault did not recover")


if __name__ == "__main__":
    unittest.main()
