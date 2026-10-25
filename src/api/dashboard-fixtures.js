/* Complete daily fixtures, with original prematch snapshots and recorded evidence. */
(() => {
  'use strict';
const matchLink=(id,text)=>{const a=document.createElement('a');a.href='/match/'+id;a.target='_blank';a.rel='noopener noreferrer';a.textContent=text;return a;};

  const $=id=>document.getElementById(id),panel=$('prediction-fixtures');if(!panel)return;
  const state={offset:0,status:'all',controller:null,generation:0,data:null,fingerprint:null,lastRead:0,clock:null,today:null,dateChosen:false};
  const active=()=>!document.hidden && globalThis.BetAppUI?.active('predictions');
  const instant=()=>state.clock?state.clock.server+Date.now()-state.clock.client:Date.now();
  const day=()=>new Intl.DateTimeFormat('en-CA',{timeZone:'Europe/Istanbul',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(instant()));
  const dateTime=value=>value?new Date(value).toLocaleString('tr-TR',{timeZone:'Europe/Istanbul',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'}):'Gözlem zamanı bilinmiyor';
  const time=value=>new Date(value).toLocaleTimeString('tr-TR',{timeZone:'Europe/Istanbul',hour:'2-digit',minute:'2-digit'});
  const node=(tag,text='',cls='')=>{const el=document.createElement(tag);el.textContent=text;el.className=cls;return el;};
  const write=(id,text)=>{$(id).textContent=text;};
  const outcomes={won:'Tuttu',lost:'Kaybetti',pending:'Bekliyor',awaiting_data:'Veri eksik',disputed:'İncelenmeli',void:'Geçersiz'};
  const statusNames={scheduled:'Başlamadı',live:'Canlı',first_half:'1. yarı',half_time:'Devre arası',second_half:'2. yarı',extra_time:'Uzatma',penalties:'Penaltılar',finished:'Maç sonu',postponed:'Ertelendi',cancelled:'İptal',abandoned:'Yarıda kaldı'};
  const live=['live','first_half','half_time','second_half','extra_time','penalties'];
  const reasons={settlement_not_recorded:'Kayıtlı sonuç kararı henüz yok.',invalid_recorded_evidence:'Sonuç kaydının kanıtı doğrulanamadı.',recorded_current_disagreement:'Kayıtlı karar ile güncel kanıt uyuşmuyor.',recorded_terminal_current_unfinished:'Maç durumu ile sonuç kararı uyuşmuyor.',current_evaluation:'Güncel kanıt eksik; kesin sonuç gösterilmez.',recorded_verified:'Kayıtlı karar ve mevcut kanıt doğrulandı.',unknown_match_status:'Maç durumu doğrulanamadı.',invalid_final_score:'Son skor doğrulanamadı.'};
  function expired(score){return score.state==='fresh' && Date.parse(score.fresh_until)<=instant();}
  function statusText(m){
    if(m.status==='scheduled' && Date.parse(m.kickoff_at)<=instant())return 'Saat geçti · Durum bilinmiyor';
    const label=statusNames[m.status]||'Durum bilinmiyor';
    return live.includes(m.status) && m.score.state==='fresh' && !expired(m.score) && m.live_minute?`${label} · ${m.live_minute}′`:label;
  }
  function scoreText(m){return m.score.home==null||m.score.away==null?'—':`${m.score.home} – ${m.score.away}`;}
  function freshness(m){
    if(m.score.state==='fresh' && !expired(m.score))return 'Yakın zamanda gözlenen skor';
    if(m.score.state==='stale' || expired(m.score))return 'Son gözlem · Güncel skor bilinmiyor';
    if(m.score.state==='recorded_final')return 'Kaydedilmiş son skor';
    if(m.score.state==='unknown_time')return 'Kaydedilmiş skor · Zamanı bilinmiyor';
    return m.status==='scheduled'?'Skor bekleniyor':'Skor doğrulanmadı';
  }
  function fact(box,label,text){const row=node('div','','fixture-fact');row.append(node('dt',label),node('dd',String(text)));box.append(row);}
  function detail(m){
    const box=node('details','','fixture-evidence');box.dataset.fixtureDetail=String(m.id);
    box.append(node('summary',`${m.home_team} — ${m.away_team} · Kayıt ve sonuç ayrıntısı`));
    const metadata=node('dl','','fixture-facts');
    fact(metadata,'Kaynak',m.source||'Bilinmiyor');fact(metadata,'Skor gözlemi',dateTime(m.score.observed_at));
    fact(metadata,'Kayıt güncellemesi',dateTime(m.updated_at));box.append(metadata);
    if(!m.snapshot){box.append(node('p','Tahmin oluşturulmadı. Başlangıçtan önce kaydedilmiş seçim yok; sonradan tahmin üretilmez.'));return box;}
    box.append(node('p',`Maç öncesi kayıt: ${dateTime(m.snapshot.captured_at)} · Model: ${m.snapshot.model_version}`,'fixture-meta'));
    for(const pick of m.snapshot.picks){
      const item=node('section','','fixture-pick-detail');item.append(node('h4',pick.label));
      const facts=node('dl','','fixture-facts');fact(facts,'Orijinal model olasılığı',pick.probability==null?'Bilinmiyor':`%${(pick.probability*100).toFixed(1)}`);
      fact(facts,'Sonuç',outcomes[pick.outcome]||'Veri eksik');fact(facts,'Doğrulama',pick.verified?'Kayıtlı karar doğrulandı':'Kesin sonuç doğrulanmadı');
      if(pick.settlement){
        fact(facts,'Kayıtlı karar',outcomes[pick.settlement.recorded_outcome]||'Bilinmiyor');
        fact(facts,'Karar sürümü',pick.settlement.revision);fact(facts,'Karar zamanı',dateTime(pick.settlement.recorded_at));
        const proof=pick.settlement.evidence;
        if(proof){
          fact(facts,'Kararda kullanılan skor',proof.ft_home==null||proof.ft_away==null?'Eksik':`${proof.ft_home} – ${proof.ft_away}`);
          fact(facts,'Kanıt kaynağı',proof.source||m.source||'Bilinmiyor');
          if(pick.actual!=null)fact(facts,'Güncel değerlendirme',pick.actual);
          const observed=node('details');observed.append(node('summary','Kayıtlı istatistik kanıtı'));
          const stats=node('dl','','fixture-facts');
          for(const [key,label] of [['home_corners','Ev korner'],['away_corners','Deplasman korner'],['home_yellow_cards','Ev sarı kart'],['away_yellow_cards','Deplasman sarı kart'],['home_red_cards','Ev kırmızı kart'],['away_red_cards','Deplasman kırmızı kart']])fact(stats,label,proof.statistics[key]??'Eksik');
          observed.append(stats);item.append(observed);
        }
      }
      item.append(facts,node('p',reasons[pick.reason]||'Sonuç kanıtı bekleniyor.','fixture-meta'));box.append(item);
    }
    if(m.snapshot.truncated)box.append(node('p','Seçim ayrıntısı sınırına ulaşıldı. Kalan kayıtlar burada gösterilmiyor.'));
    return box;
  }
  function row(m){
    const card=node('article','','daily-fixture');card.dataset.fixtureId=String(m.id);
    const heading=node('div','','fixture-heading');
    heading.append(node('span',`${time(m.kickoff_at)} · ${m.league}`,'fixture-meta'),node('span',statusText(m),'fixture-status'));
    const teams=node('div','','fixture-scoreline');const headingLink=node('h3');headingLink.append(matchLink(m.id,`${m.home_team} — ${m.away_team}`));teams.append(headingLink,node('strong',scoreText(m),'fixture-score'));
    const source=node('p',`${freshness(m)} · ${m.source||'Kaynak bilinmiyor'} · ${dateTime(m.score.observed_at)}`,'fixture-provenance');
    source.dataset.fixtureFreshness=String(m.id);
    const picks=node('div','','fixture-picks');
    if(!m.snapshot || !m.snapshot.picks.length)picks.append(node('span','Tahmin oluşturulmadı','fixture-no-pick'));
    else for(const pick of m.snapshot.picks){
      const chip=node('span','',`daily-fixture-pick ${pick.verified?'fixture-verified':''}`);
      chip.append(node('span',pick.label),node('span',pick.probability==null?'Olasılık bilinmiyor':`Maç öncesi %${(pick.probability*100).toFixed(1)}`),node('b',outcomes[pick.outcome]||'Veri eksik'));
      picks.append(chip);
    }
    card.append(heading,teams,source,picks,detail(m));return card;
  }
  function updateTimes(){
    for(const m of state.data?.items||[]){
      const card=panel.querySelector(`[data-fixture-id="${m.id}"]`);
      if(card){card.querySelector('.fixture-status').textContent=statusText(m);card.querySelector('.fixture-provenance').textContent=`${freshness(m)} · ${m.source||'Kaynak bilinmiyor'} · ${dateTime(m.score.observed_at)}`;}
    }
  }
  function render(data,started){
    const stamp=Date.parse(data.as_of);
    if(Number.isFinite(stamp))state.clock={server:Math.max(state.clock?instant():stamp,stamp+Math.max(0,Date.now()-started)),client:Date.now()};
    state.data=data;
    if(!state.dateChosen){$('fixtures-date').value=data.date;state.dateChosen=true;}
    state.offset=data.offset;
    if(!state.today)state.today=day();
    const summary=data.summary;
    for(const [id,value] of [['total',summary.total],['finished',summary.finished],['live',summary.live],['scheduled',summary.upcoming],['picks',summary.with_snapshot],['verified',data.page_summary.verified_outcomes]])write('fixtures-'+id,String(value));
    write('fixtures-as-of',`${data.date} · İstanbul · Liste kontrolü: ${dateTime(data.as_of)}`);
    write('fixtures-feedback',data.total?`${data.total} fikstür · Bu sayfada ${data.items.length} maç. Tahminsiz maçlar da dahildir.`:'Bu tarih, lig ve durum için kayıtlı fikstür yok.');
    write('fixtures-pagination',data.total?`${data.offset+1}–${data.offset+data.items.length} / ${data.total} · Sayfa ${Math.floor(data.offset/data.limit)+1}`:'0 kayıt');
    $('fixtures-previous').disabled=data.offset===0;$('fixtures-next').disabled=data.offset+data.limit>=data.total;
    const fingerprint=JSON.stringify([data.items,data.summary,data.page_summary,data.offset]);
    if(fingerprint!==state.fingerprint){
      const open=new Set([...panel.querySelectorAll('.fixture-evidence[open]')].map(el=>el.dataset.fixtureDetail));
      const focused=document.activeElement?.closest?.('.daily-fixture')?.dataset.fixtureId;
      const list=$('fixture-list');list.replaceChildren();
      for(const item of data.items){const card=row(item);const details=card.querySelector('.fixture-evidence');details.open=open.has(String(item.id));list.append(card);}
      if(!data.items.length)list.append(node('p','Fikstür yayınlanmamış veya seçilen filtrede kayıt yok. Tahmin üretmek için geçmiş sonuçlar kullanılmaz.','fixtures-empty'));
      if(focused)list.querySelector(`[data-fixture-id="${focused}"] > .fixture-evidence > summary`)?.focus({preventScroll:true});
      state.fingerprint=fingerprint;
    }
    updateTimes();
  }
  function failure(){
    state.data=null;state.fingerprint=null;$('fixture-list').replaceChildren(node('p','Fikstür doğrulanamadı. Bağlantıyı kontrol edip yeniden deneyin; önceki skorlar güncelmiş gibi gösterilmez.','fixtures-empty'));
    for(const id of ['total','finished','live','scheduled','picks','verified'])write('fixtures-'+id,'—');
    write('fixtures-feedback','Veri bağlantısı bekleniyor.');write('fixtures-as-of','Son kontrol başarısız · Güncel skor ve sonuçlar bilinmiyor.');
    write('fixtures-pagination','Sayfa doğrulanamadı');$('fixtures-previous').disabled=true;$('fixtures-next').disabled=true;
  }
  async function refresh(){
    if(!active() || state.controller)return;
    const currentDay=day();
    if(state.today && state.today!==currentDay){if($('fixtures-date').value===state.today){$('fixtures-date').value=currentDay;state.offset=0;}state.today=currentDay;}
    if(!$('fixtures-date').value)$('fixtures-date').value=day();
    const controller=new AbortController(),generation=++state.generation;state.controller=controller;state.lastRead=Date.now();
    panel.setAttribute('aria-busy','true');write('fixtures-feedback',state.data?'Mevcut liste kontrol ediliyor…':'Fikstür yükleniyor…');
    const q=new URLSearchParams({status:state.status,limit:'25',offset:String(state.offset)});
    if(state.dateChosen)q.set('date',$('fixtures-date').value);
    if($('fixtures-league').value)q.set('league',$('fixtures-league').value);
    try{
      const signal=AbortSignal.any([controller.signal,AbortSignal.timeout(15000)]);
      const load=async()=>{
        const response=await fetch('/api/prediction-fixtures?'+q,{signal});
        if(!response.ok)throw new Error('unavailable');return response.json();
      };
      let data=await load();
      if(controller.signal.aborted || generation!==state.generation || !active())return;
      // Live fixtures can leave a filtered page between polls. Recheck the first
      // page once, rather than retaining an impossible range or fetching all rows.
      if(data.offset>0 && data.offset>=data.total){state.offset=0;q.set('offset','0');data=await load();}
      if(generation===state.generation && active())render(data,state.lastRead);
    }catch(error){if(!controller.signal.aborted && generation===state.generation && active())failure();}
    finally{if(generation===state.generation){state.controller=null;panel.setAttribute('aria-busy','false');}}
  }
  function deactivate(){state.generation++;state.controller?.abort();state.controller=null;panel.setAttribute('aria-busy','false');}
  function change(reset=true){
    state.dateChosen=true;
    deactivate();if(reset)state.offset=0;state.fingerprint=null;state.data=null;$('fixture-list').replaceChildren();
    for(const id of ['total','finished','live','scheduled','picks','verified'])write('fixtures-'+id,'—');
    write('fixtures-pagination','Sayfa kontrol ediliyor…');$('fixtures-previous').disabled=true;$('fixtures-next').disabled=true;
    write('fixtures-as-of','Seçilen tarih ve filtreler kontrol ediliyor…');refresh();
  }
  $('fixtures-date').onchange=()=>change();$('fixtures-league').onchange=()=>change();
  for(const button of panel.querySelectorAll('[data-fixture-day]'))button.onclick=()=>{
    const current=new Date(day()+'T12:00:00+03:00');current.setUTCDate(current.getUTCDate()+Number(button.dataset.fixtureDay));
    $('fixtures-date').value=new Intl.DateTimeFormat('en-CA',{timeZone:'Europe/Istanbul',year:'numeric',month:'2-digit',day:'2-digit'}).format(current);change();
  };
  for(const button of panel.querySelectorAll('[data-fixture-status]'))button.onclick=()=>{
    state.status=button.dataset.fixtureStatus;for(const tab of panel.querySelectorAll('[data-fixture-status]'))tab.setAttribute('aria-pressed',String(tab===button));change();
  };
  $('fixtures-previous').onclick=()=>{state.offset=Math.max(0,state.offset-25);change(false);};
  $('fixtures-next').onclick=()=>{state.offset+=25;change(false);};
  $('fixtures-refresh').onclick=()=>{deactivate();refresh();};
  globalThis.BetAppFixtures=Object.freeze({refresh,deactivate});
  setInterval(()=>{
    if(!active())return;
    const today=day();if(state.today && state.today!==today){const wasToday=$('fixtures-date').value===state.today;state.today=today;if(wasToday){$('fixtures-date').value=today;change();return;}}
    updateTimes();
    if($('fixtures-date').value===today && Date.now()-state.lastRead>=60000)refresh();
  },15000);
  refresh();
})();
