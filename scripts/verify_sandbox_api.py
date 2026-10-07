"""Exercise the actual HTTP repository API; save observations without AI labels."""
import argparse
import json
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--api-url', default='http://127.0.0.1:8120')
    parser.add_argument('--repository-url', required=True)
    parser.add_argument('--request-id', required=True)
    parser.add_argument('--branch')
    parser.add_argument('--mode', default='suite_quick')
    parser.add_argument('--no-chaos', action='store_true')
    parser.add_argument('--profile')
    parser.add_argument('--patch-file', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    payload = {'repositoryUrl': args.repository_url, 'requestId': args.request_id}
    for name, value in (('branch', args.branch), ('chaosMode', None if args.no_chaos else args.mode),
                        ('deploymentProfile', args.profile)):
        if value:
            payload[name] = value
    if args.patch_file:
        payload['patchDiff'] = args.patch_file.read_text(encoding='utf-8')
    request = Request(args.api_url + '/repositories/validate', data=json.dumps(payload).encode(),
                      headers={'Content-Type': 'application/json'}, method='POST')
    with build_opener(ProxyHandler({})).open(request, timeout=1800) as response:
        result = json.loads(response.read())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: result.get(key) for key in ('requestId', 'exitCode', 'observationStatus',
        'schemaVersion', 'serverStarted', 'httpStatus', 'infraError', 'stderr', 'sandboxReport')}, ensure_ascii=False, indent=2))
    print('executionPlan=' + json.dumps(result.get('deployment', {}).get('executionPlan', {})))
    print('cleanup=' + json.dumps(result.get('source', {}).get('artifact_cleanup', {})))
    return 0 if result.get('observationStatus') == 'observed' and result.get('exitCode') == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
