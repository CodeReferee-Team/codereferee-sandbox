"""Authoritative scenario catalogue and ordered inspection suites.

Suites are passed through the existing chaosMode string. A single request clones
and deploys once, runs its faults serially, then removes its namespace.
"""

SCENARIOS = {
    'litmus_pod_delete': {'label': 'Pod Delete', 'runtime': False},
    'litmus_container_kill': {'label': 'Container Kill', 'runtime': True},
    'litmus_pod_cpu_hog': {'label': 'CPU Stress', 'runtime': True},
    'litmus_pod_network_latency': {'label': 'Network Latency', 'runtime': True},
    'litmus_pod_network_loss': {'label': 'Network Packet Loss', 'runtime': True},
    'deployment_scale_down': {'label': 'Deployment Scale Down', 'runtime': False},
    'service_selector_blackhole': {'label': 'Service Routing Blackhole', 'runtime': False},
    'rollout_restart': {'label': 'Rollout Restart', 'runtime': False},
    'dependency_database_outage': {'label': 'Database Dependency Outage', 'runtime': False},
    'dependency_redis_outage': {'label': 'Redis Dependency Outage', 'runtime': False},
    'litmus_pod_memory_hog': {'label': 'Memory Pressure', 'runtime': True},
    'litmus_pod_memory_oom': {'label': 'Memory Limit Exceeded', 'runtime': True},
}

SUITES = {
    'suite_quick': ('litmus_container_kill',),
    'suite_standard': ('litmus_container_kill', 'litmus_pod_cpu_hog', 'litmus_pod_network_latency'),
    'suite_deep': ('litmus_container_kill', 'litmus_pod_delete', 'litmus_pod_cpu_hog',
                   'litmus_pod_network_latency', 'litmus_pod_network_loss',
                   'service_selector_blackhole', 'deployment_scale_down', 'rollout_restart'),
}
ALIASES = {'ck': 'litmus_container_kill', 'pd': 'litmus_pod_delete', 'cpu': 'litmus_pod_cpu_hog',
           'lat': 'litmus_pod_network_latency', 'loss': 'litmus_pod_network_loss',
           'route': 'service_selector_blackhole', 'scale': 'deployment_scale_down', 'roll': 'rollout_restart',
           'db': 'dependency_database_outage', 'redis': 'dependency_redis_outage',
           'mem': 'litmus_pod_memory_hog', 'oom': 'litmus_pod_memory_oom'}
CUSTOM_V1 = tuple(ALIASES.values())


def resolve_scenarios(mode: str, extras: list[str] | None = None) -> list[str]:
    if mode.startswith('suite_custom__'):
        selection = mode.removeprefix('suite_custom__')
        if selection.startswith('v1_'):
            try:
                bits = int(selection[3:], 16)
            except ValueError as exc:
                raise ValueError('Invalid custom scenario selection.') from exc
            if bits <= 0 or bits >= 1 << len(CUSTOM_V1):
                raise ValueError('Custom scenario selection contains unknown scenarios.')
            modes = [item for index, item in enumerate(CUSTOM_V1) if bits & (1 << index)]
        else:
            modes = [ALIASES.get(item, item) for item in selection.split('__')]
    else:
        modes = list(SUITES.get(mode, (mode,)))
    modes += extras or []
    if len(modes) > 16 or any(item not in SCENARIOS for item in modes):
        raise ValueError('Unknown scenario or more than 16 selected scenarios.')
    return list(dict.fromkeys(modes))
