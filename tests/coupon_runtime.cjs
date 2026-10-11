/* Execute actual coupon code with deterministic API/DOM boundaries. */
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const source=fs.readFileSync('src/api/dashboard-coupon.js','utf8');
class Element{
 constructor(tag='div'){this.tag=tag;this.children=[];this.textContent='';this.dataset={};this.attrs={};this.value='';}
 append(...children){this.children.push(...children);}
 replaceChildren(){this.children=[];this.textContent='';}
 setAttribute(key,value){this.attrs[key]=value;}
 text(){return [this.textContent,...this.children.map(c=>c.text())].join(' ');}
}
const all=e=>[e,...e.children.flatMap(all)];
async function scenario(mode){
 const ids=new Map(['coupon-selected','coupon-total','coupon-copy','coupon-clear','coupon-reload','coupon-note','coupon-candidates','coupon-bookmaker'].map(id=>[id,new Element()]));ids.get('coupon-bookmaker').value='Bet365';
 const now=Date.now(),stamp=new Date(now-60000).toISOString(),expiry=new Date(now+60000).toISOString();
 const items=Array.from({length:6},(_,index)=>{
  const id=Math.floor(index/2)+1,selection=index%2?'under_3_5':'over_1_5';
  return {match:{id,status:'scheduled',home_team:'Home '+id,away_team:'Away '+id,kickoff_at:new Date(now+3600000).toISOString()},recommendation:{id:selection+'_first_half_goals',market:'first_half_goals',selection,label:selection,qualifies:true,probability:.7,confidence:'medium',verified_odds:{has_odds:true,match_id:id,market:'first_half_goals',selection,line:index%2?3.5:1.5,period:'first_half',bookmakers:{Bet365:{current_odds:index%2?2.2:1.8,observed_at:stamp,expires_at:expiry,freshness:'fresh',coupon_eligible:true}}}}};
 });
 if(mode==='invalid'){
  items[0].recommendation.verified_odds.bookmakers.Bet365.current_odds='1.8';
  items[1].recommendation.verified_odds.bookmakers.Bet365.expires_at=stamp;
  items[2].recommendation.verified_odds.period='full_time';
  items[3].recommendation.verified_odds.line=4.5;
  items[4].recommendation.verified_odds.has_odds=false;
  items[5].recommendation.verified_odds.match_id=999;
 }
 let copied='',release,calls=[];
 const sandbox={console,Date,Map,Number,AbortSignal,navigator:{clipboard:{writeText:async value=>{if(mode==='copy-denied')throw Error('denied');copied=value;}}},document:{getElementById:id=>ids.get(id),createElement:tag=>new Element(tag),querySelectorAll:()=>all(ids.get('coupon-candidates')).filter(e=>e.type==='checkbox')},fetch:async(path,options)=>{
  calls.push(path);
  if(mode==='request-failed')throw Error('timeout');
  if(path.startsWith('/api/predictions'))return {ok:true,json:async()=>({items,generated_at:stamp,cache:{stale:false}})};
  const request=JSON.parse(options.body);
  if(mode==='clear-race' && path==='/api/coupon/quote')await new Promise(resolve=>{release=resolve;});
  const values=request.selections.map(s=>{
   if(path.endsWith('/status'))return {...s,status:'pending',match_status:'scheduled'};
   const item=items.find(i=>i.match.id===s.match_id && i.recommendation.id===s.recommendation_id);
   const b=item.recommendation.verified_odds.bookmakers.Bet365;
   return {...s,eligible:mode!=='server-rejected',price:b.current_odds,observed_at:b.observed_at,expires_at:b.expires_at,reason:'odds_stale'};
  });return {ok:true,json:async()=>({items:values})};
 }};
 vm.runInNewContext(source,sandbox);await sandbox.BetAppCoupon.refresh();
 const inputs=()=>sandbox.document.querySelectorAll();
 if(mode==='request-failed'){assert(ids.get('coupon-note').text().includes('Bağlantı'));assert.equal(ids.get('coupon-candidates').attrs['aria-busy'],'false');return;}
 assert.equal(inputs().length,6);
 if(mode==='invalid'){assert(inputs().every(e=>e.disabled));return;}
 inputs()[0].checked=true;const pending=inputs()[0].onchange();
 if(mode==='clear-race'){ids.get('coupon-clear').onclick();release();await pending;assert(ids.get('coupon-selected').text().includes('Henüz'));return;}
 await pending;
 if(mode==='server-rejected'){assert(ids.get('coupon-selected').text().includes('Henüz'));assert(ids.get('coupon-note').text().includes('bayat'));return;}
 assert(inputs()[1].disabled);inputs()[3].checked=true;await inputs()[3].onchange();
 assert(ids.get('coupon-total').text().includes('3,96'));assert(inputs()[4].disabled);
 await ids.get('coupon-copy').onclick();
 if(mode==='copy-denied'){assert(!copied);assert(ids.get('coupon-note').text().includes('Kopyalanamadı'));return;}
 assert(copied.includes('Bet365') && copied.includes('3,96') && copied.includes('Gözlem'));
 ids.get('coupon-clear').onclick();assert(ids.get('coupon-selected').text().includes('Henüz'));assert(ids.get('coupon-copy').disabled);
 assert(calls.includes('/api/coupon/quote') && calls.includes('/api/coupon/status'));
}
(async()=>{const modes=['populated','invalid','request-failed','server-rejected','copy-denied','clear-race'];for(const mode of modes)await scenario(mode);console.log(`coupon runtime: ${modes.length} scenarios passed`);})().catch(e=>{console.error(e);process.exitCode=1;});
