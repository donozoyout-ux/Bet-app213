/* Existing Stitch shell, backed exclusively by recorded API data. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const {stage, movement, asianMovement, number} = globalThis.BetAppOdds;
  const state = {leagues: [], seasons: [], items: [], league: '', season: '', round: '', selected: null, bookmaker: 'Crown', view: 'all', offset: 0, total: 0, job: null, generation: 0, detailGeneration: 0, busy: false};
  const empty = 'Henüz veri yok';
  const displayTimezone = 'Europe/Istanbul';
  const date = value => value ? new Date(value).toLocaleString('tr-TR', {timeZone: displayTimezone}) : empty;
  const score = (home, away) => home == null || away == null ? '—' : `${home} - ${away}`;
  const write = (id, text) => {if ($(id)) $(id).textContent = text;};
  const statuses = {finished:'Bitti',scheduled:'Planlandı',live:'Canlı',first_half:'İlk yarı',half_time:'Devre arası',second_half:'İkinci yarı',extra_time:'Uzatma',penalties:'Penaltılar',postponed:'Ertelendi',cancelled:'İptal',abandoned:'Yarıda kaldı'};
  const viewNames = {all:'Tüm maçlar',live:'Canlı maçlar',today:'Bugünkü maçlar',upcoming:'Yaklaşan maçlar',history:'Geçmiş maçlar'};
  function updateBookmakers() {
    document.querySelectorAll('[data-bookmaker]').forEach(button=>{
      const active=button.dataset.bookmaker === state.bookmaker;
      for(const cls of ['bg-surface-container-lowest','text-primary','font-bold','shadow-sm'])button.classList.toggle(cls,active);
      button.classList.toggle('text-on-surface-variant',!active);button.setAttribute('aria-pressed',String(active));
    });
    write('table-bookmaker',state.bookmaker);
  }
  function clearSelection() {state.selected=null;state.detailGeneration++;clearDetail();highlightSelection();}
  function highlightSelection() {
    document.querySelectorAll('[data-match-id]').forEach(row=>{const selected=Number(row.dataset.matchId) === state.selected;row.classList.toggle('bg-surface-container-low/60',selected);row.setAttribute('aria-selected',String(selected));});
  }
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
    rounds.forEach(round => option(select,round,`${round}. Hafta`));
    if (!rounds.some(round => String(round) === state.round)) state.round = '';
    select.value = state.round;
  }
  async function loadSeasons() {
    if (!state.league) return;
    const league = state.league;
    let rows;
    try {rows = await api(`/api/leagues/${league}/seasons`);} catch (_) {write('filter-status','Sezon verileri bekleniyor');return;}
    if (state.league !== league) return;
    state.seasons = rows;
    const select = $('season-select'); if (!select) return; select.replaceChildren(); option(select,'','Tüm sezonlar');
    rows.forEach(row => option(select,row.season_name,row.season_name));
    if (!rows.some(row => row.season_name === state.season)) state.season = '';
    select.value = state.season; fillRounds();
    write('filter-status','');
  }
  async function loadLeagues() {
    const [leagueResult, bookResult] = await Promise.allSettled([api('/api/leagues'),api('/api/bookmakers')]);
    if(bookResult.status === 'fulfilled') document.querySelectorAll('[data-bookmaker]').forEach(button => button.disabled = !bookResult.value.some(book => book.name === button.dataset.bookmaker));
    if(leagueResult.status !== 'fulfilled') {write('filter-status','Lig verileri bekleniyor');return;}
    const leagues=leagueResult.value;
    state.leagues = leagues;
    const select = $('league-select'); select?.replaceChildren();
    const nav = $('league-nav'); nav?.replaceChildren();
    for (const league of leagues) {
      option(select,league.id,league.name);
      const button = document.createElement('button'); button.type='button'; button.className='px-space-sm py-2 rounded-lg text-left hover:bg-surface-container';button.textContent=league.name;
      button.onclick=()=>changeLeague(String(league.id));nav?.append(button);
    }
    if (!leagues.some(league => String(league.id) === state.league)) state.league = leagues.length ? String(leagues[0].id) : '';
    if (select) select.value = state.league;
    if(!leagues.length) {option(select,'','Henüz lig yok');if(nav)nav.textContent='Henüz lig yok';}
    write('hero-league',leagues.find(league=>String(league.id) === state.league)?.name || empty);
    await loadSeasons();
  }
  async function changeLeague(league) {
    state.league=league;state.season='';state.round='';state.offset=0;if ($('league-select')) $('league-select').value=league;
    clearSelection();write('hero-league',state.leagues.find(item=>String(item.id) === league)?.name || empty);
    try {await loadSeasons();await loadMatches();} catch (_) {write('matches-count','Veri bekleniyor');}
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
    row.dataset.matchId=String(match.id);row.tabIndex=0;row.setAttribute('aria-label',`${match.home_team} / ${match.away_team}, maç ayrıntıları`);
    row.classList.toggle('bg-surface-container-low/60',state.selected === match.id);
    cell(row, `${statuses[match.status] || match.status} • ${date(match.kickoff_at)}`);
    cell(row,match.league);cell(row,`${match.home_team} / ${match.away_team}`);
    const ht=match.ht_home == null || match.ht_away == null ? '' : ` • İY: ${score(match.ht_home,match.ht_away)}`;
    cell(row,score(match.ft_home,match.ft_away)+ht);
    ['home','draw','away'].forEach(key => cell(row,movement(match,findMarket(match,'1x2'),key)));
    cell(row,asianMovement(match,findMarket(match,'ah'),['home','line','away']));cell(row,asianMovement(match,findMarket(match,'ou'),['over','line','under']));
    row.onclick=()=>selectMatch(match.id);row.onkeydown=event=>{if(event.key === 'Enter' || event.key === ' '){event.preventDefault();selectMatch(match.id);}};return row;
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
      state.total=page.total;state.items=page.items;const body=$('matches-body');if (!body) return;body.replaceChildren();page.items.forEach(match=>body.append(renderMatch(match)));
      if (!page.items.length) emptyRows(empty);
      const range=page.items.length ? `${state.offset+1}–${state.offset+page.items.length} / ${page.total}` : `0 / ${page.total}`;
      write('matches-count',`${range} maç • ${state.bookmaker} • Europe/Istanbul`);write('table-count',`${page.total} maç`);write('hero-matches',`${page.total} maç`);
      updateBookmakers();highlightSelection();
      if ($('previous-page')) $('previous-page').disabled=state.offset === 0;if ($('next-page')) $('next-page').disabled=state.offset+50 >= page.total;
      if(state.selected && !page.items.some(item=>item.id === state.selected))clearSelection();
      if (state.selected) await selectMatch(state.selected);
    } catch (_) {
      if(generation !== state.generation)return;emptyRows('Veri bekleniyor');write('matches-count','Bağlantı bekleniyor');write('table-count','Veri bekleniyor');clearDetail();
      if ($('previous-page')) $('previous-page').disabled=true;if ($('next-page')) $('next-page').disabled=true;
    }
  }
  function clearDetail() {
    write('detail-title',empty);write('detail-home',empty);write('detail-away',empty);write('detail-home-score','—');write('detail-away-score','—');write('detail-ht','İY: —');write('detail-league','Veri bekleniyor');write('detail-time','');write('detail-status','');write('odds-updated','Veri bekleniyor');
    write('detail-error','');write('odds-stage-label','Güncel maç öncesi');
    for(const key of ['1x2','ah','ou']){write(`odds-${key}-opening`,'—');write(`odds-${key}-latest`,'—');}
  }
  async function selectMatch(id) {
    if(state.selected !== id)clearDetail();state.selected=id;highlightSelection();
    const generation=++state.detailGeneration;const book=state.bookmaker;
    try {
      const [detailResult,oddsResult]=await Promise.allSettled([api(`/api/matches/${id}`),api(`/api/matches/${id}/odds`)]);
      if(state.selected !== id || generation !== state.detailGeneration || book !== state.bookmaker)return;
      const match=detailResult.status === 'fulfilled' ? detailResult.value : state.items.find(item=>item.id === id);
      if(!match)throw new Error('Match unavailable');
      const allOdds=oddsResult.status === 'fulfilled' ? oddsResult.value : (match.odds || []);
      write('detail-error',detailResult.status !== 'fulfilled' ? 'Ayrıntılar bekleniyor; son liste verisi gösteriliyor' : oddsResult.status !== 'fulfilled' ? 'Oran bağlantısı bekleniyor; mevcut kayıt gösteriliyor' : '');
      write('detail-title',`${match.home_team} vs ${match.away_team}`);write('detail-home',match.home_team);write('detail-away',match.away_team);
      write('detail-league',`${match.league} • ${match.season} • ${match.round}. Hafta`);write('detail-time',date(match.kickoff_at)+' • Europe/Istanbul');write('detail-status',statuses[match.status] || match.status);
      write('detail-home-score',number(match.ft_home));write('detail-away-score',number(match.ft_away));write('detail-ht',`İY: ${score(match.ht_home,match.ht_away)}`);
      const markets=allOdds.filter(odds=>odds.bookmaker === book);
      write('odds-stage-label',match.status === 'finished' || markets.some(m=>stage(match,m) === 'closing') ? 'Kapanış' : 'Güncel maç öncesi');
      for(const key of ['1x2','ah','ou']){write(`odds-${key}-opening`,'—');write(`odds-${key}-latest`,'—');}
      for(const market of markets) {
        const keys=market.market === '1x2' ? ['home','draw','away'] : market.market === 'ah' ? ['home','line','away'] : ['over','line','under'];
        const format=kind=>keys.map(key=>number(market[kind]?.[key])).join(' / ');
        write(`odds-${market.market}-opening`,format('opening'));write(`odds-${market.market}-latest`,format(stage(match,market)));
      }
      write('odds-updated',markets.length ? date(markets[0].updated_at) : empty);
      updateBookmakers();
    } catch (_) {if(generation !== state.detailGeneration)return;clearDetail();write('detail-status','Bağlantı bekleniyor');}
  }
  async function health() {
    try {await api('/health');write('api-health','Bağlı');write('system-api','HTTP 200');write('production-api','API bağlı');}catch(_){write('api-health','Bağlantı bekleniyor');write('system-api','Bağlantı bekleniyor');write('production-api','API bağlantısı bekleniyor');}
    try {
      const system=await api('/api/status');
      write('system-db',[system.database_engine,system.database].filter(Boolean).join(' • '));write('hero-status',system.database === 'connected' ? 'Veritabanı bağlı' : 'Bağlantı bekleniyor');write('services-status',system.database === 'connected' ? 'Veritabanı bağlı' : 'Veri bekleniyor');
      write('total-matches',system.total_matches == null ? empty : system.total_matches);write('data-source','Goaloo');
      write('production-db',system.database === 'connected' ? 'DB bağlı' : 'DB bağlantısı bekleniyor');write('production-count',system.total_matches == null ? 'Maç verisi bekleniyor' : `${system.total_matches} maç`);
      for(const id of ['last-scraped','hero-last','header-last'])write(id,date(system.last_scraped_at));
      write('system-summary',system.total_odds == null ? 'Veri bekleniyor' : `${system.total_odds} bookmaker piyasa kaydı`);
    } catch (_) {['total-matches','system-summary'].forEach(id=>write(id,'Veri bekleniyor'));write('production-db','DB bağlantısı bekleniyor');write('production-count','Maç verisi bekleniyor');write('system-db','Bağlantı bekleniyor');write('hero-status','Bağlantı bekleniyor');}
    const counts=await Promise.allSettled([api('/api/matches?view=live&limit=1'),api('/api/matches?view=today&display_timezone=Europe%2FIstanbul&limit=1')]);
    counts.forEach((result,index)=>write(index === 0 ? 'live-matches' : 'today-matches',result.status === 'fulfilled' ? result.value.total : 'Veri bekleniyor'));
    await loadSeasons();
  }
  let jobTimer;
  async function pollJobs() {
    clearTimeout(jobTimer);
    try {
      const status=await api('/api/scraper/status');
      state.job=status.jobs.find(job=>['queued','running'].includes(job.status)) || status.jobs[0] || null;
      const job=state.job;let message='Veri bekleniyor';
      const latest=status.jobs[0];const jobNames={queued:'Sırada',running:'Çalışıyor',completed:'Tamamlandı',partial:'Kısmen tamamlandı',failed:'Başarısız'};
      write('production-job',latest ? `Son iş: ${jobNames[latest.status] || latest.status}` : 'Henüz iş yok');
      for(const id of ['production-job','system-scraper']) $(id)?.setAttribute('data-state',latest?.status || 'idle');
      if(job) {
        const count=`${job.processed_matches+job.failed_matches} / ${job.total_matches} maç işlendi`;
        message=['queued','running'].includes(job.status) ? `Premier League verileri hazırlanıyor • ${count}` : job.status === 'completed' ? 'Premier League güncel' : `Premier League verileri eksik • ${count} • ${job.failed_matches} hata`;
      }
      write('collection-progress',message);write('system-scraper',message);
    } catch (_) {state.job=null;write('production-job','İş durumu bekleniyor');write('collection-progress','Bağlantı bekleniyor');write('system-scraper','Bağlantı bekleniyor');}
    jobTimer=setTimeout(pollJobs,state.job && ['queued','running'].includes(state.job.status) ? 4000 : 30000);
  }
  function updateViewButtons() {
    document.querySelectorAll('[data-view]').forEach(button=>{const active=button.dataset.view === state.view;button.classList.toggle('bg-primary',active);button.classList.toggle('text-on-primary',active);button.classList.toggle('bg-surface-container',!active);button.classList.toggle('text-on-surface-variant',!active);button.setAttribute('aria-pressed',String(active));});
    if ($('date-select')) $('date-select').disabled=state.view === 'today';
    write('hero-view',viewNames[state.view]);
  }
  document.querySelectorAll('[data-view]').forEach(button=>button.onclick=event=>{
    event.preventDefault();state.view=button.dataset.view;state.offset=0;clearSelection();
    if(['live','today','upcoming'].includes(state.view)) {
      state.season='';state.round='';if ($('season-select')) $('season-select').value='';fillRounds();if ($('date-select')) $('date-select').value='';
    }
    updateViewButtons();loadMatches();
  });
  document.querySelectorAll('[data-bookmaker]').forEach(button=>button.onclick=()=>{state.bookmaker=button.dataset.bookmaker;updateBookmakers();loadMatches();});
  if ($('league-select')) $('league-select').onchange=()=>changeLeague($('league-select').value);
  if ($('season-select')) $('season-select').onchange=()=>{state.season=$('season-select')?.value || '';state.offset=0;clearSelection();fillRounds();loadMatches();};
  if ($('round-select')) $('round-select').onchange=()=>{state.round=$('round-select')?.value || '';state.offset=0;clearSelection();loadMatches();};
  if ($('date-select')) $('date-select').onchange=()=>{state.offset=0;clearSelection();loadMatches();};
  if ($('refresh-button')) $('refresh-button').onclick=async()=>{await health();await refresh();};
  let searchTimer;
  document.querySelectorAll('[data-team-search]').forEach(input=>input.oninput=()=>{document.querySelectorAll('[data-team-search]').forEach(other=>{if(other !== input)other.value=input.value;});clearTimeout(searchTimer);searchTimer=setTimeout(()=>{state.offset=0;clearSelection();loadMatches();},350);});
  document.addEventListener('keydown',event=>{if((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k'){event.preventDefault();const input=[...document.querySelectorAll('[data-team-search]')].find(node=>node.getClientRects().length);input?.focus();}});
  if ($('previous-page')) $('previous-page').onclick=()=>{state.offset=Math.max(0,state.offset-50);clearSelection();loadMatches();};if ($('next-page')) $('next-page').onclick=()=>{state.offset+=50;clearSelection();loadMatches();};
  async function refresh() {
    if(state.busy)return;state.busy=true;
    try {if(!state.leagues.length)await loadLeagues();await loadMatches();}catch(_){emptyRows('Veri bekleniyor');write('matches-count','Bağlantı bekleniyor');if ($('previous-page')) $('previous-page').disabled=true;if ($('next-page')) $('next-page').disabled=true;}finally{state.busy=false;}
  }
  updateViewButtons();updateBookmakers();health();refresh();pollJobs();setInterval(health,30000);setInterval(refresh,25000);
})();
