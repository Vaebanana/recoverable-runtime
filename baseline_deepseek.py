import asyncio
import os

from dotenv import load_dotenv

from browser_use import Agent, BrowserSession
from browser_use.llm import ChatDeepSeek

load_dotenv()


async def main():
	llm = ChatDeepSeek(
		base_url='https://api.deepseek.com/v1',
		model='deepseek-v4-flash',
		api_key=os.getenv('DEEPSEEK_API_KEY'),
	)

	# 指定优先使用 Microsoft Edge
	browser = BrowserSession(
		channel='msedge',
	)

	agent = Agent(
		task='打开 https://www.bing.com，搜索 Browser Use，并告诉我搜索结果页的标题',
		llm=llm,
		browser_session=browser,
		use_vision=False,
	)

	await agent.run()


asyncio.run(main())
