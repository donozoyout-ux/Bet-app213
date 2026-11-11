"""Sequential, GET-only latency sample. No refresh, writes or response data saved."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import math
import statistics
import time

import httpx


async def run(base, samples):
    report = {'observed_at': datetime.now(timezone.utc).isoformat(),
              'concurrency': 1, 'samples_per_path': samples,
              'cold_definition': 'First client request; server cache/wakeup state unknown',
              'sql_count': None, 'paths': {}}
    async with httpx.AsyncClient(base_url=base, timeout=20) as client:
        paths = ['/api/matches?view=upcoming&limit=12&include_odds=true',
                 '/api/predictions', '/api/predictions/best', '/api/coupons/daily',
                 '/api/dashboard/revision', '/api/market-performance?limit=1']
        for path in paths:
            durations = []
            for index in range(samples):
                started = time.perf_counter()
                response = await client.get(path)
                durations.append(round((time.perf_counter()-started)*1000, 2))
                response.raise_for_status()  # Stop immediately on overload/error.
                if index == 0 and path.startswith('/api/matches?'):
                    items = response.json().get('items', [])
                    if items:
                        paths.append('/api/matches/'+str(items[0]['id']))
                await asyncio.sleep(.25)
            report['paths'][path] = {'first_ms': durations[0], 'warm_n': len(durations)-1,
                'warm_median_ms': statistics.median(durations[1:]),
                'warm_p95_ms': sorted(durations[1:])[math.ceil(.95*(len(durations)-1))-1],
                'errors': 0}
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--samples', type=int, choices=range(20, 51), default=21)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args.base_url, args.samples))
    with open(args.output, 'w', encoding='utf-8') as output:
        json.dump(result, output, indent=2)
    print(json.dumps(result, indent=2))
