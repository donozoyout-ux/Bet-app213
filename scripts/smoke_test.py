"""HTTP smoke test: python scripts/smoke_test.py https://bet-app213.onrender.com"""
import argparse
import json
import httpx


def smoke_test(base_url, timeout=20):
    checks = []
    with httpx.Client(base_url=base_url.rstrip('/'), timeout=timeout, follow_redirects=True) as client:
        def check(path, content_type=None):
            response = client.get(path)
            assert response.status_code == 200, f'{path}: HTTP {response.status_code}'
            if content_type:
                assert content_type in response.headers.get('content-type', ''), f'{path}: unexpected content type'
            checks.append({'path': path, 'status': response.status_code})
            return response
        assert check('/health').json().get('status') == 'ok'
        assert '<html' in check('/', 'text/html').text.lower()
        check('/dashboard.js', 'javascript')
        check('/dashboard-odds.js', 'javascript')
        status = client.get('/api/status')
        assert status.status_code in (200, 503), f'/api/status: HTTP {status.status_code}'
        connected = False
        if status.status_code == 200:
            db_status = status.json().get('database')
            assert db_status in ('connected', 'unavailable', 'unconfigured'), 'Unknown database status'
            connected = db_status == 'connected'
        checks.append({'path': '/api/status', 'status': status.status_code})
        if connected:
            assert isinstance(check('/api/leagues').json(), list)
            assert isinstance(check('/api/matches?view=history&limit=5').json()['items'], list)
            assert isinstance(check('/api/scraper/status').json()['jobs'], list)
    return {'ok': True, 'database_connected': connected, 'checks': checks}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('base_url', nargs='?', default='http://127.0.0.1:8000')
    args = parser.parse_args()
    try:
        print(json.dumps(smoke_test(args.base_url), indent=2))
    except (AssertionError, httpx.HTTPError) as exc:
        # Do not echo credentials that could be embedded in a URL.
        print(json.dumps({'ok': False, 'error_type': type(exc).__name__}))
        raise SystemExit(1)
