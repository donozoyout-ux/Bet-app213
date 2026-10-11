/* Verified pre-match odds only; in-memory coupon draft, never places bets. */
(() => {
'use strict';
const $=id=>document.getElementById(id);
const selected=new Map();
const money=n=>Number(n).toLocaleString('tr-TR',{minimumFractionDigits:2,maximumFractionDigits:2});
const clean=el=>el.replaceChildren();
const td=(row,txt)=>{const e=document.createElement('td');e.textContent=String(txt);row.append(e);return e;};
const text=(id,value)=>{if($(id))$(id).textContent=value;};
let busy=false;
function valid(item){
 const r=item?.recommendation,m=item?.match,v=r?.verified_odds;
 const price=Number(v?.best_price);
 const kickoff=Date.parse(m?.kickoff_at);
 return r && m && v?.has_odds===true && Number.isFinite(price) && price>1 &&
   typeof v.best_bookmaker==='string' && ['Crown','Bet365','Sbobet'].includes(v.best_bookmaker) &&
   Number.isFinite(kickoff) && kickoff>Date.now() && r.qualifies!==false &&
   typeof r.id==='string' && typeof m.id==='number';
}
const key=i=>i.match.id+':'+i.recommendation.id;
function draw(){
 const box=$('coupon-selected');if(!box)return;clean(box);
 let total=1;
 if(!selected.size)box.textContent='Henüz seçim yapılmadı.';
 for(const item of selected.values()){
  const r=item.recommendation,m=item.match,v=r.verified_odds;
  total*=Number(v.best_price);
  const line=document.createElement('p');
  line.textContent=m.home_team+' — '+m.away_team+' · '+r.label+' · '+v.best_bookmaker+' '+money(v.best_price);
  box.append(line);
 }
 text('coupon-total',selected.size?('Gözlenen fiyatlardan hesaplanan kombine oran: '+money(total)+' · Garanti değil'):'Kombine oran: —');
 if($('coupon-copy'))$('coupon-copy').disabled=selected.size===0;
 document.querySelectorAll('[data-coupon-pick]').forEach(el=>{
  const picked=selected.has(el.dataset.couponPick);el.checked=picked;
  el.disabled=!picked && selected.size>=2;
 });
}
function render(items){
 const box=$('coupon-candidates');if(!box)return;clean(box);
 let shown=0;
 for(const item of items){
  if(!valid(item))continue;
  const k=key(item),r=item.recommendation,m=item.match,v=r.verified_odds;
  const row=document.createElement('tr');
  const slot=td(row,'');
  const cb=document.createElement('input');cb.type='checkbox';cb.dataset.couponPick=k;
  cb.setAttribute('aria-label',m.home_team+' '+m.away_team+' '+r.label+' taslağa ekle');
  cb.onchange=()=>{
   if(cb.checked){
    if(selected.size>=2 || [...selected.values()].some(x=>x.match.id===m.id)){cb.checked=false;return;}
    selected.set(k,item);
   }else selected.delete(k);
   draw();
  };
  slot.append(cb);
  td(row,m.home_team+' — '+m.away_team+' · '+new Date(m.kickoff_at).toLocaleString('tr-TR',{timeZone:'Europe/Istanbul'}));
  td(row,r.label);
  td(row,'%'+(Number(r.probability)*100).toFixed(1)+' (model)');
  td(row,v.best_bookmaker+' · '+money(v.best_price));
  box.append(row);shown++;
 }
 if(!shown){const tr=document.createElement('tr');const t=td(tr,'Şu anda bu sayfada doğrulanmış maç öncesi oranı bulunan seçili tahmin yok.');t.colSpan=5;box.append(tr);}
 text('coupon-note',shown+' doğrulanmış, yaklaşan tahmin listelendi · Her maçtan en fazla bir seçim · En fazla 2 seçim');
 draw();
}
async function refresh(){
 if(busy)return;busy=true;text('coupon-note','Oranlar kontrol ediliyor…');
 try {
  const rsp=await fetch('/api/predictions/best?limit=100&offset=0&market=all&sort=ranking',{signal:AbortSignal.timeout(20000)});
  if(!rsp.ok)throw Error('HTTP '+rsp.status);
  const data=await rsp.json();
  // Invalidate old prices on every refresh; do not keep stale selections.
  selected.clear();
  render(Array.isArray(data.items)?data.items:[]);
 }catch(e){text('coupon-note','Doğrulanmış fiyat listesi alınamadı. Yeniden deneyin.');selected.clear();draw();}
 finally{busy=false;}
}
function copy(){
 if(!selected.size)return;
 const lines=['BetApp213 · Oran doğrulamalı kupon taslağı','Oluşturulma: '+new Date().toLocaleString('tr-TR')];
 for(const item of selected.values()){const r=item.recommendation,m=item.match,v=r.verified_odds;lines.push(m.home_team+' — '+m.away_team+' | '+r.label+' | '+v.best_bookmaker+' '+money(v.best_price));}
 lines.push($('coupon-total')?.textContent||'','Bilgilendirme amaçlıdır; fiyatlar değişebilir, bahis yapılmadı.');
 const val=lines.join('\n');
 if(globalThis.navigator?.clipboard?.writeText){globalThis.navigator.clipboard.writeText(val).then(()=>text('coupon-note','Taslak panoya kopyalandı.'),()=>text('coupon-note','Kopyalama izni verilmedi.'));}else text('coupon-note','Tarayıcı panoya kopyalamayı desteklemiyor.');
}
if($('coupon-reload'))$('coupon-reload').onclick=refresh;
if($('coupon-clear'))$('coupon-clear').onclick=()=>{selected.clear();draw();};
if($('coupon-copy'))$('coupon-copy').onclick=copy;
globalThis.BetAppCoupon=Object.freeze({refresh});
draw();
})();
