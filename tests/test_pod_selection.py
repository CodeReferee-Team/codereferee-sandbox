"""Regression coverage for bounded, deployment-owned Litmus target selection."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from pod_selection import owned_pods, pod_evidence, select_ready_pods


NAMESPACE = 'codereferee-test'


def owner(kind, uid, controller=True):
    return {'kind': kind, 'uid': uid, 'name': uid, 'controller': controller}


def deployment():
    return {'metadata': {'name': 'api', 'uid': 'deployment-uid', 'namespace': NAMESPACE}}


def replica_set(uid='replicaset-uid', deployment_uid='deployment-uid'):
    return {'metadata': {'name': uid, 'uid': uid, 'namespace': NAMESPACE,
                         'ownerReferences': [owner('Deployment', deployment_uid)]}}


def pod(name='api-a', uid=None, replica_set_uid='replicaset-uid'):
    return {'metadata': {'name': name, 'uid': uid or name + '-uid', 'namespace': NAMESPACE,
                         'labels': {'app': 'api'},
                         'ownerReferences': [owner('ReplicaSet', replica_set_uid)]},
            'spec': {'nodeName': 'kind-control-plane', 'containers': [{'name': 'api'}]},
            'status': {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}],
                       'containerStatuses': [{'name': 'api', 'ready': True, 'restartCount': 2,
                                              'containerID': 'containerd://sample'}]}}


class PodSelectionTests(unittest.TestCase):
    def test_count_one_selects_exactly_one_of_three_in_stable_name_order(self):
        candidates = [pod('api-c'), pod('api-a'), pod('api-b')]
        selected = select_ready_pods(candidates, 1, 'api')
        self.assertEqual([p['metadata']['name'] for p in selected], ['api-a'])
        self.assertEqual(len(candidates), 3)

    def test_count_selects_exact_requested_number_without_mutating_candidates(self):
        candidates = [pod('api-c'), pod('api-a'), pod('api-b')]
        original = copy.deepcopy(candidates)
        self.assertEqual([p['metadata']['name'] for p in select_ready_pods(candidates, 2, 'api')],
                         ['api-a', 'api-b'])
        self.assertEqual(candidates, original)

    def test_count_is_strict_integer_in_bounded_range(self):
        for invalid in (-1, 0, 17, True, False, 1.0, '1', None):
            with self.subTest(count=invalid), self.assertRaises(ValueError):
                select_ready_pods([pod()], invalid, 'api')

    def test_insufficient_ready_pods_fails_without_widening_scope(self):
        with self.assertRaises(RuntimeError):
            select_ready_pods([pod()], 2, 'api')
        with self.assertRaises(RuntimeError):
            select_ready_pods([], 1, 'api')

    def test_unready_terminating_and_nonrunning_pods_are_not_selected(self):
        variants = []
        terminating = pod('api-terminating')
        terminating['metadata']['deletionTimestamp'] = '2026-10-09T00:00:00Z'
        variants.append(terminating)
        pending = pod('api-pending')
        pending['status']['phase'] = 'Pending'
        variants.append(pending)
        unready = pod('api-unready')
        unready['status']['conditions'][0]['status'] = 'False'
        variants.append(unready)
        container_unready = pod('api-container-unready')
        container_unready['status']['containerStatuses'][0]['ready'] = False
        variants.append(container_unready)
        missing_container = pod('api-other-container')
        missing_container['status']['containerStatuses'][0]['name'] = 'sidecar'
        variants.append(missing_container)
        self.assertEqual(select_ready_pods(variants + [pod('api-ready')], 1, 'api')[0]
                         ['metadata']['name'], 'api-ready')
        with self.assertRaises(RuntimeError):
            select_ready_pods(variants, 1, 'api')

    def test_owned_pods_require_controller_uid_chain_not_matching_labels(self):
        foreign = pod('api-foreign', replica_set_uid='foreign-rs')
        orphan = pod('api-orphan')
        orphan['metadata']['ownerReferences'] = []
        matching_name_wrong_uid = pod('api-wrong-uid', replica_set_uid='unknown-uid')
        matching_name_wrong_uid['metadata']['ownerReferences'][0]['name'] = 'replicaset-uid'
        candidates = [foreign, orphan, matching_name_wrong_uid, pod('api-owned')]
        self.assertEqual([p['metadata']['name'] for p in owned_pods(deployment(),
                         [replica_set(), replica_set('foreign-rs', 'foreign-deployment')], candidates)],
                         ['api-owned'])

    def test_owner_references_must_be_controlling_and_have_correct_kind(self):
        valid = pod('api-valid')
        noncontroller = pod('api-noncontroller')
        noncontroller['metadata']['ownerReferences'][0]['controller'] = False
        wrong_kind = pod('api-wrong-kind')
        wrong_kind['metadata']['ownerReferences'][0]['kind'] = 'Deployment'
        self.assertEqual(owned_pods(deployment(), [replica_set()],
                                  [noncontroller, wrong_kind, valid]), [valid])
        for invalid in (False, None):
            rs = replica_set()
            rs['metadata']['ownerReferences'][0]['controller'] = invalid
            self.assertEqual(owned_pods(deployment(), [rs], [valid]), [])

    def test_cross_namespace_pod_or_replicaset_is_not_owned(self):
        cross_pod = pod('api-other-namespace')
        cross_pod['metadata']['namespace'] = 'other'
        valid = pod('api-valid')
        self.assertEqual(owned_pods(deployment(), [replica_set()], [cross_pod, valid]), [valid])
        cross_rs = replica_set()
        cross_rs['metadata']['namespace'] = 'other'
        self.assertEqual(owned_pods(deployment(), [cross_rs], [valid]), [])

    def test_multiple_owned_replicasets_are_supported(self):
        candidates = [pod('api-old'), pod('api-new', replica_set_uid='new-rs')]
        self.assertEqual(owned_pods(deployment(), [replica_set(), replica_set('new-rs')], candidates),
                         candidates)

    def test_evidence_retains_identity_and_target_container_restart_count(self):
        selected = pod('api-selected', uid='selected-uid')
        selected['status']['containerStatuses'].insert(0,
            {'name': 'sidecar', 'ready': True, 'restartCount': 99})
        evidence = pod_evidence(selected, 'api')
        self.assertEqual(evidence['name'], 'api-selected')
        self.assertEqual(evidence['uid'], 'selected-uid')
        self.assertEqual(evidence['restart_count'], 2)


if __name__ == '__main__':
    unittest.main()
