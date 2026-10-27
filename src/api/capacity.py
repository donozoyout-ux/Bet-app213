"""Admit work before SQL checkout; reserve existing pool capacity for metadata."""
from starlette.responses import JSONResponse

FAST_READS = frozenset({'/api/leagues', '/api/bookmakers', '/api/status',
    '/api/coupons/daily', '/api/coupons/automatic', '/api/dashboard/revision', '/api/runtime-health'})


class DatabaseCapacityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not scope['path'].startswith('/api/'):
            return await self.app(scope, receive, send)
        from src.api.routes import database
        if scope['method'] not in {'GET', 'HEAD', 'OPTIONS'} and getattr(database, 'storage_blocked', False):
            response = JSONResponse(status_code=503, content={'code':'database_storage_full', 'detail':'Database writes paused pending storage recovery.'})
            return await response(scope, receive, send)
        if not hasattr(database, 'request_slot'):
            return await self.app(scope, receive, send)
        lane = 'metadata' if scope['method'] == 'GET' and scope['path'] in FAST_READS else 'general'
        # Catch only admission failure, never turn an endpoint's own timeout into
        # an admission error. Release before client backpressure on response send.
        from src.db import AdmissionTimeout
        try:
            async with database.request_slot(lane) as release:
                async def send_response(message):
                    if message['type'] == 'http.response.start': release()
                    await send(message)
                return await self.app(scope, receive, send_response)
        except AdmissionTimeout:
            response = JSONResponse(status_code=503, headers={'Retry-After':'1'},
                content={'detail':'API capacity busy. Retry shortly.', 'code':'api_busy'})
            await response(scope, receive, send)
