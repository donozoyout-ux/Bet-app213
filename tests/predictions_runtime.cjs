/* Exercise the real frontend with a recorded response computed from stored EPL data. */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('src/api/dashboard-predictions.js','utf8');
const recorded=JSON.parse(fs.readFileSync('tests/fixtures/predictions_runtime.json','utf8'));
class Element {
  constructor(){this.children=[];this.textContent='';this.attrs={};this.hidden=false;}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(){this.children=[];this.textContent='';}
  setAttribute(key,value){this.attrs[key]=value;}
  scrollIntoView(){this.scrolled=true;}
  text(){return [this.textContent,...this.children.map(node=>node.text())].join(' ');}
}
async function scenario(mode){
  const ids=new Map(['prediction-cards','prediction-window','statistics-detail'].map(id=>[id,new Element()]));
  if(mode==='missing'){ids.delete('prediction-cards');ids.delete('statistics-detail');}
  const calls=[],clicked=[];let releaseFirst;
  const filter=new Element();filter.dataset={predictionMarket:'cards'};const view=new Element();view.value='best';ids.set('prediction-view',view);
  const sandbox={console,URLSearchParams,AbortSignal,document:{getElementById:id=>ids.get(id)||null,createElement:()=>new Element(),querySelectorAll:()=>[filter]},
    fetch:async path=>{
      calls.push(path);
      const fail=mode==='failed'||mode==='detail-failed' && path.includes('/statistics');
      let payload=path.includes('/api/predictions')?structuredClone(recorded.page):structuredClone(recorded.statistics);
      if(path.includes('/best'))payload={...payload,items:recorded.page.items.flatMap(item=>item.recommendations.map(recommendation=>({match:item.match,recommendation})))};
      if(path.includes('market=cards'))payload.items=[];
      if(mode==='empty' && path.includes('/api/predictions'))payload.items=[];
      if(mode==='insufficient' && path.includes('/api/predictions'))payload.items=[{...payload.items[0],recommendations:[],prediction:{...payload.items[0].prediction,status:'insufficient_data',home_probability:null,draw_probability:null,away_probability:null}}];
      if(mode==='race' && path.includes('/statistics')){
        const first=recorded.page.items[0].match.id;
        if(path===`/api/matches/${first}/statistics`)return await new Promise(resolve=>{releaseFirst=()=>resolve({ok:true,json:async()=>payload});});
        payload.match=recorded.page.items[1].match;
      }
      return {ok:!fail,status:fail?503:200,json:async()=>payload};
    }};
  vm.createContext(sandbox);vm.runInContext(source,sandbox);
  const api=sandbox.BetAppPredictions;
  if(mode==='missing'){await api.refresh();await api.select(1);api.clear();return;}
  await api.refresh('1','2026-10-10');
  assert(calls[0].includes('league=1') && calls[0].includes('date=2026-10-10'));
  const cards=ids.get('prediction-cards');
  if(mode==='failed' || mode==='empty'){assert(cards.text().includes(mode==='failed'?'Veri hazırlanıyor':'eşikleri geçen tahmin bulunmuyor'));assert(!cards.text().includes('%'));return;}
  if(mode==='insufficient'){assert(cards.text().includes('eşikleri geçen tahmin bulunmuyor'));assert(!cards.text().includes('%'));return;}
  assert.equal(cards.children.length,recorded.page.items.length);
  assert(cards.text().includes('En Güçlü Tahmin') && cards.text().includes('İstatistikleri Gör'));
  assert(!cards.text().includes('Beklenen Gol'));
  for(const item of recorded.page.items) {assert(item.recommendations.length>=1 && item.recommendations.length<=3);for(const rec of item.recommendations)assert(cards.text().includes(rec.label));}
  assert(!cards.text().includes('value bet') && !cards.text().includes('xG'));
  // The first card calls the shared match-detail action, preserving the dashboard.
  sandbox.BetAppDashboard={selectMatch:(id,scroll)=>clicked.push([id,scroll])};
  cards.children[0].onclick();assert.deepEqual(clicked,[[recorded.page.items[0].match.id,true]]);
  cards.children[0].children.at(-1).onclick({stopPropagation(){}});assert.equal(clicked.length,2);
  if(mode==='filter'){filter.onclick();await api.refresh('1','2026-10-10');assert(calls.at(-1).includes('market=cards'));assert(cards.text().includes('eşikleri geçen'));return;}
  if(mode==='best'){view.onchange();await api.refresh('1','2026-10-10');assert(calls.at(-1).includes('/best?'));assert(cards.text().includes(recorded.page.items[0].recommendations[0].label));cards.children[0].children[0].children[0].onclick();assert.equal(clicked.length,3);return;}
  const firstId=recorded.page.items[0].match.id;
  if(mode==='race'){
    const first=api.select(firstId);await api.select(recorded.page.items[1].match.id);releaseFirst();await first;
    assert(ids.get('statistics-detail').text().includes(recorded.page.items[1].match.home_team));return;
  }
  await api.select(firstId,true);
  const box=ids.get('statistics-detail');
  if(mode==='detail-failed'){assert(box.text().includes('Bağlantı bekleniyor'));return;}
  assert(!box.hidden && box.scrolled);
  for(const word of ['Maç İstatistikleri','Son 5','Son 10','H2H','Crown','Bet365','Sbobet','Model / Piyasa','KORNERLER','KARTLAR'])assert(box.text().includes(word));
  assert(!box.text().includes('HAKEM'));
  assert(box.text().includes(recorded.statistics.match.home_team));
  const requests=calls.filter(path=>path.includes('/statistics')).length;
  await api.select(firstId);
  assert.equal(calls.filter(path=>path.includes('/statistics')).length,requests);
  api.clear();assert(box.hidden && !box.children.length);
}
(async()=>{const modes=process.argv[2]?[process.argv[2]]:['populated','insufficient','empty','failed','detail-failed','missing','race','filter','best'];for(const mode of modes)await scenario(mode);console.log(`prediction runtime: ${modes.length} scenarios passed`);})().catch(error=>{console.error(error);process.exitCode=1;});
