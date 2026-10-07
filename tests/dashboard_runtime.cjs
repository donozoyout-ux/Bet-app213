/* Execute the real dashboard scripts with the current HTML IDs and API contracts. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const markup = fs.readFileSync('src/api/dashboard.html', 'utf8');
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
      else if(path.startsWith('/api/matches?'))payload={items:[],total:0};
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
    await new Promise(resolve=>setImmediate(resolve));
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(failures.length,0,String(failures[0]));
    assert(calls.includes('/health'));
    assert(calls.includes('/api/status'));
    assert(calls.includes('/api/scraper/status'));
    if(mode!=='unavailable') {
      assert(calls.some(path=>path.startsWith('/api/matches?view=all')));
      assert(elements.get('matches-body').children.length===1);
    } else {
      assert.equal(elements.get('total-matches').textContent,'Veri bekleniyor');
    }
  } finally {process.off('unhandledRejection',listener);}
}
(async()=>{for(const mode of ['empty','unavailable','missing-controls'])await scenario(mode);console.log('dashboard runtime: 3 scenarios passed');})().catch(error=>{console.error(error);process.exitCode=1;});
