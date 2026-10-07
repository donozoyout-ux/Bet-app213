/* Execute the real dashboard scripts with the current HTML IDs and API contracts. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const markup = fs.readFileSync('src/api/dashboard.html', 'utf8');
const recorded=JSON.parse(fs.readFileSync('tests/fixtures/dashboard_production.json','utf8')).items[0];
const upcoming=JSON.parse(fs.readFileSync('tests/fixtures/dashboard_upcoming.json','utf8')).items[0];
const modes=['empty','unavailable','missing-controls','populated','upcoming','books-fail','leagues-fail','seasons-fail','odds-fail','detail-fail','count-fail','status-fail','jobs-fail'];
class Element {
  constructor(dataset={}) {this.children=[];this.value='';this.textContent='';this.dataset=dataset;this.attrs={};this.classList={toggle(){}};}
  append(node){this.children.push(node);}
  replaceChildren(){this.children=[];}
  get firstElementChild(){return this.children[0];}
  setAttribute(key,value){this.attrs[key]=value;}
  getClientRects(){return [1];}
  focus(){}
}
async function scenario(mode) {
  const elements = new Map([...markup.matchAll(/\bid="([^"]+)"/g)].map(m=>[m[1],new Element()]));
  if(mode==='missing-controls') for(const id of ['league-select','season-select','round-select','date-select','refresh-button','previous-page','next-page','league-nav'])elements.delete(id);
  const calls=[];const failures=[];const timers=[];
  const populated=!['empty','unavailable','missing-controls'].includes(mode);
  const match=mode==='upcoming' ? upcoming : recorded;
  const views=['all','live','today','upcoming','history'].map(view=>new Element({view}));
  const books=['Crown','Bet365','Sbobet'].map(bookmaker=>new Element({bookmaker}));
  const searches=[new Element(),new Element()];
  const failPaths={'books-fail':'/api/bookmakers','leagues-fail':'/api/leagues','seasons-fail':'/seasons','odds-fail':`/api/matches/${match.id}/odds`,'detail-fail':`/api/matches/${match.id}`,'count-fail':'view=live','status-fail':'/api/status','jobs-fail':'/api/scraper/status'};
  const sandbox={console,URLSearchParams,AbortSignal,setInterval(){},setTimeout(fn){timers.push(fn);return timers.length;},clearTimeout(){},
    document:{getElementById:id=>elements.get(id) || null,addEventListener(){},querySelector:selector=>selector==='[data-team-search]' ? searches[0] : null,
      querySelectorAll:selector=>selector==='[data-view]' ? views : selector==='[data-bookmaker]' ? books : selector==='[data-team-search]' ? searches : selector==='[data-match-id]' ? (elements.get('matches-body')?.children || []).filter(row=>row.dataset.matchId) : [],createElement:()=>new Element()},
    fetch:async path=>{
      calls.push(path);
      const unavailable=mode==='unavailable' && path!=='/health' || failPaths[mode] && (mode==='detail-fail' ? path===failPaths[mode] : path.includes(failPaths[mode]));
      const query=new URLSearchParams(path.split('?')[1] || '');
      let payload;
      if(path==='/health')payload={status:'ok'};
      else if(path==='/api/status')payload={database:'connected',database_engine:'postgresql',total_matches:populated?1140:0,total_odds:populated?10260:0};
      else if(path==='/api/leagues')payload=populated?[{id:1,name:'English Premier League'}]:[];
      else if(path==='/api/bookmakers')payload=books.map((book,id)=>({id:id+1,name:book.dataset.bookmaker}));
      else if(path.endsWith('/seasons'))payload=[{season_name:'2024-2025',rounds:[1,2]}];
      else if(path.startsWith('/api/matches?')) {const hasMatch=populated && !['live','today'].includes(query.get('view'));payload={items:hasMatch?[match]:[],total:hasMatch?1140:0};}
      else if(path===`/api/matches/${match.id}/odds`)payload=match.odds;
      else if(path===`/api/matches/${match.id}`)payload=match;
      else if(path==='/api/scraper/status')payload={status:'idle',jobs:populated?[{id:1,status:'partial',processed_matches:830,failed_matches:310,total_matches:1140}]:[]};
      else payload=[];
      return {ok:!unavailable,status:unavailable?503:200,json:async()=>payload};
    }};
  const listener=error=>failures.push(error);
  process.on('unhandledRejection',listener);
  try {
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync('src/api/dashboard-odds.js','utf8'),sandbox);
    vm.runInContext(fs.readFileSync('src/api/dashboard.js','utf8'),sandbox);
    async function settle(){for(let i=0;i<5;i++)await new Promise(resolve=>setImmediate(resolve));}
    await settle();
    assert.equal(failures.length,0,String(failures[0]));
    assert(calls.includes('/health'));
    assert(calls.includes('/api/status'));
    assert(calls.includes('/api/scraper/status'));
    if(populated) {
      const text=id=>elements.get(id).textContent;
      const rows=()=>elements.get('matches-body').children;
      const rowText=()=>rows()[0].children.map(td=>td.textContent).join(' ');
      assert(rowText().includes(match.home_team) && rowText().includes(match.away_team));
      if(mode!=='status-fail')assert.equal(text('total-matches'),1140);
      if(mode==='count-fail')assert.equal(text('today-matches'),0);
      if(mode==='jobs-fail')assert.equal(text('production-job'),'İş durumu bekleniyor');
      else assert(text('production-job').includes('Kısmen'));
      await rows()[0].onclick();await settle();
      assert.equal(text('detail-title'),`${match.home_team} vs ${match.away_team}`);
      assert.equal(rows()[0].attrs['aria-selected'],'true');
      if(['odds-fail','detail-fail'].includes(mode))assert(text('detail-error').includes('bekleniyor'));
      for(const book of books) {
        await book.onclick();await settle();
        const prices=match.odds.find(m=>m.bookmaker===book.dataset.bookmaker && m.market==='1x2');
        const stage=match.status==='finished'?'closing':'latest';
        assert(rowText().includes(`${prices.opening.home} → ${prices[stage].home}`));
        assert.equal(text('odds-1x2-opening'),['home','draw','away'].map(k=>String(prices.opening[k])).join(' / '));
        assert.equal(text('odds-1x2-latest'),['home','draw','away'].map(k=>String(prices[stage][k])).join(' / '));
        assert.equal(text('odds-stage-label'),stage==='closing'?'Kapanış':'Güncel maç öncesi');
        assert.equal(book.attrs['aria-pressed'],'true');
        for(const key of ['ah','ou']) {
          const market=match.odds.find(m=>m.bookmaker===book.dataset.bookmaker && m.market===key);
          const fields=key==='ah'?['home','line','away']:['over','line','under'];
          assert.equal(text(`odds-${key}-latest`),fields.map(k=>String(market[stage][k])).join(' / '));
        }
      }
      for(const view of views) {await view.onclick({preventDefault(){}});await settle();assert.equal(view.attrs['aria-pressed'],'true');assert(calls.some(path=>path.includes(`view=${view.dataset.view}`)));}
      assert.equal(text('detail-title'),'Henüz veri yok');
      elements.get('season-select').value='2024-2025';elements.get('season-select').onchange();await settle();assert(calls.some(path=>path.includes('season=2024-2025')));
      elements.get('round-select').value='1';elements.get('round-select').onchange();await settle();assert(calls.some(path=>path.includes('round=1')));
      elements.get('date-select').value='2024-08-16';elements.get('date-select').onchange();await settle();assert(calls.some(path=>path.includes('date=2024-08-16')));
      searches[1].value='Manchester';searches[1].oninput();timers[timers.length-1]();await settle();assert.equal(searches[0].value,'Manchester');assert(calls.some(path=>path.includes('team=Manchester')));
      elements.get('next-page').onclick();await settle();assert(calls.some(path=>path.includes('offset=50')));assert(text('matches-count').includes('51–51'));
      elements.get('previous-page').onclick();await settle();assert(elements.get('previous-page').disabled);
    } else if(mode!=='unavailable') {
      assert(calls.some(path=>path.startsWith('/api/matches?view=all')));
      assert(elements.get('matches-body').children.length===1);
    } else {
      assert.equal(elements.get('total-matches').textContent,'Veri bekleniyor');
    }
    assert.equal(failures.length,0,String(failures[0]));
  } finally {process.off('unhandledRejection',listener);}
}
(async()=>{const selected=process.argv[2]?[process.argv[2]]:modes;for(const mode of selected)await scenario(mode);console.log(`dashboard runtime: ${selected.length} scenarios passed`);})().catch(error=>{console.error(error);process.exitCode=1;});
