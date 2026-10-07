import argparse
import asyncio
import logging
from sqlalchemy import select
from src.config import settings
from src.db import database
from src.models import ScraperJob
from .worker import enqueue, work


def main(kind):
    parser = argparse.ArgumentParser()
    parser.add_argument('--league', type=int, default=36, help='Goaloo external league ID')
    parser.add_argument('--start-year', type=int, default=2024)
    parser.add_argument('--resume', type=int)
    parser.add_argument('--enqueue-only', action='store_true')
    args = parser.parse_args()
    if not database.configured:
        parser.error('Set DATABASE_URL first')
    if args.start_year < 2024:
        parser.error('start-year must be 2024 or later')
    logging.basicConfig(level=settings.log_level, format='%(asctime)s %(levelname)s %(name)s %(message)s')

    async def run():
        try:
            await database.initialize()
            job = await enqueue(kind, args.league, args.start_year, resume_id=args.resume)
            print(f'Queued job_id={job.id}')
            if not args.enqueue_only:
                # A web worker may own the lock. Wait for this durable job to finish.
                while True:
                    await work(once=True)
                    async with database.session() as session:
                        status = await session.scalar(select(ScraperJob.status).where(ScraperJob.id == job.id))
                    if status not in {'queued', 'running'}:
                        print(f'job_id={job.id} status={status}')
                        if status != 'completed':
                            raise SystemExit(1)
                        break
                    await asyncio.sleep(3)
        finally:
            await database.close()
    asyncio.run(run())
