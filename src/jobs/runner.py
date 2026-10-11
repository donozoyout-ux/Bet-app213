"""Low-priority durable worker, supervised by the web lifespan; no extra service."""
import asyncio
import logging
import os
import signal

from src.config import settings
from src.db import database
from src.jobs.worker import work


async def run():
    task = asyncio.create_task(work())
    from src.jobs.telegram import loop as telegram_loop
    telegram = asyncio.create_task(telegram_loop(database))
    loop = asyncio.get_running_loop()
    if os.name != 'nt':
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await task
    finally:
        telegram.cancel()
        await asyncio.gather(telegram,return_exceptions=True)
        await database.close()


if __name__ == '__main__':
    logging.basicConfig(level=settings.log_level)
    if hasattr(os, 'nice'):
        os.nice(10)
    try:
        asyncio.run(run())
    except asyncio.CancelledError:
        pass
