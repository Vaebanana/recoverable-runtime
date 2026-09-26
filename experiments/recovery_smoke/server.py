"""Local controllable job-application page for recovery experiments.

The application status lives in a SQLite file next to this module, not in the
agent process and not only in page JavaScript. When the agent process crashes
after clicking Submit, the external world still remembers that the submission
actually happened, which makes crash/reconciliation experiments meaningful.

Endpoints:
    GET  /         - page showing Application #7 status and the submit button
    POST /submit   - flip the status to SUBMITTED and increment submit_count
    GET  /status   - JSON snapshot of the durable application state
    POST /reset    - reset state for an independent experiment trial

Run from the repository root:

    uv run python experiments/recovery_smoke/server.py --port 8765
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

APPLICATION_ID = '7'
NOT_SUBMITTED = 'NOT_SUBMITTED'
SUBMITTED = 'SUBMITTED'

DEFAULT_DB_PATH = Path(__file__).resolve().parent / 'state.db'

app = FastAPI(title='Recovery Smoke Application Server')

_db_path = Path(os.environ.get('RECOVERY_SMOKE_STATE_DB', DEFAULT_DB_PATH))


def _connect() -> sqlite3.Connection:
	"""Open the state database and make sure the single application row exists."""
	_db_path.parent.mkdir(parents=True, exist_ok=True)
	connection = sqlite3.connect(_db_path)
	connection.execute(
		"""
		CREATE TABLE IF NOT EXISTS application_state (
			application_id TEXT PRIMARY KEY,
			status TEXT NOT NULL,
			submit_count INTEGER NOT NULL
		)
		"""
	)
	connection.execute(
		'INSERT OR IGNORE INTO application_state (application_id, status, submit_count) VALUES (?, ?, ?)',
		(APPLICATION_ID, NOT_SUBMITTED, 0),
	)
	connection.commit()
	return connection


def _load() -> tuple[str, int]:
	"""Return the current (status, submit_count) for the application."""
	with closing(_connect()) as connection:
		row = connection.execute(
			'SELECT status, submit_count FROM application_state WHERE application_id = ?',
			(APPLICATION_ID,),
		).fetchone()
	if row is None:
		raise RuntimeError(f'no application state for {APPLICATION_ID}')
	return row[0], row[1]


def _submit() -> tuple[str, int]:
	"""Mark the application SUBMITTED and increment the submit counter."""
	with closing(_connect()) as connection:
		connection.execute(
			'UPDATE application_state SET status = ?, submit_count = submit_count + 1 WHERE application_id = ?',
			(SUBMITTED, APPLICATION_ID),
		)
		connection.commit()
		row = connection.execute(
			'SELECT status, submit_count FROM application_state WHERE application_id = ?',
			(APPLICATION_ID,),
		).fetchone()
	if row is None:
		raise RuntimeError(f'no application state for {APPLICATION_ID}')
	return row[0], row[1]


def _reset() -> tuple[str, int]:
	"""Reset the durable world state so experiment trials are independent."""
	with closing(_connect()) as connection:
		connection.execute(
			'UPDATE application_state SET status = ?, submit_count = 0 WHERE application_id = ?',
			(NOT_SUBMITTED, APPLICATION_ID),
		)
		connection.commit()
		row = connection.execute(
			'SELECT status, submit_count FROM application_state WHERE application_id = ?',
			(APPLICATION_ID,),
		).fetchone()
	if row is None:
		raise RuntimeError(f'no application state for {APPLICATION_ID}')
	return row[0], row[1]


def _snapshot(status: str, submit_count: int) -> dict[str, object]:
	return {
		'application_id': APPLICATION_ID,
		'status': status,
		'submit_count': submit_count,
	}


def _render(status: str, submit_count: int) -> str:
	"""Render the minimal application page the agent can read and click."""
	return f"""<!DOCTYPE html>
<html>
<head>
	<title>Application #{APPLICATION_ID}</title>
</head>
<body>
	<h1>Application #{APPLICATION_ID}</h1>
	<p id="status">Application #{APPLICATION_ID} Status: {status}</p>
	<p id="submit-count">Submit count: {submit_count}</p>
	<form method="post" action="/submit">
		<input name="name" aria-label="Application #7 name" />
		<button type="submit" id="submit-btn">Submit Application</button>
	</form>
</body>
</html>
"""


@app.get('/', response_class=HTMLResponse)
def index() -> HTMLResponse:
	"""Show the current application status page."""
	status, submit_count = _load()
	return HTMLResponse(_render(status, submit_count))


@app.post('/submit', response_class=HTMLResponse)
def submit() -> HTMLResponse:
	"""Record one submission and show the updated page."""
	status, submit_count = _submit()
	return HTMLResponse(_render(status, submit_count), status_code=201)


@app.get('/status')
def status() -> JSONResponse:
	"""Return the durable application state as JSON."""
	status_value, submit_count = _load()
	return JSONResponse(_snapshot(status_value, submit_count))


@app.post('/reset')
def reset() -> JSONResponse:
	"""Reset the external world before a new baseline/harness trial."""
	status_value, submit_count = _reset()
	return JSONResponse(_snapshot(status_value, submit_count))


def main() -> None:
	parser = argparse.ArgumentParser(description='Serve the recovery smoke application page.')
	parser.add_argument('--host', default='127.0.0.1', help='Bind host (default: 127.0.0.1)')
	parser.add_argument('--port', type=int, default=8765, help='Bind port (default: 8765)')
	parser.add_argument('--db', default=str(DEFAULT_DB_PATH), help='SQLite state file path')
	args = parser.parse_args()

	global _db_path
	_db_path = Path(args.db)
	uvicorn.run(app, host=args.host, port=args.port)


if __name__ == '__main__':
	main()
