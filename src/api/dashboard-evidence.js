/* Shared read-only cohort evidence. Fixtures are never forecasts or outcomes. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id), node=(tag,text='',cls='')=>{const e=document.createElement(tag);e.textContent=text;e.className=cls;return e;};
  const hosts=['home-results-coverage','trend-coverage','learning-chart-coverage'];
  let data=null,pending=null,received=0;
  const count=v=>Number.isInteger(v)&&v>=0;
  function validateTrends(d){
    const keys=['total','decided','won','lost','pending','awaiting_data','disputed','void'];
    const rate=v=>v===null||(Number.isFinite(v)&&v>=0&&v<=1);
    const row=r=>r&&keys.every(k=>count(r[k]))&&r.decided===r.won+r.lost&&r.decided<=r.total
      &&rate(r.success_rate)&&rate(r.chart_rate)&&(r.decided?Number.isFinite(r.success_rate)&&Math.abs(r.success_rate-r.won/r.decided)<1e-10:r.success_rate===null)
      &&Number.isFinite(r.effective_matches)&&r.effective_matches>=0;
    if(!d || !Array.isArray(d.model_versions)||!d.model_versions.every(v=>typeof v==='string')||!Array.isArray(d.daily)||d.daily.length<30||d.daily.length>60||!Array.isArray(d.weekly)||!Array.isArray(d.comparisons)
      || !d.daily.every(r=>row(r)&&/^\d{4}-\d{2}-\d{2}$/.test(r.date))
      || !d.weekly.every(row)||![7,30].every(n=>d.comparisons.some(c=>c.days===n&&row(c.recent)&&row(c.previous))))throw new Error('Invalid outcome cohort');
    if(d.daily.some(r=>r.chart_rate!=null&&(!Number.isFinite(r.chart_rate)||r.chart_rate<0||r.chart_rate>1||!r.decided)))throw new Error('Invalid outcome rate');
    return d;
  }
  function stamp(value,stale=false){
    const age=Date.now()-Date.parse(value);
    return !Number.isFinite(age)||age< -60000?['Zaman doğrulanamadı','unknown']:stale||age>1800000?['Eski veri kontrolü','stale']:['Güncel veri kontrolü','fresh'];
  }
  function badge(parent,value,stale=false){const [text,state]=stamp(value,stale);const e=node('span',text,'evidence-badge');e.dataset.state=state;parent.append(e);}
  function render(d){
    if(!d?.matches||!d?.outcomes||!d?.snapshots||!count(d.matches.total)||!count(d.outcomes.saved_picks)||!count(d.outcomes.settled)||!count(d.outcomes.pending))throw new Error('Invalid diagnostic counts');
    for(const id of hosts){const box=$(id);if(!box)continue;box.replaceChildren();badge(box,d.checked_at);
      box.append(node('p',`Tüm arşiv · ${d.matches.total.toLocaleString('tr-TR')} maç kaydı · ${d.truncated?'En fazla '+d.sample_limit+' anlık görüntüde: ':''}${d.snapshots.prematch} maç öncesi kayıt · ${d.outcomes.saved_picks} benzersiz kayıtlı seçim · ${d.outcomes.settled} doğrulanmış sonuç`));
      const details=node('details');details.append(node('summary','Bekleyen seçimler ve kapsam ayrıntıları'));
      details.append(node('p',`${d.outcomes.pending} bekleyen · ${d.outcomes.awaiting_data} sonuç verisi eksik · ${d.outcomes.disputed} incelenmeli · ${d.outcomes.void} geçersiz. Bugün ve gelecekteki maçlara ait ${d.today_and_future_picks} seçim, tamamlanmış günler grafiğine dahil değildir.`,'muted'));
      if(!d.outcomes.settled)details.append(node('p','Maç arşivinin büyümesi, geçmişe ait maç öncesi tahmin üretmez. Kayıtlı seçimlerin maçları tamamlandıkça doğrulanmış sonuçlar birikir.'));
      if(d.truncated)details.append(node('p','İnceleme sınırı: bunlar tüm geçmişin toplamı değildir.','muted'));
      const at=new Date(d.checked_at);details.append(node('small',Number.isFinite(at.getTime())?'Son kontrol: '+at.toLocaleString('tr-TR',{timeZone:'Europe/Istanbul'}):'Kontrol zamanı doğrulanamadı'));box.append(details);
    }
  }
  async function refresh(){
    if(data&&Date.now()-received<60000){render(data);return;}
    if(pending)return pending;
    pending=(async()=>{try{const r=await fetch('/api/chart-diagnostics',{signal:AbortSignal.timeout(8000)});if(!r.ok)throw new Error('Diagnostics unavailable');const d=await r.json();render(d);data=d;received=Date.now();}
      catch(_){data=null;for(const id of hosts){const box=$(id);if(box){box.replaceChildren(node('span','Kapsam kontrolü alınamadı','evidence-badge'),node('p','Maç ve kayıtlı tahmin toplamları şu anda doğrulanamadı. Grafik, kendi sonuç API’sinin durumunu ayrıca gösterir.'));box.firstChild.dataset.state='error';}}}
      finally{pending=null;}})();return pending;
  }
  function emptyTimeline(chart,series,label){
    chart.append(node('p','Henüz sonuçlanan tahmin yok · 0 doğrulanmış sonuç. Başarı oranı hesaplanamaz.','chart-empty'));
    const width=Math.max(280,Math.round(chart.clientWidth||360)),height=innerWidth<600?200:240;
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.classList.add('bounded-count-timeline');svg.setAttribute('viewBox',`0 0 ${width} ${height}`);svg.setAttribute('role','img');svg.setAttribute('aria-label',label+' · 0 doğrulanmış sonuç; başarı oranı yok');
    const make=(tag,attrs,text)=>{const e=document.createElementNS(svg.namespaceURI,tag);for(const[k,v]of Object.entries(attrs))e.setAttribute(k,v);if(text!=null)e.textContent=text;svg.append(e);};
    const left=34,right=width-25,bottom=height-45;
    make('path',{d:`M${left} 25V${bottom}H${right}`,stroke:'#355275',fill:'none'});
    make('path',{d:`M${left} ${bottom}H${right}`,stroke:'#67d7ec','stroke-width':3,'data-series':'empty-count',fill:'none'});
    chart.append(svg);
  }
  globalThis.BetAppEvidence=Object.freeze({refresh,validateTrends,badge,emptyTimeline});
})();
