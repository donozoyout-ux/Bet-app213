/* Coverage only: no client secrets, synthetic records or implicit source mixing. */
(() => {
  'use strict';
  const panel=document.createElement('section');panel.setAttribute('aria-live','polite');
  panel.id='history-archive-coverage';panel.className='notice';
  document.body.append(panel);
  async function load(){
    try{
      const response=await fetch('/api/history/coverage',{signal:AbortSignal.timeout(15000)});
      if(!response.ok)throw Error('unavailable');
      const data=await response.json();panel.replaceChildren();
      const title=document.createElement('strong');title.textContent='Geçmiş veri kapsamı';panel.append(title);
      const text=document.createElement('p');
      text.textContent=data.status==='verified'?`Doğrulanmış arşiv: ${data.covered} maç. Aktif depoda tutulan/eksik: ${data.retained}.`:'Doğrulanmış üretim arşivi etkin değil; mevcut veritabanı gösteriliyor.';
      panel.append(text);
      if(data.capacity?.blocked){const warning=document.createElement('p');warning.textContent='Depolama sınırı: yeni veri/yayın yazımı duraklatıldı. Mevcut kayıtlar korunuyor.';panel.append(warning);}
      for(const part of (data.partitions||[]).slice(0,100)){
        const group=document.createElement('details'),summary=document.createElement('summary');
        summary.textContent=`${part.seasons.join(', ')} · ${part.matches} maç · doğrulanmış arşiv`;
        const list=document.createElement('div'),more=document.createElement('button');more.textContent='Maçları göster';
        let offset=0;more.onclick=async()=>{
          more.disabled=true;
          try{
            const result=await fetch(part.href+`&limit=20&offset=${offset}`,{signal:AbortSignal.timeout(90000)});
            if(!result.ok)throw Error('unavailable');
            const page=await result.json();
            for(const match of page.items){
              const link=document.createElement('a');link.textContent=`${match.home_team} — ${match.away_team}`;
              link.href=`/match/${match.id}?history_source=archive&history_generation=${encodeURIComponent(part.generation)}`;
              link.target='_blank';link.rel='noopener noreferrer';list.append(link,document.createElement('br'));
            }
            offset+=page.items.length;more.hidden=offset>=page.total;more.textContent='Sonraki maçlar';
          }catch(_){more.textContent='Arşiv açılamadı · tekrar dene';}
          finally{more.disabled=false;}
        };
        group.append(summary,list,more);panel.append(group);
      }
    }catch(_){panel.textContent='Arşiv kapsamı şu anda doğrulanamıyor; tamamlanmış yedek olarak gösterilmez.';}
  }
  load();
})();
