/* Execute the real dashboard scripts with the current HTML IDs and API contracts. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const markup = fs.readFileSync('src/api/dashboard.html', 'utf8');
const recorded=JSON.parse(fs.readFileSync('tests/fixtures/dashboard_multi_league.json','utf8'));
class Element {
  constructor() {this.children=[];this.value='';this.textContent='';this.dataset={};this.classList={toggle(){}};}
  append(node){this.children.push(node);}
  replaceChildren(){this.children=[];}
  get firstElementChild(){return this.children[0];}
  setAttribute(key,value){this[key]=value;}
}
async function scenario(mode) {
  const elements = new Map([...markup.matchAll(/\bid="([^"]+)"/g)].map(m=>[m[1],new Element()]));
  if(mode==='missing-controls') for(const id of ['league-select','season-select','round-select','date-select','refresh-button','previous-page','next-page','league-nav'])elements.delete(id);
  const calls=[];const failures=[];const selected=[];const pending=[];
  const sandbox={BetAppPredictions:{select:id=>selected.push(id),clear(){},refresh(){}},console,URLSearchParams,AbortSignal,setInterval(){},setTimeout(){},clearTimeout(){},
    document:{getElementById:id=>elements.get(id) || null,querySelector:()=>null,querySelectorAll:()=>[],createElement:()=>new Element()},
    fetch:async path=>{
      calls.push(path);
      const unavailable=mode==='unavailable' && path!=='/health';
      let payload;
      if(path==='/health')payload={status:'ok'};
      else if(path==='/api/status')payload={database:unavailable?'unavailable':'connected',total_matches:0,total_odds:0};
      else if(['multi','detail-race'].includes(mode) && path==='/api/leagues')payload=recorded.leagues;
      else if(['multi','detail-race'].includes(mode) && path==='/api/bookmakers')payload=['Crown','Bet365','Sbobet'].map(name=>({name}));
      else if(['multi','detail-race'].includes(mode) && /\/seasons$/.test(path))payload=recorded.seasons[path.split('/')[3]] || [];
      else if(path.startsWith('/api/matches?')) {
        const query=new URLSearchParams(path.split('?')[1]);
        payload=['multi','detail-race'].includes(mode) && query.get('view')==='upcoming' ? (query.get('league') ? recorded.national_page : recorded.page) : {items:[],total:0};
      }
      else if(/^\/api\/matches\/\d+(\/odds)?$/.test(path)){const id=Number(path.split('/')[3]);const match=recorded.page.items.find(item=>item.id===id);payload=path.endsWith('/odds')?match.odds:match;if(mode==='detail-race' && id===recorded.page.items[0].id)return await new Promise(resolve=>pending.push(()=>resolve({ok:true,json:async()=>payload})));}
      else if(path==='/api/scraper/status')payload={status:'idle',jobs:mode==='job-priority'?[{id:11,current_league:'Liga Portugal 1',kind:'stats_backfill',status:'queued',processed_matches:0,failed_matches:0,total_matches:30},{id:9,current_league:'English Premier League',kind:'stats_backfill',status:'running',processed_matches:3,failed_matches:0,total_matches:30}]:[]};
      else payload=[];
      return {ok:!unavailable,status:unavailable?503:200,json:async()=>payload};
    }};
  const listener=error=>failures.push(error);
  process.on('unhandledRejection',listener);
  try {
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync('src/api/dashboard-odds.js','utf8'),sandbox);
    vm.runInContext(fs.readFileSync('src/api/dashboard.js','utf8'),sandbox);
    const settle=async()=>{for(let i=0;i<5;i++)await new Promise(resolve=>setImmediate(resolve));};
    await settle();
    assert.equal(failures.length,0,String(failures[0]));
    assert(calls.includes('/health'));
    assert(calls.includes('/api/status'));
    assert(calls.includes('/api/scraper/status'));if(mode==='job-priority')assert(elements.get('collection-progress').textContent.includes('English Premier League'));
    if(['multi','detail-race'].includes(mode)) {
      if(mode==='detail-race'){const first=sandbox.BetAppDashboard.selectMatch(recorded.page.items[0].id);await sandbox.BetAppDashboard.selectMatch(recorded.page.items[1].id);pending.forEach(release=>release());await first;assert.equal(elements.get('detail-title').textContent,`${recorded.page.items[1].home_team} vs ${recorded.page.items[1].away_team}`);sandbox.BetAppDashboard.closeDetail();return;}
      const row=elements.get('matches-body').children[0];row.onclick();await settle();assert.equal(selected.at(-1),recorded.page.items[0].id);assert.equal(elements.get('detail-title').textContent,`${recorded.page.items[0].home_team} vs ${recorded.page.items[0].away_team}`);
      row.onkeydown({key:'Enter',preventDefault(){}});await settle();assert.equal(selected.at(-1),recorded.page.items[0].id);sandbox.BetAppDashboard.closeDetail();
      const select=elements.get('league-select');
      assert(select.children.some(node=>node.label==='KULÜP LİGLERİ'));
      assert(select.children.some(node=>node.label==='MİLLİ TAKIMLAR'));
      assert.equal(elements.get('league-select').value,'');
      assert(calls.some(path=>path.includes('view=upcoming') && !path.includes('league=')));
      assert.equal(elements.get('matches-body').children.length,recorded.page.items.length);
      const text=elements.get('matches-body').children[0].children.map(td=>td.textContent).join(' ');
      assert(text.includes(recorded.page.items[0].home_team));
      const crown=recorded.page.items[0].odds.find(m=>m.bookmaker==='Crown' && m.market==='1x2');
      assert(text.includes(`${crown.opening.home} → ${crown.latest.home}`));
      select.value=String(recorded.national_page.items[0].league_id);await select.onchange();await settle();
      assert.equal(elements.get('matches-body').children.length,recorded.national_page.items.length);
      assert(calls.some(path=>path.includes('league='+recorded.national_page.items[0].league_id)));
      assert(elements.get('upcoming-window').textContent.includes('en yakın maç günü'));
      select.value='';await select.onchange();await settle();
      assert.equal(elements.get('matches-body').children.length,recorded.page.items.length);
      assert.equal(elements.get('league-select').value,'');
    } else if(mode!=='unavailable') {
      assert(calls.some(path=>path.startsWith('/api/matches?view=upcoming')));
      assert(elements.get('matches-body').children.length===1);
    } else {
      assert.equal(elements.get('total-matches').textContent,'Veri bekleniyor');
    }
  } finally {process.off('unhandledRejection',listener);}
}
(async()=>{for(const mode of ['empty','unavailable','missing-controls','multi','detail-race','job-priority'])await scenario(mode);console.log('dashboard runtime: 6 scenarios passed');})().catch(error=>{console.error(error);process.exitCode=1;});
