"""Final-data requirements and audit details, independent of prediction generation."""
from datetime import timedelta
from src.models import utcnow
from src.jobs.worker import aware

FINAL_STATS = {
    'corners': ('home_corners','away_corners'),
    'yellow_cards': ('home_yellow_cards','away_yellow_cards'),
    'red_cards': ('home_red_cards','away_red_cards'),
    'cards': ('home_yellow_cards','away_yellow_cards','home_red_cards','away_red_cards'),
}
FIELD_LABELS = {
    'final_statistics':'doğrulanmış final istatistikleri',
    'ft_home':'ev sahibi final skoru','ft_away':'deplasman final skoru',
    'ht_home':'ev sahibi ilk yarı skoru','ht_away':'deplasman ilk yarı skoru',
    'consistent_half_scores':'birbiriyle tutarlı ilk yarı ve final skorları',
    **{side+'_'+stat: team+' '+label for side,team in [('home','ev sahibi'),('away','deplasman')]
       for stat,label in [('corners','korner sayısı'),('yellow_cards','sarı kart sayısı'),('red_cards','kırmızı kart sayısı')]},
}


def missing_fields(match, stats, pick):
    fields = FINAL_STATS.get(pick['market'])
    if fields:
        if not stats or not stats.is_final:return ['final_statistics', *fields]
        return [f for f in fields if getattr(stats,f,None) is None or getattr(stats,f)<0]
    fields = ['ht_home','ht_away'] if pick['market']=='first_half_goals' else ['ft_home','ft_away']
    if pick['market']=='second_half_goals':fields += ['ht_home','ht_away']
    missing=[f for f in fields if getattr(match,f,None) is None or getattr(match,f)<0]
    if not missing and 'ht_home' in fields and match.ft_home is not None and match.ft_away is not None and (match.ht_home>match.ft_home or match.ht_away>match.ft_away):
        return ['consistent_half_scores']
    return missing


def details(match, stats, pick, outcome, audit=None, now=None):
    now=now or utcnow()
    audit=audit or {}
    fields=missing_fields(match,stats,pick) if outcome=='awaiting_data' else []
    overdue=bool(match.kickoff_at and aware(match.kickoff_at)<=now-timedelta(hours=3))
    reason = ('Final veri eksik veya doğrulanmadı: '+', '.join(FIELD_LABELS.get(f,f) for f in fields)) if fields else {
        'pending': 'Final maç durumu bekleniyor' if overdue else 'Maçın tamamlanması bekleniyor',
        'awaiting_data': 'Desteklenmeyen seçim veya eksik final verisi',
        'void': 'İptal/ertelenme veya bahis çizgisinde eşitlik',
        'won': 'Yayınlanan final verisiyle doğrulandı',
        'lost': 'Yayınlanan final verisiyle doğrulandı',
    }[outcome]
    statistical=pick['market'] in FINAL_STATS
    # These are existing source observation timestamps, not invented settlement dates.
    observed=getattr(stats,'updated_at',None) if statistical else getattr(match,'status_observed_at',None)
    source=getattr(stats,'source',None) if statistical and stats and stats.is_final else ('goaloo' if observed else None)
    if match.status!='finished':observed=source=None
    return dict(reason=reason,missing_fields=fields,last_checked_at=audit.get('checked_at') or getattr(match,'live_checked_at',None),
                final_observed_at=aware(observed) if observed else None,result_source=source,
                check_source=audit.get('source'),retry_state=audit.get('state'),
                check_attempts=audit.get('attempts',0),next_check_at=audit.get('next_check_at'))
