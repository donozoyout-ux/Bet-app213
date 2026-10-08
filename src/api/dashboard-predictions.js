/* Result-based statistical estimates; no sample cards, invented metrics or ML claims. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const state={generation:0,detailGeneration:0,selected:null,detailData:null,detailLoadedAt:0,market:'all',view:'nearby',league:'',dateFilter:'',tab:'predictions',confidence:'',filterCache:null};
  const pct=value=>value == null ? '—' : `%${(100*value).toFixed(1)}`;
  const num=value=>value == null ? '—' : Number(value).toFixed(2);
  const date=value=>value ? new Date(value).toLocaleString('tr-TR',{timeZone:'Europe/Istanbul'}) : 'Veri hazırlanıyor';
  const confidence={low:'Düşük',medium:'Orta',high:'Yüksek'};
  async function api(path){const response=await fetch(path,{signal:AbortSignal.timeout(15000)});if(!response.ok)throw new Error(`HTTP ${response.status}`);return response.json();}
  function node(tag,text,className=''){const element=document.createElement(tag);element.textContent=text;element.className=className;return element;}
  function line(parent,label,value){const row=node('div','','stats-line');row.append(node('span',label,'text-outline'),node('span',value,'font-semibold'));parent.append(row);}
  function clear(){state.selected=null;state.detailGeneration++;state.detailData=null;state.detailLoadedAt=0;const box=$('statistics-detail');if(box){box.replaceChildren();box.hidden=true;}const dialog=$('analysis-dialog');if(dialog?.open)dialog.close();}
  function form(parent,label,data){
    const box=node('section','','stats-block');box.append(node('h4',label,'font-bold text-primary'));
    if(!data || !data.sample_size){box.append(node('p','Yetersiz veri'));parent.append(box);return;}
    line(box,'Form (en yeni önce)',data.form.join(' '));line(box,'Maç / G / B / M',`${data.sample_size} / ${data.wins} / ${data.draws} / ${data.losses}`);
    line(box,'Atılan / yenilen gol',`${data.goals_for} / ${data.goals_against}`);line(box,'Ortalama atılan / yenilen',`${num(data.avg_goals_for)} / ${num(data.avg_goals_against)}`);
    for(const [label,key] of [['Gol yememe','clean_sheet_rate'],['KG Var','btts_rate'],['1.5 Üst','over_15_rate'],['2.5 Üst','over_25_rate'],['3.5 Üst','over_35_rate']])line(box,label,pct(data[key]));
    line(box,'Ortalama toplam gol',num(data.avg_total_goals));parent.append(box);
  }
  function extraStatistics(box,data,wanted){
    const extra=data.additional_statistics || {};
    const observed=extra.observed_match_statistics;
    if(observed){const section=node('section','','stats-block');section.append(node('h4','Kaydedilmiş Maç İstatistikleri','font-bold text-primary'));line(section,'Kayıt zamanı',date(extra.observed_statistics_at));for(const [label,key] of [['Korner','corners'],['İY korner','corners_ht'],['Sarı kart','yellow_cards'],['Kırmızı kart','red_cards'],['Şut','shots'],['İsabetli şut','shots_on_target'],['Faul','fouls'],['Ofsayt','offsides'],['Topla oynama (%)','possession']]){if(wanted==='corners' && !key.startsWith('corners') || wanted==='cards' && !['yellow_cards','red_cards','fouls'].includes(key) || wanted==='goals')continue;const a=observed['home_'+key],b=observed['away_'+key];if(a!=null || b!=null || key==='red_cards')line(section,label,`${a==null?'—':a} / ${b==null?'—':b}`);}box.append(section);}
    for(const [metric,title] of [['corners','KORNERLER'],['cards','KARTLAR']]){
      if(metric!==wanted)continue;const section=node('section','','stats-block');section.append(node('h4',title,'font-bold text-primary'));
      const values=extra[metric];
      if(!values){section.append(node('p','Yetersiz veri'));box.append(section);continue;}
      if(!['home','away'].some(side=>{const recent=values[side]?.last_10;return recent && (recent.for_sample_size || recent.against_sample_size || recent.yellow_for_sample_size || recent.red_sample_size);})){section.append(node('p','Yetersiz veri'));box.append(section);continue;}
      if(metric==='cards')section.append(node('p',values.prediction.basis==='yellow_plus_red'?'Tahmin tanımı: sarı + kırmızı kart, her kayıt 1 kart':'Tahmin tanımı: sarı kart sayısı. Kırmızı kart eksikleri sıfır sayılmaz.'));
      for(const [side,label] of [['home',data.match.home_team],['away',data.match.away_team]]){
        const profile=values[side];line(section,label,'');
        for(const [key,name] of [['last_5','Son 5'],['last_10','Son 10'],['season','Sezon'],[side==='home'?'home_split':'away_split',side==='home'?'Ev sahibi tarafı':'Deplasman tarafı']]){
          const summary=profile[key];
          line(section,name,summary.status==='insufficient_data'?'Yetersiz veri':`${num(summary.for_avg)} / ${num(summary.against_avg)} • ${summary.for_sample_size}/${summary.against_sample_size} bilinen maç`);
        }
        const recent=profile.last_10;
        line(section,metric==='corners'?'Toplam korner ortalaması':recent.rate_basis==='yellow_plus_red'?'Toplam sarı + kırmızı ortalaması':'Toplam sarı kart ortalaması',`${num(recent.total_avg)} • ${recent.sample_size} tam kayıt`);
        if(metric==='cards'){line(section,'Sarı kart ort. (takım / rakip)',`${num(recent.yellow_cards_for_avg)} / ${num(recent.yellow_cards_against_avg)} • ${recent.yellow_for_sample_size}/${recent.yellow_against_sample_size} bilinen maç`);line(section,'Kırmızı kart ortalaması',`${num(recent.red_cards_for_avg)} • ${recent.red_sample_size} bilinen maç`);line(section,'Sarı + kırmızı toplamı',`${num(recent.total_match_cards_avg)} • ${recent.all_card_sample_size} tam kayıt`);}
        for(const [threshold,rate] of Object.entries(recent.over_rates || {}))line(section,threshold.replace('over_','').replace('_','.')+' Üst (gözlenen)',pct(rate));
      }
      const p=values.prediction;
      if(p.status!=='ok')section.append(node('p','Yetersiz veri • Tahmin için yarışma 20, her takım 5, her ilgili venue 3 tam kayıt gerekir.'));
      else {line(section,metric==='corners'?'Beklenen korner (ev / dep / toplam)':'Beklenen kart (ev / dep / toplam)',`${num(p.expected_home)} / ${num(p.expected_away)} / ${num(p.expected_total)}`);for(const [threshold,probability] of Object.entries(p.over_probabilities))line(section,threshold+' Üst (model)',pct(probability));}
      line(section,'Model örneği / yarışma örneği',`${p.sample_size} / ${p.league_sample_size}`);box.append(section);
    }
    if(wanted==='cards' && extra.referee){const ref=extra.referee,section=node('section','','stats-block');section.append(node('h4','HAKEM','font-bold text-primary'));line(section,'Ad',ref.name);line(section,'Bilinen maç',String(ref.matches_officiated));if(ref.status!=='ok')section.append(node('p','Yetersiz hakem verisi'));for(const [label,key] of [['Sarı kart / maç','average_total_yellow_cards'],['Kırmızı kart / maç','average_total_red_cards'],['Faul / maç','average_total_fouls']])line(section,label,num(ref[key]));box.append(section);}
    if(wanted==='cards' && !extra.referee)box.append(node('p','Hakem: Yetersiz veri'));
    if(wanted==='cards' && extra.events?.length){const section=node('details','','stats-block');section.append(node('summary','Kaydedilmiş Maç Olayları','font-bold'));const labels={goal:'Gol',yellow_card:'Sarı kart',red_card:'Kırmızı kart',second_yellow_red:'İkinci sarı / kırmızı',corner:'Korner',penalty:'Penaltı',own_goal:'Kendi kalesine gol',substitution:'Değişiklik',var:'VAR',unknown:'Kaynak olayı'};for(const event of extra.events){const minute=event.minute==null?'—':event.minute+(event.stoppage_minute==null?'':'+'+event.stoppage_minute);section.append(node('p',`${minute} • ${event.team_side==='home'?data.match.home_team:data.match.away_team} • ${labels[event.event_type] || event.event_type}${event.player_name?' • '+event.player_name:''}${event.secondary_player_name?' / '+event.secondary_player_name:''}`));}box.append(section);}
  }
  function renderStatistics(data){
    const box=$('statistics-detail');if(!box)return;box.replaceChildren();box.hidden=false;
    const m=data.match,p=data.prediction;
    box.append(node('h3','Maç İstatistikleri','font-headline-md text-headline-md font-bold text-primary'));
    box.append(node('p',`${m.home_team} / ${m.away_team} • ${m.league} • ${m.season} • ${m.round_label || m.round} • ${date(m.kickoff_at)}`,'stats-meta'));
    if(['summary','predictions'].includes(state.tab) && !data.recommendations?.length)box.append(node('p','Yetersiz veri'));
    if(['summary','predictions'].includes(state.tab) && data.recommendations?.length){const chosen=node('section','','stats-block');chosen.append(node('h4','Seçilen Güçlü Tahminler','font-bold text-primary'));for(const rec of data.recommendations)line(chosen,rec.label,`${pct(rec.probability)} • ${confidence[rec.confidence]} Güven • ${rec.sample_size} maç`);box.append(chosen);}
    if(['predictions','goals'].includes(state.tab)){
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
    line(model,'Veri kesim zamanı',date(data.as_of));box.append(model);}
    if(['form','goals'].includes(state.tab)){
    const stats=node('div','','statistics-grid');
    form(stats,`${m.home_team} • Son 5`,data.home_form);form(stats,`${m.away_team} • Son 5`,data.away_form);
    form(stats,`${m.home_team} • Son 10`,data.home_last_10);form(stats,`${m.away_team} • Son 10`,data.away_last_10);
    form(stats,`${m.home_team} • Ev sahibi tarafı (son 10)`,data.home_split);form(stats,`${m.away_team} • Deplasman tarafı (son 10)`,data.away_split);box.append(stats);}
    if(state.tab==='h2h'){
    const h2h=node('section','','stats-block');h2h.append(node('h4','Son Karşılaşmalar (H2H)','font-bold text-primary'));
    if(!data.h2h_summary.sufficient)h2h.append(node('p','Yeterli H2H verisi yok'));
    for(const match of data.h2h)h2h.append(node('p',`${date(match.date)} • ${match.home} ${match.home_goals}–${match.away_goals} ${match.away}`));
    if(data.h2h.length)line(h2h,`${m.home_team} G / B / M`,`${data.h2h_summary.wins} / ${data.h2h_summary.draws} / ${data.h2h_summary.losses}`);
    box.append(h2h);}
    if(['corners','cards'].includes(state.tab))extraStatistics(box,data,state.tab);
    if(state.tab==='odds'){
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
    line(market,'Model / Piyasa Farkı (puan)',difference ? ['home','draw','away'].map(key=>`${difference[key]>=0?'+':''}${(100*difference[key]).toFixed(1)}`).join(' / ') : '—');box.append(market);}
    if(['summary','predictions'].includes(state.tab)){
    const explanation=node('details','','stats-block');explanation.append(node('summary','Model ve güven kuralları','font-bold'));
    explanation.append(node('p','Poisson gol modeli; venue oranları aynı yarışmanın ortalamalarına ağırlık 5 ile yaklaştırılır. En az 20 yarışma maçı, her takım için 5 ve her venue için 3 maç gerekir. Beklenen Gol ölçülmüş şut bazlı bir metrik değildir.'));
    explanation.append(node('p','Orta güven: takım 8, venue 5, yarışma 50 maç; son 5/10 gol oranları farkı en fazla 0.5 ve piyasa farkı en fazla 15 puan. Yüksek: takım 10, venue 8, yarışma 100, en az 2 bookmaker ve en fazla 10 puan fark. Milli takımlarda yüksek güven verilmez. Güven düzeyi örnek ve tutarlılık kurallarını ifade eder; doğruluk olasılığı değildir.'));
    explanation.append(node('p','Seçim: olasılık en az %60, veri güvenilirliği en az 0.70, skor en az 0.74; takım 8, venue 5, yarışma 50 ve veri tamlığı %60. Sonuç/çifte şans/handikap aynı gruptadır; gol toplamları ve KG aynı gruptadır. Her gruptan en fazla bir, maç başına en fazla üç tahmin seçilir. Tüm hesaplar API aday listesinde korunur.'));
    explanation.append(node('p','En fazla son 200 yarışma sonucu ve üç yıllık dönemdeki son 2000 kayıt incelenir. Beklenen Gol 0–8 aralığında sınırlandırılır. Ev/deplasman, kaynakta belirtilen takım tarafıdır; gerçek stad veya tarafsız saha bilgisi modellenmez.'));
    if(data.historical)explanation.append(node('p','Geçmiş maçta yalnızca başlama zamanından önce kaydedilmiş sonuçlar kullanılır. Sonradan toplanmış arşiv sonuçları ve fiyatları tahmine katılmaz; bu yüzden örnek yetersiz kalabilir.'));
    box.append(explanation);}
  }
  async function select(id,scroll=false){
    const box=$('statistics-detail');
    const changed=state.selected!==id;const dialog=$('analysis-dialog');if(dialog && !dialog.open){dialog.showModal?.();if(!dialog.showModal)dialog.open=true;}
    if(changed){state.tab='summary';setTab('summary');}
    if(state.selected===id && state.detailData && Date.now()-state.detailLoadedAt<600000){if(scroll)box?.scrollIntoView?.({behavior:'smooth',block:'start'});return state.detailData;}
    state.selected=id;state.detailData=null;state.detailLoadedAt=0;const generation=++state.detailGeneration;
    if(box){box.hidden=false;box.replaceChildren();box.append(node('p','Veri hazırlanıyor','analysis-loading'));}
    try {const data=await api(`/api/matches/${id}/statistics`);if(state.selected!==id || generation!==state.detailGeneration)return;if(data.match.id!==id)throw new Error('Match identity mismatch');renderStatistics(data);state.detailData=data;state.detailLoadedAt=Date.now();if(scroll)box?.scrollIntoView?.({behavior:'smooth',block:'start'});return data;}
    catch(_){if(generation!==state.detailGeneration)return;if(box){box.replaceChildren();box.append(node('p','Veri hazırlanıyor • Bağlantı bekleniyor'));}}
  }
  function card(item){
    const m=item.match,recs=item.recommendations,element=node('article','','prediction-card');
    element.tabIndex=0;element.setAttribute('aria-label',`${m.home_team} / ${m.away_team}, istatistikleri gör`);
    element.append(node('p',`${m.league} • ${date(m.kickoff_at)}`,'text-outline text-body-sm'),node('h2',`${m.home_team} / ${m.away_team}`,'font-headline-md text-headline-md font-bold'));
    element.append(node('p','En Güçlü Tahmin','font-bold text-primary'));
    element.append(node('p',`${recs[0].label} ${pct(recs[0].probability)}`,'prediction-main'));
    element.append(node('p',`${confidence[recs[0].confidence]} Güven • Örnek: ${recs[0].sample_size} maç`,'text-outline text-body-sm'));
    if(recs.length>1){for(const rec of recs.slice(1,3))element.append(node('p',`${rec.label} ${pct(rec.probability)} • ${confidence[rec.confidence]} Güven`));}

    const open=()=>{if(globalThis.BetAppDashboard?.selectMatch)globalThis.BetAppDashboard.selectMatch(m.id,true);else select(m.id,true);};
    const button=node('button','Analizi aç →','prediction-open');button.type='button';button.onclick=event=>{event.stopPropagation();open();};element.append(button);
    element.onclick=open;element.onkeydown=event=>{if(event.target===element && ['Enter',' '].includes(event.key)){event.preventDefault();open();}};
    return element;
  }
  async function refresh(league='',dateFilter=''){
    if(globalThis.BetAppMarket)return globalThis.BetAppMarket.refresh(league,dateFilter);
    state.league=league;state.dateFilter=dateFilter;
    const box=$('prediction-cards');if(!box)return;const generation=++state.generation;
    const query=new URLSearchParams({limit:state.view==='best'?'20':'8',market:state.market});if(league)query.set('league',league);if(dateFilter)query.set('date',dateFilter);
    try {const page=await api(`/api/predictions${state.view==='best'?'/best':''}?${query}`);if(generation!==state.generation)return;box.replaceChildren();let shown=0;
      if(state.view==='best'){
        const list=node('ol','','prediction-global-list');for(const [index,item] of page.items.entries()){if(state.confidence && item.recommendation.confidence!==state.confidence)continue;const row=node('li');const button=node('button',`${index+1}. ${item.match.home_team} / ${item.match.away_team} — ${item.recommendation.label} ${pct(item.recommendation.probability)} • ${confidence[item.recommendation.confidence]} Güven`,'prediction-global-button');button.type='button';button.onclick=()=>globalThis.BetAppDashboard?.selectMatch ? globalThis.BetAppDashboard.selectMatch(item.match.id,true) : select(item.match.id,true);row.append(button);list.append(row);shown++;}box.append(list);
      }else{for(const item of page.items){if(item.recommendations?.length && (!state.confidence || item.recommendations.some(r=>r.confidence===state.confidence))){box.append(card({...item,recommendations:item.recommendations.filter(r=>!state.confidence || r.confidence===state.confidence)}));shown++;}}}
      if(!shown)box.append(node('p','Bu filtrede eşikleri geçen tahmin bulunmuyor.'));const note=$('prediction-window');if(note)note.textContent=`${state.view==='best'?'En güçlü seçilmiş tahminler • Önümüzdeki 7 gün':'Önümüzdeki '+(page.window_hours===72?'72 saat':'7 gün')} • ${date(page.generated_at)}`;}
    catch(_){if(generation!==state.generation)return;box.replaceChildren();box.append(node('p','Veri hazırlanıyor • Bağlantı bekleniyor'));}
  }
  function setTab(tab){
    state.tab=tab;const dialog=$('analysis-dialog');if(dialog)dialog.scrollTop=0;
    document.querySelectorAll('[data-analysis-tab]').forEach(button=>{const active=button.dataset.analysisTab===tab;button.setAttribute('aria-selected',String(active));button.setAttribute('tabindex',active?'0':'-1');});
    const panel=$('analysis-panel');panel?.setAttribute('aria-labelledby','analysis-tab-'+tab);
    if($('match-detail'))$('match-detail').hidden=tab!=='odds';
    if(state.detailData)renderStatistics(state.detailData);
  }
  function close(){clear();globalThis.BetAppDashboard?.closeDetail?.();}
  document.querySelectorAll('[data-analysis-tab]').forEach(button=>{
    button.onclick=()=>setTab(button.dataset.analysisTab);
    button.onkeydown=event=>{const buttons=[...document.querySelectorAll('[data-analysis-tab]')];const index=buttons.indexOf(button);let next;if(event.key==='ArrowRight')next=(index+1)%buttons.length;else if(event.key==='ArrowLeft')next=(index+buttons.length-1)%buttons.length;else if(event.key==='Home')next=0;else if(event.key==='End')next=buttons.length-1;else return;event.preventDefault();buttons[next].focus();setTab(buttons[next].dataset.analysisTab);};
  });
  if($('analysis-close'))$('analysis-close').onclick=close;
  if($('analysis-dialog'))$('analysis-dialog').oncancel=event=>{event.preventDefault();close();};
  const conf=$('prediction-confidence');if(conf)conf.onchange=()=>{state.confidence=conf.value;refresh(state.league,state.dateFilter);globalThis.BetAppDashboard?.refreshMatches?.();};
  async function filterMatches(items){
    if(state.market==='all' && !state.confidence)return items;
    const query=new URLSearchParams({limit:'100',market:state.market});if(state.league)query.set('league',state.league);if(state.dateFilter)query.set('date',state.dateFilter);
    const key=query.toString();let picks;
    if(state.filterCache?.key===key && Date.now()-state.filterCache.at<600000)picks=state.filterCache.items;
    else{picks=[];let offset=0;while(true){query.set('offset',String(offset));const page=await api('/api/predictions/best?'+query);picks.push(...page.items);if(page.items.length<100)break;offset+=100;}state.filterCache={key,at:Date.now(),items:picks};}
    const ids=new Set(picks.filter(item=>!state.confidence || item.recommendation.confidence===state.confidence).map(item=>item.match.id));return items.filter(item=>ids.has(item.id));
  }
  globalThis.BetAppPredictions=Object.freeze({refresh,select,clear,setTab,filterMatches});
  document.querySelectorAll('[data-prediction-market]').forEach(button=>button.onclick=()=>{state.market=button.dataset.predictionMarket;document.querySelectorAll('[data-prediction-market]').forEach(other=>other.setAttribute('aria-pressed',String(other===button)));refresh(state.league,state.dateFilter);globalThis.BetAppDashboard?.refreshMatches?.();});
  const view=$('prediction-view');if(view)view.onchange=()=>{state.view=view.value;refresh(state.league,state.dateFilter);};
})();
