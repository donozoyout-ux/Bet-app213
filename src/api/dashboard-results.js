/* Read-only persisted prediction outcomes; all dates use Istanbul kickoff days. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const panel=$('home-daily-results');
  if(!panel)return;
  const active=()=>!document.hidden && globalThis.BetAppUI?.active('home');
  const node=(tag,text='',cls='')=>{const el=document.createElement(tag);el.textContent=text;el.className=cls;return el;};
  const rate=value=>value==null?'—':`%${(value*100).toLocaleString('tr-TR',{maximumFractionDigits:1})}`;
  const localDate=value=>new Intl.DateTimeFormat('tr-TR',{timeZone:'Europe/Istanbul',day:'numeric',month:'long',year:'numeric'}).format(new Date(value+'T12:00:00+03:00'));
  const detail=d=>`${localDate(d.date)} · Kazandı ${d.won} · Kaybetti ${d.lost} · Sonuçlanan ${d.decided} · Bekliyor ${d.pending} · Veri eksik ${d.awaiting_data} · İncelenmeli ${d.disputed} · Geçersiz ${d.void}${d.decided?` · Başarı ${rate(d.success_rate)}`:' · Sonuçlanan tahmin yok'}`;
  let latest=null,controller=null,generation=0;

  function unavailable(text){
    latest=null;
    $('home-results-chart').replaceChildren(node('p',text,'chart-empty'));
    $('home-results-summary').textContent='Sonuç sayıları ve başarı oranı doğrulanamadı.';
    $('home-results-daily').replaceChildren();
    for(const id of ['home-results-settled','home-results-won','home-results-lost','home-results-rate'])$(id).textContent='—';
  }

  function render(data){
    const days=Number($('home-results-window').value)||7;
    const series=data.daily.slice(-days),summary=data.comparisons.find(c=>c.days===days)?.recent;
    if(data.truncated){unavailable('Veri sınırına ulaşıldı. Eksik toplamlar gösterilmiyor; Performans sayfasında filtreleri daraltın.');return;}
    if(!summary || series.length!==days){unavailable('Günlük sonuç verisi doğrulanamadı.');return;}
    latest=data;
    for(const [id,key] of [['settled','decided'],['won','won'],['lost','lost']])$('home-results-'+id).textContent=String(summary[key]);
    $('home-results-rate').textContent=rate(summary.success_rate);
    $('home-results-summary').textContent=`Son ${days} tamamlanmış İstanbul günü · ${summary.start} – ${series.at(-1).date} · ${summary.decided} sonuçlanan: ${summary.won} kazandı, ${summary.lost} kaybetti${summary.decided?`, gözlenen başarı ${rate(summary.success_rate)}`:'. Başarı oranı hesaplanamaz'}. ${summary.sufficient?'Gözlemsel sonuçlar':'Örneklem yetersiz'} · Modelin geliştiğini kanıtlamaz.`;
    const table=$('home-results-daily');table.replaceChildren();
    for(const d of series){const row=node('tr');for(const value of [localDate(d.date),d.won,d.lost,d.decided,d.pending,d.awaiting_data,d.disputed,d.void,rate(d.success_rate)])row.append(node('td',String(value)));table.append(row);}
    const chart=$('home-results-chart');chart.replaceChildren();
    if(!series.some(d=>d.decided)){
      chart.append(node('p','Henüz sonuçlanan tahmin yok. Günlük sayılar 0; bekleyen, eksik, incelenmeli ve geçersiz kayıtlar kazanç/kayıp sayılmaz.','chart-empty'));
      return;
    }
    const mobile=globalThis.innerWidth<600,width=Math.max(280,Math.round(chart.clientWidth||360)),height=mobile?200:240;
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
    svg.setAttribute('viewBox',`0 0 ${width} ${height}`);svg.setAttribute('role','group');
    svg.setAttribute('aria-label',`${days} günlük kazanan, kaybeden ve toplam sonuçlanan tahminler. Noktalar arasında yön tuşlarını kullanın.`);
    const make=(tag,attrs,text)=>{const el=document.createElementNS(svg.namespaceURI,tag);for(const [k,v] of Object.entries(attrs))el.setAttribute(k,v);if(text!=null)el.textContent=text;svg.append(el);return el;};
    const left=38,right=width-30,top=18,bottom=height-34;
    const max=Math.max(2,...series.map(d=>d.decided)),step=Math.max(1,Math.ceil(max/4)),ceiling=Math.ceil(max/step)*step;
    const x=i=>left+i*(right-left)/(series.length-1),y=n=>bottom-n*(bottom-top)/ceiling;
    for(let count=0;count<=ceiling;count+=step){make('path',{d:`M${left} ${y(count)}H${right}`,stroke:'#29415e'});make('text',{x:left-8,y:y(count)+4,'text-anchor':'end',fill:'#b7c7df','font-size':12},String(count));}
    const interval=Math.ceil((days-1)/(mobile?3:6));
    series.forEach((d,i)=>{if(i%interval===0 || i===days-1)make('text',{x:x(i),y:height-10,'text-anchor':'middle',fill:'#b7c7df','font-size':12},d.date.slice(5).split('-').reverse().join('.'));});
    const tooltip=node('p','Bir noktaya dokunun veya Tab ile odaklanıp yön tuşlarını kullanın.','results-tooltip');tooltip.id='home-results-tooltip';tooltip.setAttribute('role','status');
    const focusPoints=[];
    for(const [key,color,dash] of [['decided','#a5b4fc','6 4'],['won','#22d3ee',''],['lost','#fb7185','2 3']]){
      make('path',{d:series.map((d,i)=>`${i?'L':'M'}${x(i)} ${y(d[key])}`).join(' '),stroke:color,'stroke-width':2.5,'stroke-dasharray':dash,fill:'none','data-series':key});
      series.forEach((d,i)=>{
        const dot=make('circle',{cx:x(i),cy:y(d[key]),r:4,fill:color,stroke:'#102139','stroke-width':1.5,'data-series':key});
        const show=()=>{tooltip.textContent=detail(d);};
        for(const event of ['pointerenter','focus','click'])dot.addEventListener(event,show);
        if(key==='decided'){
          dot.setAttribute('tabindex',i===0?'0':'-1');dot.setAttribute('role','button');
          dot.setAttribute('aria-label',detail(d));dot.setAttribute('aria-describedby',tooltip.id);focusPoints.push(dot);
          dot.addEventListener('keydown',event=>{
            if(event.key==='Enter'||event.key===' '){event.preventDefault();show();return;}
            const next=event.key==='ArrowRight'?Math.min(days-1,i+1):event.key==='ArrowLeft'?Math.max(0,i-1):event.key==='Home'?0:event.key==='End'?days-1:null;
            if(next==null)return;event.preventDefault();focusPoints.forEach((point,j)=>point.setAttribute('tabindex',j===next?'0':'-1'));focusPoints[next].focus();
          });
        }
      });
    }
    chart.append(svg,tooltip);
  }

  async function refresh(){
    if(!active() || controller)return;
    controller=new AbortController();const current=++generation;
    panel.setAttribute('aria-busy','true');
    try{
      const response=await fetch('/api/performance-trends',{signal:AbortSignal.any([controller.signal,AbortSignal.timeout(15000)])});
      if(!response.ok)throw new Error('unavailable');
      const data=await response.json();
      if(current===generation && active())render(data);
    }catch(error){if(current===generation && active())unavailable('Veri bağlantısı bekleniyor. Sonuçlar doğrulanamadığı için sıfır veya başarı oranı gösterilmiyor.');}
    finally{if(current===generation){controller=null;panel.setAttribute('aria-busy','false');}}
  }
  $('home-results-window').addEventListener('change',()=>{if(latest)render(latest);else refresh();});
  $('home-results-retry').addEventListener('click',refresh);
  globalThis.addEventListener('resize',()=>{if(latest && active())render(latest);});
  // Abort off-page work and bound outages without influencing other home panels.
  globalThis.addEventListener('hashchange',()=>{if(!active() && controller){generation++;controller.abort();controller=null;panel.setAttribute('aria-busy','false');}});
  globalThis.BetAppResults=Object.freeze({refresh});
})();
