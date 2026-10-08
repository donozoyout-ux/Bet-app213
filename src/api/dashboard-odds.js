/* Prices remain grouped by bookmaker. In-play raw data is never a closing price. */
(() => {
  const liveStates = ['live','first_half','half_time','second_half','extra_time','penalties'];
  const number = value => value == null ? '—' : String(value);
  function stage(match, market) {
    if (match.status === 'finished') return 'closing';
    if (liveStates.includes(match.status) && Object.values(market?.closing || {}).some(v => v != null)) return 'closing';
    return 'latest';
  }
  function movement(match, market, field) {
    if (!market) return '—';
    const opening=market.opening?.[field], selected=market[stage(match,market)]?.[field];
    if(opening == null && selected == null)return '—';
    return `${number(opening)} → ${number(selected)}`;
  }
  function asianMovement(match, market, fields) {
    if (!market) return '—';
    if(fields.every(field=>market.opening?.[field] == null && market[stage(match,market)]?.[field] == null))return '—';
    const format = kind => fields.map(field => number(market[kind]?.[field])).join(' / ');
    return `${format('opening')} → ${format(stage(match,market))}`;
  }
  const api = {stage, movement, asianMovement, liveStates, number};
  if (typeof module !== 'undefined') module.exports = api;
  else globalThis.BetAppOdds = Object.freeze(api);
})();
