import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import namespace_cleanup as cleanup


def response(code=0, text=''):
    return subprocess.CompletedProcess([],code,text,'')


class NamespaceCleanupTests(unittest.TestCase):
    def test_accepts_full_length_owned_name_and_rejects_invalid_names(self):
        namespace = 'codereferee-' + 'a' * (63 - len('codereferee-'))
        with patch.object(cleanup.subprocess, 'run', side_effect=[response(), response()]):
            self.assertTrue(cleanup.delete_namespace_confirmed(namespace))
        with patch.object(cleanup.subprocess, 'run') as run:
            for invalid in (namespace + 'a', 'codereferee-test-', 'codereferee-'):
                self.assertFalse(cleanup.delete_namespace_confirmed(invalid))
        run.assert_not_called()

    def test_waits_until_terminating_namespace_is_actually_absent(self):
        with patch.object(cleanup.subprocess,'run',side_effect=[response(),response(text=json.dumps({'status':{'phase':'Terminating'}})),response()]) as run, patch.object(cleanup.time,'sleep'):
            self.assertTrue(cleanup.delete_namespace_confirmed('codereferee-test'))
        self.assertIn('--wait=false',run.call_args_list[0].args[0])
        self.assertEqual(len(run.call_args_list),3)

    def test_api_error_with_empty_stdout_does_not_prove_absence(self):
        with patch.object(cleanup.subprocess,'run',side_effect=[response(),response(1),response()]) as run, patch.object(cleanup.time,'sleep'):
            self.assertTrue(cleanup.delete_namespace_confirmed('codereferee-test'))
        self.assertEqual(len(run.call_args_list),3)

    def test_process_timeout_and_rejected_delete_are_not_success(self):
        for outcome in (response(1),subprocess.TimeoutExpired('kubectl',15)):
            with self.subTest(outcome=type(outcome).__name__), patch.object(cleanup.subprocess,'run',side_effect=[outcome]):
                self.assertFalse(cleanup.delete_namespace_confirmed('codereferee-test'))

    def test_budget_exhaustion_and_unowned_namespace_never_delete(self):
        with patch.object(cleanup.subprocess,'run') as run:
            self.assertFalse(cleanup.delete_namespace_confirmed('default'))
            self.assertFalse(cleanup.delete_namespace_confirmed('codereferee-test',timeout_seconds=0))
        run.assert_not_called()
