/* Execute actual coupon code with deterministic API/DOM boundaries. */
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const source=fs.readFileSync('src/api/dashboard-coupon.js','utf8');
class Element{
 constructor(tag='div'){this.tag=tag;this.children=[];this.textContent='';this.dataset={};this.attrs={};this.value='';this.classList={toggle(){}};}
 append(...children){this.children.push(...children);}
 replaceChildren(){this.children=[];this.textContent='';}
 setAttribute(key,value){this.attrs[key]=value;}
 text(){return [this.textContent,...this.children.map(c=>c.text())].join(' ');}
}
const all=e=>[e,...e.children.flatMap(all)];
async function scenario(mode){let dailyData;
 const ids=new Map(['coupon-selected','coupon-total','coupon-copy','coupon-clear','coupon-reload','coupon-note','coupon-candidates','coupon-bookmaker','coupon-show-unpriced'].map(id=>[id,new Element()]));ids.get('coupon-bookmaker').value='Bet365';
 if(mode.startsWith('automatic'))for(const id of ['coupon-daily','coupon-mode','coupon-manual','coupon-automatic','coupon-save','coupon-saved-note','coupon-history','coupon-history-refresh','coupon-telegram','telegram-status','telegram-schedule','telegram-admin-token','telegram-verify','telegram-check-note'])ids.set(id,new Element());
 const now=Date.now(),stamp=new Date(now-60000).toISOString(),expiry=new Date(now+60000).toISOString();
 const items=Array.from({length:6},(_,index)=>{
  const id=Math.floor(index/2)+1,selection=index%2?'under_3_5':'over_1_5';
  return {match:{id,status:'scheduled',home_team:'Home '+id,away_team:'Away '+id,kickoff_at:new Date(now+3600000).toISOString()},recommendation:{id:selection+'_first_half_goals',market:'first_half_goals',selection,label:selection,qualifies:true,probability:.7,reliability:.9,score:.9,confidence:'medium',verified_odds:{has_odds:true,match_id:id,market:'first_half_goals',selection,line:index%2?3.5:1.5,period:'first_half',bookmakers:{Bet365:{current_odds:index%2?2.2:1.8,observed_at:stamp,expires_at:expiry,freshness:'fresh',coupon_eligible:true}}}}};
 });
 if(mode==='invalid'){
  items[0].recommendation.verified_odds.bookmakers.Bet365.current_odds='1.8';
  items[1].recommendation.verified_odds.bookmakers.Bet365.expires_at=stamp;
  items[2].recommendation.verified_odds.period='full_time';
  items[3].recommendation.verified_odds.line=4.5;
  items[4].recommendation.verified_odds.has_odds=false;
  items[5].recommendation.verified_odds.match_id=999;
 }
 let copied='',release,calls=[],versions=new Map(),manualRevision=0;
 const saved=(selected,kind='automatic',revision=1)=>({id:kind+'-'+revision,revision,kind,created_at:stamp,combined_odds:selected.reduce((p,i)=>p*i.quote.price,1),items:selected});
 const asSaved=item=>({...structuredClone(item),quote:{eligible:true,bookmaker:'Bet365',price:item.recommendation.verified_odds.bookmakers.Bet365.current_odds,observed_at:stamp,expires_at:expiry},status:'pending'});
 const sandbox={console,Date,Map,Number,URLSearchParams,AbortSignal,crypto:{randomUUID:()=> '12345678-1234-1234-1234-123456789012'},navigator:{clipboard:{writeText:async value=>{if(mode==='copy-denied')throw Error('denied');copied=value;}}},document:{getElementById:id=>ids.get(id),createElement:tag=>new Element(tag),querySelectorAll:()=>all(ids.get('coupon-candidates')).filter(e=>e.type==='checkbox')},fetch:async(path,options)=>{
  calls.push(path);
  if(mode==='request-failed')throw Error('timeout');
  if(mode==='automatic-list-failed' && path.startsWith('/api/predictions'))throw Error('candidate list unavailable');
  if(path.startsWith('/api/predictions'))return {ok:true,json:async()=>({items,generated_at:stamp,cache:{stale:false}})};
  if(path==='/api/telegram/verify'){assert.equal(options.headers.Authorization,'Bearer management-test-only');return {ok:true,json:async()=>({connection_verified:true,message:'Kimlik ve grup izinleri doğrulandı; mesaj gönderilmedi.',checked_at:stamp})};}
  if(path==='/api/telegram/status')return {ok:true,json:async()=>({configured:true,today:[{slot:new Date(now).toISOString(),status:'failed',error_code:'telegram_400'}]})};
  if(path.startsWith('/api/coupons?'))return {ok:true,json:async()=>({items:[...versions.values()].map(v=>({...v,selections:v.items.length}))})};
  if(path.startsWith('/api/coupons/automatic?') || path.startsWith('/api/coupons/daily?')){
   if(path.includes('/daily?'))assert(!options.method);
   if(dailyData)return {ok:true,json:async()=>structuredClone(dailyData)};
   assert(path.includes('strategy=balanced') && !path.includes('max_matches'));
   if(mode==='automatic-empty')return {ok:true,json:async()=>({items:[],message:'Tam eşleşen güncel oran bekleniyor.',data_updates:{analysis_generated_at:stamp,price_observed_at:stamp,checked_at:stamp}})};
   if(mode==='automatic-diagnostics'){const data=saved([asSaved(items[0])]);return {ok:true,json:async()=>({...data,coupon_date:'2026-10-15',strategy:'balanced',selection_mode:'dynamic',selection_policy:{risk_indicator:.4,risk_budget:1.6,rejections:{shared_team:2,combined_risk_budget:3}},diagnostics:{inspected_matches:20,eligible_matches:1,match_rejections:{quality_rejected:19},candidate_coverage_complete:true},data_updates:{checked_at:stamp}})};}
   const data=saved(items.filter((_,index)=>index%2===0).map(asSaved));versions.set(data.id,data);dailyData=data;return {ok:true,json:async()=>structuredClone(data)};
  }
  if(path.startsWith('/api/coupons')){
   if(!options.method)return {ok:true,json:async()=>structuredClone(versions.get(path.split('/').at(-1)))};
   assert(options.headers['X-Coupon-Key']);const selected=JSON.parse(options.body).selections.map(s=>asSaved(items.find(i=>i.match.id===s.match_id && i.recommendation.id===s.recommendation_id)));
   const data=saved(selected,'manual',++manualRevision);versions.set(data.id,data);return {ok:true,json:async()=>structuredClone(data)};
  }
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
 if(mode==='invalid'){
  assert.equal(inputs().length,0); // Unverified prices are hidden by default.
  ids.get('coupon-show-unpriced').checked=true;ids.get('coupon-show-unpriced').onchange();
  assert.equal(inputs().length,6);assert(inputs().every(e=>e.disabled));return;
 }
 if(mode==='automatic-diagnostics'){const daily=ids.get('coupon-daily').text();assert(daily.includes('Dinamik seçim'));assert(daily.includes('Uygun bulunan: 1'));assert(daily.includes('İncelenen toplam maç: 20'));assert(daily.includes('Kalite eşikleri karşılanmadı: 19'));assert(daily.includes('Model olasılığı: %70.0'));assert(daily.includes('Birleşik risk göstergesi: 0.400 / 1.6'));assert(daily.includes('Ortak takım nedeniyle bağımlılık riski: 2 aday'));assert(daily.includes('Birleşik risk bütçesi: 3 aday'));return;}
 if(mode==='automatic-empty'){assert(ids.get('coupon-daily').text().includes('Tam eşleşen'));assert(ids.get('coupon-daily').text().includes('Son analiz:'));assert(ids.get('coupon-daily').text().includes('Son fiyat gözlemi:'));return;}
 if(mode!=='automatic-list-failed')assert.equal(inputs().length,6);
 if(mode.startsWith('automatic')){
  // Navigating to Kuponum must populate qualified selections without any click.
  assert.equal(ids.get('coupon-selected').children.length,3);
  assert(ids.get('coupon-daily').text().includes('5,83'));
  assert(ids.get('telegram-schedule').text().includes('grup kimliğini'));
  await ids.get('telegram-verify').onclick();assert(ids.get('telegram-check-note').text().includes('yönetim erişim'));
  ids.get('telegram-admin-token').value='management-test-only';await ids.get('telegram-verify').onclick();assert(ids.get('telegram-check-note').text().includes('mesaj gönderilmedi'));assert(!calls.some(path=>path.endsWith('/telegram')));
  if(mode==='automatic-list-failed'){assert(ids.get('coupon-candidates').text().includes('alınamadı'));return;}
  assert(ids.get('coupon-total').text().includes('5,83'));
  await ids.get('coupon-automatic').onclick();assert(ids.get('coupon-total').text().includes('5,83'));
  assert.equal(ids.get('coupon-selected').children.length,3);
  ids.get('coupon-selected').children[0].children.at(-1).onclick();assert.equal(ids.get('coupon-selected').children.length,2);assert(ids.get('coupon-telegram').disabled);
  await ids.get('coupon-save').onclick();assert(ids.get('coupon-saved-note').text().includes('Sürüm 1'));
  ids.get('coupon-selected').children[0].children.at(-1).onclick();await ids.get('coupon-save').onclick();assert(ids.get('coupon-saved-note').text().includes('Sürüm 2'));
  assert.equal(versions.get('automatic-1').items.length,3);await ids.get('coupon-automatic').onclick();assert.equal(ids.get('coupon-selected').children.length,3);
  ids.get('coupon-manual').onclick();assert.equal(ids.get('coupon-selected').children.length,1);assert(ids.get('coupon-mode').text().includes('Manuel'));
  await sandbox.BetAppCoupon.refresh();assert.equal(ids.get('coupon-selected').children.length,1);assert(ids.get('coupon-daily').text().includes('5,83'));return;
 }
 inputs()[0].checked=true;const pending=inputs()[0].onchange();
 if(mode==='clear-race'){ids.get('coupon-clear').onclick();release();await pending;assert(ids.get('coupon-selected').text().includes('Henüz'));return;}
 await pending;
 if(mode==='server-rejected'){assert(ids.get('coupon-selected').text().includes('Henüz'));assert(ids.get('coupon-note').text().includes('bayat'));return;}
 assert(inputs()[1].disabled);inputs()[3].checked=true;await inputs()[3].onchange();
 assert(ids.get('coupon-total').text().includes('3,96'));assert(!inputs()[4].disabled); // No fixed distinct-match limit.
 await ids.get('coupon-copy').onclick();
 if(mode==='copy-denied'){assert(!copied);assert(ids.get('coupon-note').text().includes('Kopyalanamadı'));return;}
 assert(copied.includes('Bet365') && copied.includes('3,96') && copied.includes('Gözlem'));
 ids.get('coupon-clear').onclick();assert(ids.get('coupon-selected').text().includes('Henüz'));assert(ids.get('coupon-copy').disabled);
 assert(calls.includes('/api/coupon/quote') && calls.includes('/api/coupon/status'));
}
(async()=>{const modes=['populated','invalid','request-failed','server-rejected','copy-denied','clear-race','automatic','automatic-list-failed','automatic-empty','automatic-diagnostics'];for(const mode of modes)await scenario(mode);console.log(`coupon runtime: ${modes.length} scenarios passed`);})().catch(e=>{console.error(e);process.exitCode=1;});
