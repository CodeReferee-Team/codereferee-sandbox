"""A process-tree execution deadline and an operator-owned recovery inventory."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time


DEADLINE_ENV = 'CODEREFEREE_EXECUTION_DEADLINE'
JOURNAL_ENV = 'CODEREFEREE_EXECUTION_JOURNAL'
PHASE_LIMITS_ENV = 'CODEREFEREE_PHASE_TIMEOUTS'


def write_inventory(path: Path, data: dict) -> None:
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data), encoding='utf-8')
    for attempt in range(5):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(.01 * (attempt+1))


def remaining_timeout(default: float, phase: str | None = None) -> float:
    limits = json.loads(os.getenv(PHASE_LIMITS_ENV, '{}'))
    limit = limits.get(phase, default)
    value = os.getenv(DEADLINE_ENV)
    if not value:
        return limit
    remaining = float(value) - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(['execution-deadline'], 0)
    return min(limit, remaining)


def record_resource(kind: str, value: str) -> None:
    filename = os.getenv(JOURNAL_ENV)
    if not filename:
        return
    path = Path(filename)
    data = json.loads(path.read_text(encoding='utf-8'))
    resources = data.setdefault(kind, [])
    if value not in resources:
        resources.append(value)
        write_inventory(path, data)


def record_phase(phase: str) -> None:
    filename = os.getenv(JOURNAL_ENV)
    if filename:
        path = Path(filename)
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('phase') != phase:
            data['phase_started_monotonic'] = time.monotonic()
        data['phase'] = phase
        write_inventory(path, data)


def begin_cleanup() -> None:
    # Cleanup has a separate parent-side bound. The work deadline must not
    # prevent the child from stopping containers or deleting its namespace.
    os.environ.pop(DEADLINE_ENV, None)
