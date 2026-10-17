import pytest
from sqlalchemy.engine import make_url
from scripts.render_external_database import external_database_url, APPROVED_HOST, EXTERNAL_HOST


@pytest.mark.parametrize('host', [APPROVED_HOST, EXTERNAL_HOST])
def test_external_worker_preserves_credentials_and_requires_tls(host):
    result = make_url(external_database_url(
        f'postgresql://betappv33_user:encoded%40password@{host}/betappv33?sslmode=require'))
    assert result.host == EXTERNAL_HOST
    assert result.username == 'betappv33_user'
    assert result.password == 'encoded@password'
    assert result.database == 'betappv33'
    assert result.query == {'ssl':'require'}
    assert result.drivername == 'postgresql+asyncpg'


@pytest.mark.parametrize('value', [
    'postgresql://user:secret@wrong-host/betappv33',
    f'postgresql://user:secret@{APPROVED_HOST}/wrong_database',
    'not a database URL',
])
def test_wrong_database_fails_without_echoing_credentials(value):
    with pytest.raises(ValueError) as exc:
        external_database_url(value)
    assert 'secret' not in str(exc.value) or str(exc.value) == 'Invalid existing PostgreSQL secret'
    assert value not in str(exc.value)
