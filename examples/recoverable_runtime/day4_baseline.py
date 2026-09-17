"""Run the Day 4 Browser Use baselines and persist their trajectories."""

import argparse
import asyncio
import os
from datetime import datetime
from importlib.metadata import version
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

from browser_use import Agent, BrowserSession
from browser_use.agent.views import AgentHistoryList
from browser_use.llm import ChatDeepSeek

load_dotenv()

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TRACE_DIR = REPOSITORY_ROOT / 'experiments' / 'day4' / 'traces'


class BaselineTask(BaseModel):
	"""Validated input for one Day 4 baseline task."""

	task_id: str
	prompt: str


class BaselineSummary(BaseModel):
	"""Serializable summary of one Browser Use agent run."""

	task_id: str
	task: str
	browser_use_version: str
	timestamp: datetime
	steps: int
	duration_seconds: float
	agent_done: bool
	agent_success: bool | None
	has_errors: bool
	errors: list[str | None]
	urls: list[str | None]
	final_result: str | None


TASKS = {
	'level1_quotes': BaselineTask(
		task_id='level1_quotes',
		prompt="""
Go to https://quotes.toscrape.com/.

Extract the first 3 quotes shown on the homepage,
together with the author of each quote.

Do not use another website.

Return the result clearly.
""",
	),
	'level2_books': BaselineTask(
		task_id='level2_books',
		prompt="""
Go to https://books.toscrape.com/.

Navigate through the website to the Travel category.

Inspect the first 5 books in that category.

For each book, return:
- title
- price
- availability

Then identify the cheapest book among those 5.

Do not use search engines or other websites.
""",
	),
}


def resolve_task_ids(selection: str) -> list[str]:
	"""Resolve a CLI task selection to the baseline task IDs to run."""
	if selection == 'all':
		return list(TASKS)
	if selection not in TASKS:
		raise ValueError(f'Unknown task: {selection}')
	return [selection]


def save_run_artifacts(
	*,
	task: BaselineTask,
	history: AgentHistoryList,
	trace_dir: Path = TRACE_DIR,
	timestamp: datetime | None = None,
) -> tuple[Path, Path]:
	"""Save the official trajectory and a compact, validated run summary."""
	trace_dir.mkdir(parents=True, exist_ok=True)
	history_path = trace_dir / f'{task.task_id}_history.json'
	summary_path = trace_dir / f'{task.task_id}_summary.json'

	history.save_to_file(history_path)
	summary = BaselineSummary(
		task_id=task.task_id,
		task=task.prompt.strip(),
		browser_use_version=version('browser-use'),
		timestamp=timestamp or datetime.now().astimezone(),
		steps=history.number_of_steps(),
		duration_seconds=history.total_duration_seconds(),
		agent_done=history.is_done(),
		agent_success=history.is_successful(),
		has_errors=history.has_errors(),
		errors=history.errors(),
		urls=history.urls(),
		final_result=history.final_result(),
	)
	summary_path.write_text(summary.model_dump_json(indent=2) + '\n', encoding='utf-8')
	return history_path, summary_path


async def on_step_start(agent: Agent) -> None:
	"""Print the step number and current URL before Browser Use processes a state."""
	state = await agent.browser_session.get_browser_state_summary()
	print(f'\n[STEP START] step={agent.state.n_steps} url={state.url}')


async def on_step_end(agent: Agent) -> None:
	"""Print the URL and action results after Browser Use completes a step."""
	state = await agent.browser_session.get_browser_state_summary()
	completed_step = max(0, agent.state.n_steps - 1)
	print(f'[STEP END] step={completed_step} url={state.url}')
	if agent.state.last_result:
		print('results:', agent.state.last_result)


def create_agent(task: BaselineTask) -> Agent:
	"""Create the DeepSeek-backed Browser Use agent for one baseline task."""
	api_key = os.getenv('DEEPSEEK_API_KEY')
	if not api_key:
		raise RuntimeError('DEEPSEEK_API_KEY is required in the environment or .env file')

	llm = ChatDeepSeek(
		base_url='https://api.deepseek.com/v1',
		model='deepseek-v4-flash',
		api_key=api_key,
	)
	browser = BrowserSession(channel='msedge')
	return Agent(
		task=task.prompt,
		llm=llm,
		browser_session=browser,
		use_vision=False,
	)


async def run_task(task: BaselineTask, trace_dir: Path = TRACE_DIR) -> tuple[Path, Path]:
	"""Run one baseline task and persist its full and summarized traces."""
	print(f'\n========== {task.task_id} ==========\n')
	agent = create_agent(task)
	history = await agent.run(on_step_start=on_step_start, on_step_end=on_step_end)
	history_path, summary_path = save_run_artifacts(task=task, history=history, trace_dir=trace_dir)

	print('\n========== RESULT ==========')
	print(history.final_result())
	print('\nSteps:', history.number_of_steps())
	print('Errors:', history.errors())
	print('History:', history_path)
	print('Summary:', summary_path)
	return history_path, summary_path


async def main(selection: str) -> None:
	"""Run the selected Day 4 baseline task or both tasks in order."""
	for task_id in resolve_task_ids(selection):
		await run_task(TASKS[task_id])


def parse_args() -> argparse.Namespace:
	"""Parse the Day 4 baseline command-line options."""
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument(
		'--task',
		choices=[*TASKS, 'all'],
		default='level1_quotes',
		help='Baseline task to run (default: level1_quotes).',
	)
	return parser.parse_args()


if __name__ == '__main__':
	args = parse_args()
	asyncio.run(main(args.task))
