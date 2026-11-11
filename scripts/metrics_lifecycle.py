"""Optional request-scoped collection. Never supplies an SLO verdict.

Receiver URLs are trusted operator settings, not repository/request inputs.
Successful receipt confirms the measured series, not that every WAL sample drained.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse
from urllib.request import ProxyHandler, build_opener

from scripts import install_observability as wiring


def receiver_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Receiver must be an operator-owned HTTP URL without credentials/query.')
    if any(c in value for c in '\r\n"\\'):
        raise ValueError('Invalid receiver URL.')
    return value.rstrip('/')


class RequestMetrics:
    def __init__(self, request_id: str, target: dict):
        self.request_id = request_id
        self.target = target
        self.namespace = wiring.observation_namespace(request_id, target['namespace'])
        self.cluster = os.getenv('CODEREFEREE_KIND_CLUSTER_NAME', 'codereferee')
        self.remote = os.getenv('CODEREFEREE_METRICS_REMOTE_WRITE_URL', '')
        self.query_base = os.getenv('CODEREFEREE_METRICS_QUERY_URL', '')
        self.enabled = bool(self.remote or self.query_base)
        self.attempted = False
        self.ready = False
        self.command_deadline = None
        self.stop = threading.Event()
        self.thread = None
        self.bindings = {}
        self.service_bindings = {}
        self.bindings_lock = threading.Lock()
        self.started = time.time()
        self.report = {'schema_version': 'metrics-observation.v1',
            'status': 'pending' if self.enabled else 'disabled',
            'request_id': request_id, 'cluster': self.cluster, 'namespace': target['namespace'],
            'workload': target['deployment'], 'observation_namespace': self.namespace,
            'scrape_interval_seconds': 1, 'pod_bindings': [], 'errors': [],
            'units': {'cpu': 'cores (rate of cumulative CPU seconds)', 'memory': 'bytes',
                      'restart': 'Kubernetes container restartCount snapshots'},
            'node_metrics_scope': 'shared-node diagnostics, not application usage',
            'cleanup': {'attempted': False, 'removed': None}}

    def query(self, expression: str, *, start=None, end=None):
        params = {'query': expression}
        route = '/api/v1/query'
        if start is not None:
            route = '/api/v1/query_range'
            params.update(start=start, end=end, step=1)
        # Local infrastructure must not inherit developer debugging proxies.
        timeout = 5 if self.command_deadline is None else min(5, self.command_deadline-time.monotonic())
        if timeout <= 0:
            raise TimeoutError('Metrics query exceeded its execution budget.')
        with build_opener(ProxyHandler({})).open(self.query_base + route + '?' + urlencode(params), timeout=timeout) as response:
            payload = json.load(response)
        if payload.get('status') != 'success':
            raise RuntimeError('Receiver query failed.')
        return payload['data']['result']

    def selector(self, metric: str) -> str:
        labels = {'codereferee_request_id': self.request_id, 'codereferee_cluster': self.cluster,
                  'namespace': self.target['namespace']}
        matchers = [k + '=' + json.dumps(v) for k, v in labels.items()]
        with self.bindings_lock:
            names = sorted({value['pod'] for value in self.bindings.values()})
        if names:
            matchers.append('pod=~' + json.dumps('|'.join(re.escape(name) for name in names)))
        return metric + '{' + ','.join(matchers) + '}'

    def latest_sample(self, metric):
        # query_range evaluation timestamps repeat old values during scrape gaps.
        # timestamp() exposes the source sample time; never treat a lookback
        # value as proof that a new sample was received.
        values = self.query('max(timestamp(' + self.selector(metric) + '))')
        return float(values[0]['value'][1]) if values else 0

    def command(self, arguments):
        with wiring.execution_budget(self.command_deadline):
            return wiring.run(arguments)

    def snapshot(self):
        pods = json.loads(self.command(wiring.kubectl_command('get', 'pods', '-n', self.target['namespace'],
            '-l', self.target['labelSelector'], '-o', 'json')).stdout)
        now = datetime.now(timezone.utc).isoformat()
        for pod in pods.get('items', []):
            metadata = pod['metadata']
            statuses = {v['name']: v for v in pod.get('status', {}).get('containerStatuses', [])}
            for container in pod.get('spec', {}).get('containers', []):
                key = (metadata['uid'], container['name'])
                status = statuses.get(container['name'], {})
                with self.bindings_lock:
                    binding = self.bindings.setdefault(key, {'pod': metadata['name'], 'uid': metadata['uid'],
                        'container': container['name'], 'node': pod.get('spec', {}).get('nodeName'),
                        'first_observed_at': now, 'restart_count_initial': status.get('restartCount'),
                        'resources': container.get('resources', {}), 'created_at': metadata.get('creationTimestamp')})
                    binding.update(last_observed_at=now, restart_count_final=status.get('restartCount'),
                        container_id=status.get('containerID'), deletion_timestamp=metadata.get('deletionTimestamp'))
        for name, target in self.target.get('workloads', {}).items():
            pods = json.loads(self.command(wiring.kubectl_command('get', 'pods', '-n', self.target['namespace'],
                '-l', target['labelSelector'], '-o', 'json')).stdout)
            with self.bindings_lock:
                bindings = self.service_bindings.setdefault(name, {})
                for pod in pods.get('items', []):
                    metadata = pod['metadata']
                    for container in pod.get('status', {}).get('containerStatuses', []):
                        key = (metadata['uid'], container['name'])
                        bindings[key] = {'pod': metadata['name'], 'uid': metadata['uid'],
                            'container': container['name'], 'container_id': container.get('containerID'),
                            'restart_count': container.get('restartCount'), 'service': name}

    def collect_service_metrics(self, ended):
        """Additional per-service evidence; the primary Judge series still describes the fault target."""
        result = {}
        with self.bindings_lock:
            bindings = {name: list(values.values()) for name, values in self.service_bindings.items()}
        for name in self.target.get('workloads', {}):
            rows = bindings.get(name, [])
            names = sorted({row['pod'] for row in rows})
            record = {'pod_bindings': rows, 'status': 'unavailable'}
            if names:
                labels = {'codereferee_request_id': self.request_id, 'codereferee_cluster': self.cluster,
                          'namespace': self.target['namespace']}
                selector = '{' + ','.join(k + '=' + json.dumps(v) for k, v in labels.items())
                selector += ',pod=~' + json.dumps('|'.join(re.escape(n) for n in names)) + '}'
                try:
                    record['cpu_cores'] = self.query('rate(container_cpu_usage_seconds_total' + selector + '[10s])',
                                                      start=self.started, end=ended)
                    record['memory_working_set_bytes'] = self.query('container_memory_working_set_bytes' + selector,
                                                                    start=self.started, end=ended)
                    if record['cpu_cores'] and record['memory_working_set_bytes']:
                        record['status'] = 'collected'
                except Exception:
                    record['error'] = 'service_metrics_query_failed'
            result[name] = record
        return result

    def watch(self):
        while not self.stop.wait(2):
            try:
                self.snapshot()
            except Exception:
                if 'Pod metadata sampling failed.' not in self.report['errors']:
                    self.report['errors'].append('Pod metadata sampling failed.')

    def start(self):
        if not self.enabled:
            return
        try:
            if not self.remote or not self.query_base:
                raise ValueError('Both METRICS_REMOTE_WRITE_URL and METRICS_QUERY_URL must be configured.')
            self.query_base = receiver_url(self.query_base)
            # Multiple business workloads need namespace-scoped collection;
            # primary Judge queries remain restricted to the fault target.
            deployment = None if self.target.get('workloads') else self.target['deployment']
            rendered = wiring.render(self.remote, self.request_id, self.cluster, self.target['namespace'], deployment)
            self.attempted = True  # apply can partially succeed
            wiring.apply(rendered)
            for workload in wiring.WORKLOADS:
                wiring.wait_ready(workload, 120, self.namespace)
            wiring.wait_agent_config(wiring.config_hash(rendered), 120, self.namespace)
            self.snapshot()
            self.thread = threading.Thread(target=self.watch, daemon=True)
            self.thread.start()
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                cpu = self.query(self.selector('container_cpu_usage_seconds_total'))
                memory = self.query(self.selector('container_memory_working_set_bytes'))
                fresh = min(self.latest_sample('container_cpu_usage_seconds_total'),
                            self.latest_sample('container_memory_working_set_bytes')) >= self.started
                if cpu and memory and fresh:
                    self.ready = True
                    self.started = time.time()
                    self.report['status'] = 'collecting'
                    self.report['started_at'] = self.started
                    return
                time.sleep(2)
            raise RuntimeError('No request CPU/memory series received during warmup.')
        except Exception as exc:
            self.report['status'] = 'error'
            self.report['errors'].append(str(exc)[-1200:])

    def finish(self):
        if not self.enabled:
            return self.report
        ended = time.time()
        self.report['ended_at'] = ended
        try:
            if self.ready:
                self.snapshot()
                deadline = time.monotonic() + 20
                raw = {}
                while time.monotonic() < deadline:
                    raw = {name: self.query(self.selector(metric), start=self.started, end=ended)
                           for name, metric in (('cpu_seconds', 'container_cpu_usage_seconds_total'),
                                                ('memory_bytes', 'container_memory_working_set_bytes'))}
                    latest = {key: self.latest_sample(metric) for key, metric in
                              (('cpu_seconds', 'container_cpu_usage_seconds_total'),
                               ('memory_bytes', 'container_memory_working_set_bytes'))}
                    if all(value >= ended - 3 for value in latest.values()):
                        break
                    time.sleep(2)
                self.report['series'] = {key: [{'labels': s['metric'], 'evaluation_point_count': len(s.get('values', []))}
                                              for s in values] for key, values in raw.items()}
                self.report['last_received_sample'] = latest
                self.report['receipt_confirmed'] = all(value >= ended - 3 for value in latest.values())
                self.report['status'] = 'received' if self.report['receipt_confirmed'] else 'partial'
                self.report['cpu_cores'] = self.query('rate(' + self.selector('container_cpu_usage_seconds_total') + '[10s])',
                                                     start=self.started, end=ended)
                self.report['memory_working_set_bytes'] = raw['memory_bytes']
                self.report['full_wal_drain_proven'] = False
                self.report['range_step_seconds'] = 1
                self.report['range_semantics'] = 'PromQL evaluation grid; not proof of one fresh scrape per second'
                if self.target.get('workloads'):
                    self.report['services'] = self.collect_service_metrics(ended)
        except Exception as exc:
            self.report['status'] = 'error'
            self.report['errors'].append(str(exc)[-1200:])
        finally:
            self.stop.set()
            if self.thread:
                remaining = 6 if self.command_deadline is None else max(0, self.command_deadline-time.monotonic())
                self.thread.join(timeout=min(6, remaining))
            with self.bindings_lock:
                self.report['pod_bindings'] = list(self.bindings.values())
            if self.attempted:
                self.report['cleanup']['attempted'] = True
                try:
                    wiring.uninstall(self.namespace)
                    self.report['cleanup']['removed'] = True
                except Exception:
                    self.report['cleanup']['removed'] = False
                    self.report['errors'].append('Observation namespace cleanup failed.')
        return self.report
