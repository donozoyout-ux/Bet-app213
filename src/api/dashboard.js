/* Bind the existing Stitch shell to recorded data. Unavailable data is never simulated. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const state = {leagues: [], selected: null, bookmaker: 'Crown', status: '', offset: 0, total: 0, job: null, busy: false, generation: 0};
  const empty = 'Henüz veri yok';
  const number = n => n == null ? '—' : String(n);
  const score = (h, a) => h == null || a == null ? '—' : `${h} - ${a}`;
  const date = value => value ? new Date(value).toLocaleString('tr-TR') : empty;
  const write = (id, text) => {if ($(id)) $(id).textContent = text;};
  const statuses = {finished: 'Bitti', scheduled: 'Planlandı', live: 'Canlı', half_time: 'Devre arası', postponed: 'Ertelendi', cancelled: 'İptal', abandoned: 'Yarıda kaldı'};
  async function api(path, options = {}) {
    const response = await fetch(path, {...options, signal: AbortSignal.timeout(15000)});
    if (!response.ok) {
      let message = `HTTP ${response.status}`;
      try { const body = await response.json(); if (typeof body.detail === 'string') message = body.detail; } catch (_) {}
      throw new Error(message);
    }
    return response.json();
  }
  function option(select, value, text) {
    const item = document.createElement('option'); item.value = value; item.textContent = text; select.append(item);
  }
  async function loadSeasons() {
    const select = $('season-select'); select.replaceChildren(); option(select, '', 'Tüm sezonlar');
    const league = $('league-select').value;
    if (!league) return;
    const rows = await api(`/api/leagues/${league}/seasons`);
    rows.forEach(row => option(select, row.season_name, row.season_name));
    // Rounds are discovered from recorded matches; no fictitious season choices.
    const rounds = $('round-select'); rounds.replaceChildren(); option(rounds, '', 'Tüm haftalar');
    let offset = 0; const found = new Set();
    while (true) {
      const page = await api(`/api/matches?league=${league}&limit=200&offset=${offset}`);
      page.items.forEach(m => found.add(m.round)); offset += page.items.length;
      if (!page.items.length || offset >= page.total) break;
    }
    [...found].sort((a,b) => a-b).forEach(n => option(rounds, n, `${n}. Hafta`));
  }
  async function loadLeagues() {
    state.leagues = await api('/api/leagues');
    const select = $('league-select'); select.replaceChildren();
    const nav = $('league-nav');
    const books = await api('/api/bookmakers');
    document.querySelectorAll('[data-bookmaker]').forEach(button => button.disabled = !books.some(book => book.name === button.dataset.bookmaker));
    state.leagues.forEach(league => {
      option(select, league.id, league.name);
      let button = [...nav.querySelectorAll('a')].find(link => link.textContent.includes('Premier League') && league.external_id === 36);
      if (!button) {
        button = document.createElement('button'); button.type='button';
        button.className='px-space-sm py-2 rounded-lg text-left hover:bg-surface-container';button.textContent=league.name;nav.append(button);
      }
      button.removeAttribute('aria-disabled');
      button.onclick = async event => {event.preventDefault();select.value = league.id; state.offset = 0; await loadSeasons(); await loadMatches();};
    });
    await loadSeasons();
  }
  function cell(row, text) {
    const index = row._cellIndex || 0; row._cellIndex = index + 1;
    let td = row.children[index];
    if (!td) {td=document.createElement('td');td.className='py-3 px-2 align-middle';row.append(td);}
    const value=td.firstElementChild || td;value.textContent=text;return td;
  }
  function marketText(odds, key, field) {
    const market = odds.find(o => o.bookmaker === state.bookmaker && o.market === key);
    if (!market) return '—';
    const current = market.closing[field] ?? market.latest[field];
    return `${number(market.opening[field])} / ${number(current)}`;
  }
  async function loadMatches() {
    const generation = ++state.generation;
    const query = new URLSearchParams({limit: '50', offset: String(state.offset), include_odds: 'true'});
    [['league', $('league-select').value], ['season', $('season-select').value], ['round', $('round-select').value], ['status', state.status === 'today' ? '' : state.status], ['team', document.querySelector('[data-team-search]').value]].forEach(([k,v]) => {if (v) query.set(k,v);});
    if (state.status === 'today') query.set('date', new Date().toISOString().slice(0,10));
    try {
      const page = await api(`/api/matches?${query}`);
      if (generation !== state.generation) return;
      state.total = page.total;
      const body = $('matches-body'); body.replaceChildren();
      page.items.forEach(match => {
        const row = $('match-row-template').content.firstElementChild.cloneNode(true);
        row.classList.toggle('bg-surface-container-low/60', state.selected === match.id);
        cell(row, statuses[match.status] || match.status); cell(row, match.league);
        const teams = cell(row, `${match.home_team} / ${match.away_team}`); teams.title = date(match.kickoff_at);
        cell(row, score(match.ft_home, match.ft_away));
        ['home','draw','away'].forEach(key => cell(row, marketText(match.odds, '1x2', key)));
        cell(row, marketText(match.odds, 'ah', 'line')); cell(row, marketText(match.odds, 'ou', 'line')); cell(row, empty);
        row.onclick = () => selectMatch(match.id); body.append(row);
      });
      if (!page.items.length) {const row = document.createElement('tr'); const td = cell(row, empty); td.colSpan = 10; body.append(row);}
      write('matches-count', `${page.total} maç • ${state.bookmaker} • Açılış / Güncel prematch`);
      write('table-count', `${page.total} maç`);
      $('previous-page').disabled = state.offset === 0; $('next-page').disabled = state.offset + 50 >= state.total;
      if (state.selected) await selectMatch(state.selected);
    } catch (error) {
      if (generation !== state.generation) return;
      const body = $('matches-body'); body.replaceChildren(); const row = document.createElement('tr'); const td = cell(row, 'Veri bekleniyor'); td.colSpan = 10; body.append(row);
      write('matches-count', error.message); clearDetail();
    }
  }
  function clearDetail() {
    write('detail-title', empty); write('detail-home-score', '—');write('detail-away-score','—');write('detail-home',empty);write('detail-away',empty);write('detail-ht', 'İY: —');
    for (const market of ['1x2','ah','ou']) {write(`odds-${market}-opening`,'—');write(`odds-${market}-latest`,'—');}
    write('odds-updated','Veri bekleniyor');$('match-update-button').disabled=true;
    write('detail-league', 'Veri bekleniyor'); write('detail-time', ''); write('detail-status', '');
  }
  async function selectMatch(id) {
    state.selected = id;
    try {
      const [match, allOdds] = await Promise.all([api(`/api/matches/${id}`),api(`/api/matches/${id}/odds`)]);
      if (state.selected !== id) return;
      write('detail-title', `${match.home_team} vs ${match.away_team}`);
      write('detail-league', `${match.league} • ${match.season} • ${match.round}. Hafta`);
      write('detail-time', date(match.kickoff_at));write('detail-home',match.home_team);write('detail-away',match.away_team);write('detail-home-score',number(match.ft_home));write('detail-away-score',number(match.ft_away));
      write('detail-ht', `İY: ${score(match.ht_home, match.ht_away)}`);
      write('detail-status', `${statuses[match.status] || match.status} • ${match.odds_complete ? 'Oranlar tamamlandı' : 'Eksik oranlar var'}`);
      const rows = allOdds.filter(o => o.bookmaker === state.bookmaker);
      for (const market of ['1x2','ah','ou']) {write(`odds-${market}-opening`,'—');write(`odds-${market}-latest`,'—');}
      rows.forEach(odds => {
        const keys = odds.market === '1x2' ? ['home','draw','away'] : (odds.market === 'ah' ? ['home','line','away'] : ['over','line','under']);
        const format = stage => keys.map(k => number(odds[stage][k])).join(' / ');
        write(`odds-${odds.market}-opening`,format('opening'));
        write(`odds-${odds.market}-latest`,`${format('latest')} / Kapanış: ${format('closing')}`);
      });
      write('odds-updated', rows.length ? date(rows[0].updated_at) : empty);
      $('match-update-button').disabled=Boolean(state.job && ['queued','running'].includes(state.job.status));
      document.querySelectorAll('[data-bookmaker]').forEach(button => button.classList.toggle('bg-surface-container-lowest', button.dataset.bookmaker === state.bookmaker));
    } catch (error) {clearDetail(); write('detail-status', error.message);}
  }
  async function health() {
    try {await api('/health'); write('api-health', 'Bağlı'); write('system-api', 'HTTP 200');}
    catch (_) {write('api-health', 'Bağlantı yok'); write('system-api', 'Bağlantı yok');}
    try {
      const system = await api('/api/status');
      write('system-db', [system.database_engine,system.database].filter(Boolean).join(' • ')); write('hero-status', system.database === 'connected' ? 'Veritabanı bağlı' : 'Veri bekleniyor');
      write('total-matches', system.total_matches == null ? empty : system.total_matches);
      write('last-scraped', date(system.last_scraped_at)); write('hero-last', date(system.last_scraped_at)); write('data-source', 'Goaloo');
      write('header-last', date(system.last_scraped_at));
      write('services-status', system.database === 'connected' ? 'Veritabanı bağlı' : 'Veri bekleniyor');
      write('system-summary', system.total_odds == null ? 'Veri bekleniyor' : `${system.total_odds} bookmaker piyasa kaydı`);
      const today = new Date().toISOString().slice(0,10);
      const [live, daily] = await Promise.all([api('/api/matches?status=live&limit=1'), api(`/api/matches?date=${today}&limit=1`)]);
      write('live-matches', live.total); write('today-matches', daily.total);
    } catch (_) {
      ['total-matches','live-matches','today-matches','last-scraped','system-db','hero-status','hero-last','system-summary'].forEach(id => write(id, 'Veri bekleniyor'));
    }
  }
  function displayJob(job) {
    state.job = job;
    write('job-status', job ? `İş #${job.id}: ${job.status}` : 'Henüz iş yok');
    write('job-counts', job ? `${job.processed_matches}/${job.total_matches} • Hata: ${job.failed_matches}` : '');
    write('job-error', job ? [job.current_league, job.current_season, job.current_round ? `${job.current_round}. Hafta` : '', job.last_error].filter(Boolean).join(' • ') : empty);
    const done = job ? job.processed_matches + job.failed_matches : 0;
    $('job-progress').style.width = `${job && job.total_matches ? Math.min(100,100*done/job.total_matches) : 0}%`;
    write('system-scraper', job ? job.status : 'Henüz iş yok');
    ['update-button','backfill-button','match-update-button'].forEach(id => $(id).disabled = Boolean(job && ['queued','running'].includes(job.status)) || (id==='match-update-button' && !state.selected));
  }
  let jobTimer;
  async function pollJobs() {
    clearTimeout(jobTimer);
    try {
      if (state.job && ['running','queued'].includes(state.job.status)) displayJob(await api(`/api/scraper/jobs/${state.job.id}`));
      else {const status = await api('/api/scraper/status'); displayJob(status.jobs[0] || null);}
    } catch (error) {write('system-scraper', 'Veri bekleniyor'); write('job-error', error.message);}
    jobTimer=setTimeout(pollJobs, state.job && ['running','queued'].includes(state.job.status) ? 4000 : 30000);
  }
  async function submit(kind) {
    const league = state.leagues.find(l => String(l.id) === $('league-select').value);
    if (!league) {write('job-error', 'Lig verisi bekleniyor'); return;}
    const token = $('scraper-token').value;
    if (!token) {write('job-error', 'Kazıyıcı erişim anahtarını girin'); return;}
    if (kind==='match' && !state.selected) return;
    const path=kind==='match' ? `match/${state.selected}` : kind;
    try {displayJob(await api(`/api/scraper/${path}`, {method:'POST',headers:{'Content-Type':'application/json',Authorization:`Bearer ${token}`},body:JSON.stringify({league_id:league.external_id,start_year:2024})}));clearTimeout(jobTimer);jobTimer=setTimeout(pollJobs,4000);}
    catch (error) {write('job-error', error.message);}
  }
  $('update-button').onclick = () => submit('update'); $('backfill-button').onclick = () => submit('backfill');
  $('match-update-button').onclick=()=>submit('match');
  $('refresh-button').onclick = async () => {await health(); await loadMatches();};
  $('league-select').onchange = async () => {state.offset = 0; await loadSeasons(); await loadMatches();};
  ['season-select','round-select'].forEach(id => $(id).onchange = () => {state.offset = 0; loadMatches();});
  document.querySelectorAll('[data-status-filter]').forEach(button => button.onclick = () => {state.status=button.dataset.statusFilter; state.offset=0; loadMatches();});
  document.querySelectorAll('[data-bookmaker]').forEach(button => button.onclick = () => {state.bookmaker=button.dataset.bookmaker; loadMatches();});
  let searchTimer;
  document.querySelectorAll('[data-team-search]').forEach(input => input.oninput = () => {
    document.querySelectorAll('[data-team-search]').forEach(other => {if (other !== input) other.value=input.value;});
    clearTimeout(searchTimer); searchTimer=setTimeout(() => {state.offset=0; loadMatches();},350);
  });
  $('previous-page').onclick=()=>{state.offset=Math.max(0,state.offset-50);loadMatches();};
  $('next-page').onclick=()=>{state.offset+=50;loadMatches();};
  async function refresh() {
    if (state.busy) return; state.busy=true;
    try {if (!state.leagues.length) await loadLeagues(); await loadMatches();}
    catch (error) {write('matches-count', error.message);}
    finally {state.busy=false;}
  }
  health(); refresh(); pollJobs(); setInterval(health,30000); setInterval(refresh,25000);
})();
