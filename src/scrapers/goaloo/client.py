import asyncio
import json
import logging
import time
import httpx
from src.config import settings

log = logging.getLogger('goaloo')


class SourceError(ValueError):
    """The source returned an error or an unrecognized schema."""


class GoalooClient:
    def __init__(self, transport=None):
        self.client = httpx.AsyncClient(
            transport=transport, timeout=settings.request_timeout, follow_redirects=True,
            headers={'User-Agent': 'Mozilla/5.0 (compatible; BetApp213/2.0)',
                     'Referer': 'https://www.goaloo.com/football/',
                     'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json'},
        )
        self.semaphore = asyncio.Semaphore(settings.concurrency)
        self.rate_lock = asyncio.Lock()
        self.next_request = 0.0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    async def get(self, url, params=None):
        return await self._request(url, params, as_json=True)

    async def get_text(self, url, params=None):
        return await self._request(url, params, as_json=False)

    async def _request(self, url, params, as_json):
        async with self.semaphore:
            for attempt in range(settings.retries):
                async with self.rate_lock:
                    await asyncio.sleep(max(0, self.next_request - time.monotonic()))
                    self.next_request = time.monotonic() + settings.request_interval
                try:
                    response = await self.client.get(url, params=params)
                    response.raise_for_status()
                    if not as_json:
                        return response.text.lstrip('\ufeff')
                    payload = json.loads(response.text.lstrip('\ufeff'))
                    if not isinstance(payload, dict):
                        raise SourceError('Goaloo returned a non-object response')
                    if payload.get('ErrCode', 0) != 0 or 'code' in payload:
                        raise SourceError('Goaloo rejected request or returned an application error')
                    return payload
                except (httpx.HTTPError, ValueError) as exc:
                    log.warning('[SOURCE] request failed url=%s attempt=%s error=%s', url, attempt + 1, type(exc).__name__)
                    if attempt + 1 == settings.retries:
                        raise SourceError(f'Goaloo request failed after {settings.retries} attempts: {type(exc).__name__}') from exc
                    await asyncio.sleep(min(20, 2 ** attempt))
