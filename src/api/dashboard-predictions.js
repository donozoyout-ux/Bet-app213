/* Result-based statistical estimates; no sample cards, invented metrics or ML claims. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const state={generation:0,detailGeneration:0,selected:null,detailData:null,detailLoadedAt:0};
  const pct=value=>value == null ? '—' : `%${(100*value).toFixed(1)}`;
  const num=value=>value == null ? '—' : Number(value).toFixed(2);
  const date=value=>value ? new Date(value).toLocaleString('tr-TR',{timeZone:'Europe/Istanbul'}) : 'Veri hazırlanıyor';
  const confidence={low:'Düşük',medium:'Orta',high:'Yüksek'};
  async function api(path){const response=await fetch(path,{signal:AbortSignal.timeout(15000)});if(!response.ok)throw new Error(`HTTP ${response.status}`);return response.json();}
  function node(tag,text,className=''){const element=document.createElement(tag);element.textContent=text;element.className=className;return element;}
  function line(parent,label,value){const row=node('div','','stats-line');row.append(node('span',label,'text-outline'),node('span',value,'font-semibold'));parent.append(row);}
  function clear(){state.selected=null;state.detailGeneration++;state.detailData=null;state.detailLoadedAt=0;const box=$('statistics-detail');if(box){box.replaceChildren();box.hidden=true;}}
  function form(parent,label,data){
    const box=node('section','','stats-block');box.append(node('h4',label,'font-bold text-primary'));
    if(!data || !data.sample_size){box.append(node('p','Yetersiz veri'));parent.append(box);return;}
    line(box,'Form (en yeni önce)',data.form.join(' '));line(box,'Maç / G / B / M',`${data.sample_size} / ${data.wins} / ${data.draws} / ${data.losses}`);
    line(box,'Atılan / yenilen gol',`${data.goals_for} / ${data.goals_against}`);line(box,'Ortalama atılan / yenilen',`${num(data.avg_goals_for)} / ${num(data.avg_goals_against)}`);
    for(const [label,key] of [['Gol yememe','clean_sheet_rate'],['KG Var','btts_rate'],['1.5 Üst','over_15_rate'],['2.5 Üst','over_25_rate'],['3.5 Üst','over_35_rate']])line(box,label,pct(data[key]));
    line(box,'Ortalama toplam gol',num(data.avg_total_goals));parent.append(box);
  }
  function renderStatistics(data){
    const box=$('statistics-detail');if(!box)return;box.replaceChildren();box.hidden=false;
    const m=data.match,p=data.prediction;
    box.append(node('h3','Maç İstatistikleri','font-headline-md text-headline-md font-bold text-primary'));
    box.append(node('p',`${m.home_team} / ${m.away_team} • ${m.league} • ${m.season} • ${m.round_label || m.round} • ${date(m.kickoff_at)}`));
    const model=node('section','','stats-block');model.append(node('h4','İstatistiksel Tahmin','font-bold text-primary'));
    if(p.status!=='ok')model.append(node('p','Yetersiz veri','font-bold'));
    else {
      line(model,'1 / X / 2',`${pct(p.home_probability)} / ${pct(p.draw_probability)} / ${pct(p.away_probability)}`);
      line(model,'2.5 Üst / Alt',`${pct(p.over_25_probability)} / ${pct(p.under_25_probability)}`);
      line(model,'KG Var / Yok',`${pct(p.btts_probability)} / ${pct(p.no_btts_probability)}`);
      line(model,'Beklenen Gol',`${num(p.expected_home_goals)} / ${num(p.expected_away_goals)}`);
    }
    line(model,'Güven',p.status==='ok' ? confidence[p.confidence] : '—');line(model,'Takım/venue örneği (benzersiz maç)',String(p.sample_size));
    line(model,'Yarışma örneği',String(p.league_sample_size));line(model,'Takım / venue örnekleri',`${p.home_sample_size} / ${p.away_sample_size} • ${p.home_venue_sample_size} / ${p.away_venue_sample_size}`);
    line(model,'Veri kesim zamanı',date(data.as_of));box.append(model);
    const stats=node('div','','statistics-grid');
    form(stats,`${m.home_team} • Son 5`,data.home_form);form(stats,`${m.away_team} • Son 5`,data.away_form);
    form(stats,`${m.home_team} • Son 10`,data.home_last_10);form(stats,`${m.away_team} • Son 10`,data.away_last_10);
    form(stats,`${m.home_team} • Ev sahibi tarafı (son 10)`,data.home_split);form(stats,`${m.away_team} • Deplasman tarafı (son 10)`,data.away_split);box.append(stats);
    const h2h=node('section','','stats-block');h2h.append(node('h4','Son Karşılaşmalar (H2H)','font-bold text-primary'));
    if(!data.h2h_summary.sufficient)h2h.append(node('p','Yeterli H2H verisi yok'));
    for(const match of data.h2h)h2h.append(node('p',`${date(match.date)} • ${match.home} ${match.home_goals}–${match.away_goals} ${match.away}`));
    if(data.h2h.length)line(h2h,`${m.home_team} G / B / M`,`${data.h2h_summary.wins} / ${data.h2h_summary.draws} / ${data.h2h_summary.losses}`);
    box.append(h2h);
    const market=node('section','','stats-block');market.append(node('h4','1X2 • Model / Piyasa Karşılaştırması','font-bold text-primary'));
    for(const name of ['Crown','Bet365','Sbobet']){
      const book=data.bookmakers.find(item=>item.bookmaker===name);const block=node('div','','stats-book');block.append(node('h5',name,'font-bold'));
      if(!book){block.append(node('p','—'));market.append(block);continue;}
      const prices=kind=>['home','draw','away'].map(key=>num(book[kind]?.[key])).join(' / ');
      line(block,'Açılış',prices('opening'));line(block,book.stage==='closing'?'Kapanış':'Güncel maç öncesi',prices('prices'));
      const probs=book.implied_probabilities;
      line(block,'Marjdan arındırılmış 1 / X / 2',probs ? `${pct(probs.home)} / ${pct(probs.draw)} / ${pct(probs.away)}` : 'Veri kesiminde kullanılabilir fiyat yok');market.append(block);
    }
    const consensus=data.bookmaker_consensus;
    line(market,'Piyasa ortak olasılığı',consensus ? `${pct(consensus.home)} / ${pct(consensus.draw)} / ${pct(consensus.away)}` : 'Yetersiz veri');
    const difference=p.model_market_difference;
    line(market,'Model / Piyasa Farkı (puan)',difference ? ['home','draw','away'].map(key=>`${difference[key]>=0?'+':''}${(100*difference[key]).toFixed(1)}`).join(' / ') : '—');box.append(market);
    const explanation=node('details','','stats-block');explanation.append(node('summary','Model ve güven kuralları','font-bold'));
    explanation.append(node('p','Poisson gol modeli; venue oranları aynı yarışmanın ortalamalarına ağırlık 5 ile yaklaştırılır. En az 20 yarışma maçı, her takım için 5 ve her venue için 3 maç gerekir. Beklenen Gol ölçülmüş şut bazlı bir metrik değildir.'));
    explanation.append(node('p','Orta güven: takım 8, venue 5, yarışma 50 maç; son 5/10 gol oranları farkı en fazla 0.5 ve piyasa farkı en fazla 15 puan. Yüksek: takım 10, venue 8, yarışma 100, en az 2 bookmaker ve en fazla 10 puan fark. Milli takımlarda yüksek güven verilmez. Bu düzeyler doğruluk garantisi değildir.'));
    explanation.append(node('p','En fazla son 200 yarışma sonucu ve üç yıllık dönemdeki son 2000 kayıt incelenir. Beklenen Gol 0–8 aralığında sınırlandırılır. Ev/deplasman, kaynakta belirtilen takım tarafıdır; gerçek stad veya tarafsız saha bilgisi modellenmez.'));
    if(data.historical)explanation.append(node('p','Geçmiş maçta yalnızca başlama zamanından önce kaydedilmiş sonuçlar kullanılır. Sonradan toplanmış arşiv sonuçları ve fiyatları tahmine katılmaz; bu yüzden örnek yetersiz kalabilir.'));
    box.append(explanation);
  }
  async function select(id,scroll=false){
    const box=$('statistics-detail');
    if(state.selected===id && state.detailData && Date.now()-state.detailLoadedAt<600000){if(scroll)box?.scrollIntoView?.({behavior:'smooth',block:'start'});return state.detailData;}
    state.selected=id;state.detailData=null;state.detailLoadedAt=0;const generation=++state.detailGeneration;
    if(box){box.hidden=false;box.replaceChildren();box.append(node('p','Veri hazırlanıyor'));}
    try {const data=await api(`/api/matches/${id}/statistics`);if(state.selected!==id || generation!==state.detailGeneration)return;renderStatistics(data);state.detailData=data;state.detailLoadedAt=Date.now();if(scroll)box?.scrollIntoView?.({behavior:'smooth',block:'start'});return data;}
    catch(_){if(generation!==state.detailGeneration)return;if(box){box.replaceChildren();box.append(node('p','Veri hazırlanıyor • Bağlantı bekleniyor'));}}
  }
  function card(item){
    const m=item.match,p=item.prediction,element=node('article','','prediction-card');
    element.tabIndex=0;element.setAttribute('aria-label',`${m.home_team} / ${m.away_team}, istatistikleri gör`);
    element.append(node('p',`${m.league} • ${date(m.kickoff_at)}`,'text-outline text-body-sm'),node('h2',`${m.home_team} / ${m.away_team}`,'font-headline-md text-headline-md font-bold'));
    if(p.status!=='ok')element.append(node('p','Yetersiz veri','prediction-main'));
    else {
      const choices=[['Ev Sahibi',p.home_probability],['Beraberlik',p.draw_probability],['Deplasman',p.away_probability]].sort((a,b)=>b[1]-a[1]);
      element.append(node('p',`${choices[0][0]} ${pct(choices[0][1])}`,'prediction-main'));
      element.append(node('p',`1: ${pct(p.home_probability)} • X: ${pct(p.draw_probability)} • 2: ${pct(p.away_probability)}`));
      element.append(node('p',`2.5 Üst: ${pct(p.over_25_probability)} • KG Var: ${pct(p.btts_probability)}`));
      element.append(node('p',`Beklenen Gol: ${num(p.expected_home_goals)} – ${num(p.expected_away_goals)}`));
    }
    element.append(node('p',`Güven: ${p.status==='ok'?confidence[p.confidence]:'—'} • Örnek: ${p.sample_size} maç`,'text-outline text-body-sm'));
    element.append(node('p',`Veri kesimi: ${date(item.as_of)}`,'text-outline text-body-sm'));
    const open=()=>{if(globalThis.BetAppDashboard?.selectMatch)globalThis.BetAppDashboard.selectMatch(m.id,true);else select(m.id,true);};
    const button=node('button','İstatistikleri Gör','prediction-button');button.type='button';button.onclick=event=>{event.stopPropagation();open();};element.append(button);
    element.onclick=open;element.onkeydown=event=>{if(event.target===element && ['Enter',' '].includes(event.key)){event.preventDefault();open();}};
    return element;
  }
  async function refresh(league='',dateFilter=''){
    const box=$('prediction-cards');if(!box)return;const generation=++state.generation;
    const query=new URLSearchParams({limit:'8'});if(league)query.set('league',league);if(dateFilter)query.set('date',dateFilter);
    try {const page=await api(`/api/predictions?${query}`);if(generation!==state.generation)return;box.replaceChildren();for(const item of page.items)box.append(card(item));if(!page.items.length)box.append(node('p','Veri hazırlanıyor • Yakın tarihte kayıtlı maç yok'));const note=$('prediction-window');if(note)note.textContent=`Önümüzdeki ${page.window_hours===72?'72 saat':'7 gün'} • ${date(page.generated_at)}`;}
    catch(_){if(generation!==state.generation)return;box.replaceChildren();box.append(node('p','Veri hazırlanıyor • Bağlantı bekleniyor'));}
  }
  globalThis.BetAppPredictions=Object.freeze({refresh,select,clear});
})();
