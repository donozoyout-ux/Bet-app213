"""Resolve the approved existing Render database for external GitHub workers."""
from sqlalchemy.engine import make_url

APPROVED_HOST = 'dpg-db39f66k1f9s738tgug0-a'
EXTERNAL_HOST = APPROVED_HOST + '.frankfurt-postgres.render.com'


def external_database_url(value):
    try:
        url = make_url(value)
    except Exception:
        raise ValueError('Invalid existing PostgreSQL secret') from None
    if url.host not in {APPROVED_HOST, EXTERNAL_HOST} or url.database != 'betappv33':
        raise ValueError('History worker requires the approved existing Render database')
    if url.drivername not in {'postgres', 'postgresql', 'postgresql+asyncpg'}:
        raise ValueError('History worker requires PostgreSQL')
    query = {k:v for k,v in url.query.items() if k not in {'sslmode','ssl'}}
    query['ssl'] = 'require'  # asyncpg uses ssl, rather than libpq's sslmode.
    return url.set(drivername='postgresql+asyncpg', host=EXTERNAL_HOST,
                   query=query).render_as_string(hide_password=False)
