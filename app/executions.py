"""Persistent request identity and one process-safe execution slot per Sandbox."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time


execution_context = ContextVar('sandbox_execution_context', default=None)


class ExecutionSlot:
    def __init__(self, directory: Path):
        self.path = directory / 'execution.lock'
        self.handle = None

    def acquire(self) -> bool:
        self.handle = self.path.open('a+b')
        self.handle.seek(0)
        if self.path.stat().st_size == 0:
            self.handle.write(b'\0')
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, BlockingIOError):
            self.handle.close()
            self.handle = None
            return False

    def release(self) -> None:
        if self.handle is not None:
            # Closing an exclusively locked handle releases it on both platforms.
            self.handle.close()
            self.handle = None


class ExecutionStore:
    def __init__(self):
        self.directory = Path(os.getenv('CODEREFEREE_EXECUTION_STATE_DIR',
            str(Path(__file__).resolve().parents[1] / '.runtime' / 'executions')))
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'executions.sqlite3'
        with self.connection() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS executions (
                execution_id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
                state TEXT NOT NULL, resource_id TEXT, started REAL NOT NULL,
                updated REAL NOT NULL, result TEXT)''')
            connection.execute('CREATE INDEX IF NOT EXISTS request_lookup ON executions(request_id, started)')

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def identity(request) -> str:
        canonical = json.dumps(request.model_dump(mode='json'), sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical.encode('utf-8')).hexdigest()

    def get(self, execution_id: str):
        with self.connection() as connection:
            row = connection.execute('SELECT * FROM executions WHERE execution_id=?', (execution_id,)).fetchone()
        return dict(row) if row else None

    def latest(self, request_id: str):
        with self.connection() as connection:
            row = connection.execute('SELECT * FROM executions WHERE request_id=? ORDER BY started DESC LIMIT 1',
                                     (request_id,)).fetchone()
        return dict(row) if row else None

    def unfinished(self):
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM executions WHERE state IN ('running','cleanup_pending') ORDER BY started")]

    def start(self, execution_id: str, request_id: str):
        now = time.time()
        with self.connection() as connection:
            connection.execute('INSERT INTO executions VALUES (?,?,?,NULL,?,?,NULL)',
                               (execution_id, request_id, 'running', now, now))

    def attempt(self, execution_id: str, resource_id: str):
        with self.connection() as connection:
            connection.execute('UPDATE executions SET resource_id=?, updated=? WHERE execution_id=?',
                               (resource_id, time.time(), execution_id))

    def finish(self, execution_id: str, state: str, result: dict):
        with self.connection() as connection:
            connection.execute('UPDATE executions SET state=?,result=?,updated=? WHERE execution_id=?',
                               (state, json.dumps(result, ensure_ascii=True), time.time(), execution_id))

    def discard(self, execution_id: str):
        with self.connection() as connection:
            connection.execute('DELETE FROM executions WHERE execution_id=?', (execution_id,))


def public_execution(row):
    result = json.loads(row['result']) if row['result'] else None
    response = {'executionId': row['execution_id'], 'requestId': row['request_id'], 'state': row['state'],
                'startedAt': row['started'], 'updatedAt': row['updated'], 'result': result}
    if row['state'] == 'running' and row['resource_id']:
        directory = Path(os.getenv('CODEREFEREE_EXECUTION_STATE_DIR',
            str(Path(__file__).resolve().parents[1] / '.runtime' / 'executions')))
        path = directory / (row['resource_id'] + '.json')
        if path.exists():
            data = json.loads(path.read_text(encoding='utf-8'))
            response['phase'] = data.get('phase')
            started = data.get('phase_started_monotonic')
            response['phaseElapsedSeconds'] = round(time.monotonic()-started, 3) if started else None
    return response
