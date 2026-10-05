from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from collect_baseline import kubectl_command, load_image_into_cluster  # noqa: E402
from run_litmus_pod_delete import ROLE  # noqa: E402


class ClusterRuntimeTests(unittest.TestCase):
    def test_explicit_context_is_added_without_changing_global_context(self) -> None:
        with patch.dict(os.environ, {"CODEREFEREE_KUBECTL_CONTEXT": "kind-test"}, clear=True):
            self.assertEqual(
                kubectl_command("get", "nodes"),
                ["kubectl", "--context", "kind-test", "get", "nodes"],
            )

    def test_kind_provider_derives_context_from_cluster_name(self) -> None:
        environment = {
            "CODEREFEREE_CLUSTER_PROVIDER": "kind",
            "CODEREFEREE_KIND_CLUSTER_NAME": "codereferee-ci",
            "CODEREFEREE_KIND_COMMAND": "C:/tools/kind.exe",
        }
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(
                kubectl_command("get", "nodes"),
                ["kubectl", "--context", "kind-codereferee-ci", "get", "nodes"],
            )

    @patch("collect_baseline.subprocess.run")
    def test_kind_provider_loads_locally_built_image(self, run_mock) -> None:
        run_mock.return_value = subprocess.CompletedProcess([], 0, "", "")
        environment = {
            "CODEREFEREE_CLUSTER_PROVIDER": "kind",
            "CODEREFEREE_KIND_CLUSTER_NAME": "codereferee-ci",
            "CODEREFEREE_KIND_COMMAND": "C:/tools/kind.exe",
        }
        with patch.dict(os.environ, environment, clear=True):
            load_image_into_cluster("codereferee/job:abc123")
        command = run_mock.call_args.args[0]
        self.assertEqual(
            command,
            ["C:/tools/kind.exe", "load", "docker-image", "codereferee/job:abc123", "--name", "codereferee-ci"],
        )

    @patch("collect_baseline.subprocess.run")
    def test_existing_provider_does_not_load_image(self, run_mock) -> None:
        with patch.dict(os.environ, {"CODEREFEREE_CLUSTER_PROVIDER": "existing"}, clear=True):
            load_image_into_cluster("codereferee/job:abc123")
        run_mock.assert_not_called()

    def test_litmus_runner_role_can_create_jobs_without_wildcard_permissions(self) -> None:
        manifest = yaml.safe_load((ROOT / "k8s" / "litmus-runner-rbac.yaml").read_text(encoding="utf-8"))
        self.assertEqual(ROLE, manifest["metadata"]["name"])
        job_rules = [rule for rule in manifest["rules"] if "jobs" in rule["resources"]]
        self.assertTrue(any("create" in rule["verbs"] for rule in job_rules))
        self.assertFalse(any("*" in rule["verbs"] or "*" in rule["resources"] for rule in manifest["rules"]))


if __name__ == "__main__":
    unittest.main()
