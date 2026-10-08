/* Existing Stitch shell, backed exclusively by recorded API data. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const {stage, movement, asianMovement, liveStates, number} = globalThis.BetAppOdds;
  const state = {leagues: [], seasons: [], league: '', season: '', round: '', selected: null, bookmaker: 'Crown', view: 'upcoming', offset: 0, total: 0, job: null, generation: 0, detailGeneration:0, busy: false};
  const empty = 'Henüz veri yok';
  const displayTimezone = 'Europe/Istanbul';
  const date = value => value ? new Date(value).toLocaleString('tr-TR', {timeZone: displayTimezone}) : empty;
  const score = (home, away) => home == null || away == null ? '—' : `${home} - ${away}`;
  const write = (id, text) => {if ($(id)) $(id).textContent = text;};
  const statuses = {finished:'Bitti',scheduled:'Planlandı',live:'Canlı',first_half:'İlk yarı',half_time:'Devre arası',second_half:'İkinci yarı',extra_time:'Uzatma',penalties:'Penaltılar',postponed:'Ertelendi',cancelled:'İptal',abandoned:'Yarıda kaldı'};
  let predictionRefreshAt=0,predictionFilter='';
  function refreshPredictions(force=false){const dateFilter=$('date-select')?.value || '';const key=state.league+'|'+dateFilter;if(force || key!==predictionFilter || Date.now()-predictionRefreshAt>=600000){predictionRefreshAt=Date.now();predictionFilter=key;globalThis.BetAppPredictions?.refresh(state.league,dateFilter);}}
  async function api(path) {
    const response = await fetch(path, {signal: AbortSignal.timeout(15000)});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }
  function option(select, value, text) {
    const node = document.createElement('option'); node.value = value; node.textContent = text; select?.append(node);
  }
  function fillRounds() {
    const select = $('round-select'); if (!select) return; select.replaceChildren(); option(select,'','Tüm haftalar');
    const seasons = state.season ? state.seasons.filter(s => s.season_name === state.season) : state.seasons;
    const rounds = [...new Set(seasons.flatMap(s => s.rounds))].sort((a,b) => a-b);
    rounds.forEach(round => option(select,round,seasons.find(s=>s.round_labels?.[round])?.round_labels[round] || `${round}. Hafta`));
    if (!rounds.some(round => String(round) === state.round)) state.round = '';
    select.value = state.round;
  }
  async function loadSeasons() {
    if (!state.league) {
      state.seasons=[];state.season='';state.round='';
      const select=$('season-select');if(select){select.replaceChildren();option(select,'','Tüm sezonlar');select.disabled=true;}
      fillRounds();if($('round-select'))$('round-select').disabled=true;return;
    }
    if($('season-select'))$('season-select').disabled=false;if($('round-select'))$('round-select').disabled=false;
    const league = state.league;
    const rows = await api(`/api/leagues/${league}/seasons`);
    if (state.league !== league) return;
    state.seasons = rows;
    const select = $('season-select'); if (!select) return; select.replaceChildren(); option(select,'','Tüm sezonlar');
    rows.forEach(row => option(select,row.season_name,row.season_name));
    if (!rows.some(row => row.season_name === state.season)) state.season = '';
    select.value = state.season; fillRounds();
  }
  async function loadLeagues() {
    const [leagueResult, bookResult] = await Promise.allSettled([api('/api/leagues'),api('/api/bookmakers')]);
    if(bookResult.status === 'fulfilled')document.querySelectorAll('[data-bookmaker]').forEach(button => button.disabled = !bookResult.value.some(book => book.name === button.dataset.bookmaker));
    if(leagueResult.status !== 'fulfilled')return;
    const leagues=leagueResult.value;
    state.leagues = leagues;
    const select = $('league-select'); select?.replaceChildren();
    option(select,'','Tüm Ligler');
    for (const [type,label] of [['club','KULÜP LİGLERİ'],['national','MİLLİ TAKIMLAR']]) {
      const rows=leagues.filter(league=>(league.competition_type || 'club') === type);if(!rows.length)continue;
      const group=document.createElement('optgroup');group.label=label;
      for (const league of rows) {
        option(group,league.id,league.name);
      }
      select?.append(group);
    }
    if (!leagues.some(league => String(league.id) === state.league)) state.league = '';
    if (select) select.value = state.league;
    await loadSeasons();
  }
  async function changeLeague(league) {
    state.league=league;state.season='';state.round='';state.offset=0;if ($('league-select')) $('league-select').value=league;
    state.selected=null;clearDetail();
    refreshPredictions(true);
    await loadMatches();
    try {await loadSeasons();} catch (_) {write('matches-count','Sezon verileri bekleniyor');}
  }
  function cell(row, text) {
    const index=row._cellIndex || 0;row._cellIndex=index+1;
    let td=row.children[index];
    if (!td) {td=document.createElement('td');td.className='py-3 px-2 align-middle';row.append(td);}
    (td.firstElementChild || td).textContent=text;return td;
  }
  function findMarket(match, key) {
    return (match.odds || []).find(odds => odds.bookmaker === state.bookmaker && odds.market === key);
  }
  function renderMatch(match) {
    const row=$('match-row-template')?.content?.firstElementChild?.cloneNode(true) || document.createElement('tr');
    row.classList.toggle('bg-surface-container-low/60',state.selected === match.id);
    const live=liveStates.includes(match.status);
    cell(row, `${statuses[match.status] || match.status}${live ? '' : ' • '+date(match.kickoff_at)}`);
    cell(row,match.league);cell(row,`${match.home_team} / ${match.away_team}`);
    const ht=match.ht_home == null || match.ht_away == null ? '' : ` • İY: ${score(match.ht_home,match.ht_away)}`;
    cell(row,score(match.ft_home,match.ft_away)+ht);
    ['home','draw','away'].forEach(key => cell(row,movement(match,findMarket(match,'1x2'),key)));
    cell(row,asianMovement(match,findMarket(match,'ah'),['home','line','away']));cell(row,asianMovement(match,findMarket(match,'ou'),['over','line','under']));
    row.onclick=()=>selectMatch(match.id);return row;
  }
  function emptyRows(text) {
    const body=$('matches-body');if (!body) return;body.replaceChildren();const row=document.createElement('tr');const td=cell(row,text);td.colSpan=9;body.append(row);
  }
  async function loadMatches() {
    const generation=++state.generation;
    const query=new URLSearchParams({view:state.view,display_timezone:displayTimezone,limit:'50',offset:String(state.offset),include_odds:'true'});
    [['league',state.league],['season',state.season],['round',state.round],['date',($('date-select')?.value || '')],['team',(document.querySelector('[data-team-search]')?.value || '')]].forEach(([key,value])=>{if(value)query.set(key,value);});
    try {
      const page=await api(`/api/matches?${query}`);if(generation !== state.generation)return;
      state.total=page.total;const body=$('matches-body');if (!body) return;body.replaceChildren();page.items.forEach(match=>body.append(renderMatch(match)));
      if (!page.items.length) emptyRows(empty);
      write('matches-count',`${page.total} maç • ${state.bookmaker} • Europe/Istanbul`);write('table-count',`${page.total} maç`);
      write('upcoming-window',state.view === 'upcoming' ? (page.upcoming_expanded ? 'Önümüzdeki 7 günde maç yok; en yakın maç günü gösteriliyor' : 'Önümüzdeki 7 gün') : '');
      if ($('previous-page')) $('previous-page').disabled=state.offset === 0;if ($('next-page')) $('next-page').disabled=state.offset+50 >= page.total;
      if (state.selected) await selectMatch(state.selected);
    } catch (_) {
      if(generation !== state.generation)return;emptyRows('Veri bekleniyor');write('matches-count','Bağlantı bekleniyor');write('table-count','Veri bekleniyor');clearDetail();
      if ($('previous-page')) $('previous-page').disabled=true;if ($('next-page')) $('next-page').disabled=true;
    }
  }
  function clearDetail() {
    state.detailGeneration++;globalThis.BetAppPredictions?.clear();
    write('detail-title',empty);write('detail-home',empty);write('detail-away',empty);write('detail-home-score','—');write('detail-away-score','—');write('detail-ht','İY: —');write('detail-league','Veri bekleniyor');write('detail-time','');write('detail-status','');write('odds-updated','Veri bekleniyor');
    for(const key of ['1x2','ah','ou']){write(`odds-${key}-opening`,'—');write(`odds-${key}-latest`,'—');}
  }
  async function selectMatch(id,scrollStatistics=false) {
    if(state.selected!==id)clearDetail();state.selected=id;
    const generation=++state.detailGeneration;
    globalThis.BetAppPredictions?.select(id,scrollStatistics);
    try {
      const [matchResult,oddsResult]=await Promise.allSettled([api(`/api/matches/${id}`),api(`/api/matches/${id}/odds`)]);if(state.selected !== id || generation!==state.detailGeneration)return;
      if(matchResult.status!=='fulfilled'){write('detail-status','Bağlantı bekleniyor');return;}
      const match=matchResult.value,allOdds=oddsResult.status==='fulfilled'?oddsResult.value:(match.odds || []);
      write('detail-title',`${match.home_team} vs ${match.away_team}`);write('detail-home',match.home_team);write('detail-away',match.away_team);
      write('detail-league',`${match.league} • ${match.season} • ${match.round_label || `${match.round}. Hafta`}`);write('detail-time',date(match.kickoff_at)+' • Europe/Istanbul');write('detail-status',statuses[match.status] || match.status);
      write('detail-home-score',number(match.ft_home));write('detail-away-score',number(match.ft_away));write('detail-ht',`İY: ${score(match.ht_home,match.ht_away)}`);
      const markets=allOdds.filter(odds=>odds.bookmaker === state.bookmaker);
      write('odds-stage-label',match.status === 'finished' || markets.some(m=>stage(match,m) === 'closing') ? 'Kapanış' : 'Güncel');
      for(const key of ['1x2','ah','ou']){write(`odds-${key}-opening`,'—');write(`odds-${key}-latest`,'—');}
      for(const market of markets) {
        const keys=market.market === '1x2' ? ['home','draw','away'] : market.market === 'ah' ? ['home','line','away'] : ['over','line','under'];
        const format=kind=>keys.map(key=>number(market[kind][key])).join(' / ');
        write(`odds-${market.market}-opening`,format('opening'));write(`odds-${market.market}-latest`,format(stage(match,market)));
      }
      write('odds-updated',markets.length ? date(markets[0].updated_at) : empty);
      document.querySelectorAll('[data-bookmaker]').forEach(button=>{
        const active=button.dataset.bookmaker === state.bookmaker;
        for(const cls of ['bg-surface-container-lowest','text-primary','font-bold','shadow-sm'])button.classList.toggle(cls,active);
        button.classList.toggle('text-on-surface-variant',!active);button.setAttribute('aria-pressed',String(active));
      });
    } catch (_) {if(generation!==state.detailGeneration)return;write('detail-status','Bağlantı bekleniyor');}
  }
  async function health() {
    try {await api('/health');write('api-health','Bağlı');write('system-api','HTTP 200');}catch(_){write('api-health','Bağlantı bekleniyor');write('system-api','Bağlantı bekleniyor');}
    try {
      const system=await api('/api/status');
      write('system-db',[system.database_engine,system.database].filter(Boolean).join(' • '));write('hero-status',system.database === 'connected' ? 'Veritabanı bağlı' : 'Bağlantı bekleniyor');write('services-status',system.database === 'connected' ? 'Veritabanı bağlı' : 'Veri bekleniyor');
      write('total-matches',system.total_matches == null ? empty : system.total_matches);write('data-source','Goaloo');
      write('hero-matches',system.total_matches == null ? empty : `${system.total_matches} maç`);
      for(const id of ['last-scraped','hero-last','header-last'])write(id,date(system.last_scraped_at));
      write('system-summary',system.total_odds == null ? 'Veri bekleniyor' : `${system.total_odds} bookmaker piyasa kaydı`);
      const [live,today]=await Promise.all([api('/api/matches?view=live&limit=1'),api('/api/matches?view=today&display_timezone=Europe%2FIstanbul&limit=1')]);write('live-matches',live.total);write('today-matches',today.total);
      await loadSeasons();
    } catch (_) {['total-matches','live-matches','today-matches','system-summary'].forEach(id=>write(id,'Veri bekleniyor'));}
    refreshPredictions();
  }
  let jobTimer;
  async function pollJobs() {
    clearTimeout(jobTimer);
    try {
      const status=await api('/api/scraper/status');
      state.job=status.jobs.find(job=>['queued','running'].includes(job.status)) || status.jobs[0] || null;
      const job=state.job;let message='Veri bekleniyor';
      if(job) {
        const count=`${job.processed_matches+job.failed_matches} / ${job.total_matches} maç işlendi`;
        const competition=job.current_league || 'Yarışma';
        message=['queued','running'].includes(job.status) ? `${competition} verileri hazırlanıyor • ${count}` : job.status === 'completed' ? `${competition} güncel` : `${competition} verileri eksik • ${count} • ${job.failed_matches} hata`;
      }
      write('collection-progress',message);write('system-scraper',message);
    } catch (_) {write('collection-progress','Bağlantı bekleniyor');write('system-scraper','Bağlantı bekleniyor');}
    jobTimer=setTimeout(pollJobs,state.job && ['queued','running'].includes(state.job.status) ? 4000 : 30000);
  }
  function updateViewButtons() {
    document.querySelectorAll('[data-view]').forEach(button=>{const active=button.dataset.view === state.view;button.classList.toggle('bg-primary',active);button.classList.toggle('text-on-primary',active);button.classList.toggle('bg-surface-container',!active);button.classList.toggle('text-on-surface-variant',!active);button.setAttribute('aria-pressed',String(active));});
    if ($('date-select')) $('date-select').disabled=state.view === 'today';
  }
  document.querySelectorAll('[data-view]').forEach(button=>button.onclick=()=>{
    state.view=button.dataset.view;state.offset=0;state.selected=null;clearDetail();
    if(['live','today','upcoming'].includes(state.view)) {
      state.season='';state.round='';if ($('season-select')) $('season-select').value='';fillRounds();if ($('date-select')) $('date-select').value='';
    }
    updateViewButtons();refreshPredictions();loadMatches();
  });
  document.querySelectorAll('[data-bookmaker]').forEach(button=>button.onclick=()=>{state.bookmaker=button.dataset.bookmaker;loadMatches();});
  if ($('league-select')) $('league-select').onchange=()=>changeLeague($('league-select').value);
  if ($('season-select')) $('season-select').onchange=()=>{state.season=$('season-select')?.value || '';state.offset=0;fillRounds();loadMatches();};
  if ($('round-select')) $('round-select').onchange=()=>{state.round=$('round-select')?.value || '';state.offset=0;loadMatches();};
  if ($('date-select')) $('date-select').onchange=()=>{state.offset=0;refreshPredictions(true);loadMatches();};
  if ($('refresh-button')) $('refresh-button').onclick=async()=>{refreshPredictions(true);await health();await refresh();};
  let searchTimer;
  document.querySelectorAll('[data-team-search]').forEach(input=>input.oninput=()=>{document.querySelectorAll('[data-team-search]').forEach(other=>{if(other !== input)other.value=input.value;});clearTimeout(searchTimer);searchTimer=setTimeout(()=>{state.offset=0;loadMatches();},350);});
  if ($('previous-page')) $('previous-page').onclick=()=>{state.offset=Math.max(0,state.offset-50);loadMatches();};if ($('next-page')) $('next-page').onclick=()=>{state.offset+=50;loadMatches();};
  async function refresh() {
    if(state.busy)return;state.busy=true;
    try {if(!state.leagues.length)await loadLeagues();await loadMatches();}catch(_){emptyRows('Veri bekleniyor');write('matches-count','Bağlantı bekleniyor');if ($('previous-page')) $('previous-page').disabled=true;if ($('next-page')) $('next-page').disabled=true;}finally{state.busy=false;}
  }
  globalThis.BetAppDashboard=Object.freeze({selectMatch});
  updateViewButtons();refreshPredictions();health();refresh();pollJobs();setInterval(health,30000);setInterval(refresh,25000);
})();
