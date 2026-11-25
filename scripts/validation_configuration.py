"""Request-scoped validation configuration. Never writes to the remote repository."""
from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

try:
    from .execution_plan import ConfigurationRequired
except ImportError:
    from execution_plan import ConfigurationRequired

MAX_BYTES = 64 * 1024
CONFIG_PATH = '.codereferee/validation.yaml'


def validate_configuration(value: str) -> str:
    if not isinstance(value, str) or len(value.encode('utf-8')) > MAX_BYTES:
        raise ConfigurationRequired('validationYaml must be UTF-8 YAML text of at most 64 KiB.')
    text = value.removeprefix('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
    if not text.strip() or '\x00' in text:
        raise ConfigurationRequired('validationYaml must be nonempty YAML text without NUL bytes.')
    try:
        if any(isinstance(token, yaml.tokens.AliasToken) for token in yaml.scan(text)):
            raise ConfigurationRequired('YAML aliases are not supported in uploaded configuration; use explicit values.')
        data = yaml.safe_load(text)
    except (yaml.YAMLError, RecursionError) as exc:
        raise ConfigurationRequired('validationYaml is not valid safe YAML.') from exc
    if not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] not in (1, 2, 3):
        raise ConfigurationRequired('Upload a complete validation.yaml with version: 1, 2, or 3, not a fragment.')
    active, visited = set(), 0

    def check(node, depth=0):
        nonlocal visited
        visited += 1
        if depth > 32 or visited > 4096:
            raise ConfigurationRequired('validationYaml is too deeply nested or complex.')
        if not isinstance(node, (dict, list)):
            if node is not None and not isinstance(node, (str, int, float, bool)):
                raise ConfigurationRequired('validationYaml supports ordinary YAML scalar values, lists, and mappings only.')
            return
        identity = id(node)
        if identity in active:
            raise ConfigurationRequired('Recursive YAML aliases are not supported.')
        if isinstance(node, dict) and any(not isinstance(key, str) for key in node):
            raise ConfigurationRequired('validationYaml mapping keys must be strings.')
        active.add(identity)
        children = list(node.keys()) + list(node.values()) if isinstance(node, dict) else node
        for child in children:
            check(child, depth + 1)
        active.remove(identity)

    check(data)
    return text


def config_file(repository: Path) -> Path:
    directory = repository / '.codereferee'
    path = directory / 'validation.yaml'
    if directory.is_symlink() or path.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ConfigurationRequired('Uploaded configuration cannot follow symlinks or replace a non-directory.')
    if not path.resolve().is_relative_to(repository.resolve()):
        raise ConfigurationRequired('Uploaded configuration must stay inside the repository clone.')
    return path


def apply_configuration(repository: Path, content: str) -> dict:
    text = validate_configuration(content)
    path = config_file(repository)
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding='utf-8', newline='\n')
    return {'source': 'request_upload', 'path': CONFIG_PATH,
            'uploaded_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
            'uploaded_bytes': len(text.encode('utf-8'))}


def effective_configuration(repository: Path, original: dict | None) -> dict | None:
    if original is None:
        return None
    path = config_file(repository)
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ConfigurationRequired('Uploaded validation configuration must remain present and within 64 KiB after patches.')
    text = validate_configuration(path.read_text(encoding='utf-8'))
    return dict(original, effective_sha256=hashlib.sha256(text.encode('utf-8')).hexdigest())
