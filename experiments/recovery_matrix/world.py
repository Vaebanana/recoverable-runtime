"""Shared real-world helpers for the local application experiment."""

from __future__ import annotations

import asyncio

import httpx

from browser_use.browser.session import BrowserSession


async def reset_world(server_url: str) -> dict[str, object]:
	async with httpx.AsyncClient() as client:
		response = await client.post(f'{server_url}/reset')
		response.raise_for_status()
		return response.json()


async def fetch_world_state(server_url: str) -> dict[str, object]:
	async with httpx.AsyncClient() as client:
		response = await client.get(f'{server_url}/status')
		response.raise_for_status()
		return response.json()


async def wait_for_submit_count(
	server_url: str,
	expected_count: int,
	*,
	timeout: float = 10.0,
) -> dict[str, object]:
	"""Wait for the browser's form POST to become visible in external state."""
	deadline = asyncio.get_running_loop().time() + timeout
	while True:
		state = await fetch_world_state(server_url)
		if int(str(state.get('submit_count', -1))) >= expected_count:
			return state
		if asyncio.get_running_loop().time() >= deadline:
			return state
		await asyncio.sleep(0.1)


async def submit_once(
	browser_session: BrowserSession,
	server_url: str,
	*,
	expected_count: int,
) -> dict[str, object]:
	"""Execute one real browser submit and wait until the external world records it."""
	page = await browser_session.must_get_current_page()
	await page.goto(server_url)
	elements = await page.get_elements_by_css_selector('#submit-btn')
	if not elements:
		raise RuntimeError('submit button (#submit-btn) not found')
	await elements[0].click()
	state = await wait_for_submit_count(server_url, expected_count)
	if int(str(state.get('submit_count', -1))) < expected_count:
		raise RuntimeError(f'submit did not reach external world: expected count {expected_count}, got {state}')
	return state
