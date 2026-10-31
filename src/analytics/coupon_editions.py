"""Immutable coupon contract, pure revision gate and read-only stored adapters.

No production writer is wired to this module. Persisting the returned proposal
requires a separately reviewed atomic compare-and-append transaction.
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from zoneinfo import ZoneInfo

from src.analytics.coupon_products import BANKO_MIN, BANKO_MAX, TARGETS, _price
from src.analytics.dynamic_coupon import select_portfolio
from src.analytics.coupon import draft_pick
from src.analytics.odds_matching import parse_market_and_selection

KINDS = ('banko', 'balanced', 'combined_6_7', 'combined_8_10')
TZ = ZoneInfo('Europe/Istanbul')
SCHEMA = 'coupon-edition-v1'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode('utf-8')).hexdigest()


def stamp(value):
    value = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('timezone_aware_timestamp_required')
    return value.astimezone(timezone.utc)


def scope(day, kind):
    if date.fromisoformat(day).isoformat() != day or kind not in KINDS:
        raise ValueError('invalid_publication_scope')


@dataclass(frozen=True)
class Edition:
    """Canonical JSON is immutable; every consumer receives a detached copy."""
    document: str

    def __post_init__(self):
        data = json.loads(self.document)
        scope(data['day'], data['kind'])
        content = {k: v for k, v in data.items() if k not in ('edition_id', 'content_digest')}
        if data['schema'] != SCHEMA or data['content_digest'] != digest(content):
            raise ValueError('edition_digest_mismatch')
        if data['edition_id'] != 'ce1:' + data['content_digest']:
            raise ValueError('edition_identity_mismatch')
        if self.document != canonical(data):
            raise ValueError('noncanonical_edition')

    def as_dict(self):
        return json.loads(self.document)


def _items(records):
    result = []
    for record in records:
        m, r, q = record['match'], record['recommendation'], record['quote']
        # Explicitly retain line/period and all price evidence, unlike stable().
        item = dict(match={k: m[k] for k in ('id', 'kickoff_at', 'home_team_id', 'away_team_id', 'home_team', 'away_team', 'league_id') if k in m},
                    recommendation={k: r[k] for k in ('id', 'market', 'selection', 'line', 'period', 'label', 'probability', 'reliability', 'score', 'qualifies', 'confidence') if k in r},
                    quote={k: q[k] for k in ('eligible', 'price', 'bookmaker', 'observed_at', 'expires_at', 'line', 'period') if k in q})
        for key in ('id', 'market', 'selection'):
            if not isinstance(r.get(key), str) or not r[key]:
                raise ValueError('missing_selection_identity')
        pick = draft_pick(r['id'])
        if not pick or (pick['market'], pick['selection']) != (r['market'], r['selection']):
            raise ValueError('recommendation_identity_mismatch')
        if type(m['id']) is not int or m['id'] <= 0:
            raise ValueError('invalid_match_id')
        _, _, line, period = parse_market_and_selection(r['market'], r['selection'])
        if 'line' not in q or q['line'] != line or q.get('period') != period:
            raise ValueError('quote_selection_mismatch')
        for key, expected in (('match_id', m['id']), ('recommendation_id', r['id']),
                              ('market', r['market']), ('selection', r['selection'])):
            if key in q and q[key] != expected:
                raise ValueError('quote_identity_mismatch')
        item['recommendation'].update(line=line, period=period)
        item['match']['kickoff_at'] = stamp(m['kickoff_at']).isoformat()
        for key in ('observed_at', 'expires_at'):
            item['quote'][key] = stamp(q[key]).isoformat()
        for key in ('line', 'period'):
            if key in r and key in q and r[key] != q[key]:
                raise ValueError('quote_selection_mismatch')
        result.append(item)
    return sorted(result, key=lambda x: (x['match']['id'], x['recommendation']['id']))


def propose(product, *, day, now, model_version, previous=None, new_revision=False):
    """Pure gate. Retry returns the original; changed content needs explicit revision.

    This is NOT a database publication or a concurrent-writer lock. Callers must
    retain the canonical document and compare the previous ID atomically.
    """
    kind = product['kind']
    scope(day, kind)
    now = stamp(now)
    if now.astimezone(TZ).date().isoformat() != day:
        raise ValueError('outside_publication_day')
    if not isinstance(model_version, str) or not model_version.strip():
        raise ValueError('model_version_required')
    if product['status'] not in ('candidate_only', 'unavailable'):
        raise ValueError('candidate_contract_required')
    items = _items(product['items'])
    state = 'published' if items else 'no_eligible'
    if (product['status'] == 'candidate_only') != bool(items):
        raise ValueError('candidate_state_mismatch')
    reason = None if items else product.get('reason')
    if not items and (not isinstance(reason, str) or not reason):
        raise ValueError('unavailability_reason_required')
    if reason == 'search_budget_exhausted':
        state = 'incomplete'
    total = None
    for item in items:
        total = _price(item) if total is None else total * _price(item)
    content = dict(schema=SCHEMA, day=day, kind=kind, model_version=model_version,
                   state=state, reason=reason, items=items,
                   combined_odds=str(total) if total is not None else None)
    old = previous.as_dict() if previous else None
    if old:
        if (old['day'], old['kind']) != (day, kind):
            raise ValueError('revision_scope_mismatch')
        if now < stamp(old['captured_as_of']):
            raise ValueError('revision_before_previous')
        if all(old[k] == v for k, v in content.items()):
            return previous  # Do not re-evaluate the original at retry time.
        if not new_revision:
            raise ValueError('explicit_revision_required')
    elif new_revision:
        raise ValueError('previous_edition_required')
    if items:
        vetted, _ = select_portfolio(items, now, strategy='cautious' if kind == 'banko' else 'balanced')
        if len(vetted) != len(items):
            raise ValueError('unqualified_publication')
        if kind == 'banko' and (len(items) != 1 or not BANKO_MIN <= total <= BANKO_MAX):
            raise ValueError('invalid_banko')
        if kind in TARGETS and (len(items) < 2 or not TARGETS[kind][0] <= total <= TARGETS[kind][1]):
            raise ValueError('invalid_target_range')
    content.update(captured_as_of=now.isoformat(), revision=old['revision'] + 1 if old else 1,
                   previous_id=old['edition_id'] if old else None,
                   first_edition_id=(old['first_edition_id'] or old['edition_id']) if old else None,
                   diff={k: {'before': old[k], 'after': v} for k, v in content.items() if old and old[k] != v})
    checksum = digest(content)
    return Edition(canonical(dict(content, content_digest=checksum, edition_id='ce1:' + checksum)))


def project(edition, now):
    """Shared Home/Kuponum/audit projection: never drops stale frozen selections."""
    now = stamp(now)
    data = edition.as_dict()
    if stamp(data['captured_as_of']) > now:
        raise ValueError('edition_not_yet_published')
    stale = (now.astimezone(TZ).date().isoformat() != data['day'] or any(
        now >= stamp(i['quote']['expires_at']) or
        (now - stamp(i['quote']['observed_at'])).total_seconds() > 900 or
        now >= stamp(i['match']['kickoff_at']) for i in data['items']))
    return dict(data, status='stale' if stale and data['items'] else data['state'],
                published=data['state'] == 'published', checked_at=now.isoformat(),
                evidence_status='frozen', first_record=data['revision'] == 1)


def project_four(editions, day, now):
    """No live-candidate fallback; callers supply stored editions only."""
    result = {}
    for kind in KINDS:
        scope(day, kind)
        edition = editions.get(kind)
        if edition is None:
            result[kind] = dict(day=day, kind=kind, status='absent', published=False,
                                reason='not_published', items=[])
        else:
            data = edition.as_dict()
            if (data['day'], data['kind']) != (day, kind):
                raise ValueError('projection_scope_mismatch')
            result[kind] = project(edition, now)
    return result


def telegram_dry_run(edition, now):
    data = project(edition, now)
    # Canonical frozen body is also the reviewable dry-run text. No send adapter.
    return dict(edition=data, text=edition.document, dry_run=True,
                sendable=data['status'] == 'published',
                deduplication_key=(data['day'], data['kind'], data['edition_id'], data['content_digest']))


async def read_saved(session, coupon_id):
    """One PK SELECT, no autoflush/model/odds lookup; no credentials or DDL.

    Legacy rows retain original IDs and payload, never acquire invented evidence
    or first-publication claims. The adapter does not expose private manual rows.
    """
    from sqlalchemy import select
    from src.models import SavedCoupon
    with session.no_autoflush:
        row = (await session.execute(select(SavedCoupon.id, SavedCoupon.root_id,
            SavedCoupon.revision, SavedCoupon.created_at, SavedCoupon.payload)
            .where(SavedCoupon.id == coupon_id, SavedCoupon.kind == 'automatic',
                   SavedCoupon.owner_hash.is_(None)))).one_or_none()
    if row is None:
        return None
    return dict(source='saved_coupons', source_id=row.id, root_id=row.root_id,
                revision=row.revision, captured_as_of=row.created_at,
                first_record=row.revision == 1, evidence_status='legacy_unverified',
                legacy_payload=json.loads(canonical(row.payload)),
                source_digest=digest(row.payload))
