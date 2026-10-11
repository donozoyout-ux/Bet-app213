/* Verified quotes, in-memory draft, read-only settlement. */
(() => {
'use strict';
const $=id=>document.getElementById(id),selected=new Map();
const money=n=>n==null?'—':Number(n).toLocaleString('tr-TR',{minimumFractionDigits:2,maximumFractionDigits:2});
const date=s=>s?new Date(s).toLocaleString('tr-TR',{timeZone:'Europe/Istanbul'}):'Gözlem zamanı bilinmiyor';
const confidence={high:'Yüksek',medium:'Orta',low:'Düşük'};
const outcomes={won:'Kazandı',lost:'Kaybetti',pending:'Bekliyor',awaiting_data:'Final veri bekleniyor',void:'İptal / iade'};
const reasons={match_started:'Maç başladı',analysis_stale:'Analiz güncel değil',selection_unavailable:'Seçim artık güçlü tahminler arasında değil',odds_stale:'Oran bayat',no_verified_odds:'Bu market için doğrulanmış güncel oran yok'};
const text=(id,value)=>{if($(id))$(id).textContent=value;};
const node=(tag,value='')=>{const e=document.createElement(tag);e.textContent=value;return e;};
const td=(row,value)=>{const e=node('td',value);row.append(e);return e;};
const key=i=>i.match.id+':'+i.recommendation.id;
const book=()=>$('coupon-bookmaker')?.value || 'Bet365';
let busy=false,adding=false,copying=false,candidates=[],draftGeneration=0,pendingPick=null;
function valid(item){
 const r=item?.recommendation,m=item?.match,v=r?.verified_odds,b=v?.bookmakers?.[book()];
 const observed=Date.parse(b?.observed_at),expiry=Date.parse(b?.expires_at);
 const period=r?.market==='first_half_goals'?'first_half':r?.market==='second_half_goals'?'second_half':'full_time';
 const parts=r?.selection?.match(/^(over|under)_(\d+)_(\d+)$/),line=parts?Number(parts[2]+'.'+parts[3]):null;
 return r?.qualifies===true && m?.status==='scheduled' && Date.parse(m.kickoff_at)>Date.now() &&
 v?.has_odds===true && v.match_id===m.id && v.market===r.market && v.selection===r.selection && r.id===r.selection+'_'+r.market &&
 v.period===period && (line==null || v.line===line) &&
 b?.coupon_eligible===true && b.freshness==='fresh' && Number.isFinite(b.current_odds) && b.current_odds>1 &&
 Number.isFinite(observed) && observed<=Date.now() && Number.isFinite(expiry) && expiry>Date.now();
}
async function api(path,payload){
 const options={signal:AbortSignal.timeout(15000)};
 if(payload){options.method='POST';options.headers={'Content-Type':'application/json'};options.body=JSON.stringify(payload);}
 const rsp=await fetch(path,options);if(!rsp.ok)throw Error('HTTP '+rsp.status);return rsp.json();
}
const request=items=>({selections:items.map(item=>({match_id:item.match.id,recommendation_id:item.recommendation.id,bookmaker:item.quote?.bookmaker || book()}))});
function draw(){
 const box=$('coupon-selected');if(!box)return;box.replaceChildren();
 let total=1,complete=selected.size>0;
 if(!selected.size)box.append(node('p','Henüz seçim yapılmadı.'));
 for(const [id,item] of selected){
  const q=item.quote,r=item.recommendation,m=item.match;
  const started=m.status!=='scheduled' || Date.parse(m.kickoff_at)<=Date.now(),fresh=q?.eligible && Date.parse(q.expires_at)>Date.now();
  if(!fresh && !started)complete=false;
  if(q?.price>1)total*=q.price;else complete=false;
  const row=node('div','');row.append(node('p',`${m.home_team} — ${m.away_team} · ${r.label}`));
  row.append(node('p',`Model: %${(100*r.probability).toFixed(1)} · Güven: ${confidence[r.confidence] || '—'}`));
  row.append(node('p',`${q?.bookmaker || book()} · ${money(q?.price)} · Gözlem: ${date(q?.observed_at)}${started?' · Taslağa alınan maç öncesi fiyat':fresh?' · Güncel gözlem':' · Bayat / doğrulama bekleniyor'}`));
  row.append(node('p',item.statusError?'Sonuç bağlantısı bekleniyor':outcomes[item.status || 'pending']));
  const remove=node('button','Seçimi çıkar');remove.type='button';remove.onclick=()=>{selected.delete(id);draw();};row.append(remove);box.append(row);
 }
 text('coupon-total',complete?`Gözlenen fiyatlardan kombine oran: ${money(total)} · Fiyat veya kazanma garantisi değildir`:'Kombine oran: — (güncel doğrulama bekleniyor)');
 if($('coupon-copy'))$('coupon-copy').disabled=!complete || copying;
 document.querySelectorAll('[data-coupon-pick]').forEach(el=>{const picked=selected.has(el.dataset.couponPick) || pendingPick===el.dataset.couponPick;el.checked=picked;const item=candidates.find(i=>key(i)===el.dataset.couponPick);el.disabled=adding || !picked && (selected.size>=2 || !item || !valid(item) || [...selected.values()].some(x=>x.match.id===item.match.id));});
}
async function add(item,checked){
 const id=key(item);if(!checked){selected.delete(id);draw();return;}
 if(adding || selected.size>=2 || [...selected.values()].some(i=>i.match.id===item.match.id) || !valid(item)){draw();return;}
 adding=true;pendingPick=id;const generation=draftGeneration;text('coupon-note','Fiyat sunucuda doğrulanıyor…');draw();
 try{
  const data=await api('/api/coupon/quote',request([item]));const quote=data.items[0];
  if(generation!==draftGeneration)return;
  if(!quote?.eligible){text('coupon-note',reasons[quote?.reason] || 'Oran doğrulanamadı.');return;}
  selected.set(id,{...item,quote,status:'pending'});text('coupon-note','Fiyat sunucuda doğrulandı; taslağa eklendi.');
 }catch(_){text('coupon-note','Oran doğrulanamadı. Yeniden deneyin.');}
 finally{adding=false;pendingPick=null;draw();}
}
function render(items){
 const box=$('coupon-candidates');if(!box)return;box.replaceChildren();
 const perMatch=new Map();candidates=[];
 for(const item of items){
  if(!item.recommendation?.qualifies || (perMatch.get(item.match.id)||0)>=2)continue;
  perMatch.set(item.match.id,(perMatch.get(item.match.id)||0)+1);candidates.push(item);
  const r=item.recommendation,m=item.match,v=r.verified_odds,row=node('tr');
  const cb=node('input');cb.type='checkbox';cb.dataset.couponPick=key(item);cb.onchange=()=>add(item,cb.checked);cb.setAttribute('aria-label',`${m.home_team} ${m.away_team} ${r.label} taslağa ekle`);td(row,'').append(cb);
  td(row,`${m.home_team} — ${m.away_team} · ${date(m.kickoff_at)}`);td(row,r.label);
  td(row,`%${(100*r.probability).toFixed(1)}`);td(row,confidence[r.confidence] || '—');
  const prices=td(row,'');for(const name of ['Crown','Bet365','Sbobet']){const b=v?.bookmakers?.[name];prices.append(node('p',`${name}: ${money(b?.current_odds)} · ${b?.freshness==='fresh'?'Güncel gözlem':b?.current_odds?'Bayat':'Doğrulanmış oran yok'} · ${date(b?.observed_at)}`));}
  if(!valid(item))prices.append(node('p','Seçilen bookmaker için güncel doğrulanmış fiyat gerekli.'));
  box.append(row);
 }
 if(!candidates.length){const row=node('tr');td(row,'Güçlü tahmin bekleniyor. Sahte oran üretilmez.').colSpan=6;box.append(row);}
 draw();
}
async function updateSelected(){
 if(!selected.size)return;
 const items=[...selected.values()];
 try{
  const statuses=await api('/api/coupon/status',request(items));
  for(const status of statuses.items){const item=selected.get(status.match_id+':'+status.recommendation_id);if(item){item.status=status.status;item.match.status=status.match_status;item.statusError=false;}}
 }catch(_){for(const item of selected.values())item.statusError=true;}
 const upcoming=items.filter(i=>i.match.status==='scheduled' && Date.parse(i.match.kickoff_at)>Date.now());
 if(upcoming.length){
  try{const quotes=await api('/api/coupon/quote',request(upcoming));for(const q of quotes.items){const item=selected.get(q.match_id+':'+q.recommendation_id);if(item)item.quote={...item.quote,...q,price:q.price??item.quote.price,observed_at:q.observed_at??item.quote.observed_at,expires_at:q.expires_at??item.quote.expires_at};}}
  catch(_){for(const item of upcoming)item.quote.eligible=false;}
 }
 draw();
}
async function refresh(){
 if(busy)return;busy=true;text('coupon-note','Oranlar kontrol ediliyor…');$('coupon-candidates')?.setAttribute('aria-busy','true');
 try{
  const data=await api('/api/predictions/best?limit=100&offset=0&market=all&sort=ranking');
  render(data.items || []);text('coupon-note',`${candidates.length} güçlü tahmin · Analiz: ${date(data.generated_at)}${data.cache?.stale?' · Analiz bayat':''} · Her maçtan 1, en fazla 2 farklı maç`);
 }catch(_){const box=$('coupon-candidates');if(box){box.replaceChildren();const row=node('tr');td(row,'Fiyat listesi alınamadı. Oranları Yenile ile yeniden deneyin.').colSpan=6;box.append(row);}text('coupon-note','Bağlantı bekleniyor. Yeniden deneyin.');}
 finally{await updateSelected();busy=false;$('coupon-candidates')?.setAttribute('aria-busy','false');}
}
async function copy(){
 if(!selected.size || copying)return;copying=true;draw();
 try{
  await updateSelected();if($('coupon-total')?.textContent.startsWith('Kombine oran: —')){text('coupon-note','Bayat fiyatlar kopyalanamaz; oranları yenileyin.');return;}
  const lines=['BetApp213 · Kupon taslağı',`Oluşturulma: ${date(new Date().toISOString())}`];
  for(const item of selected.values()){const q=item.quote,r=item.recommendation,m=item.match;lines.push(`${m.home_team} — ${m.away_team} | ${r.label} | Model %${(100*r.probability).toFixed(1)} | Güven ${confidence[r.confidence]} | ${q.bookmaker} ${money(q.price)} | Gözlem ${date(q.observed_at)} | ${item.statusError?'Sonuç doğrulanamıyor':outcomes[item.status || 'pending']}`);}
  lines.push($('coupon-total').textContent,'Bilgilendirme amaçlı taslak; bahis oynanmadı, başarı garantisi yok.');
  if(!navigator.clipboard?.writeText)throw Error('Clipboard unavailable');
  await navigator.clipboard.writeText(lines.join('\n'));text('coupon-note','Taslak panoya kopyalandı.');
 }catch(_){text('coupon-note','Kopyalanamadı. Panoya erişim iznini kontrol edin.');}
 finally{copying=false;draw();}
}
if($('coupon-reload'))$('coupon-reload').onclick=refresh;
if($('coupon-clear'))$('coupon-clear').onclick=()=>{draftGeneration++;selected.clear();text('coupon-note','Taslak temizlendi.');draw();};
if($('coupon-copy'))$('coupon-copy').onclick=copy;
if($('coupon-bookmaker'))$('coupon-bookmaker').onchange=()=>{draftGeneration++;selected.clear();render(candidates);};
globalThis.BetAppCoupon=Object.freeze({refresh});draw();
})();
