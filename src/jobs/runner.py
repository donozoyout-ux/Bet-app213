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
    learning_db = Database(settings.database_url)
    learning = asyncio.create_task(learning_loop(learning_db))
    loop = asyncio.get_running_loop()
    if os.name != 'nt':
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await task
    finally:
        learning.cancel()
        telegram.cancel()
        await asyncio.gather(telegram,learning,return_exceptions=True)
        await learning_db.close()
        await database.close()


if __name__ == '__main__':
    logging.basicConfig(level=settings.log_level)
    if hasattr(os, 'nice'):
        os.nice(10)
    try:
        asyncio.run(run())
    except asyncio.CancelledError:
        pass
