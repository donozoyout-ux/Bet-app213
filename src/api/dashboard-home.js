/* Home panels consume existing APIs. Coupon rules and Telegram stay on the server. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const element = (tag, text = '', cls = '') => {
    const el = document.createElement(tag); el.textContent = text; el.className = cls; return el;
  };
  const write = (id, value) => { if ($(id)) $(id).textContent = value; };
  const number = n => Number.isFinite(n) ? n.toLocaleString('tr-TR', {minimumFractionDigits:2, maximumFractionDigits:2}) : '—';
  const time = v => v ? new Date(v).toLocaleString('tr-TR', {timeZone:'Europe/Istanbul', day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit'}) : 'Gözlem bilinmiyor';
  const books = ['Crown', 'Bet365', 'Sbobet'];
  const active = () => !document.hidden && globalThis.BetAppUI?.active('home');
  const pending = new Map();
  let expiryTimer,lastDaily;

  function message(id, text, retry) {
    const box = $(id); if (!box) return;
    box.replaceChildren(); const state = element('div', '', 'panel-empty');
    state.append(element('p', text));
    if (retry) { const button = element('button', 'Yeniden dene'); button.type='button'; button.onclick=retry; state.append(button); }
    box.append(state); box.setAttribute('aria-busy','false');
  }
  async function request(key, path, render, method = 'GET') {
    if (!active() || pending.has(key)) return;
    pending.set(key, true); $(key)?.setAttribute('aria-busy','true');
    try {
      const response = await fetch(path, {method, signal:AbortSignal.timeout(15000)});
      if (!response.ok) throw new Error(String(response.status));
      const data = await response.json(); if (active()) render(data);
    } catch (error) {
      if (active()) {
        if(key==='home-coupon-list')globalThis.BetAppRevision?.failed('coupons');
        if(key==='home-coupon-list' && lastDaily?.items?.length && lastDaily.items.every(eligible) && (!lastDaily.valid_until || Date.parse(lastDaily.valid_until)>Date.now())){write('home-coupon-observed','API hatası · Henüz geçerli kaydedilmiş kupon korunuyor.');return;}
        message(key, error.message==='504'?'API 504 zaman aşımı. Yeniden deneyin.':'Veri bağlantısı bekleniyor. Diğer panelleri kullanabilirsiniz.', refresh);
        if (key==='home-coupon-list') { write('home-coupon-total','—'); write('home-coupon-date','Tarih doğrulanamadı'); }
        if (key==='home-live-list') write('home-live-count','Durum bekleniyor');
      }
    } finally { pending.delete(key); $(key)?.setAttribute('aria-busy','false'); }
  }
  function openMatch(button, match) {
    button.href='/match/'+match.id;button.target='_blank';button.rel='noopener noreferrer'; button.setAttribute('aria-label',`${match.home_team} — ${match.away_team}, analizi aç`);

  }
  function fresh(observed, expires) {
    const seen=Date.parse(observed), end=Date.parse(expires), now=Date.now();
    return Number.isFinite(seen) && seen<=now && now-seen<=900000 && Number.isFinite(end) && end>now;
  }
  function eligible(item) {
    const q=item.quote, r=item.recommendation, m=item.match;
    return m?.status==='scheduled' && Date.parse(m.kickoff_at)>Date.now() && r?.qualifies===true &&
      r.probability>=.70 && r.reliability>=.80 && r.score>=.80 &&
      books.includes(q?.bookmaker) && Number.isFinite(q.price) && q.price>1 && fresh(q.observed_at,q.expires_at);
  }
  function renderCoupon(data) {
    if(data.publication_version && data.publication_version===lastDaily?.publication_version && data.items?.length===lastDaily.items?.length){lastDaily=data;return;}
    lastDaily=data;
    clearTimeout(expiryTimer);
    const items=data.items || [], box=$('home-coupon-list'); if (!box) return;
    write('home-coupon-date',data.coupon_date || 'Bugünün kuponu');
    write('home-coupon-total','—'); write('home-coupon-size',`${items.length} maç · Dinamik seçim`);
    const duplicates=new Set(items.map(i=>i.match?.id)).size!==items.length;
    if (!items.length || duplicates || !items.every(eligible)) {
      message('home-coupon-list',!items.length ? data.message || 'Bugün kalite şartlarını geçen, taze fiyatlı seçim bulunamadı.' : 'Seçimlerin güncel fiyat doğrulaması bekleniyor.', refresh);
      if(data.diagnostics){const d=data.diagnostics;const info=element('p',`İncelenen: ${d.inspected_matches ?? '—'} · Kaliteli adaylı maç: ${d.quality_matches ?? '—'} · Taze eşleşen fiyatlı maç: ${d.eligible_matches ?? '—'} · Kaliteden elenen: ${d.match_rejections?.quality_rejected ?? 0}`,'panel-footnote');box.append(info);}
      write('home-coupon-observed',data.data_updates?.checked_at ? `Son kontrol: ${time(data.data_updates.checked_at)}` : 'Kalite eşikleri korunur; kupon zorla doldurulmaz.');
      return;
    }
    box.replaceChildren();
    for (const item of items) {
      const {match:m,recommendation:r,quote:q}=item;
      const row=element('a','','coupon-fixture'); openMatch(row,m);
      const context=element('span','','fixture-context');
      context.append(element('time',time(m.kickoff_at)),element('small',m.league || 'Lig kaydı'));
      const teams=element('span','','fixture-teams'); teams.append(element('strong',m.home_team),element('span',m.away_team));
      const pick=element('span','','fixture-pick'); pick.append(element('small','Seçim'),element('strong',r.label));
      const price=element('span','','fixture-price'); price.append(element('strong',number(q.price)),element('small',q.bookmaker));
      const quality=element('span','','fixture-quality'); quality.append(element('strong',`%${Math.round(r.probability*100)}`),element('small','Model olasılığı'));
      row.append(context,teams,pick,price,quality,element('span','›','fixture-arrow')); box.append(row);
    }
    write('home-coupon-total',number(data.combined_odds));
    write('home-coupon-observed',`Son fiyat gözlemi: ${time(new Date(Math.max(...items.map(i=>Date.parse(i.quote.observed_at)))).toISOString())}`);
    const expires=Math.min(...items.map(i=>Math.min(Date.parse(i.quote.expires_at),Date.parse(i.quote.observed_at)+900000)));
    expiryTimer=setTimeout(()=>{
      write('home-coupon-total','—'); message('home-coupon-list','Fiyatların tazelik süresi doldu. Güncel kaynak fiyatları bekleniyor.',refresh);
    },Math.max(1,expires-Date.now()));
  }
  function renderLive(data) {
    const box=$('home-live-list'); if (!box) return;
    write('home-live-count',`${data.total ?? 0} maç canlı`);
    const items=(data.items || []).filter(m=>globalThis.BetAppOdds.liveStates.includes(m.status));
    if (!items.length) { message('home-live-list','Şu anda canlı maç yok. Günün fikstürünü maç merkezinde inceleyebilirsiniz.'); return; }
    box.replaceChildren();
    for (const m of items.slice(0,3)) {
      const row=element('a','','live-fixture'); openMatch(row,m);
      row.append(element('span',m.live_minute ? `${m.live_minute}′` : 'Canlı','live-minute'));
      const main=element('span','','live-teams');
      main.append(element('span',m.home_team),element('strong',m.ft_home==null || m.ft_away==null?'—':`${m.ft_home} – ${m.ft_away}`),element('span',m.away_team));
      main.append(element('small',m.league));row.append(main,element('span','›','fixture-arrow'));box.append(row);
    }
  }
  function observedPrice(market, stage, side) {
    const price=market[stage]?.[side], seen=Date.parse(market.field_observed_at?.[stage+'_'+side]);
    return Number.isFinite(price) && price>1 && Number.isFinite(seen) && seen<=Date.now() && Date.now()-seen<=900000 ? price : null;
  }
  function renderMovements(data) {
    const box=$('home-movement-list'); if (!box) return; box.replaceChildren();
    for (const m of data.items || []) {
      for (const market of m.odds || []) {
        if (!books.includes(market.bookmaker)) continue;
        const stage=globalThis.BetAppOdds.stage(m,market);
        if (market.market!=='1x2' && (!Number.isFinite(market.opening?.line) || market.opening.line!==market[stage]?.line)) continue;
        const side=market.market==='1x2' || market.market==='ah' ? 'home' : 'over';
        const opening=market.opening?.[side], current=observedPrice(market,stage,side);
        if (!Number.isFinite(opening) || opening<=1 || current==null || opening===current) continue;
        const label=market.market==='1x2'?'Maç sonucu 1':market.market==='ah'?'Ev sahibi handikap':`${market[stage]?.line ?? '—'} Üst`;
        const row=element('a','','movement-fixture');openMatch(row,m);
        const info=element('span','','movement-teams');info.append(element('strong',`${m.home_team} — ${m.away_team}`),element('small',`${market.bookmaker} · ${label}`));
        const prices=element('span','','movement-prices');prices.append(element('del',number(opening)),element('strong',number(current)));
        row.append(info,prices,element('span',`${current>opening?'↗':'↘'} %${Math.abs((current/opening-1)*100).toFixed(1)}`,'movement-change'));
        box.append(row); break;
      }
      if (box.children.length===3) break;
    }
    if (!box.children.length) message('home-movement-list','Açılış ve taze güncel fiyatı birlikte doğrulanmış oran hareketi yok.');
  }
  function renderModel(data) {
    const box=$('home-model-body'); if (!box) return;
    const recs=(data.items || []).flatMap(i=>(i.recommendations || []).map(r=>({match:i.match,...r})));
    if (!recs.length) { message('home-model-body',data.error || ((data.cache?.stale || data.cache?.expires_at && Date.parse(data.cache.expires_at)<=Date.now())?'Analiz güncel değil. Yeni veri kontrolü bekleniyor.':'Kalite eşiklerini geçen yaklaşan istatistiksel tahmin bulunmuyor. Oran eksikliği ayrı değerlendirilir.')); return; }
    const r=recs[0];box.replaceChildren();
    const top=element('div','','model-overview');
    const ring=element('div','','probability-ring');ring.style.setProperty('--probability',String(r.probability*100));
    const center=element('div');center.append(element('strong',`%${Math.round(r.probability*100)}`),element('small','Model olasılığı'));ring.append(center);
    const counts=element('dl','','model-counts');
    for (const [key,label] of [['high','Yüksek güven'],['medium','Orta güven'],['low','Düşük güven']]) {
      const line=element('div');line.append(element('dt',label),element('dd',String(recs.filter(v=>v.confidence===key).length)));counts.append(line);
    }
    top.append(ring,counts);box.append(top);
    const pick=element('a','','model-featured');openMatch(pick,r.match);pick.append(element('strong',`${r.match.home_team} — ${r.match.away_team}`),element('span',r.label));box.append(pick);
    box.append(element('p',`Bu görünümde ${recs.length} seçim · ${r.sample_size} maçlık örneklem. Model olasılığı gerçekleşmiş başarı oranı değildir.`,'model-note'));
  }
  function refresh() {
    globalThis.BetAppResults?.refresh();
    request('home-coupon-list','/api/coupons/daily?strategy=balanced',renderCoupon);
    request('home-live-list','/api/matches?view=live&include_odds=true&limit=3',renderLive);
    request('home-movement-list','/api/matches?view=upcoming&include_odds=true&limit=12',renderMovements);
    const snapshot=globalThis.BetAppMarket?.homeSnapshot?.();if(snapshot)renderModel(snapshot);
  }
  globalThis.setInterval?.(()=>{if(lastDaily?.items?.length && (lastDaily.valid_until && Date.parse(lastDaily.valid_until)<=Date.now() || lastDaily.items.some(i=>!eligible(i)))){renderCoupon({...lastDaily,items:[],message:'Kaydedilmiş kuponun maç veya fiyat geçerliliği sona erdi. Yeni doğrulama bekleniyor.'});}},1000);
  globalThis.BetAppHome=Object.freeze({refresh,renderModel,acceptRevision:meta=>{const fresh=meta.daily?.balanced;if(fresh?.valid && fresh.version===lastDaily?.publication_version)lastDaily.valid_until=fresh.valid_until;},refreshCoupon:()=>request('home-coupon-list','/api/coupons/daily?strategy=balanced',renderCoupon)});
  refresh();
})();
