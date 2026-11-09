"""Build Vite SPAs and serve their static output without a development server."""
from __future__ import annotations

import json
import re
import shlex


NGINX_IMAGE = ('nginxinc/nginx-unprivileged:stable-alpine@'
               'sha256:f5116ed4606f3eb015dd4a8e28cb9b351476dc0ad4bc6e9f93b19cec130cdf03')

# Run only inside the isolated image builder. Repository Vite configuration is
# executable JavaScript and must never be imported by the Sandbox host.
EXPORT_OUTPUT = r'''
import { resolveConfig } from 'vite';
import fs from 'node:fs/promises';
import path from 'node:path';
const options = JSON.parse(process.env.CODEREFEREE_VITE_OPTIONS);
const config = await resolveConfig(options, 'build', options.mode || 'production');
const fail = message => { throw new Error('CODEREFEREE_STATIC_CONFIGURATION_REQUIRED: ' + message + '. Add an explicit Dockerfile for custom deployments.'); };
if (config.build.ssr || config.build.lib) fail('SSR and library builds need a server contract');
const root = await fs.realpath(process.cwd());
const output = await fs.realpath(path.resolve(config.root, config.build.outDir))
  .catch(() => fail('build output directory is missing or unreadable'));
const relative = path.relative(root, output);
if (!relative || relative.startsWith('..') || path.isAbsolute(relative) || relative.split(path.sep).some(p => ['node_modules', '.git'].includes(p))) fail('build output must be a dedicated directory inside the build context');
const base = config.base;
if (!/^\/(?:[A-Za-z0-9_-]+\/)*$/.test(base)) fail('base must be an absolute local path ending in /');
const index = await fs.stat(path.join(output, 'index.html')).catch(() => null);
if (!index?.isFile()) fail('build output has no index.html');
async function rejectLinks(directory) {
  for (const entry of await fs.readdir(directory, {withFileTypes: true})) {
    if (entry.isSymbolicLink()) fail('static output contains a symbolic link');
    if (entry.isDirectory()) await rejectLinks(path.join(directory, entry.name));
  }
}
await rejectLinks(output);
const destination = '/codereferee-output/site' + base;
await fs.mkdir(destination, {recursive: true});
await fs.cp(output, destination, {recursive: true});
const port = Number(process.env.CODEREFEREE_STATIC_PORT);
// Missing assets must remain 404, instead of returning the SPA's HTML as JS.
const configText = `server {
  listen ${port};
  server_name _;
  absolute_redirect off;
  root /usr/share/nginx/html;
  include /etc/nginx/mime.types;
  include /etc/nginx/codereferee-proxy/*.conf;
  location ~* \\.(?:js|mjs|css|map|json|png|jpe?g|gif|svg|ico|webp|avif|woff2?|ttf|wasm)$ { try_files $uri =404; }
  location ${base} { try_files $uri $uri/ ${base}index.html; }
  ${base === '/' ? '' : 'location = / { return 302 ' + base + '; } location / { return 404; }'}
}
`;
await fs.writeFile('/codereferee-output/default.conf', configText);
'''


def build_options(script: str) -> dict:
    """Accept a final `vite build`, including explicit output/base/mode flags."""
    if not isinstance(script, str):
        raise ValueError('Vite SPA requires a build script.')
    command = shlex.split(script.split('&&')[-1].strip())
    if command[:2] != ['vite', 'build']:
        raise ValueError('Vite SPA requires a build script ending in vite build, or an explicit Dockerfile.')
    options = {}
    flags = {'--outDir': 'outDir', '--base': 'base', '--mode': 'mode'}
    index = 2
    while index < len(command):
        flag, separator, value = command[index].partition('=')
        if flag not in flags:
            raise ValueError('Custom Vite build arguments require an explicit Dockerfile.')
        if not separator:
            index += 1
            if index >= len(command):
                raise ValueError('Vite build argument is missing its value.')
            value = command[index]
        if not value or value.startswith('-') or re.search(r'[$`\r\n]', value):
            raise ValueError('Invalid Vite build argument.')
        if flag == '--outDir':
            options.setdefault('build', {})['outDir'] = value
        else:
            options[flags[flag]] = value
        index += 1
    return options


def dockerfile(node: str, install: list[str], build: list[str], options: dict, port: int) -> str:
    return (
        f'FROM node:{node}-bookworm-slim AS builder\nWORKDIR /app\n'
        'RUN apt-get update && apt-get install -y --no-install-recommends python3 make g++ git && rm -rf /var/lib/apt/lists/*\n'
        'COPY . .\n'
        f'RUN {json.dumps(install)}\nRUN {json.dumps(build)}\n'
        f'RUN {json.dumps(["env", "CODEREFEREE_VITE_OPTIONS=" + json.dumps(options), "CODEREFEREE_STATIC_PORT=" + str(port), "node", "--input-type=module", "-e", EXPORT_OUTPUT])}\n'
        f'FROM {NGINX_IMAGE}\n'
        'COPY --from=builder /codereferee-output/site/ /usr/share/nginx/html/\n'
        'COPY --from=builder /codereferee-output/default.conf /etc/nginx/conf.d/default.conf\n'
        f'EXPOSE {port}\nUSER 101:101\n'
    )
