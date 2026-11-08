"""Fail-closed authorization for the manual GitHub Actions R2 pilot trigger."""
import jwt
from jwt import PyJWKClient


ISSUER = 'https://token.actions.githubusercontent.com'
AUDIENCE = 'betapp-r2-board-edition-pilot'
REPOSITORY = 'donozoyout-ux/Bet-app213'
REF = 'refs/heads/master'
WORKFLOW = f'{REPOSITORY}/.github/workflows/r2-board-edition-pilot.yml@{REF}'
SUBJECT = f'repo:{REPOSITORY}:ref:{REF}'
JWKS = PyJWKClient(f'{ISSUER}/.well-known/jwks', timeout=5, lifespan=300)


def verify_github_actions_token(token: str) -> dict:
    signing_key = JWKS.get_signing_key_from_jwt(token)
    return jwt.decode(token, signing_key.key, algorithms=['RS256'],
        audience=AUDIENCE, issuer=ISSUER, leeway=30,
        options={'require': ['exp', 'iat', 'iss', 'aud', 'sub']})


def allowed_pilot_claims(claims: dict) -> bool:
    """Only the manually dispatched workflow on the production branch may trigger."""
    return (claims.get('repository') == REPOSITORY
        and claims.get('ref') == REF
        and claims.get('workflow_ref') == WORKFLOW
        and claims.get('event_name') == 'workflow_dispatch'
        and claims.get('sub') == SUBJECT)
