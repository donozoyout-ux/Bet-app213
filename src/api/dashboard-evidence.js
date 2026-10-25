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
    if(d.current_day!=null && (!row(d.current_day)||d.current_day.date!==d.as_of))throw new Error('Invalid current-day cohort');
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
  function counts(chart,summary,label){
    const categories=[['total','Kayıtlı'],['won','Kazandı'],['lost','Kaybetti'],['pending','Bekliyor'],['awaiting_data','Veri eksik'],['disputed','İncelenmeli'],['void','Geçersiz']];
    const width=Math.max(280,Math.round(chart.clientWidth||360)),height=238,max=Math.max(1,summary.total);
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.classList.add('bounded-count-timeline');svg.setAttribute('viewBox',`0 0 ${width} ${height}`);svg.setAttribute('role','img');
    svg.setAttribute('aria-label',label+' · '+categories.map(([key,name])=>`${name}: ${summary[key]}`).join(' · '));
    const make=(tag,attrs,text)=>{const e=document.createElementNS(svg.namespaceURI,tag);for(const[k,v]of Object.entries(attrs))e.setAttribute(k,v);if(text!=null)e.textContent=text;svg.append(e);};
    categories.forEach(([key,name],i)=>{const y=12+i*32,available=width-148;
      make('text',{x:4,y:y+15,fill:'#333','font-size':12},name);
      make('rect',{x:94,y,width:available,height:20,rx:3,fill:'#f1f1f1'});
      make('rect',{x:94,y,width:available*summary[key]/max,height:20,rx:3,fill:key==='total'?'#111':'#666','data-count':key,'data-value':summary[key]});
      make('text',{x:width-8,y:y+15,'text-anchor':'end',fill:'#111','font-size':12},String(summary[key]));
    });chart.append(svg);
  }
  function emptyTimeline(chart,series,label){
    chart.append(node('p','Tamamlanmış günler · Henüz sonuçlanan tahmin yok · 0 doğrulanmış sonuç. Başarı oranı hesaplanamaz.','chart-empty'));
    const summary=Object.fromEntries(['total','won','lost','pending','awaiting_data','disputed','void'].map(key=>[key,series.reduce((n,d)=>n+d[key],0)]));
    chart.append(node('p',label+' · Seçim sayıları; başarı eğilimi değildir.','muted'));counts(chart,summary,label);
  }
  function currentDay(chart,data){
    const id=chart.id+'-current-day';let box=$(id);
    if(!data?.current_day||data.truncated){box?.remove();return;}
    if(!box){box=node('section','','current-day-evidence');box.id=id;(chart.parentElement.querySelector('.results-legend')||chart).before(box);}
    const d=data.current_day;box.replaceChildren(node('h3','Bugün · '+d.date+' · İstanbul'));
    box.append(node('p','Gün henüz tamamlanmadı. Bu kayıtlar aşağıdaki 7/30 günlük dönem karşılaştırmasına dahil değildir. Aynı sayfa filtreleri uygulanır.','muted'));
    counts(box,d,'Bugünkü kayıtlı seçimler');
    box.append(node('p',`${d.decided} doğrulanmış sonuç · ${d.won} kazandı / ${d.lost} kaybetti · Başarı: ${d.success_rate==null?'N/A — henüz hesaplanamaz':(100*d.success_rate).toLocaleString('tr-TR',{maximumFractionDigits:1})+'%'} · Gözlenen sonuç; modelin geliştiğini kanıtlamaz.`, 'current-day-summary'));
  }
  globalThis.BetAppEvidence=Object.freeze({refresh,validateTrends,badge,emptyTimeline,currentDay});
})();
