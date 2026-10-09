"""Cooperative memory backpressure on the existing Linux container."""
import asyncio
import logging
import os
from pathlib import Path


def memory_constrained(root=Path('/sys/fs/cgroup')):
    if os.getenv('BETAPP_PROCESS_ROLE') != 'scraper':
        return False
    for usage, limit in [('memory.current', 'memory.max'),
                         ('memory/memory.usage_in_bytes', 'memory/memory.limit_in_bytes')]:
        try:
            used, maximum = int((root / usage).read_text()), int((root / limit).read_text())
            return maximum > 0 and used / maximum >= .85
        except (OSError, ValueError):
            continue
    return False


async def wait_for_capacity():
    reported = False
    while memory_constrained():
        if not reported:
            logging.getLogger('scraper').warning('[WORKER] memory pressure; pausing at durable boundary')
            reported = True
        await asyncio.sleep(5)
