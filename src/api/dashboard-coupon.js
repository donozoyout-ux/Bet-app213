/* Verified quotes, in-memory draft, read-only settlement. */
(() => {
'use strict';
const $=id=>document.getElementById(id),selected=new Map();
const money=n=>n==null?'—':!Number.isFinite(Number(n))?String(n):Number(n).toLocaleString('tr-TR',{minimumFractionDigits:2,maximumFractionDigits:2});
const date=s=>s?new Date(s).toLocaleString('tr-TR',{timeZone:'Europe/Istanbul'}):'Gözlem zamanı bilinmiyor';
const confidence={high:'Yüksek',medium:'Orta',low:'Düşük'};
const outcomes={won:'Kazandı',lost:'Kaybetti',pending:'Bekliyor',awaiting_data:'Final veri bekleniyor',void:'İptal / iade'};
const reasons={outside_manual_window:'Maç yedi günlük pencerenin dışında',match_started:'Maç başladı',analysis_stale:'Analiz güncel değil',selection_unavailable:'Seçim artık güçlü tahminler arasında değil',odds_stale:'Oran bayat',no_verified_odds:'Bu market için doğrulanmış güncel oran yok'};
const text=(id,value)=>{if($(id))$(id).textContent=value;};
const node=(tag,value='')=>{const e=document.createElement(tag);e.textContent=value;return e;};
const td=(row,value)=>{const e=node('td',value);row.append(e);return e;};
const key=i=>i.match.id+':'+i.recommendation.id;
const kickoff=i=>Date.parse(i?.match?.kickoff_at);
const inWeek=i=>{const t=kickoff(i),now=Date.now();return Number.isFinite(t)&&t>now&&t<=now+7*24*60*60*1000&&i?.match?.status==='scheduled';};
const byKickoff=(a,b)=>kickoff(a)-kickoff(b)||String(a?.match?.id).localeCompare(String(b?.match?.id))||String(a?.recommendation?.id).localeCompare(String(b?.recommendation?.id));
const book=()=>$('coupon-bookmaker')?.value || 'Bet365';
let busy=false,adding=false,copying=false,candidates=[],draftGeneration=0,pendingPick=null,saved=null,ownerKey=null,dirty=false,sharing=false,historyOffset=0,manualDraft=null,verifying=false;
const draftKey='betapp-coupon-draft-v1';
function persist(){
 if(saved?.kind==='automatic' && !dirty)return;
 try{globalThis.localStorage?.setItem(draftKey,JSON.stringify({items:[...selected.values()],saved,dirty}));}catch(_){}
}
function restore(){
 try{const data=JSON.parse(globalThis.localStorage?.getItem(draftKey) || 'null');
  const day=s=>new Date(s).toLocaleDateString('en-CA',{timeZone:'Europe/Istanbul'});
  if(data?.saved?.kind==='automatic' && !data.dirty && day(data.saved.created_at)!==day(Date.now()))return;
  if(data && Array.isArray(data.items)){for(const item of data.items)if(item?.match?.id && item?.recommendation?.id)selected.set(key(item),item);saved=data.saved || null;dirty=!!data.dirty;if(dirty || saved?.kind==='manual')manualDraft={items:[...selected.values()],saved,dirty};}
 }catch(_){}
}
function valid(item){
 const r=item?.recommendation,m=item?.match,v=r?.verified_odds,b=v?.bookmakers?.[book()];
 const observed=Date.parse(b?.observed_at),expiry=Date.parse(b?.expires_at);
 const period=r?.market==='first_half_goals'?'first_half':r?.market==='second_half_goals'?'second_half':'full_time';
 const parts=r?.selection?.match(/^(over|under)_(\d+)_(\d+)$/),line=parts?Number(parts[2]+'.'+parts[3]):null;
 return r?.qualifies===true && r.probability>=.70 && r.reliability>=.80 && r.score>=.80 && m?.status==='scheduled' && Date.parse(m.kickoff_at)>Date.now() &&
 v?.has_odds===true && v.match_id===m.id && v.market===r.market && v.selection===r.selection && r.id===r.selection+'_'+r.market &&
 v.period===period && (line==null || v.line===line) &&
 b?.coupon_eligible===true && b.freshness==='fresh' && Number.isFinite(b.current_odds) && b.current_odds>1 &&
 Number.isFinite(observed) && observed<=Date.now() && Number.isFinite(expiry) && expiry>Date.now();
}
async function api(path,payload){
 const options={signal:AbortSignal.timeout(15000)};
 if(path.startsWith('/api/coupons')){
  if(!ownerKey){try{ownerKey=globalThis.localStorage?.getItem('betapp-coupon-key');if(!ownerKey){ownerKey=makeOwner();globalThis.localStorage?.setItem('betapp-coupon-key',ownerKey);}}catch(_){ownerKey=makeOwner();}}
  if(ownerKey)options.headers={'X-Coupon-Key':ownerKey};
 }
 if(payload){options.method='POST';options.headers={...options.headers,'Content-Type':'application/json'};options.body=JSON.stringify(payload);}
 const rsp=await fetch(path,options);if(!rsp.ok)throw Error('HTTP '+rsp.status);return rsp.json();
}
function makeOwner(){
 if(globalThis.crypto?.randomUUID)return globalThis.crypto.randomUUID();
 if(!globalThis.crypto?.getRandomValues)return null;
 const bytes=new Uint8Array(16);globalThis.crypto.getRandomValues(bytes);return [...bytes].map(b=>b.toString(16).padStart(2,'0')).join('');
}
const request=items=>({selections:items.map(item=>({match_id:item.match.id,recommendation_id:item.recommendation.id,bookmaker:item.quote?.bookmaker || book()}))});
function draw(){
 persist();
 const box=$('coupon-selected');if(!box)return;box.replaceChildren();
 let total=1,logTotal=0,complete=selected.size>0;
 text('coupon-mode',dirty || saved?.kind==='manual'?'Manuel kupon · Günlük otomatik kupondan ayrı':'Günlük otomatik kupon · Toplam oran üst sınırı yok');
 if(saved)text('coupon-saved-note',`Kayıt: ${date(saved.created_at)} · Sürüm ${saved.revision} · ${saved.kind==='automatic'?'Otomatik':'Düzenlenmiş'}`);
 if(!selected.size)box.append(node('p','Henüz seçim yapılmadı.'));
 for(const [id,item] of [...selected.entries()].sort((a,b)=>byKickoff(a[1],b[1]))){
  const q=item.quote,r=item.recommendation,m=item.match;
  const started=m.status!=='scheduled' || Date.parse(m.kickoff_at)<=Date.now(),fresh=q?.eligible && Date.parse(q.expires_at)>Date.now();
  if(!fresh && !started)complete=false;
  if(q?.price>1){total*=q.price;logTotal+=Math.log10(q.price);}else complete=false;
  const row=node('div','');row.append(node('p',`${date(m.kickoff_at)} · ${m.home_team} — ${m.away_team} · ${r.label}`));
  row.append(node('p',`Model: %${(100*r.probability).toFixed(1)} · Güven: ${confidence[r.confidence] || '—'}`));
  row.append(node('p',`${q?.bookmaker || book()} · ${money(q?.price)} · Gözlem: ${date(q?.observed_at)}${started?' · Taslağa alınan maç öncesi fiyat':fresh?' · Güncel gözlem':' · Bayat / doğrulama bekleniyor'}`));
  row.append(node('p',item.statusError?'Sonuç bağlantısı bekleniyor':outcomes[item.status || 'pending']));
  if(m.score || m.half_time_score)row.append(node('p',`Maç sonucu: ${m.score || '—'} · İlk yarı: ${m.half_time_score || '—'}`));
  const remove=node('button','Seçimi çıkar');remove.type='button';remove.onclick=()=>{draftGeneration++;selected.delete(id);dirty=true;draw();};row.append(remove);box.append(row);
 }
 text('coupon-total',complete?`Gözlenen fiyatlardan kombine oran: ${Number.isFinite(total)?money(total):(10**(logTotal-Math.floor(logTotal))).toFixed(4)+'e+'+Math.floor(logTotal)} · Fiyat veya kazanma garantisi değildir`:'Kombine oran: — (güncel doğrulama bekleniyor)');
 if(complete && new Set([...selected.values()].map(i=>i.quote.bookmaker)).size>1)text('coupon-total',$('coupon-total').textContent+' · Farklı kaynakların taslak çarpımı; tek bookmaker teklifi değildir.');
 if($('coupon-copy'))$('coupon-copy').disabled=!complete || copying;
 if($('coupon-telegram'))$('coupon-telegram').disabled=!saved?.id || dirty || sharing;
 document.querySelectorAll('[data-coupon-pick]').forEach(el=>{const picked=selected.has(el.dataset.couponPick) || pendingPick===el.dataset.couponPick;el.checked=picked;const item=candidates.find(i=>key(i)===el.dataset.couponPick);el.disabled=adding || !picked && (!item || !valid(item) || [...selected.values()].some(x=>x.match.id===item.match.id));});
}
async function add(item,checked){
 const id=key(item);if(!checked){draftGeneration++;selected.delete(id);dirty=true;draw();return;}
 if(adding || [...selected.values()].some(i=>i.match.id===item.match.id) || !valid(item)){draw();return;}
 adding=true;pendingPick=id;const generation=draftGeneration;text('coupon-note','Fiyat sunucuda doğrulanıyor…');draw();
 try{
  const data=await api('/api/coupon/quote',request([item]));const quote=data.items[0];
  if(generation!==draftGeneration)return;
  if(!quote?.eligible){text('coupon-note',reasons[quote?.reason] || 'Oran doğrulanamadı.');return;}
  selected.set(id,{...item,quote,status:'pending'});dirty=true;text('coupon-note','Fiyat sunucuda doğrulandı; taslağa eklendi.');
 }catch(_){text('coupon-note','Oran doğrulanamadı. Yeniden deneyin.');}
 finally{adding=false;pendingPick=null;draw();}
}
function render(items){
 const box=$('coupon-candidates');if(!box)return;box.replaceChildren();
 const perMatch=new Map();candidates=[];let eligibleCount=0;
 // Prefer a genuinely priced prediction within each fixture; ranking alone can hide it.
 for(const item of items.filter(inWeek).filter(i=>i.recommendation?.qualifies).sort(byKickoff)){
  const id=item.match.id, prior=perMatch.get(id) || [];
  if(prior.length<2){prior.push(item);perMatch.set(id,prior);}
 }
 for(const item of [...perMatch.values()].flat().sort((a,b)=>Number(valid(b))-Number(valid(a)) || byKickoff(a,b))){
  candidates.push(item);
  if(!valid(item) && !$('coupon-show-unpriced')?.checked)continue;
  const r=item.recommendation,m=item.match,v=r.verified_odds,row=node('tr'),selectable=valid(item);
  if(selectable)eligibleCount++;
  row.classList.toggle('coupon-unpriced',!selectable);
  const cb=node('input');cb.type='checkbox';cb.dataset.couponPick=key(item);cb.onchange=()=>add(item,cb.checked);cb.setAttribute('aria-label',`${m.home_team} ${m.away_team} ${r.label} taslağa ekle`);td(row,'').append(cb);
  td(row,`${m.home_team} — ${m.away_team} · ${date(m.kickoff_at)}`);td(row,r.label);
  td(row,`%${(100*r.probability).toFixed(1)}`);td(row,confidence[r.confidence] || '—');
  const prices=td(row,'');const quote=v?.bookmakers?.[book()];
  if(selectable)prices.append(node('strong',`${book()} · ${money(quote.current_odds)} · Güncel`));
  else prices.append(node('span','Oran bekleniyor · Henüz seçilemez'));
  if(!selectable)row.title='Bu tahmin için seçilen bahis şirketinde güncel ve doğrulanmış fiyat yok.';
  box.append(row);
 }
 if(!candidates.length){const row=node('tr');td(row,'Önümüzdeki 7 günde uygun, güçlü ve doğrulanmış tahmin bulunamadı.').colSpan=6;box.append(row);}
 text('coupon-window-count',`${eligibleCount} seçilebilir / ${candidates.length} tahmin · Yakın tarihten uzağa · En fazla 7 gün.`);
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
 if(busy)return;busy=true;$('coupon-candidates')?.setAttribute('aria-busy','true');
 // Daily generation is independent of the optional manual candidate list.
 if($('coupon-automatic'))await generate(false);
 if($('coupon-tools') && !$('coupon-tools').open){busy=false;$('coupon-candidates')?.setAttribute('aria-busy','false');return;}
 try{
  let items=[],offset=0,data;do{data=await api('/api/predictions/best?limit=100&offset='+offset+'&market=all&sort=ranking');items.push(...(data.items || []));offset+=100;}while(data.items?.length===100);
  render(items);
 }catch(_){const box=$('coupon-candidates');if(box){box.replaceChildren();const row=node('tr');td(row,'Fiyat listesi alınamadı. Oranları Yenile ile yeniden deneyin.').colSpan=6;box.append(row);}if(!$('coupon-automatic'))text('coupon-note','Bağlantı bekleniyor. Yeniden deneyin.');}
 finally{await updateSelected();await history();await management();busy=false;$('coupon-candidates')?.setAttribute('aria-busy','false');}
}
async function copy(){
 if(!selected.size || copying)return;copying=true;draw();
 try{
  await updateSelected();if($('coupon-total')?.textContent.startsWith('Kombine oran: —')){text('coupon-note','Bayat fiyatlar kopyalanamaz; oranları yenileyin.');return;}
  const lines=['Kuponanalizii · Kupon taslağı',`Oluşturulma: ${date(new Date().toISOString())}`];
  for(const item of selected.values()){const q=item.quote,r=item.recommendation,m=item.match;lines.push(`${m.home_team} — ${m.away_team} | ${r.label} | Model %${(100*r.probability).toFixed(1)} | Güven ${confidence[r.confidence]} | ${q.bookmaker} ${money(q.price)} | Gözlem ${date(q.observed_at)} | ${item.statusError?'Sonuç doğrulanamıyor':outcomes[item.status || 'pending']}`);}
  lines.push($('coupon-total').textContent,'Bilgilendirme amaçlı taslak; bahis oynanmadı, başarı garantisi yok.');
  if(!navigator.clipboard?.writeText)throw Error('Clipboard unavailable');
  await navigator.clipboard.writeText(lines.join('\n'));text('coupon-note','Taslak panoya kopyalandı.');
 }catch(_){text('coupon-note','Kopyalanamadı. Panoya erişim iznini kontrol edin.');}
 finally{copying=false;draw();}
}
if($('coupon-reload'))$('coupon-reload').onclick=refresh;
if($('coupon-clear'))$('coupon-clear').onclick=()=>{draftGeneration++;selected.clear();saved=null;dirty=true;text('coupon-saved-note','');text('coupon-note','Taslak temizlendi.');draw();};
if($('coupon-copy'))$('coupon-copy').onclick=copy;
if($('coupon-bookmaker'))$('coupon-bookmaker').onchange=()=>render(candidates);
if($('coupon-show-unpriced'))$('coupon-show-unpriced').onchange=()=>render(candidates);

function loadCoupon(data){
 draftGeneration++;selected.clear();saved=data;dirty=false;
 for(const item of data.items || [])selected.set(key(item),item);
 draw();text('coupon-note',data.items?.length?'Kupon yüklendi; çıkarabilir veya uygun başka seçim ekleyebilirsiniz.':'Güncel, güçlü ve doğrulanmış seçim bulunamadı.');
}
function renderDaily(data){
 const box=$('coupon-daily');if(!box)return;
 box.replaceChildren();
 const diagnostics=data.diagnostics || {}, updates=data.data_updates || {};
 const summary=node('div');summary.className='daily-coupon-summary';
 summary.append(node('strong',`Strateji: ${{balanced:'Dengeli',cautious:'Temkinli',value:'Değer odaklı'}[data.strategy || 'balanced']} · Hedef: ${data.target_matches ?? 4} maç`));
 summary.append(node('span',`Uygun bulunan: ${diagnostics.eligible_matches ?? '—'} · Seçilen: ${data.items?.length ?? 0} · İncelenen toplam maç: ${diagnostics.inspected_matches ?? '—'}`));
 box.append(summary);
 const list=node('div');list.className='daily-selections';
 for(const item of data.items || []){
  const card=node('article'),m=item.match,r=item.recommendation,q=item.quote;
  card.className='daily-selection';
  card.append(node('p',`${date(m.kickoff_at)} · ${m.league || m.league_name || 'Lig bilgisi bekleniyor'}`));
  card.append(node('h3',`${m.home_team} — ${m.away_team}`));
  const price=node('div');price.className='daily-selection-price';price.append(node('strong',r.label),node('b',`${money(q.price)} · ${q.bookmaker}`));card.append(price);
  card.append(node('p',`Model olasılığı: %${(100*r.probability).toFixed(1)} · Güven: ${confidence[r.confidence] || '—'} · Güvenilirlik: %${(100*r.reliability).toFixed(1)}`));
  card.append(node('p',`Son fiyat gözlemi: ${date(q.observed_at)}`));list.append(card);
 }
 box.append(list);
 box.append(node('p',data.items?.length?`Günlük kombine oran: ${money(data.combined_odds)} · Üst sınır yok`:data.message || 'Bugün doğrulanmış uygun seçim yok.'));
 const labels={analysis_missing:'Analiz eksik / yeni analiz bekleniyor',analysis_stale:'Analiz güncel değil',insufficient_prematch_data:'Yeterli maç öncesi örneklem yok',quality_rejected:'Kalite eşikleri karşılanmadı',price_rejected:'Taze ve tam eşleşen oran yok',match_started_or_unavailable:'Maç başladı veya uygun durumda değil'};
 const reasons=node('div');reasons.className='daily-rejections';
 for(const [reason,count] of Object.entries(diagnostics.match_rejections || {}))reasons.append(node('span',`${labels[reason] || reason}: ${count}`));
 box.append(reasons);
 if(diagnostics.candidate_coverage_complete===false)box.append(node('p','Eski analiz kaydında tüm market adayları saklanmamış. Tam kapsam, worker yeni analizi yayımladığında doğrulanabilir.'));
 box.append(node('p',`Son analiz: ${date(updates.analysis_generated_at || data.cache?.latest_generated_at || data.cache?.generated_at)} · Son fiyat gözlemi: ${date(updates.price_observed_at || diagnostics.last_price_observed_at)} · Son güncelleme: ${date(updates.checked_at || diagnostics.checked_at)}`));
}
async function generate(activate=true){
 const generation=draftGeneration;
 const button=$('coupon-automatic');if(button)button.disabled=true;
 try{const strategy=$('coupon-strategy')?.value || 'balanced';const count=$('coupon-target')?.value || '4';const data=await api('/api/coupons/automatic?'+new URLSearchParams({strategy,max_matches:count}),{});if(generation!==draftGeneration)return;text('coupon-day',data.coupon_date || new Date().toLocaleDateString('tr-TR',{timeZone:'Europe/Istanbul'}));renderDaily(data);
  if(data.items?.length && (activate || !dirty && saved?.kind!=='manual')){if(activate && (dirty || saved?.kind==='manual'))manualDraft={items:[...selected.values()],saved,dirty};loadCoupon(data);if($('coupon-tools') && !$('coupon-tools').open)text('coupon-note',`${data.items.length} maç seçildi · Hedef ${data.target_matches ?? 4} · Son kontrol: ${date(data.data_updates?.checked_at)}`);}
  else if(!data.items?.length){if(!dirty && saved?.kind==='automatic'){selected.clear();saved=null;text('coupon-saved-note','');draw();}text('coupon-note',data.message || 'Bugün kalite eşiğini geçen ve güncel oranı doğrulanmış seçim bulunamadı; manuel taslak korunuyor.');}historyOffset=0;if(!$('coupon-tools') || $('coupon-tools').open)await history();}
 catch(_){text('coupon-day','Tarih doğrulanamadı');text('coupon-note','Otomatik kupon oluşturulamadı. Yeniden deneyin.');text('coupon-daily','Günlük kupon bağlantısı bekleniyor. Yeniden deneyin.');}
 finally{if(button)button.disabled=false;}
}
async function save(){
 if(!selected.size){text('coupon-note','Boş kupon kaydedilmez.');return;}
 try{
  const path=saved?.kind==='manual'?'/api/coupons/'+saved.id:'/api/coupons';
  // Immutable versions: updates create the next revision.
  const payload=request([...selected.values()]);
  const options={signal:AbortSignal.timeout(15000),method:saved?.kind==='manual'?'PUT':'POST',headers:{'Content-Type':'application/json','X-Coupon-Key':ownerKey || ''},body:JSON.stringify(payload)};
  if(!ownerKey){await history();options.headers['X-Coupon-Key']=ownerKey || '';}
  const r=await fetch(path,options);if(!r.ok)throw Error('save');loadCoupon(await r.json());text('coupon-note','Kupon PostgreSQL kaydına alındı.');historyOffset=0;await history();
 }catch(_){text('coupon-note','Kaydedilemedi. Oranları ve kupon sürümünü yenileyin.');}
}
async function history(){
 const box=$('coupon-history');if(!box)return;
 try{const data=await api('/api/coupons?limit=10&offset='+historyOffset);box.replaceChildren();
  if($('coupon-history-previous'))$('coupon-history-previous').disabled=historyOffset===0;
  if($('coupon-history-next'))$('coupon-history-next').disabled=data.items.length<10;
  for(const item of data.items){const button=node('button',`${date(item.created_at)} · ${item.selections} maç · ${money(item.combined_odds)} · Sürüm ${item.revision}`);button.type='button';button.onclick=async()=>{try{loadCoupon(await api('/api/coupons/'+item.id));}catch(_){text('coupon-note','Kayıt açılamadı.');}};box.append(button);}
  if(!data.items.length)box.append(node('p','Henüz kaydedilmiş kupon yok.'));
 }catch(_){box.replaceChildren();box.append(node('p','Kupon geçmişi bağlantı bekliyor.'));}
}
async function management(){
 const labels={sent:'Gönderildi',sending:'Gönderim başladı; tekrar gönderilmez',uncertain:'Sonuç belirsiz; grubu kontrol edin, otomatik tekrar yok',failed:'Telegram reddetti; bot/grup ayarlarını kontrol edin',missed:'Gönderim penceresi kaçırıldı',pending:'Uygun kupon bekleniyor',not_due:'Gönderim saati bekleniyor',no_eligible_selections:'Kaliteli ve taze fiyatlı seçim yok',waiting_for_quotes:'Taze oran bekleniyor',duplicate_selections:'Aynı seçimler tekrar gönderilmez',duplicate_version:'Bu sürüm daha önce işlendi'};
 try{const data=await api('/api/telegram/status');text('telegram-check-note',(data.connection?.message || 'Bağlantı henüz doğrulanmadı.')+(data.connection?.checked_at?' · Kontrol: '+date(data.connection.checked_at):''));text('telegram-status',data.connection_verified?'Telegram kimliği ve grup izinleri doğrulandı.':data.configured?'Telegram ayarları mevcut; bağlantı sonucu aşağıda.':data.configuration_status==='invalid'?'Telegram ayarlarının biçimi geçersiz; sunucu yapılandırmasını kontrol edin.':'Telegram yapılandırılmamış. Kupon sistemi kullanılabilir.');text('telegram-schedule','Türkiye saati 12:00 ve 18:00 · Günlük en fazla 2 otomatik gönderim. '+(data.today || []).map(s=>date(s.slot)+' · '+(s.error_code==='telegram_400'?'Telegram isteği geçersiz: grup kimliğini ve botun gruba eklenmesini kontrol edin':s.error_code==='telegram_chat_not_found'?'Telegram hedef grubu bulamıyor: grup kimliğini kontrol edin':s.error_code==='telegram_missing_permissions'?'Botun mesaj gönderme izni yetersiz':s.error_code==='telegram_message_too_long'?'Telegram mesajı boyut sınırını aştı':s.error_code==='telegram_chat_migrated'?'Telegram grup kimliği değişmiş olabilir':s.error_code==='telegram_bot_removed'?'Bot gruptan çıkarılmış':s.error_code==='telegram_bot_forbidden'||s.error_code==='telegram_403'?'Botun grupta gönderim izni yok; grup izinlerini kontrol edin':labels[s.status] || 'Gönderim durumu doğrulanamıyor')).join(' • '));}
 catch(_){text('telegram-status','Telegram yönetim bilgisi alınamadı.');}
}
async function verifyConnection(){
 if(verifying)return;
 const token=$('telegram-admin-token')?.value;
 if(!token){text('telegram-check-note','Salt okunur kontrol için sunucu yönetim erişim anahtarı gerekli. Bot tokenını bu alana yazmayın.');return;}
 verifying=true;if($('telegram-verify'))$('telegram-verify').disabled=true;text('telegram-check-note','Bot kimliği ve grup izinleri kontrol ediliyor; mesaj gönderilmez…');
 try{const r=await fetch('/api/telegram/verify',{method:'POST',headers:{Authorization:'Bearer '+token},signal:AbortSignal.timeout(15000)});if(!r.ok){text('telegram-check-note',r.status===401?'Yönetim erişim anahtarı kabul edilmedi.':'Bağlantı kontrolü yapılamadı; sunucu ayarlarını kontrol edin.');return;}const data=await r.json();text('telegram-status',data.connection_verified?'Telegram kimliği ve grup izinleri doğrulandı.':'Telegram bağlantısı doğrulanamadı; yardım metnini izleyin.');text('telegram-check-note',(data.message || 'Bağlantı durumu doğrulanamadı.')+(data.checked_at?' · Kontrol: '+date(data.checked_at):''));}
 catch(_){text('telegram-check-note','Bağlantı kontrolü tamamlanamadı. Mesaj gönderilmedi; daha sonra yeniden kontrol edin.');}
 finally{verifying=false;if($('telegram-verify'))$('telegram-verify').disabled=false;}
}
async function share(){
 if(sharing)return;
 if(!saved?.id || dirty){text('coupon-note','Önce düzenlenmiş kuponu kaydedin.');return;}
 const token=$('telegram-admin-token')?.value;
 if(!token){text('coupon-note','Manuel grup paylaşımı için yönetim erişim anahtarı gerekli.');return;}
 sharing=true;draw();
 try{const r=await fetch('/api/coupons/'+saved.id+'/telegram',{method:'POST',headers:{Authorization:'Bearer '+token,'X-Coupon-Key':ownerKey || ''},signal:AbortSignal.timeout(30000)});if(!r.ok)throw Error('share');const data=await r.json();const labels={sent:'Telegram grubuna gönderildi.',disabled:'Telegram ayarları eksik.',duplicate_version:'Bu sürüm daha önce işlendi; tekrar gönderilmedi.',uncertain:'Gönderim sonucu belirsiz. Çift mesajı önlemek için otomatik tekrar yapılmaz.',failed:'Telegram isteği reddetti; yönetim ayarlarını kontrol edin.',quotes_changed:'Oranlar değişti veya bayatladı; kuponu yeniden kaydedin.'};text('coupon-note',labels[data.status] || 'Gönderim durumu: '+data.status);await management();}
 catch(_){text('coupon-note','Paylaşım sonucu doğrulanamadı. Yönetim durumunu kontrol edin; kör tekrar yapmayın.');}
 finally{sharing=false;draw();}
}
if($('coupon-automatic'))$('coupon-automatic').onclick=()=>generate(true);
if($('coupon-manual'))$('coupon-manual').onclick=()=>{draftGeneration++;if(dirty || saved?.kind==='manual')manualDraft={items:[...selected.values()],saved,dirty};selected.clear();saved=manualDraft?.saved || null;for(const item of manualDraft?.items || [])selected.set(key(item),item);dirty=manualDraft?.dirty ?? true;text('coupon-saved-note','');text('coupon-note','Manuel kupon ayrı kaydedilir; günlük otomatik seçimler yukarıda korunur.');if($('coupon-edit-panel'))$('coupon-edit-panel').open=true;draw();};
if($('coupon-save'))$('coupon-save').onclick=save;
if($('coupon-history-previous'))$('coupon-history-previous').onclick=()=>{historyOffset=Math.max(0,historyOffset-10);history();};
if($('coupon-history-next'))$('coupon-history-next').onclick=()=>{historyOffset+=10;history();};
if($('coupon-history-refresh'))$('coupon-history-refresh').onclick=history;
if($('coupon-telegram'))$('coupon-telegram').onclick=share;
if($('telegram-verify'))$('telegram-verify').onclick=verifyConnection;
if($('coupon-tools'))$('coupon-tools').ontoggle=()=>{if($('coupon-tools').open)refresh();};
globalThis.BetAppCoupon=Object.freeze({refresh});restore();draw();
})();
