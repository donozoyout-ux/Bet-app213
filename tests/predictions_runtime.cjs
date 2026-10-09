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
  showModal(){this.open=true;}close(){this.open=false;}focus(){this.focused=true;}
  text(){return [this.textContent,...this.children.map(node=>node.text())].join(' ');}
}
async function scenario(mode){
  const ids=new Map(['prediction-cards','prediction-window','statistics-detail','analysis-dialog','analysis-close','analysis-panel','match-detail','prediction-confidence'].map(id=>[id,new Element()]));
  if(mode==='missing'){ids.delete('prediction-cards');ids.delete('statistics-detail');}
  const calls=[],clicked=[];let releaseFirst;
  const tabs=['predictions','form','goals','corners','cards','odds','h2h'].map(key=>{const el=new Element();el.dataset={analysisTab:key};return el;});
  const filter=new Element();filter.dataset={predictionMarket:'cards'};const view=new Element();view.value='best';ids.set('prediction-view',view);
  const sandbox={console,URLSearchParams,AbortSignal,document:{getElementById:id=>ids.get(id)||null,createElement:()=>new Element(),querySelectorAll:selector=>selector==='[data-analysis-tab]'?tabs:[filter]},
    fetch:async path=>{
      calls.push(path);
      const fail=mode==='failed'||mode==='detail-failed' && path.includes('/statistics');
      let payload=path.includes('/api/predictions')?structuredClone(recorded.page):structuredClone(recorded.statistics);
      if(['one','two','three'].includes(mode) && path.includes('/api/predictions')){const count={one:1,two:2,three:3}[mode];payload.items[0].recommendations=[...payload.items[0].recommendations,recorded.page.items[1].recommendations[0]].slice(0,count);}
      if(path.includes('/best'))payload={...payload,items:recorded.page.items.flatMap(item=>item.recommendations.map(recommendation=>({match:item.match,recommendation})))};
      if(path.includes('market=cards'))payload.items=[];
      if(mode==='pagination' && path.includes('/best')){const offset=Number(new URLSearchParams(path.split('?')[1]).get('offset') || 0);const item=recorded.page.items[offset===0?0:1];const recommendation=item.recommendations.find(r=>r.confidence==='high');payload.items=Array.from({length:offset===0?100:1},()=>({match:item.match,recommendation}));}
      if(mode==='empty' && path.includes('/api/predictions'))payload.items=[];
      if(mode==='insufficient' && path.includes('/api/predictions'))payload.items=[{...payload.items[0],recommendations:[],prediction:{...payload.items[0].prediction,status:'insufficient_data',home_probability:null,draw_probability:null,away_probability:null}}];
      if(mode==='card-categories' && path.includes('/statistics')){
        const summary={for_avg:2,against_avg:3,for_sample_size:2,against_sample_size:2,matches_considered:5,sample_size:2,total_avg:5,missing_total_sample_size:3,over_rates:{},red_sample_size:0};
        const profile=Object.fromEntries(['last_5','last_10','season','home_split','away_split'].map(key=>[key,summary]));
        payload.additional_statistics={yellow_cards:{home:profile,away:profile,prediction:{basis:'yellow_only',status:'insufficient_data',sample_size:2,league_sample_size:2,insufficient_reasons:[{code:'league_sample_size',actual:2,required:20}]}}};
      }
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
  assert(cards.text().includes('En Güçlü Tahmin') && cards.text().includes('Analizi aç →'));
  assert(!cards.text().includes('Beklenen Gol'));
  for(const item of recorded.page.items.slice(['one','two','three'].includes(mode)?1:0)) {assert(item.recommendations.length>=1 && item.recommendations.length<=3);for(const rec of item.recommendations)assert(cards.text().includes(rec.label));}
  assert(!cards.text().includes('value bet') && !cards.text().includes('xG'));
  if(['one','two','three'].includes(mode)){const count={one:1,two:2,three:3}[mode];const first=cards.children[0];assert.equal(first.children.filter(node=>node.textContent.includes('%')).length,count);}
  // The first card calls the shared match-detail action, preserving the dashboard.
  sandbox.BetAppDashboard={selectMatch:(id,scroll)=>clicked.push([id,scroll])};
  cards.children[0].onclick();assert.deepEqual(clicked,[[recorded.page.items[0].match.id,true]]);
  cards.children[0].children.at(-1).onclick({stopPropagation(){}});assert.equal(clicked.length,2);
  if(mode==='pagination'){const conf=ids.get('prediction-confidence');conf.value='high';conf.onchange();const matches=await api.filterMatches(recorded.page.items.map(item=>item.match));assert.equal(matches.length,2);assert(calls.some(path=>path.includes('offset=100')));const before=calls.length;await api.filterMatches(recorded.page.items.map(item=>item.match));assert.equal(calls.length,before);return;}
  if(mode==='confidence'){const conf=ids.get('prediction-confidence');conf.value='high';conf.onchange();await api.refresh('1','2026-10-10');assert(cards.children.length>0 && cards.children.length<recorded.page.items.length);assert(!cards.text().includes('Orta Güven'));return;}
  if(mode==='filter'){filter.onclick();await api.refresh('1','2026-10-10');assert(calls.at(-1).includes('market=cards'));assert(cards.text().includes('eşikleri geçen'));assert.equal((await api.filterMatches(recorded.page.items.map(item=>item.match))).length,0);return;}
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
  assert(ids.get('analysis-dialog').open);
  assert(box.text().includes('Seçilen Güçlü Tahminler'));
  for(const [key,words] of [['form',['Son 5','Son 10']],['goals',['Beklenen Gol']],['h2h',['H2H']],['odds',['Crown','Bet365','Sbobet','Model / Piyasa']],['corners',['KORNERLER','Yetersiz veri']],['cards',['KARTLAR','Yetersiz veri']]]){tabs.find(tab=>tab.dataset.analysisTab===key).onclick();for(const word of words)assert(box.text().includes(word));assert(box.text().includes(recorded.statistics.match.home_team));}
  if(mode==='card-categories'){
    const all=(el)=>[el,...el.children.flatMap(all)];
    assert(box.text().includes('2.00 / 3.00'));
    assert(box.text().includes('2 / 20 gerekli'));
    assert(box.text().includes('Tahmin önerisi değildir'));
    let red=all(box).find(el=>el.attrs['data-card-category']==='red_cards');red.onclick();
    assert(box.text().includes('KIRMIZI KARTLAR') && box.text().includes('Yetersiz veri'));
    const yellow=all(box).find(el=>el.attrs['data-card-category']==='yellow_cards');yellow.onclick();
    assert(box.text().includes('2.00 / 3.00'));return;
  }
  tabs[0].onkeydown({key:'ArrowRight',preventDefault(){}});assert(tabs[1].focused);
  tabs[0].onclick();
  assert(!box.text().includes('HAKEM'));
  assert(box.text().includes(recorded.statistics.match.home_team));
  const requests=calls.filter(path=>path.includes('/statistics')).length;
  await api.select(firstId);
  assert.equal(calls.filter(path=>path.includes('/statistics')).length,requests);
  ids.get('analysis-close').onclick();assert(box.hidden && !box.children.length && !ids.get('analysis-dialog').open);
  await api.select(firstId);ids.get('analysis-dialog').oncancel({preventDefault(){}});assert(!ids.get('analysis-dialog').open);
}
(async()=>{const modes=process.argv[2]?[process.argv[2]]:['populated','insufficient','empty','failed','detail-failed','missing','race','filter','best','one','two','three','confidence','pagination','card-categories'];for(const mode of modes)await scenario(mode);console.log(`prediction runtime: ${modes.length} scenarios passed`);})().catch(error=>{console.error(error);process.exitCode=1;});
