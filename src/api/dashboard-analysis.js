/* Read-only presentation of published recommendations and recorded match evidence. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  if (!$('match-analysis')) return;
  // A reload always revalidates the strip; never restore a legacy match query blindly.
  if(globalThis.location?.hash.startsWith('#predictions?')){const query=new URLSearchParams(globalThis.location.hash.split('?')[1]);query.delete('match');globalThis.history.replaceState(null,'','#predictions'+(query.size?'?'+query:''));}
  const state = {items:[], selected:null, detail:null, rec:null, quotes:new Map(), generation:0,
    detailGeneration:0, board:null, clock:null, listController:null, detailController:null, quoteController:null};
  const now = () => state.clock ? state.clock.stamp + Date.now() - state.clock.received : Date.now();
  const validNumber = v => typeof v === 'number' && Number.isFinite(v);
  const pct = v => validNumber(v) ? `%${(v*100).toFixed(1)}` : 'Veri yok';
  const num = v => validNumber(v) ? v.toLocaleString('tr-TR',{maximumFractionDigits:2}) : 'Veri yok';
  const date = v => Number.isFinite(Date.parse(v)) ? new Date(v).toLocaleString('tr-TR',{
    timeZone:'Europe/Istanbul',dateStyle:'medium',timeStyle:'short'}) : 'Zaman doğrulanmamış';
  const confidence = {high:'Yüksek',medium:'Orta',low:'Düşük'};
  const families = {result:'Maç sonucu',goals:'Toplam gol',btts:'Karşılıklı gol',double_chance:'Çifte şans',
    corners:'Korner',cards:'Sarı + kırmızı kart',yellow_cards:'Sarı kart',red_cards:'Kırmızı kart',
    first_half_goals:'İlk yarı gol',second_half_goals:'İkinci yarı gol',asian_handicap:'Asya handikapı'};
  const reasons = {no_source_rows:'Kaynakta eşleşen fiyat satırı yok',line_mismatch:'Market çizgisi eşleşmiyor',
    source_market_unavailable:'Kaynak bu marketi sağlamıyor',odds_stale:'Oran güncelliğini yitirmiş',
    odds_unknown:'Fiyatın gözlem zamanı doğrulanmamış',missing_stage_price:'Bu seçim için kaynak fiyatı yok',
    observed_after_cutoff:'Kanıt zamanı veri kesiminden sonra',unsupported_bookmaker:'Bookmaker desteklenmiyor',
    match_not_scheduled:'Maç artık planlı değil',selection_unavailable:'Kupon kalite kontrolünü geçen seçim yok',
    analysis_stale:'Analiz güncel değil',no_verified_odds:'Doğrulanmış güncel fiyat yok',
    outside_manual_window:'Kupon tarih aralığı dışında',match_started:'Maç başlamış'};
  const node = (tag,text='',cls='') => {const el=document.createElement(tag);el.textContent=text;el.className=cls;return el;};
  const write = (id,text) => {if ($(id)) $(id).textContent=text;};
  const active = () => globalThis.BetAppUI?.active('predictions');
  const future = m => m?.status === 'scheduled' && Date.parse(m.kickoff_at)>now();
  const boardFresh = () => state.board && state.board.cache?.stale===false &&
    Date.parse(state.board.generated_at)<=now() && Date.parse(state.board.cache.expires_at)>now();
  const priceFresh = vo => vo?.has_odds && validNumber(vo.best_price) && vo.best_price>1 && vo.coupon_eligible && vo.freshness==='fresh' &&
    Date.parse(vo.expires_at)>now() && Number.isFinite(Date.parse(vo.observed_at)) &&
    Date.parse(vo.observed_at)<=now();
  const reason = vo => reasons[vo?.reason] || (vo?.has_odds && !priceFresh(vo) ?
    'Oran güncel değil veya kanıt zamanı doğrulanmamış' : vo?.message || 'Doğrulanmış fiyat yok');
  function row(parent,label,value) {const el=node('div','','analysis-fact');el.append(node('span',label),node('strong',value));parent.append(el);}
  function empty(text) {write('analysis-strip-count','0 uygun maç · En fazla 8');$('analysis-strip-previous').disabled=true;$('analysis-strip-next').disabled=true;write('match-analysis-status',text);$('analysis-match-strip').replaceChildren();clearDetail();}
  function clearDetail() {
    state.detailGeneration++;state.detailController?.abort();state.selected=null;state.detail=null;state.rec=null;
    $('analysis-selected').hidden=true;$('analysis-blocks').replaceChildren();
  }
  async function api(path,controller,body) {
    const response=await fetch(path,{signal:AbortSignal.any([controller.signal,AbortSignal.timeout(15000)]),
      ...(body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});
    if(!response.ok)throw new Error(`HTTP ${response.status}`);
    return response.json();
  }
  function clock(page,started) {
    const stamp=Date.parse(page.cache?.checked_at);
    if(Number.isFinite(stamp))state.clock={stamp:state.clock?Math.max(now(),stamp+Date.now()-started):stamp+Date.now()-started,received:Date.now()};
  }
  function strongest(recs) {
    return [...(recs || [])].filter(r=>r.qualifies===true && validNumber(r.probability) && r.probability>=0 && r.probability<=1 && validNumber(r.score))
      .sort((a,b)=>b.score-a.score || b.reliability-a.reliability || b.probability-a.probability)[0];
  }
  function items(page) {
    const unique=new Map();
    for(const item of page.items || []) {
      if(!future(item.match))continue;
      const previous=unique.get(item.match.id);
      unique.set(item.match.id,{...item,recommendations:[...(previous?.recommendations || []),...(item.recommendations || [])]});
    }
    return [...unique.values()].map(item=>({...item,recommendation:strongest(item.recommendations)}))
      .filter(item=>item.recommendation).slice(0,8);
  }
  function coupon(item) {
    const q=state.quotes.get(item.match.id),r=item.recommendation;
    if(!priceFresh(r.verified_odds))return {text:'İstatistiksel tahmin · '+reason(r.verified_odds),eligible:false};
    if(q?.recommendation_id===r.id && q.eligible && Date.parse(q.expires_at)>now())
      return {text:'Kupon kontrolünü geçti',eligible:true};
    return {text:q ? reasons[q.reason] || 'Kupon uygunluğu doğrulanmadı' : 'Kupon uygunluğu kontrol ediliyor',eligible:false};
  }
  function renderStrip() {
    const strip=$('analysis-match-strip');strip.replaceChildren();
    for(const item of state.items) {
      const m=item.match,r=item.recommendation,status=coupon(item);
      const button=node('button','','analysis-match-card');button.type='button';button.dataset.analysisMatch=String(m.id);
      button.setAttribute('aria-pressed',String(m.id===state.selected));button.setAttribute('aria-controls','analysis-selected');
      button.append(node('span',`${m.league} · ${date(m.kickoff_at)}`,'analysis-card-meta'),
        node('strong',`${m.home_team} / ${m.away_team}`,'analysis-card-teams'),node('span',r.label,'analysis-card-pick'),
        node('b',pct(r.probability),'analysis-card-probability'),
        node('span',priceFresh(r.verified_odds)?`${num(r.verified_odds.best_price)} · ${r.verified_odds.best_bookmaker}`:reason(r.verified_odds),'analysis-card-price'),
        node('small',status.text,status.eligible?'analysis-eligible':'analysis-model-only'));
      if(priceFresh(r.verified_odds) && validNumber(r.verified_odds.best_value) && r.verified_odds.best_value<=0)button.append(node('small','Negatif değer · Avantajlı bahis değil','analysis-negative'));
      button.onclick=()=>select(m.id);
      button.onkeydown=event=>{const all=[...strip.querySelectorAll('button')],index=all.indexOf(button);
        const next=event.key==='ArrowRight'?index+1:event.key==='ArrowLeft'?index-1:event.key==='Home'?0:event.key==='End'?all.length-1:null;
        if(next==null)return;event.preventDefault();const target=all[(next+all.length)%all.length];target.focus();target.scrollIntoView({block:'nearest',inline:'center'});target.click();};
      strip.append(button);
    }
    $('analysis-strip-previous').disabled=state.items.length===0;$('analysis-strip-next').disabled=state.items.length===0;
    write('analysis-strip-count',`${state.items.length} benzersiz maç · En fazla 8`);
  }
  async function quote(generation) {
    state.quoteController?.abort();state.quoteController=new AbortController();
    const selections=state.items.filter(i=>priceFresh(i.recommendation.verified_odds)).map(i=>({match_id:i.match.id,
      recommendation_id:i.recommendation.id,bookmaker:i.recommendation.verified_odds.best_bookmaker}));
    if(!selections.length)return;
    try {
      // Existing read-only quote endpoint; never saves, shares or settles a coupon.
      const result=await api('/api/coupon/quote',state.quoteController,{selections});
      if(generation!==state.generation || !active())return;
      for(const q of result.items || [])if(selections.some(s=>s.match_id===q.match_id && s.recommendation_id===q.recommendation_id && s.bookmaker===q.bookmaker))state.quotes.set(q.match_id,q);
      for(const s of selections)if(!state.quotes.has(s.match_id))state.quotes.set(s.match_id,{...s,eligible:false,reason:'quote_unavailable'});
    } catch (_) {if(generation!==state.generation || !active())return;
      for(const s of selections)state.quotes.set(s.match_id,{...s,eligible:false,reason:'quote_unavailable'});
    }
    renderStrip();if(state.detail)renderDetail();
  }
  async function refresh() {
    if(!active() || state.listController && !state.listController.signal.aborted)return;
    const generation=++state.generation,controller=new AbortController(),started=Date.now();state.listController=controller;
    $('match-analysis').setAttribute('aria-busy','true');write('match-analysis-status','Güncel maçlar kontrol ediliyor…');
    try {
      const page=await api('/api/predictions?limit=8',controller);
      if(controller.signal.aborted || generation!==state.generation || !active())return;
      clock(page,started);state.board=page;state.quotes.clear();
      write('analysis-check-time',`Veri kontrolü: ${date(page.cache?.checked_at)} · Analiz: ${date(page.generated_at)}`);
      if(!boardFresh()){state.items=[];empty('Analiz güncel değil veya güncelliği doğrulanamıyor. Yeni veri bekleniyor.');return;}
      state.items=items(page);
      if(!state.items.length){empty('Kalite eşiklerini geçen yaklaşan maç yok. Listeyi doldurmak için eşikler düşürülmez.');return;}
      write('match-analysis-status','Yalnızca yaklaşan ve kalite kontrolünü geçen maçlar.');
      const selected=state.items.find(i=>i.match.id===state.selected)?.match.id || state.items[0].match.id;
      renderStrip();select(selected);quote(generation);
    } catch (_) {if(!controller.signal.aborted && generation===state.generation && active()){
      state.items=[];state.board=null;empty('Maç verileri kontrol edilemedi. Bağlantıyı yeniden deneyin.');
    }} finally {if(state.listController===controller){state.listController=null;$('match-analysis').setAttribute('aria-busy','false');}}
  }
  async function select(id) {
    const item=state.items.find(i=>i.match.id===id);
    if(!item || !boardFresh() || !future(item.match))return;
    state.openBlock=state.selected===id?state.openBlock:'main';state.detailController?.abort();state.detailController=new AbortController();const controller=state.detailController;
    const generation=++state.detailGeneration;state.selected=id;state.detail=null;state.rec=null;renderStrip();
    $('analysis-selected').hidden=false;write('analysis-selected-title',`${item.match.home_team} / ${item.match.away_team}`);
    write('analysis-selected-meta',`${item.match.league} · ${date(item.match.kickoff_at)} · Europe/Istanbul`);
    $('analysis-blocks').replaceChildren(node('p','Seçilen maçın kanıtları yükleniyor…','analysis-empty'));
    try {
      const data=await api(`/api/matches/${id}/statistics`,controller);
      if(controller.signal.aborted || generation!==state.detailGeneration || state.selected!==id || !active())return;
      if(data.match?.id===id && (!future(data.match) || data.historical || Date.parse(data.match.kickoff_at)!==Date.parse(item.match.kickoff_at))){
        state.items=state.items.filter(i=>i.match.id!==id);clearDetail();renderStrip();
        if(state.items.length)select(state.items[0].match.id);else empty('Maç artık yaklaşan uygun karşılaşmalar arasında değil.');return;
      }
      const rec=[...(data.recommendations || []),...(data.candidates || [])].find(r=>r.id===item.recommendation.id && r.qualifies===true);
      if(data.match?.id!==id || data.historical || !future(data.match) || Date.parse(data.match.kickoff_at)!==Date.parse(item.match.kickoff_at) ||
        !boardFresh() || !rec || !Number.isFinite(Date.parse(data.as_of)) || Date.parse(data.as_of)>now() ||
        !(data.cache_seconds>0) || Date.parse(data.as_of)+data.cache_seconds*1000<=now())throw new Error('Invalid analysis evidence');
      state.detail=data;state.rec=rec;state.detailExpires=Date.parse(data.as_of)+data.cache_seconds*1000;renderDetail();
    } catch (_) {if(!controller.signal.aborted && generation===state.detailGeneration && active())
      $('analysis-blocks').replaceChildren(node('p','Bu maçın güncel analiz kanıtı doğrulanamadı. Sonraki veri kontrolünü bekleyin.','analysis-empty'));}
  }
  function block(key,title,teaser,build,open=false) {
    const details=node('details','','analysis-block');details.dataset.analysisBlock=key;details.name='match-analysis-evidence';details.open=open;
    const summary=node('summary');summary.append(node('span',title,'analysis-block-title'),node('span',teaser,'analysis-block-teaser'));
    const body=node('div','','analysis-block-body');build(body);details.append(summary,body);
    details.ontoggle=()=>{if(details.isConnected===false)return;if(details.open)state.openBlock=key;else if(state.openBlock===key)state.openBlock=null;if(details.open)$('analysis-blocks').querySelectorAll('details').forEach(other=>{if(other!==details)other.open=false;});};
    return details;
  }
  function main(body,data,r) {
    const item=state.items.find(i=>i.match.id===state.selected),status=coupon(item);
    row(body,'Model olasılığı',pct(r.probability));row(body,'Güven',confidence[r.confidence] || 'Bilinmiyor');
    row(body,'Güvenilirlik',pct(r.reliability));row(body,'Sıralama skoru',num(r.score));row(body,'Örneklem',`${r.sample_size} benzersiz maç`);
    body.append(node('p',status.text,status.eligible?'analysis-eligible':'analysis-model-only'));
    if(validNumber(r.verified_odds?.best_value)){row(body,'Model beklenen değeri',`${(100*r.verified_odds.best_value).toFixed(1)}%`);if(r.verified_odds.best_value<=0)body.append(node('p','Negatif değer · Avantajlı bahis değil','analysis-negative'));}
    body.append(node('small','Tek seçim kontrolü. Kuponun tamamı ayrıca bağımsızlık ve fiyat kontrolünden geçer.'));
  }
  function goalSummary(body,label,stats,r) {
    const box=node('section','','analysis-team-evidence');box.append(node('h4',label));
    if(!stats?.sample_size){box.append(node('p','Doğrulanmış örneklem yok'));body.append(box);return;}
    row(box,'Atılan / yenilen gol ort.',`${num(stats.avg_goals_for)} / ${num(stats.avg_goals_against)} · ${stats.sample_size} maç`);
    const threshold=(r.selection || '').match(/(?:over|under)_(\d+)_(\d+)/);
    const key=threshold?`over_${threshold[1]}${threshold[2]}`:null;
    if(r.market==='btts')row(box,'KG Var geçmişi',pct(stats.btts_rate));
    else if(key)row(box,`${threshold[1]}.${threshold[2]} Üst geçmişi`,pct(stats[key+'_rate']));
    else {row(box,'G / B / M',`${stats.wins} / ${stats.draws} / ${stats.losses}`);row(box,'Galibiyet oranı',pct(stats.win_rate));}
    body.append(box);
  }
  function market(body,data,r) {
    const metric=data.additional_statistics?.[r.market];
    if(['corners','cards','yellow_cards','red_cards','first_half_goals','second_half_goals'].includes(r.market)) {
      if(!metric){body.append(node('p','Bu market için doğrulanmış istatistik sunulmuyor.'));return;}
      body.append(node('p',r.market==='corners'?'Kullanılan / verilen kornerler · Gerçek geçmiş ortalamaları':
        families[r.market]+' · Doğrulanmış geçmiş ortalamaları'));
      for(const [side,label] of [['home',data.match.home_team],['away',data.match.away_team]]) {
        const section=node('section','','analysis-team-evidence');section.append(node('h4',label));
        for(const [window,title] of [['last_5','Son 5'],['last_10','Son 10'],['home_split','İç saha'],['away_split','Deplasman']]) {
          const s=metric[side]?.[window];if(!s){row(section,title,'Örneklem yok');continue;}
          row(section,title,`${num(s.for_avg)} / ${num(s.against_avg)} · ${num(s.for_sample_size)}/${num(s.against_sample_size)} bilinen gözlem`);
          const rate=s.over_rates?.[r.selection] ?? s.under_rates?.[r.selection];
          if(rate!=null)row(section,`${r.label} gözlenen geçmiş`,`${pct(rate)} · ${num(s.sample_size)} tam kayıt`);
          else row(section,'Seçim çizgisi geçmişi','Bu çizgi/yön için API gözlem sunmuyor');
          if(s.missing_total_sample_size)row(section,'Eksik toplam',`${s.missing_total_sample_size} kayıt · Bilinmeyen değer sıfır sayılmaz`);
          if(r.market==='cards') {row(section,'Sarı kart (takım / rakip)',`${num(s.yellow_cards_for_avg)} / ${num(s.yellow_cards_against_avg)}`);
            row(section,'Kırmızı kart (takım)',`${num(s.red_cards_for_avg)} · ${num(s.red_sample_size)} bilinen gözlem`);}
        }
        body.append(section);
      }
      body.append(node('small','API bu pencereler için toplulaştırılmış gerçek gözlemleri sunar; tekil kart/korner sayıları ve her satırın kanıt zamanı sunulmaz.'));
    } else {
      for(const [side,label] of [['home',data.match.home_team],['away',data.match.away_team]]) {
        goalSummary(body,label+' · Son 5',data[side+'_form'],r);
        goalSummary(body,label+' · Son 10',data[side+'_last_10'],r);
        goalSummary(body,label+(side==='home'?' · İç saha':' · Deplasman'),data[side+'_split'],r);
      }
      if(r.market==='asian_handicap')body.append(node('p','Bu Asya çizgisine özgü gerçekleşmiş sonuç geçmişi API’de sunulmuyor. Model ve eşleşen fiyat ayrı gösterilir.'));
    }
  }
  function teamForm(body,data) {
    for(const [side,label] of [['home',data.match.home_team],['away',data.match.away_team]]) {
      const section=node('section','','analysis-team-evidence');section.append(node('h4',label));
      for(const [key,title] of [[side+'_form','Son 5'],[side+'_last_10','Son 10'],[side+'_split',side==='home'?'İç saha':'Deplasman']]) {
        const s=data[key];row(section,title,s?.sample_size?`${(s.form || []).join(' · ')} · G/B/M ${s.wins}/${s.draws}/${s.losses} · ${s.sample_size} maç`:'Yeterli gözlem yok');
      }
      const list=node('ol','','analysis-history');
      for(const m of data[side+'_last_10']?.matches || [])list.append(node('li',`${date(m.date)} · ${m.home} ${m.home_goals}–${m.away_goals} ${m.away} · Goaloo / kayıtlı sonuç`));
      section.append(list);body.append(section);
    }
    if(data.domestic_context) {
      const box=node('aside','','analysis-domestic');box.append(node('h4','UEFA · Yerel lig bağlamı'),node('p','Ayrı gözlem; bu veri kupa modeline veya güven düzeyine eklenmez.'));
      for(const side of ['home','away']) {const context=data.domestic_context[side];
        row(box,data.match[side+'_team'],context?.available?`${context.form?.form?.join(' · ')} · ${context.form?.sample_size} kayıt`:'Yerel lig bağlamı yetersiz');
        if(context?.source_competitions?.length)box.append(node('small',context.source_competitions.map(c=>c.name).join(' · ')));
      }
      body.append(box);
    }
    body.append(node('small','Tarihler maç başlangıçlarıdır. Sonuçların ayrı kaynak gözlem zamanları bu API’de sunulmuyor.'));
  }
  function sources(body,data,r) {
    const vo=r.verified_odds;
    body.append(node('p',`${r.label} · ${vo?.period || 'Periyot doğrulanmamış'} · Çizgi: ${vo?.line==null?'Çizgi doğrulanmamış / çizgisiz market':num(vo.line)}`));
    if(!priceFresh(vo))body.append(node('p',reason(vo),'analysis-model-only'));
    for(const name of ['Crown','Bet365','Sbobet']) {
      const b=vo?.bookmakers?.[name],box=node('section','','analysis-source');box.append(node('h4',name));
      if(!b || !validNumber(b.current_odds)){box.append(node('p',reason(vo)));body.append(box);continue;}
      const current=b.current_odds>1 && b.coupon_eligible && b.freshness==='fresh' && Date.parse(b.expires_at)>now() && Date.parse(b.observed_at)<=now();
      row(box,current?'Doğrulanmış fiyat':'Kaydedilmiş fiyat · Kupon için güncel değil',num(b.current_odds));
      row(box,'Kaynak çizgisi',b.line==null?'Çizgisiz / bildirilmemiş':num(b.line));row(box,'Gözlem',date(b.observed_at));
      row(box,'Tazelik',current?'Taze · Mevcut 900 saniye kuralı':'Güncel değil / doğrulanmamış');
      if(validNumber(b.value)){row(box,'Model beklenen değeri',`${(100*b.value).toFixed(1)}%`);
        box.append(node('p',b.value<=0?'Avantajlı bahis değil':'Pozitif model değeri; başarı garantisi değildir',b.value<=0?'analysis-negative':'analysis-model-only'));}
      body.append(box);
    }
  }
  function renderDetail() {
    if(!state.detail || !state.rec || !future(state.detail.match) || !boardFresh())return;
    const data=state.detail,r=state.rec,box=$('analysis-blocks');
    const focused=document.activeElement?.closest?.('[data-analysis-block]')?.dataset.analysisBlock;
    const open=box.querySelector('details[open]')?.dataset.analysisBlock ?? state.openBlock;box.replaceChildren();
    box.append(block('main','Ana Tahmin',`${r.label} · ${pct(r.probability)}`,b=>main(b,data,r),open==='main'),
      block('market','Markete Özel İstatistik',families[r.market] || r.market,b=>market(b,data,r),open==='market'),
      block('form','Takım Formu','Son 5 / 10 · Ev ve deplasman',b=>teamForm(b,data),open==='form'),
      block('source','Oran ve Kaynak',priceFresh(r.verified_odds)?`${num(r.verified_odds.best_price)} · ${r.verified_odds.best_bookmaker}`:reason(r.verified_odds),b=>sources(b,data,r),open==='source'),
      block('why','Neden Bu Tahmin?','Kayıtlı istatistiksel kanıt',b=>{
        b.append(node('p',`${r.sample_size} benzersiz maçlık örneklem; model olasılığı ${pct(r.probability)}, güvenilirlik ${pct(r.reliability)}, skor ${num(r.score)}.`));
        const e=r.evidence || {};if(e.home_sample!=null)row(b,'Takım örnekleri',`${e.home_sample} / ${e.away_sample ?? 'Bilinmiyor'}`);
        if(!Object.keys(e).length)b.append(node('p','Bu seçimi açıklayacak ek kayıtlı kanıt yetersiz.'));
        b.append(node('small','Yalnızca kayıtlı model ölçüleri. Harici AI çağrısı yapılmaz.'));
      },open==='why'));
    if(focused)box.querySelector(`[data-analysis-block="${focused}"] > summary`)?.focus({preventScroll:true});
  }
  function deactivate() {state.items=[];state.board=null;state.quotes.clear();$('analysis-match-strip').replaceChildren();state.generation++;state.listController?.abort();state.listController=null;state.quoteController?.abort();clearDetail();}
  function tick() {
    if(!active())return;
    if(state.board && !boardFresh()){state.items=[];empty('Analiz süresi doldu. Yeni veri kontrolü bekleniyor.');return;}
    const remaining=state.items.filter(i=>future(i.match));
    if(remaining.length!==state.items.length){state.items=remaining;renderStrip();
      if(!remaining.length)empty('Yaklaşan uygun maç kalmadı.');else if(!remaining.some(i=>i.match.id===state.selected))select(remaining[0].match.id);}
    if(state.detail && state.detailExpires<=now()) {state.detail=null;state.rec=null;
      $('analysis-blocks').replaceChildren(node('p','Detay verisinin süresi doldu. Yeni analiz bekleniyor.','analysis-empty'));}
    // Only rerender when a quote transitions to expired, preserving accordion focus.
    const signature=state.items.map(i=>coupon(i).eligible+':'+priceFresh(i.recommendation.verified_odds)).join('|')+':'+priceFresh(state.rec?.verified_odds)+':'+Object.values(state.rec?.verified_odds?.bookmakers || {}).map(b=>Date.parse(b.expires_at)>now()).join(',');
    if(signature!==state.quoteSignature){state.quoteSignature=signature;renderStrip();if(state.detail)renderDetail();}
  }
  $('analysis-strip-previous').onclick=()=>$('analysis-match-strip').scrollBy({left:-320,behavior:'smooth'});
  $('analysis-strip-next').onclick=()=>$('analysis-match-strip').scrollBy({left:320,behavior:'smooth'});
  $('analysis-refresh').onclick=()=>{state.listController?.abort();state.listController=null;refresh();};
  document.addEventListener('visibilitychange',()=>{if(!document.hidden && active()){tick();refresh();}});
  globalThis.BetAppMatchAnalysis=Object.freeze({refresh,deactivate});
  setInterval(tick,1000);
})();
