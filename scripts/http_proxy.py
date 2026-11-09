"""Declared same-origin routes between services in one request namespace."""
import re

try:
    from .execution_plan import ConfigurationRequired
except ImportError:
    from execution_plan import ConfigurationRequired


def validate_proxy_routes(routes, services, owner):
    if not isinstance(routes, dict) or not 1 <= len(routes) <= 4:
        raise ConfigurationRequired('proxyRoutes requires one to four path mappings.')
    result = []
    for prefix, route in routes.items():
        if not isinstance(prefix, str) or len(prefix) > 96 or not re.fullmatch(r'/(?:[A-Za-z0-9_-]+/)+', prefix):
            raise ConfigurationRequired('Proxy prefixes must be local paths such as /api/, excluding /.')
        if not isinstance(route, dict) or set(route) - {'service', 'stripPrefix'}:
            raise ConfigurationRequired('Proxy routes accept service and optional stripPrefix.')
        peer = route.get('service')
        if not isinstance(peer, str) or peer not in services or peer == owner:
            raise ConfigurationRequired('Proxy routes must name another declared service.')
        strip = route.get('stripPrefix', False)
        if type(strip) is not bool:
            raise ConfigurationRequired('stripPrefix must be a boolean.')
        result.append({'prefix': prefix, 'service': peer, 'stripPrefix': strip})
    return result


def proxy_config(routes, endpoints):
    locations = []
    for route in routes:
        host, port = endpoints[route['service']]
        upstream = f'http://{host}:{port}' + ('/' if route['stripPrefix'] else '')
        settings = (f'proxy_pass {upstream};\n'
                    'proxy_http_version 1.1;\nproxy_set_header Host $host;\n'
                    'proxy_set_header X-Forwarded-Proto $scheme;\n'
                    'proxy_connect_timeout 3s;\nproxy_read_timeout 30s;\n'
                    'proxy_next_upstream off;\n')
        prefix = route['prefix']
        locations.append(f'location ^~ {prefix} {{\n{settings}}}\n'
                         f'location = {prefix[:-1]} {{\n{settings}}}\n')
    return ''.join(locations)
