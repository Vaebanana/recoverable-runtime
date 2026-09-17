import importlib
import json
from datetime import datetime, timezone
from importlib.metadata import version as package_version
from pathlib import Path
from types import SimpleNamespace

from browser_use.agent.views import AgentHistoryList


class BrowserStateStub:
	async def get_browser_state_summary(self):
		return SimpleNamespace(url='https://example.test/current')


class AgentRunStub:
	def __init__(self):
		self.callbacks = None

	async def run(self, *, on_step_start=None, on_step_end=None):
		self.callbacks = (on_step_start, on_step_end)
		return AgentHistoryList(history=[])


def test_save_run_artifacts_writes_official_history_and_summary(tmp_path):
	day4_baseline = importlib.import_module('examples.recoverable_runtime.day4_baseline')
	task = day4_baseline.BaselineTask(task_id='level1_quotes', prompt='Collect three quotes.')
	history = AgentHistoryList(history=[])
	timestamp = datetime(2026, 9, 15, 12, 30, tzinfo=timezone.utc)

	history_path, summary_path = day4_baseline.save_run_artifacts(
		task=task,
		history=history,
		trace_dir=tmp_path,
		timestamp=timestamp,
	)

	assert history_path == tmp_path / 'level1_quotes_history.json'
	assert json.loads(history_path.read_text(encoding='utf-8')) == {'history': []}
	assert summary_path == tmp_path / 'level1_quotes_summary.json'
	assert json.loads(summary_path.read_text(encoding='utf-8')) == {
		'task_id': 'level1_quotes',
		'task': 'Collect three quotes.',
		'browser_use_version': package_version('browser-use'),
		'timestamp': '2026-09-15T12:30:00Z',
		'steps': 0,
		'duration_seconds': 0.0,
		'agent_done': False,
		'agent_success': None,
		'has_errors': False,
		'errors': [],
		'urls': [],
		'final_result': None,
	}


def test_resolve_task_ids_runs_both_baselines_in_order():
	day4_baseline = importlib.import_module('examples.recoverable_runtime.day4_baseline')

	assert day4_baseline.resolve_task_ids('all') == ['level1_quotes', 'level2_books']


def test_default_trace_dir_is_anchored_to_repository():
	day4_baseline = importlib.import_module('examples.recoverable_runtime.day4_baseline')
	assert day4_baseline.__file__ is not None
	repository_root = Path(day4_baseline.__file__).resolve().parents[2]

	assert day4_baseline.TRACE_DIR == repository_root / 'experiments' / 'day4' / 'traces'


def test_summary_preserves_steps_without_a_url():
	day4_baseline = importlib.import_module('examples.recoverable_runtime.day4_baseline')

	summary = day4_baseline.BaselineSummary(
		task_id='level1_quotes',
		task='Collect three quotes.',
		browser_use_version='0.13.10',
		timestamp=datetime(2026, 9, 15, 12, 30, tzinfo=timezone.utc),
		steps=1,
		duration_seconds=1.0,
		agent_done=False,
		agent_success=None,
		has_errors=False,
		errors=[None],
		urls=[None],
		final_result=None,
	)

	assert summary.model_dump(mode='json')['urls'] == [None]


async def test_step_end_reports_the_completed_step_and_current_results(capsys):
	day4_baseline = importlib.import_module('examples.recoverable_runtime.day4_baseline')
	agent = SimpleNamespace(
		browser_session=BrowserStateStub(),
		state=SimpleNamespace(n_steps=2, last_result=['current result']),
		history=SimpleNamespace(history=[SimpleNamespace(result=['stale result'])]),
	)

	await day4_baseline.on_step_end(agent)

	assert capsys.readouterr().out == ("[STEP END] step=1 url=https://example.test/current\nresults: ['current result']\n")


async def test_step_start_reports_current_step_and_url(capsys):
	day4_baseline = importlib.import_module('examples.recoverable_runtime.day4_baseline')
	agent = SimpleNamespace(
		browser_session=BrowserStateStub(),
		state=SimpleNamespace(n_steps=1),
	)

	await day4_baseline.on_step_start(agent)

	assert capsys.readouterr().out == '\n[STEP START] step=1 url=https://example.test/current\n'


def test_create_agent_uses_deepseek_edge_without_vision(monkeypatch):
	day4_baseline = importlib.import_module('examples.recoverable_runtime.day4_baseline')
	monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-api-key')

	agent = day4_baseline.create_agent(day4_baseline.BaselineTask(task_id='task', prompt='Do the task.'))

	assert agent.llm.model == 'deepseek-v4-flash'
	assert agent.browser_session.browser_profile.channel.value == 'msedge'
	assert agent.settings.use_vision is False


async def test_run_task_forwards_hooks_and_writes_to_selected_trace_dir(tmp_path, monkeypatch):
	day4_baseline = importlib.import_module('examples.recoverable_runtime.day4_baseline')
	agent = AgentRunStub()
	monkeypatch.setattr(day4_baseline, 'create_agent', lambda task: agent)
	task = day4_baseline.BaselineTask(task_id='level1_quotes', prompt='Collect three quotes.')

	history_path, summary_path = await day4_baseline.run_task(task, trace_dir=tmp_path)

	assert agent.callbacks == (day4_baseline.on_step_start, day4_baseline.on_step_end)
	assert history_path == tmp_path / 'level1_quotes_history.json'
	assert summary_path == tmp_path / 'level1_quotes_summary.json'
