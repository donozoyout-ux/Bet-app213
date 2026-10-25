// Isolated recorded API fixture; never calls a provider or the production database.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const recorded=JSON.parse(fs.readFileSync('tests/fixtures/predictions_runtime.json','utf8'));
class Element {
  constructor(tag='div'){this.tag=tag;this.children=[];this.dataset={};this.attributes={};this.textContent='';this.open=false;}
  append(...els){this.children.push(...els);} replaceChildren(...els){this.children=[];this.append(...els);}
  setAttribute(k,v){this.attributes[k]=String(v);} getAttribute(k){return this.attributes[k];}
  querySelectorAll(selector){const all=this.children.flatMap(e=>[e,...e.querySelectorAll('*')]);
    return selector==='*'?all:all.filter(e=>selector==='details[open]'?e.tag==='details'&&e.open:e.tag===selector);}
  querySelector(selector){return this.querySelectorAll(selector)[0] || null;}
  scrollBy(){} scrollIntoView(){} focus(){}
  text(){return [this.textContent,...this.children.map(e=>e.text())].join(' ');}
}
const settle=async()=>{for(let i=0;i<12;i++)await new Promise(r=>setImmediate(r));};
async function run(mode) {
  const ids=[...fs.readFileSync('src/api/dashboard.html','utf8').matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);
  const elements=new Map(ids.map(id=>[id,new Element()])),ticks=[],calls=[];
  let stamp=Date.parse(recorded.page.generated_at),release;
  const template=structuredClone(recorded.page.items[0]);
  if(['quoted','quote-mismatch'].includes(mode))for(const r of template.recommendations)r.verified_odds={has_odds:true,coupon_eligible:true,freshness:'fresh',best_price:1.2,best_bookmaker:'Crown',best_value:-.16,observed_at:new Date(stamp).toISOString(),expires_at:new Date(stamp+900000).toISOString()};
  const list=Array.from({length:mode==='empty'?0:10},(_,i)=>({...structuredClone(template),
    match:{...template.match,id:mode==='duplicate'?1:i+1,status:'scheduled',kickoff_at:new Date(stamp+86400000).toISOString()}}));
  if(mode==='unqualified')list.forEach(i=>i.recommendations.forEach(r=>r.qualifies=false));
  const sandbox={console,URLSearchParams,AbortSignal,AbortController,Map,Set,Number,Intl,
    Date:class extends Date {static now(){return stamp;}},setInterval:f=>ticks.push(f),
    location:{hash:'#predictions?match=14'},history:{replaceState(_s,_t,url){sandbox.location.hash=url;}},
    BetAppUI:{active:p=>p==='predictions'},document:{getElementById:id=>elements.get(id),createElement:tag=>new Element(tag),addEventListener(){}},
    fetch:async(path,options)=>{calls.push([path,options.method || 'GET']);
      if(mode==='error')throw new Error('offline');
      let data;
      if(path==='/api/coupon/quote')data={items:JSON.parse(options.body).selections.map(s=>({...s,match_id:mode==='quote-mismatch'?99999:s.match_id,eligible:true,expires_at:new Date(stamp+900000).toISOString()}))};
      else if(path.startsWith('/api/predictions?'))data={items:list,generated_at:new Date(stamp).toISOString(),cache:{stale:mode==='stale',checked_at:new Date(stamp).toISOString(),expires_at:new Date(stamp+600000).toISOString()}};
      else {const id=Number(path.split('/')[3]);data={...structuredClone(recorded.statistics),match:{...list[id-1]?.match},historical:false,as_of:new Date(stamp).toISOString(),cache_seconds:600};
        if(mode==='race'&&id===1)return await new Promise(resolve=>release=()=>resolve({ok:true,json:async()=>data}));}
      return {ok:true,json:async()=>data};}};
  vm.createContext(sandbox);vm.runInContext(fs.readFileSync('src/api/dashboard-analysis.js','utf8'),sandbox);
  assert.equal(sandbox.location.hash,'#predictions');
  await sandbox.BetAppMatchAnalysis.refresh();await settle();
  const strip=elements.get('analysis-match-strip'),blocks=elements.get('analysis-blocks');
  if(['empty','unqualified','stale','error'].includes(mode)){assert.equal(strip.children.length,0);assert.equal(elements.get('analysis-selected').hidden,true);return;}
  assert.equal(strip.children.length,mode==='duplicate'?1:8);
  if(mode==='race'){assert.equal(strip.children[1].href,'/match/2');sandbox.BetAppMatchAnalysis.select(2);await settle();release();await settle();
    assert.equal(strip.children[1].getAttribute('aria-pressed'),'true');assert.equal(strip.children[0].getAttribute('aria-pressed'),'false');}
  if(mode==='stable'){
    const originalCard=strip.children[0],originalBlocks=blocks.children[0];
    const originalDetailCalls=calls.filter(([url])=>url.startsWith('/api/matches/')).length;
    await sandbox.BetAppMatchAnalysis.refresh();await settle();
    assert.equal(strip.children[0],originalCard,'unchanged published snapshot must not recreate cards');
    assert.equal(blocks.children[0],originalBlocks,'unchanged snapshot must preserve open details');
    assert.equal(calls.filter(([url])=>url.startsWith('/api/matches/')).length,originalDetailCalls,
      'unchanged snapshot must not refetch selected match evidence');
  }
  assert.equal(blocks.querySelectorAll('details').length,5);
  assert(blocks.text().includes('Model olasılığı'));assert(blocks.text().includes('Crown'));
  if(mode==='quoted')assert(blocks.text().includes('Kupon kontrolünü geçti'));
  if(mode==='quote-mismatch'){assert(!blocks.text().includes('Kupon kontrolünü geçti'));assert(blocks.text().includes('Kupon uygunluğu doğrulanmadı'));}
  assert(calls.every(([path,method])=>method==='GET'||path==='/api/coupon/quote'),'No save/send endpoint is called');
  stamp+=86400001;ticks.forEach(t=>t());assert.equal(strip.children.length,0);assert.equal(elements.get('analysis-selected').hidden,true);
}
(async()=>{for(const mode of ['eight','duplicate','empty','unqualified','stale','error','race','quoted','quote-mismatch','stable'])await run(mode);
  console.log('minimal analysis runtime: 9 scenarios passed');})().catch(e=>{console.error(e);process.exitCode=1;});
