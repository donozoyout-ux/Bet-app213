"""Low-priority durable worker, supervised by the web lifespan; no extra service."""
import asyncio
import logging
import os
import signal

from src.config import settings
from src.db import database, Database
from src.jobs.worker import work


async def run():
    task = asyncio.create_task(work())
    from src.jobs.telegram import loop as telegram_loop
    telegram = asyncio.create_task(telegram_loop(database))
    from src.analytics.learning import loop as learning_loop
    # A separate pool prevents learning reads from occupying the scraper /
    # Telegram connection budget. This task opens at most one session at a time.
    learning_db = Database(settings.database_url, role='learning')
    from src.jobs.settlement import loop as settlement_loop
    settlement_db = Database(settings.database_url, role='settlement')
    settlement = asyncio.create_task(settlement_loop(settlement_db))
    learning = asyncio.create_task(learning_loop(learning_db))
    from src.analytics.learning_ai import loop as ai_loop, config as ai_config
    ai_db = Database(settings.database_url, role='ai') if ai_config().active else None
    ai = asyncio.create_task(ai_loop(ai_db)) if ai_db else None
    loop = asyncio.get_running_loop()
    if os.name != 'nt':
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await task
    finally:
        if ai:ai.cancel()
        learning.cancel()
        settlement.cancel()
        telegram.cancel()
        await asyncio.gather(*[t for t in (telegram,learning,settlement,ai) if t is not None],return_exceptions=True)
        if ai_db:await ai_db.close()
        await learning_db.close()
        await settlement_db.close()
        await database.close()


if __name__ == '__main__':
    logging.basicConfig(level=settings.log_level)
    if hasattr(os, 'nice'):
        os.nice(10)
    try:
        asyncio.run(run())
    except asyncio.CancelledError:
        pass
