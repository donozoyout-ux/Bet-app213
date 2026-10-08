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
  setAttribute(){}
}
async function scenario(mode) {
  const elements = new Map([...markup.matchAll(/\bid="([^"]+)"/g)].map(m=>[m[1],new Element()]));
  if(mode==='missing-controls') for(const id of ['league-select','season-select','round-select','date-select','refresh-button','previous-page','next-page','league-nav'])elements.delete(id);
  const calls=[];const failures=[];
  const sandbox={console,URLSearchParams,AbortSignal,setInterval(){},setTimeout(){},clearTimeout(){},
    document:{getElementById:id=>elements.get(id) || null,querySelector:()=>null,querySelectorAll:()=>[],createElement:()=>new Element()},
    fetch:async path=>{
      calls.push(path);
      const unavailable=mode==='unavailable' && path!=='/health';
      let payload;
      if(path==='/health')payload={status:'ok'};
      else if(path==='/api/status')payload={database:unavailable?'unavailable':'connected',total_matches:0,total_odds:0};
      else if(mode==='multi' && path==='/api/leagues')payload=recorded.leagues;
      else if(mode==='multi' && path==='/api/bookmakers')payload=['Crown','Bet365','Sbobet'].map(name=>({name}));
      else if(mode==='multi' && /\/seasons$/.test(path))payload=recorded.seasons[path.split('/')[3]] || [];
      else if(path.startsWith('/api/matches?')) {
        const query=new URLSearchParams(path.split('?')[1]);
        payload=mode==='multi' && query.get('view')==='upcoming' ? (query.get('league') ? recorded.national_page : recorded.page) : {items:[],total:0};
      }
      else if(path==='/api/scraper/status')payload={status:'idle',jobs:[]};
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
    assert(calls.includes('/api/scraper/status'));
    if(mode==='multi') {
      const nav=elements.get('league-nav');
      assert(nav.children.some(node=>node.textContent==='KULÜP LİGLERİ'));
      assert(nav.children.some(node=>node.textContent==='MİLLİ TAKIMLAR'));
      assert.equal(elements.get('league-select').value,'');
      assert(calls.some(path=>path.includes('view=upcoming') && !path.includes('league=')));
      assert.equal(elements.get('matches-body').children.length,recorded.page.items.length);
      const text=elements.get('matches-body').children[0].children.map(td=>td.textContent).join(' ');
      assert(text.includes(recorded.page.items[0].home_team));
      const crown=recorded.page.items[0].odds.find(m=>m.bookmaker==='Crown' && m.market==='1x2');
      assert(text.includes(`${crown.opening.home} → ${crown.latest.home}`));
      await nav.children.find(node=>node.textContent==='UEFA Nations League').onclick();await settle();
      assert.equal(elements.get('matches-body').children.length,recorded.national_page.items.length);
      assert(calls.some(path=>path.includes('league='+recorded.national_page.items[0].league_id)));
      assert(elements.get('upcoming-window').textContent.includes('en yakın maç günü'));
      await nav.children.find(node=>node.textContent==='Tüm Ligler').onclick();await settle();
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
(async()=>{for(const mode of ['empty','unavailable','missing-controls','multi'])await scenario(mode);console.log('dashboard runtime: 4 scenarios passed');})().catch(error=>{console.error(error);process.exitCode=1;});
