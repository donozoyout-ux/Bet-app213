"""Bounded, read-only match dossier. No model execution or settlement writes."""
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, or_, and_
from sqlalchemy.orm import defer
from src.models import Match, MatchStatistics, PredictionSnapshot, utcnow
from src.jobs.worker import aware
from .read_cache import ReadCache

cache = ReadCache(ttl=20, limit=128)


async def history(session, match, scope, limit, offset=0):
    from .routes import match_query, match_response
    cutoff = min(aware(match.kickoff_at), utcnow()) if match.kickoff_at else utcnow()
    home, away = match.home_team_id, match.away_team_id
    conditions = {
        'home': or_(Match.home_team_id == home, Match.away_team_id == home),
        'away': or_(Match.home_team_id == away, Match.away_team_id == away),
        'home_venue': Match.home_team_id == home,
        'away_venue': Match.away_team_id == away,
        'h2h': or_(and_(Match.home_team_id == home, Match.away_team_id == away),
                   and_(Match.home_team_id == away, Match.away_team_id == home)),
    }
    query, _, _ = match_query()
    rows = (await session.execute(query.options(defer(Match.raw)).where(
        Match.id != match.id, Match.status == 'finished', Match.kickoff_at < cutoff,
        conditions[scope]).order_by(Match.kickoff_at.desc(), Match.id.desc())
        .offset(offset).limit(limit + 1))).all()
    stats = {s.match_id: s for s in await session.scalars(select(MatchStatistics)
        .options(defer(MatchStatistics.raw)).where(MatchStatistics.match_id.in_([r[0].id for r in rows[:limit]])))}
    items = []
    for row in rows[:limit]:
        m = row[0]
        s = stats.get(m.id)
        items.append(dict(**match_response(row), home_team_id=m.home_team_id,
            away_team_id=m.away_team_id, statistics=observations(s)))
    return dict(items=items, has_more=len(rows) > limit, offset=offset, limit=limit,
                cutoff=cutoff, coverage='Yalnızca aktif veritabanındaki kayıtlar; sezon arşivi taranmaz.')


def observations(stats):
    fields = ('home_corners', 'away_corners', 'home_yellow_cards', 'away_yellow_cards',
              'home_red_cards', 'away_red_cards')
    return dict(**{f: getattr(stats, f, None) for f in fields},
                source=getattr(stats, 'source', None), observed_at=getattr(stats, 'updated_at', None),
                is_final=bool(stats and stats.is_final))


def aggregate(items, team):
    values = {k: [] for k in ('goals_for', 'goals_against', 'first_half_for', 'first_half_against',
                             'btts', 'over_25', 'corners', 'yellow_cards', 'red_cards')}
    for metric in ('corners','yellow_cards','red_cards'):
        values[metric+'_against'] = []
        values[metric+'_total'] = []
    form = []
    for m in items:
        side, other = ('home', 'away') if m['home_team_id'] == team else ('away', 'home')
        gf, ga = m['ft_' + side], m['ft_' + other]
        for key, value in [('goals_for', gf), ('goals_against', ga),
                           ('first_half_for', m['ht_' + side]), ('first_half_against', m['ht_' + other])]:
            if value is not None: values[key].append(value)
        if gf is not None and ga is not None:
            form.append('G' if gf > ga else 'M' if gf < ga else 'B')
            values['btts'].append(int(gf > 0 and ga > 0))
            values['over_25'].append(int(gf + ga > 2.5))
        for metric in ('corners', 'yellow_cards', 'red_cards'):
            value = m['statistics'][side + '_' + metric]
            if m['statistics']['is_final'] and value is not None: values[metric].append(value)
            against = m['statistics'][other + '_' + metric]
            if m['statistics']['is_final']:
                if against is not None: values[metric+'_against'].append(against)
                if value is not None and against is not None: values[metric+'_total'].append(value+against)
    return dict(matches=len(items), form=form, wins=form.count('G'), draws=form.count('B'), losses=form.count('M'),
                **{k: dict(value=sum(v)/len(v) if v else None, sample=len(v)) for k,v in values.items()})


async def current_recommendations(session, match, now):
    """Read exactly the *published* league board used by the home page.

    Unlike PredictionSnapshot these selections rotate as new observations arrive.
    Never refresh a model, infer a pick after kickoff or accept an expired board.
    """
    if match.status != 'scheduled' or not match.kickoff_at or aware(match.kickoff_at) <= now:
        return dict(current_recommendations=[], current_generated_at=None, current_expires_at=None,
                    current_note='Başlamış veya bitmiş maç için güncel öneri yayımlanmaz.')
    from src.analytics.board_cache import read_board
    try:
        items, _, _ = await read_board(session, str(match.league_id), None, None, now)
    except Exception:
        # The immutable dossier must remain readable when the optional
        # rotating recommendation board is temporarily unavailable.
        return dict(current_recommendations=[], current_generated_at=None, current_expires_at=None,
                    current_note='Güncel öneri yayınına şu anda ulaşılamıyor. İlk kaydedilen tahminler korunuyor.')
    publication = session.info.get('prediction_snapshot', {})
    matching = next((item for item in items if item['match']['id'] == match.id), None)
    latest = (matching or {}).get('recommendations', [])[:2]
    return dict(
        current_recommendations=[
            {key: pick.get(key) for key in ('id', 'label', 'selection', 'market',
              'probability', 'reliability', 'sample_size', 'confidence', 'line', 'period', 'engine_version') if key in pick}
            for pick in latest
        ],
        current_generated_at=publication.get('latest_generated_at') if matching else None,
        current_expires_at=publication.get('expires_at') if matching else None,
        current_model_version=publication.get('model_version') if matching else None,
        current_board_version=publication.get('publication_versions', {}).get(str(match.league_id)) if matching else None,
        current_note=('Ana sayfa ile aynı güncel lig yayını. Yalnızca istatistiksel öneriler; '
                      'doğrulanmış oynanabilir oran anlamına gelmez.') if matching else
                     'Bu maç için geçerli güncel lig yayını veya nitelikli öneri yok. Eski seçimler değişmez.'
    )


async def dossier(session, match_id):
    from .routes import match_query, match_response, odds_data
    from src.analytics.settlements import decisions
    from src.analytics.performance import display_result
    query, _, _ = match_query()
    row = (await session.execute(query.options(defer(Match.raw)).where(Match.id == match_id))).first()
    if not row: raise HTTPException(404, 'Maç bulunamadı.')
    match = row[0]
    stats = await session.get(MatchStatistics, match_id)
    snapshot = await session.get(PredictionSnapshot, match_id)
    predictions = []
    valid = bool(snapshot and match.kickoff_at and aware(snapshot.captured_at) < aware(match.kickoff_at)
                 and aware(snapshot.captured_at) <= utcnow())
    if valid:
        saved = await decisions(session, match_id)
        for index, pick in enumerate(snapshot.picks):
            outcome, actual, reason = display_result(snapshot, match, stats, pick, saved.get(index))
            predictions.append(dict(pick=pick, outcome=outcome if reason == 'recorded_verified' or outcome == 'disputed' else 'awaiting_data',
                                    actual=actual if reason == 'recorded_verified' else None,
                                    verification=reason, recorded_at=saved[index].recorded_at if index in saved else None))
    histories, profiles = {}, {}
    for scope in ('home', 'away', 'home_venue', 'away_venue', 'h2h'):
        page = await history(session, match, scope, 10)
        histories[scope] = page
        if scope != 'h2h':
            team = match.home_team_id if scope.startswith('home') else match.away_team_id
            profiles[scope] = {str(n): aggregate(page['items'][:n], team) for n in (5, 10)}
    prices = []
    for price in await odds_data(session, match_id):
        if price.bookmaker not in ('Crown', 'Bet365', 'Sbobet'): continue
        data = price.model_dump(exclude={'raw'})
        data['source'] = price.source
        data['source_url'] = price.raw.get('source_url') if isinstance(price.raw, dict) else None
        prices.append(data)
    live = await current_recommendations(session, match, utcnow())
    return jsonable_encoder(dict(**live, match=match_response(row), team_ids=dict(home=match.home_team_id, away=match.away_team_id),
        observations=observations(stats), odds=prices, histories=histories, profiles=profiles,
        predictions=predictions, captured_at=snapshot.captured_at if valid else None,
        model_version=snapshot.model_version if valid else None, as_of=utcnow(), cache_seconds=20,
        prediction_note='Maç öncesi dondurulmuş seçimler.' if valid else 'Doğrulanabilir maç öncesi tahmin kaydı yok. Geriye dönük tahmin üretilmedi.'))
