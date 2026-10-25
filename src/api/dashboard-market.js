/* Kuponanalizii: API presentation, URL history and lazy loading. No model calculations. */
(() => {
'use strict';
const $=id=>document.getElementById(id),value=id=>$(id)?.value || '';
const node=(tag,text='',cls='')=>{const el=document.createElement(tag);el.textContent=text;el.className=cls;return el;};
const write=(id,text)=>{if($(id))$(id).textContent=text;},pct=v=>v==null?'—':'%'+(100*v).toFixed(1),num=v=>v==null?'—':Number(v).toLocaleString('tr-TR',{minimumFractionDigits:2,maximumFractionDigits:2});
const date=v=>v?new Date(v).toLocaleString('tr-TR',{timeZone:'Europe/Istanbul',dateStyle:'medium',timeStyle:'short'}):'Veri bekleniyor';
const today=()=>new Intl.DateTimeFormat('en-CA',{timeZone:'Europe/Istanbul',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
const families={all:'Tümü',result:'Sonuç',double_chance:'Çifte Şans',goals:'Gol',first_half_goals:'1. Yarı Gol',second_half_goals:'2. Yarı Gol',btts:'KG',asian_handicap:'Handikap',corners:'Korner',cards:'Sarı + Kırmızı',yellow_cards:'Sarı Kart',red_cards:'Kırmızı Kart'};
const pickLabel=r=>r?.market==='double_chance'?({'1x':'Çifte Şans 1X — Ev Sahibi veya Beraberlik','x2':'Çifte Şans X2 — Beraberlik veya Deplasman','12':'Çifte Şans 12 — Beraberlik Olmaz'}[r.selection]||r.label):r?.label;
const confidences={high:'Yüksek',medium:'Orta',low:'Düşük'},outcomes={won:'✓ Tuttu',lost:'✕ Kaybetti',pending:'◷ Bekliyor',awaiting_data:'⚠ Veri eksik',void:'— Geçersiz',disputed:'⚠ İncelenmeli'};
const pages=['home','predictions','markets','matches','performance','coupon','lessons'];
const state={page:'home',market:'goals',offset:0,performanceOffset:0,controllers:new Map(),lastMatch:null};
function cell(row,text,cls=''){const el=node('td',text,cls);row.append(el);return el;}
function empty(id,text,span,retryFn){const box=$(id);if(!box)return;box.replaceChildren();if(span){const row=node('tr'),td=cell(row,'','empty-state');td.colSpan=span;td.append(node('span',text));if(retryFn){const btn=node('button','Yeniden Dene','retry-action');btn.type='button';btn.onclick=()=>retryFn();td.append(btn);}box.append(row);}else{const wrap=node('div','','empty-state');wrap.append(node('p',text));if(retryFn){const btn=node('button','Yeniden Dene','retry-action');btn.type='button';btn.onclick=()=>retryFn();wrap.append(btn);}box.append(wrap);}box.setAttribute('aria-busy','false');}
function loading(id,span){const box=$(id);if(!box)return;if(!box.children.length || box.dataset?.loaded!=='true')empty(id,'API verileri yükleniyor…',span);box.setAttribute('aria-busy','true');}
function done(id){const box=$(id);if(box){box.setAttribute('aria-busy','false');box.dataset.loaded='true';}}
function errorText(e){if(e.code==='analysis_publication_pending')return 'Kaydedilmiş maç analizi güncelleniyor; arka plan yayını bekleniyor.';if(e.code==='analysis_archive_missing')return 'Kayıtlı maç öncesi analiz yok; geçmişe dönük tahmin üretilmez.';if(e.name==='TimeoutError' || e.status===504)return 'API isteği zaman aşımına uğradı. Yeniden deneyin.';return e.status===503?'PostgreSQL verileri şu anda kullanılamıyor. Yeniden bağlantı bekleniyor.':'API kullanılamıyor. Bağlantı bekleniyor; diğer bölümleri kullanabilirsiniz.';}
async function api(path,signal){const r=await fetch(path,{signal:signal || AbortSignal.timeout(15000)});if(!r.ok){const e=new Error('API unavailable');e.status=r.status;try{e.code=(await r.json()).detail?.code;}catch(_){}throw e;}return r.json();}
async function request(key,path,success,failure){const pending=state.controllers.get(key);if(pending && !pending.signal.aborted && pending.path===path)return;pending?.abort();const c=new AbortController();c.path=path;state.controllers.set(key,c);try{const started=Date.now();const data=await api(path,AbortSignal.any([c.signal,AbortSignal.timeout(15000)]));if(!c.signal.aborted){if(data.cache?.checked_at)data.requestElapsed=Date.now()-started;success(data);}}catch(e){if(!c.signal.aborted){if(['home','picks'].includes(key))globalThis.BetAppRevision?.failed('predictions');failure?.(e);}}finally{if(state.controllers.get(key)===c)state.controllers.delete(key);}}
function openRow(row,id){row.dataset.matchId=String(id);row.tabIndex=0;row.setAttribute('aria-label','Maç analizini aç');row.onclick=()=>globalThis.BetAppDashboard?.selectMatch(id);row.onkeydown=e=>{if(['Enter',' '].includes(e.key)){e.preventDefault();row.onclick();}};}
function availabilityMessage(a){return ({insufficient_data:'Yetersiz istatistiksel geçmiş. Tam korner veya sarı + kırmızı kart kayıtları bekleniyor.',no_upcoming_matches:'Bu tarih ve lig için yaklaşan maç yok.',filtered_out:'Nitelikli tahminler var; güven veya olasılık filtresi dışında.',no_qualified_recommendations:'Modelin kalite ve sıralama eşiklerini geçen tahmin yok.'})[a?.status] || 'Bu filtrede modelin seçtiği nitelikli tahmin yok.';}
const oddsReasons={no_source_rows:'Kaynakta bu market satırı yok',source_market_unavailable:'Kaynak bu marketi sağlamıyor',line_mismatch:'Market çizgisi eşleşmiyor',odds_stale:'Oranın güncelliği yitirilmiş',odds_unknown:'Oran zamanı doğrulanmamış',unsupported_bookmaker:'Bahis şirketi desteklenmiyor',observed_after_cutoff:'Kanıt zamanı veri kesiminden sonra',missing_stage_price:'Kaynakta bu seçim için fiyat yok',match_not_scheduled:'Maç başlamış veya planlı değil'};
const oddsReason=vo=>oddsReasons[vo?.reason] || ((vo?.freshness==='stale' || vo?.expires_at && Date.parse(vo.expires_at)<=now())?'Oranın güncelliği yitirilmiş':vo?.message || 'Oran henüz doğrulanmadı');
let checkedClock=null;
function acceptClock(p){const stamp=Date.parse(p.cache?.checked_at);if(Number.isFinite(stamp))write('header-last',date(p.cache.checked_at));if(Number.isFinite(stamp)){const effective=stamp+(p.requestElapsed || 0);checkedClock={stamp:checkedClock?Math.max(now(),effective):effective,received:Date.now()};}}
const now=()=>checkedClock?checkedClock.stamp+Date.now()-checkedClock.received:Date.now();
const upcoming=m=>m.status==='scheduled' && Date.parse(m.kickoff_at)>now();
const fresh=vo=>vo?.has_odds && vo.coupon_eligible && vo.freshness==='fresh' && (!vo.expires_at || Date.parse(vo.expires_at)>now());
function snapshotNote(p){const c=p.cache || {};write('upcoming-board',c.active_window?'Bugün + sonraki 3 gün':'Önümüzdeki 7 gün');return `${c.active_window?'Bugün + sonraki 3 gün · ':''}Analiz: ${date(p.generated_at)} · Veri kontrolü: ${date(c.checked_at)}${(c.stale || c.expires_at && Date.parse(c.expires_at)<=now())?' · Veriler güncel değil; yeni analiz bekleniyor':''}`;}
function card(item){const m=item.match,recs=(item.recommendations || []).slice(0,2);if(!recs.length)return null;const el=node('article','','prediction-card');el.classList.add('home-market-card');el.setAttribute('role','button');el.setAttribute('aria-label',`${m.home_team} — ${m.away_team}, analizi aç`);el.tabIndex=0;el.dataset.matchId=String(m.id);
 const meta=node('div','','card-meta');meta.append(node('span',m.league,'league-label'),node('time',date(m.kickoff_at)));el.append(meta);const teams=node('h3','','card-teams');teams.append(node('span',m.home_team),node('span',m.away_team));el.append(teams);
 const primary=node('div','','pick-primary');primary.append(node('span','✦ MODELİN ÖNE ÇIKARDIĞI','eyebrow'));const line=node('div','','pick-line');line.append(node('span',pickLabel(recs[0])),node('strong',pct(recs[0].probability)));primary.append(line);
 const vo=recs[0].verified_odds;
 const metaPick=node('div','','pick-meta');
 metaPick.append(node('span',`${recs[0].sample_size} maç örneği`),node('span',`${confidences[recs[0].confidence]} güven`,'confidence-badge'));
 if(fresh(vo)){
   metaPick.append(node('span',`En İyi Oran: ${num(vo.best_price)} (${vo.best_bookmaker}) · Değer: ${vo.best_value>=0?'+':''}${(100*vo.best_value).toFixed(1)}%${vo.best_value<0?' · Negatif değer, avantajlı bahis değil':''}`,vo.best_value<0?'odds-badge odds-negative':'odds-badge'));
 } else {
   metaPick.append(node('span','İstatistiksel tahmin · '+oddsReason(vo),'odds-missing-badge'));
 }
 primary.append(metaPick);el.append(primary);
 for(const r of recs.slice(1)){
   const rVo=r.verified_odds;
   const row=node('div','','other-pick');
   const oddsTxt = fresh(rVo) ? ` · Oran: ${num(rVo.best_price)} (${rVo.best_bookmaker})` : ' · '+oddsReason(rVo);
   row.append(node('span',pickLabel(r)),node('b',`${pct(r.probability)} · ${confidences[r.confidence]}${oddsTxt}`));
   el.append(row);
 }
 const footer=node('div','','card-footer');footer.append(node('span',`${recs.length} nitelikli seçim`),node('span','Analizi Aç →','prediction-open'));el.append(footer);el.onclick=()=>globalThis.BetAppDashboard?.selectMatch(m.id);el.onkeydown=e=>{if(e.target===el && ['Enter',' '].includes(e.key)){e.preventDefault();el.onclick();}};return el;
}
function renderCards(id,items,message){const box=$(id);if(!box)return;box.replaceChildren();for(const item of items){if(!upcoming(item.match))continue;const el=card(item);if(el)box.append(el);}if(!box.children.length)empty(id,message || 'Modelin kalite eşiklerini geçen yaklaşan maç tahmini bulunmuyor.');done(id);}
// Model-only selections are statistical estimates, not priced coupon entries.
function groupPicks(items){const grouped=new Map();for(const item of items){if(!grouped.has(item.match.id))grouped.set(item.match.id,{match:item.match,recommendations:[]});grouped.get(item.match.id).recommendations.push(item.recommendation);}return [...grouped.values()];}
function yesterday(){
  loading('yesterday-winners');
  return request('yesterday','/api/predictions/yesterday-winners',data=>{
    const box=$('yesterday-winners');if(!box)return;
    box.replaceChildren();
    for(const pick of (data.items || []).slice(0,2)){
      const el=node('article','','prediction-card');
      const meta=node('div','','card-meta');
      meta.append(node('span',pick.league,'league-label'),node('time',date(pick.kickoff_at)));
      el.append(meta);
      el.append(node('h3',pick.home_team+' — '+pick.away_team,'card-teams'));
      el.append(node('p','✓ Kazandı · '+pickLabel(pick),'pick-line'));
      el.append(node('p','Maç sonucu: '+(pick.ft_home==null?'—':pick.ft_home)+' - '+(pick.ft_away==null?'—':pick.ft_away),'pick-meta'));
      el.tabIndex=0;el.setAttribute('role','button');
      el.onclick=()=>globalThis.BetAppDashboard?.selectMatch(pick.match_id);
      el.onkeydown=e=>{if(['Enter',' '].includes(e.key)){e.preventDefault();el.onclick();}};
      box.append(el);
    }
    if(!box.children.length)empty('yesterday-winners','Dün maç öncesinde kaydedilip kazandığı doğrulanan tahmin bulunmuyor.');
    done('yesterday-winners');
    write('yesterday-winners-note',data.date+' · '+data.total_won+' kazanan seçim · '+data.settled+' sonuçlanmış seçim · Yalnızca önceden kaydedilmiş tahminler');
  },e=>empty('yesterday-winners',errorText(e),null,()=>yesterday()));
}
let homeRetries=0,homeSnapshot=null;
async function home(){loading('prediction-cards');return request('home','/api/predictions?limit=6',p=>{acceptClock(p);homeRetries=0;p={...p,items:p.items.filter(i=>upcoming(i.match))};homeSnapshot=p;write('home-publication-health',`Planlı maç: ${p.cache?.scheduled_matches ?? '—'} · Kaliteli tahmin bulunan maç: ${p.cache?.quality_matches ?? '—'} · Doğrulanmış oranlı maç: ${p.cache?.verified_price_matches ?? '—'} · Yayın: ${p.cache?.status || 'bilinmiyor'}`);globalThis.BetAppHome?.renderModel(p);renderCards('prediction-cards',p.items,'Kalite eşiklerini geçen güncel istatistiksel tahmin yok. Fiyat kapsamı ayrıca gösterilir.');write('home-prediction-note',snapshotNote(p));write('header-last',date(p.cache?.checked_at));},e=>{write('home-prediction-note','Veri kontrolü başarısız · '+errorText(e));if(homeSnapshot && homeSnapshot.cache?.expires_at && Date.parse(homeSnapshot.cache.expires_at)>now()){write('home-prediction-note','Bağlantı kesildi · önceki doğrulanmış analiz korunuyor. '+errorText(e));}else{homeSnapshot=null;globalThis.BetAppHome?.renderModel({items:[],error:errorText(e)});empty('prediction-cards',errorText(e),null,()=>home());}if(homeRetries<2 && state.page==='home'){homeRetries++;setTimeout(()=>{if(state.page==='home')home();},homeRetries*3500);}});}
function selectedOdds(vo){const name=value('pred-bookmaker');if(!name || !vo)return vo;const b=vo.bookmakers?.[name];if(!b)return {...vo,has_odds:false,coupon_eligible:false,reason:'no_source_rows'};return {...vo,has_odds:b.current_odds!=null,best_price:b.current_odds,best_bookmaker:name,best_value:b.value,best_implied_probability:b.implied_probability,freshness:b.freshness,coupon_eligible:b.coupon_eligible,expires_at:b.expires_at,observed_at:b.observed_at,reason:b.freshness==='stale'?'odds_stale':b.freshness!=='fresh'?'odds_unknown':null};}
let displayedPicks=[],picksSnapshot=null;
function renderPicks(items){displayedPicks=items;const box=$('market-picks');if(!box)return;box.replaceChildren();for(const item of items){if(value('pred-view')==='verified' && !fresh(selectedOdds(item.recommendation.verified_odds)))continue;const m=item.match,r=item.recommendation,row=node('tr'),fixture=cell(row,'');fixture.append(node('strong',`${m.home_team} — ${m.away_team}`),node('small',`${m.league} · ${date(m.kickoff_at)}`));const market=cell(row,'');market.append(node('strong',pickLabel(r)),node('small',families[r.market]));cell(row,'').append(node('strong',pct(r.probability),'probability'));const vo=selectedOdds(r.verified_odds);const oddsCell=cell(row,'');if(fresh(vo)){oddsCell.append(node('strong',`${num(vo.best_price)} (${vo.best_bookmaker})`));const bks=vo.bookmakers || {};const parts=[];for(const bName of ['Crown','Bet365','Sbobet']){const b=bks[bName];if(b && b.current_odds)parts.push(`${bName}: ${num(b.current_odds)} · Kanıt: ${date(b.observed_at)} · ${b.freshness==='fresh'?'Taze':'Güncel değil'}`);else parts.push(`${bName}: kaynakta doğrulanmış fiyat yok`);}if(parts.length)oddsCell.append(node('small',parts.join(' · ')));}else{oddsCell.append(node('small',oddsReason(vo),'muted'));}const valCell=cell(row,'');if(fresh(vo) && vo.best_value!=null){const v=vo.best_value;valCell.append(node('strong',`${v>=0?'+':''}${(100*v).toFixed(1)}%`,v>=0?'positive-value':'negative-value'));valCell.append(node('small',`İma: ${pct(vo.best_implied_probability)} · ${v>0?'Pozitif model değeri':'Avantajlı bahis değil'}`));}else{valCell.append(node('span',oddsReason(vo),'muted'));}cell(row,confidences[r.confidence]);const q=r.evidence || {};cell(row,`${r.sample_size} maç · Güven endeksi ${pct(r.reliability)}${q.home_sample!=null?` · Takım örneği ${q.home_sample}/${q.away_sample}`:''}`);row.dataset.kickoff=m.kickoff_at;row.dataset.recommendationId=r.id;openRow(row,m.id);box.append(row);}if(!box.children.length)empty('market-picks','Güncel doğrulanmış seçim yok; oranın güncelliği yitirilmiş veya kaynak fiyatı doğrulanmamış.',7);done('market-picks');}
async function refresh(){if(state.page!=='predictions')return;loading('market-picks',7);const q=new URLSearchParams({statistical_all:'true',priced_only:String(value('pred-view')==='verified'),limit:'20',offset:String(state.offset),market:value('pred-market') || 'all',sort:value('market-sort') || 'ranking',min_probability:String(Math.max(0,Math.min(100,Number(value('min-probability') || 0)))/100)});for(const [id,key] of [['pred-date','date'],['pred-league','league'],['prediction-confidence','confidence'],['pred-team','team'],['pred-bookmaker','bookmaker']])if(value(id))q.set(key,value(id));return request('picks','/api/predictions/best?'+q,p=>{acceptClock(p);picksSnapshot=p;const items=p.items.filter(i=>upcoming(i.match));renderPicks(items);if(!items.length)empty('market-picks',availabilityMessage(p.availability),7);write('prediction-window',snapshotNote(p));write('market-board-note',`${p.items.length} istatistiksel tahmin · ${p.evaluated_matches} maç değerlendirildi`);if($('market-previous'))$('market-previous').disabled=state.offset===0;if($('market-next'))$('market-next').disabled=p.total!=null?state.offset+20>=p.total:p.items.length<20;},e=>{empty('market-picks',errorText(e),7,()=>refresh());write('market-board-note','Veri bekleniyor');});}

function renderCoverage(rows){const box=$('stats-league-coverage');if(!box)return;box.replaceChildren();for(const league of rows || []){const row=node('tr');const label=cell(row,league.name);if(league.stats_backfill){const j=league.stats_backfill;label.append(node('small',` · ${({queued:'Sırada',running:'İşleniyor',completed:'Tamamlandı',partial:'Kısmen tamamlandı',failed:'Yeniden deneme bekleniyor'})[j.status] || j.status}${j.total?' · '+j.processed+'/'+j.total:''}${j.failed?' · '+j.failed+' hata':''}`));}cell(row,String(league.finished_matches));cell(row,String(league.total_stats_rows));for(const metric of ['corners','cards']){const n=league['matches_with_'+metric],percent=league.stats_coverage_percent?.[metric],td=cell(row,`${n} / ${league.finished_matches} · ${percent==null?'—':'%'+percent.toFixed(1)}`),meter=node('div','','coverage-meter'),bar=node('span');bar.style.width=Math.max(0,Math.min(100,percent || 0))+'%';meter.append(bar);td.append(meter);}box.append(row);}if(!rows?.length)empty('stats-league-coverage','Lig bazında kapsam verisi henüz API tarafından sağlanmıyor.',5);}
async function diagnostics(){return request('diagnostics','/api/diagnostics',d=>{renderCoverage(d.stats_coverage_by_league);write('enabled-league-count',d.database_connected?(d.enabled_leagues?.length ?? d.enabled_competitions ?? '—'):'—');const active=d.active_stats_backfill || d.active_backfill;write('active-backfill-progress',!d.database_connected?'PostgreSQL kullanılamıyor':active?`Backfill çalışıyor · ${active.league || d.active_backfill_competition || 'İstatistik'} · ${active.processed+active.failed} / ${active.total} · ${active.failed} hata`:'Aktif istatistik backfill işi yok');write('queued-count',d.queued_stats_backfills?.length ?? d.queued_backfills ?? '—');},e=>{write('active-backfill-progress',errorText(e));empty('stats-league-coverage',errorText(e),5);});}
function metrics(id,entries){const box=$(id);if(!box)return;box.replaceChildren();for(const [label,text,cls] of entries){const el=node('div','','summary-card');el.append(node('span',label),node('b',String(text??'—'),cls || ''));box.append(el);}}
async function marketAnalysis(){if(state.page!=='markets')return;const market=state.market;loading('market-analysis-picks');diagnostics();request('market-picks',`/api/predictions/best?limit=100&market=${market}&date=${today()}`,p=>{renderCards('market-analysis-picks',groupPicks(p.items),availabilityMessage(p.availability));const samples=p.items.map(i=>i.recommendation.sample_size);write('market-analysis-note',samples.length?`Örneklem: ${Math.min(...samples)}–${Math.max(...samples)} maç`:`${p.evaluated_matches} maç değerlendirildi`);},e=>empty('market-analysis-picks',errorText(e)));request('market-performance',`/api/market-performance?limit=1&market=${market}`,d=>{const s=d.summary;metrics('market-category-cards',[['Kaydedilmiş tahmin',s.total],['Sonuçlanan',s.settled],['Gerçek başarı oranı',pct(s.success_rate)]]);},e=>empty('market-category-cards',errorText(e)));}
function activate(market){state.market=market;document.querySelectorAll('[data-market-family]').forEach(b=>{const selected=b.dataset.marketFamily===market;b.setAttribute('aria-selected',String(selected));b.tabIndex=selected?0:-1;});$('market-analysis-panel')?.setAttribute('aria-labelledby','market-tab-'+market);marketAnalysis();}
let recordedWins=null;
let winsAudio=null;
function winningNotification(){
 const control=$('performance-wins-sound');
 if(!control?.checked)return;
 // Sound is user opt-in and produced only for a newly observed settled winner.
 try{const AC=globalThis.AudioContext || globalThis.webkitAudioContext;if(!AC)return;
   winsAudio=winsAudio || new AC();
   if(winsAudio.state!=='running')return;
   const oscillator=winsAudio.createOscillator(),gain=winsAudio.createGain();
   oscillator.type='sine';oscillator.frequency.value=660;gain.gain.value=.035;
   oscillator.connect(gain);gain.connect(winsAudio.destination);
   oscillator.start();oscillator.stop(winsAudio.currentTime+.11);
 }catch(_){}
}
function trendDetail(d,label){
return `${label} · Sonuçlanan ${d.decided} · Kazandı ${d.won} · Kaybetti ${d.lost} · Bekliyor ${d.pending} · Veri eksik ${d.awaiting_data} · İncelenmeli ${d.disputed} · Geçersiz ${d.void} · Başarı ${pct(d.success_rate)} · Ortalama model güveni ${pct(d.mean_probability)} · Brier ${d.brier_score==null?'—':d.brier_score.toFixed(3)}${d.sufficient?'':' · Örneklem düşük'}`;
}
function renderPeriodBars(card,c){
const bars=node('div','','trend-bars');bars.setAttribute('aria-label',`${c.days} günlük dönem karşılaştırma grafiği`);
for(const [key,label] of [['previous','Önceki dönem'],['recent','Son dönem']]){
 const d=c[key],column=node('div','','trend-bar-column');
 const track=node('div','','trend-bar-track'),bar=node('div','',`trend-bar ${key}`);
 if(d.success_rate!=null)bar.style.height=`${d.success_rate*100}%`;
 else bar.classList.add('no-data');
 track.append(bar);column.append(node('strong',pct(d.success_rate)),track,node('b',`${label} · ${c.days} gün`),node('span',`${d.won} / ${d.decided} kazandı · ${d.lost} kaybetti`));
 if(!d.sufficient)column.append(node('small',d.decided?'Örneklem düşük':'Sonuçlanan tahmin yok','muted'));
 column.tabIndex=0;column.title=trendDetail(d,`${d.start} – ${d.end_exclusive} (hariç)`);column.setAttribute('aria-label',column.title);bars.append(column);
}
const delta=c.raw_difference;
const difference=node('p',delta==null?'Fark hesaplanamıyor · sonuçlanan tahmin yok':`${delta>=0?'+':''}${(delta*100).toFixed(1)} puan · Gözlenen fark; öğrenme kanıtı değildir`,delta==null?'muted':delta>0?'trend-positive':delta<0?'trend-negative':'muted');
card.append(bars,difference);
}
function renderTrendChart(data){
const chart=$('trend-chart');chart.replaceChildren();
const weekly=data.aggregation==='weekly',series=weekly?data.weekly:data.daily;
if(!(series||[]).some(d=>d.chart_rate!=null)){
 if(data.truncated)chart.append(node('p','Veri sınırına ulaşıldı; grafik gösterilemiyor. Filtreleri daraltın.','chart-empty'));
 else if(globalThis.BetAppEvidence)globalThis.BetAppEvidence.emptyTimeline(chart,data.daily.slice(-Number(value('trend-window')||7)),'Kayıtlı günlük sonuçlar');
 else chart.append(node('p','Henüz sonuçlanan tahmin yok.','chart-empty'));
 return;
}
chart.append(node('h3',weekly?'Haftalık başarı trendi':'Günlük başarı trendi'),node('p','Çizgi = başarı oranı · Alt etiket = sonuçlanan tahmin adedi · Noktaya dokunun veya odaklanın.','muted'));
const chartWidth=Math.max(280,Math.round(chart.clientWidth || 360)),chartHeight=globalThis.innerWidth<600?200:240,plotBottom=chartHeight-50,plotHeight=plotBottom-30;
const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox',`0 0 ${chartWidth} ${chartHeight}`);svg.setAttribute('role','group');svg.setAttribute('aria-label','Zamana göre başarı oranı ve sonuçlanan tahmin sayısı');
const make=(tag,attrs,text)=>{const el=document.createElementNS(svg.namespaceURI,tag);for(const [k,v] of Object.entries(attrs))el.setAttribute(k,v);if(text!=null)el.textContent=text;svg.append(el);return el;};
for(const rate of [0,50,100]){const y=plotBottom-rate*plotHeight/100;make('path',{d:`M44 ${y}H${chartWidth-20}`,stroke:'#29415e'});make('text',{x:3,y:y+4,fill:'#94a3b8','font-size':12},`%${rate}`);}
const tooltip=node('p','Tarih, sonuç ve model ayrıntıları için bir noktayı seçin.','trend-tooltip');tooltip.setAttribute('role','status');
let previous=null,points=0;
for(let i=0;i<(series||[]).length;i++){
 const d=series[i],x=50+i*(chartWidth-75)/Math.max(1,series.length-1),label=d.end_exclusive?`${d.date} – ${d.end_exclusive} (hariç)`:d.date;
 if((weekly&&i%(chartWidth<600?2:1)===0)||(!weekly&&i%10===0)||i===series.length-1)make('text',{x,y:chartHeight-10,'text-anchor':'middle',fill:'#94a3b8','font-size':11},d.date.slice(5).split('-').reverse().join('.'));
 if(d.chart_rate==null){previous=null;continue;}
 const y=plotBottom-d.chart_rate*plotHeight;
 if(previous)make('path',{d:`M${previous.x} ${previous.y}L${x} ${y}`,stroke:'#38bdf8','stroke-width':2.5,fill:'none'});
 const dot=make('circle',{cx:x,cy:y,r:5,fill:'#38bdf8',stroke:d.sufficient?'#38bdf8':'#94a3b8','stroke-width':2,tabindex:0,role:'button','aria-label':trendDetail(d,label)});
 const title=document.createElementNS(svg.namespaceURI,'title');title.textContent=trendDetail(d,label);dot.append(title);
 for(const event of ['pointerenter','focus','click'])dot.addEventListener(event,()=>{tooltip.textContent=trendDetail(d,label);});
 if(weekly||i%3===0)make('text',{x,y:chartHeight-30,'text-anchor':'middle',fill:'#94a3b8','font-size':11},`${d.decided}`);
 previous={x,y};points++;
}
chart.append(svg,tooltip);
if(!points)chart.append(node('p',data.truncated?'Veri sınırına ulaşıldı; grafik gösterilemiyor. Filtreleri daraltın.':'Henüz sonuçlanan tahmin yok. Bekleyen veya eksik kayıtlar başarı oranına katılmaz.','muted'));
else if(series.some(d=>d.decided&&!d.sufficient))chart.append(node('p','Örneklem düşük · gri kenarlı noktalar gözlemdir; güvenilir gelişim karşılaştırması değildir.','muted'));
}
let latestTrends=null;
globalThis.addEventListener?.('resize',()=>{if(state.page==='performance' && latestTrends)renderTrendChart(latestTrends);});
function renderTrends(data){
globalThis.BetAppEvidence?.validateTrends(data);
latestTrends=data;
if(!data.comparisons)throw new Error('Invalid trends');
write('trend-status',data.truncated?'Veri sınırına ulaşıldı · İyileşme kanıtlanmadı':'İyileşme kanıtlanmadı');
write('trend-model',`Model değişikliği doğrulanmadı · Kayıtlı sürümler: ${data.model_versions.join(', ')||'Yok'} · ${data.as_of} öncesi 60 gün · İlk gözlenen maç günü: ${data.first_observed_kickoff||'Yok'}`);
if($('trend-as-of')&&!value('trend-as-of'))$('trend-as-of').value=data.as_of;
const periods=$('trend-periods');periods.replaceChildren();
for(const comparison of data.comparisons){
 const card=node('article','','trend-period');card.dataset.days=String(comparison.days);card.hidden=String(comparison.days)!==(value('trend-window')||'7');card.append(node('h3',`${comparison.days} gün / önceki ${comparison.days} gün`));
 renderPeriodBars(card,comparison);
 const evidence=node('details');evidence.append(node('summary','Dönem ayrıntıları ve belirsizlik'));
 evidence.append(node('p',comparison.eligible?`Ham başarı farkı: ${comparison.raw_difference==null?'—':(comparison.raw_difference*100).toFixed(1)+' yüzde puan'} · ${comparison.direction==='increase'?'Artış sinyali; kalıcılık/öğrenme kanıtı değil':comparison.direction==='decrease'?'Düşüş sinyali':'Belirsizlik aralıkları ayrışmıyor'}`:'Yetersiz veya eksik örneklem · güvenilir gelişim karşılaştırması yapılamıyor'));
 for(const [key,label] of [['recent','Son dönem'],['previous','Önceki dönem']]){
  const p=comparison[key];const interval=p.cluster_95?`${pct(p.cluster_95.lower)} – ${pct(p.cluster_95.upper)}`:'—';
  evidence.append(node('p',`${label}: ${p.start} ≤ gün < ${p.end_exclusive} · ${p.won}/${p.decided} tuttu · ${p.lost} kaybetti · ${p.distinct_matches} farklı maç (${p.effective_matches.toFixed(1)} etkin) · Başarı ${pct(p.success_rate)} · Maç gruplu %95 aralık ${interval}`));
  evidence.append(node('p',`Model olasılığı ${pct(p.mean_probability)} · Aynı ${p.probability_valid} kayıtta gerçekleşen ${pct(p.calibration_observed)} · Brier ${p.brier_score==null?'—':p.brier_score.toFixed(3)} · Bekliyor ${p.pending} / Veri eksik ${p.awaiting_data} / İncelenmeli ${p.disputed} / Geçersiz ${p.void} · Maç gününden sonra kaydedilen karar ${p.late_recorded??0} (zamanı bilinen ${p.settlement_timestamp_available??0})`));
  if(p.calibration_gap!=null&&p.calibration_gap<0)evidence.append(node('p','Model olasılığı gerçekleşenden yüksek: aşırı güven olasılığı.'));
  const detail=node('details');detail.append(node('summary',label+' · Lig / market / sürüm dağılımı'));
  for(const group of ['league','market','model_version'])for(const b of p.breakdowns[group])detail.append(node('p',`${group}: ${b.name} · ${b.won}/${b.decided} · ${b.distinct_matches} maç · ${b.sufficient?pct(b.success_rate):'Yetersiz örneklem'} · Brier ${b.brier_score==null?'—':b.brier_score.toFixed(3)}`));
  evidence.append(detail);
 }
 card.append(evidence);
 periods.append(card);
}
const daily=$('trend-daily');daily.replaceChildren();for(const d of data.daily){const row=node('tr');for(const v of [d.date,`${d.won}/${d.decided}`,d.lost,d.distinct_matches,pct(d.success_rate),pct(d.mean_probability),d.brier_score==null?'—':d.brier_score.toFixed(3),`${d.pending} / ${d.awaiting_data} / ${d.disputed} / ${d.void}`])cell(row,String(v));daily.append(row);}
renderTrendChart(data);
}
function refreshTrends(){if(!$('trend-periods'))return;const q=new URLSearchParams();for(const [id,key] of [['performance-from','date_from'],['performance-to','date_to'],['trend-as-of','as_of'],['performance-league','league'],['performance-market','market'],['performance-exact','exact_market'],['performance-confidence','confidence']])if(value(id))q.set(key,value(id));globalThis.BetAppEvidence?.refresh();write('trend-status','Gelişim verisi yükleniyor…');return request('trends','/api/performance-trends?'+q,renderTrends,()=>{latestTrends=null;write('trend-status','Gelişim verisi alınamadı · İyileşme kanıtlanmadı');for(const id of ['trend-periods','trend-chart','trend-daily'])$(id)?.replaceChildren();write('trend-model','');});}
if($('trend-as-of'))$('trend-as-of').onchange=refreshTrends;
if($('trend-window'))$('trend-window').onchange=()=>{document.querySelectorAll('.trend-period').forEach(card=>{card.hidden=card.dataset.days!==value('trend-window');});if(latestTrends)renderTrendChart(latestTrends);};
function renderMarketEvidence(markets){
const box=$('performance-markets');if(!box)return;
box.replaceChildren();
const ordered=[...markets].sort((a,b)=>Number(b.settled>=20)-Number(a.settled>=20)||(b.wilson_95?.lower??-1)-(a.wilson_95?.lower??-1)||b.settled-a.settled||String(a.id).localeCompare(String(b.id)));
let previous=null;
for(const market of ordered){
 const adequate=market.settled>=20;
 if(adequate!==previous){const group=node('tr','','sample-group');const heading=node('th',adequate?'Karşılaştırılabilir · en az 20 sonuçlanan tahmin':'Yetersiz örneklem · 20’den az sonuçlanan tahmin');heading.colSpan=12;heading.scope='colgroup';group.append(heading);box.append(group);previous=adequate;}
 const row=node('tr','',adequate?'sample-comparable':'sample-insufficient');
 cell(row,market.label);cell(row,`${market.won}/${market.settled}`);
 for(const key of ['won','lost','pending','awaiting_data','disputed','void'])cell(row,String(market[key]??0),key);
 const rate=cell(row,pct(market.success_rate));rate.title=`${market.won} tuttu / ${market.settled} sonuçlanan tahmin; yalnızca tarihsel betimleme`;
 cell(row,market.wilson_95?`${pct(market.wilson_95.lower)} – ${pct(market.wilson_95.upper)}`:'—');
 cell(row,pct(market.average_reliability));cell(row,market.average_sample_size==null?'—':market.average_sample_size.toFixed(1));box.append(row);
}
if(!markets.length)empty('performance-markets','Henüz kaydedilmiş tahmin yok.',12);
}
function renderPerformance(data){const summary=data.summary;write('performance-summary',`${summary.total} toplam · ${summary.void} geçersiz · ${summary.awaiting_data} veri eksik · ${summary.disputed??0} incelenmeli · ${summary.settled} sonuçlanan · ${summary.won} kazandı · ${summary.lost} kaybetti · ${summary.pending} bekliyor · Genel mikro başarı ${pct(summary.success_rate)} (${summary.won}/${summary.settled}) · Ortalama model güven endeksi (doğruluk değil) ${pct(summary.average_reliability)}`);renderMarketEvidence(data.markets);
const rank={won:0,lost:1,awaiting_data:2,disputed:3,void:4,pending:5};
const seen=new Set(data.items.filter(i=>i.outcome==='won').map(i=>i.id || i.snapshot_id || [i.match.id,i.recommendation.id,i.captured_at].join(':')));
if(recordedWins && [...seen].some(k=>!recordedWins.has(k)))winningNotification();
recordedWins=seen;
const ordered=[...data.items].sort((a,b)=>(rank[a.outcome]??4)-(rank[b.outcome]??4) || Date.parse(b.match.kickoff_at)-Date.parse(a.match.kickoff_at));
const resultGroups=$('performance-result-groups');
if(resultGroups){resultGroups.replaceChildren();for(const [outcome,label] of [['won','Kazanan Tahminler'],['lost','Kaybeden Tahminler'],['pending','Bekleyen Maçlar'],['awaiting_data','Veri eksik'],['disputed','İncelenmeli'],['void','Geçersiz']]){
const matches=ordered.filter(x=>x.outcome===outcome);
const section=node('section','','performance-result-group '+outcome),title=node('h3',label+' · '+matches.length),list=node('div','','performance-result-list');
section.append(title,list);for(const item of matches){const el=node('button','','performance-result-card');el.type='button';el.append(node('strong',item.match.home_team+' / '+item.match.away_team),node('span',pickLabel(item.recommendation)+' · '+date(item.match.kickoff_at)),node('small','Gerçek sonuç: '+(item.actual??'—')),node('strong',outcomes[item.outcome]||'⚠ Veri eksik','performance-outcome '+item.outcome));el.onclick=()=>globalThis.BetAppDashboard?.selectMatch(item.match.id);list.append(el);}
if(!matches.length)list.append(node('p','Bu sayfada kayıt bulunmuyor.','muted'));resultGroups.append(section);}}
const picks=$('performance-picks');if(picks){picks.replaceChildren();for(const item of ordered){const row=node('tr');cell(row,date(item.captured_at));cell(row,item.match.kickoff_at?date(item.match.kickoff_at):'Tarih bilinmiyor');cell(row,item.match.home_team+' / '+item.match.away_team,'match-name');cell(row,pickLabel(item.recommendation));cell(row,pct(item.recommendation.probability),'model-value');cell(row,item.outcome==='disputed'?`Güncel: ${item.actual??'—'} · Kayıtlı: ${item.recorded_actual??'—'}`:(item.actual==null?'—':String(item.actual)));cell(row,outcomes[item.outcome]||'⚠ Veri eksik','performance-outcome '+item.outcome);openRow(row,item.match.id);picks.append(row);}if(!data.items.length)empty('performance-picks','Bu filtrede kaydedilmiş tahmin yok.',7);}
const exact=$('performance-exact');if(exact && !exact.value){exact.replaceChildren();const all=node('option','Tüm seçimler');all.value='';exact.append(all);for(const market of data.markets){const option=node('option',market.label);option.value=market.id;exact.append(option);}}
write('performance-note',`${data.total} kayıt · Sayfa ${Math.floor(state.performanceOffset/50)+1}`);if($('performance-previous'))$('performance-previous').disabled=state.performanceOffset===0;if($('performance-next'))$('performance-next').disabled=state.performanceOffset+50>=data.total;}

async function refreshPerformance(){if(state.page!=='performance')return;refreshTrends();const q=new URLSearchParams({limit:'50',offset:String(state.performanceOffset),market:value('performance-market') || 'all'});for(const [id,key] of [['performance-exact','exact_market'],['performance-league','league'],['performance-from','date_from'],['performance-to','date_to'],['performance-confidence','confidence']])if(value(id))q.set(key,value(id));loading('performance-picks',7);write('performance-summary','API verileri yükleniyor…');return request('performance','/api/market-performance?'+q,d=>{renderPerformance(d);const s=d.summary;metrics('daily-summary',[['Toplam',s.total],['Sonuçlanan',s.settled],['Kazandı',s.won,'won'],['Kaybetti',s.lost,'lost'],['Bekliyor',s.pending,'pending'],['Veri eksik',s.awaiting_data,'awaiting_data'],['İncelenmeli',s.disputed??0,'disputed'],['Geçersiz',s.void],['Genel mikro başarı',pct(s.success_rate)]]);done('performance-picks');},e=>{write('performance-summary',e.status===400?'Tarih aralığını kontrol edin.':errorText(e));empty('performance-picks',errorText(e),7);empty('performance-markets','Performans verisi bekleniyor.',12);});}
async function leagues(){return request('leagues','/api/leagues',rows=>{write('enabled-league-count',String(rows.length));document.querySelectorAll('[data-league-options]').forEach(select=>{const before=select.value;select.replaceChildren();const all=node('option','Tüm Ligler');all.value='';select.append(all);for(const league of rows){const option=node('option',league.name);option.value=String(league.id);select.append(option);}select.value=before;});},()=>{});}
function parseRoute(){const hash=(globalThis.location?.hash || '#home').slice(1);if(/^match=\d+$/.test(hash))return {page:'home',match:Number(hash.slice(6))};const [section,search='']=hash.split('?');return {page:pages.includes(section)?section:'home',match:Number(new URLSearchParams(search).get('match')) || null};}
function loadPage(){if(document.hidden)return;if(state.page==='home'){home();yesterday();globalThis.BetAppHome?.refresh();}else if(state.page==='predictions'){globalThis.BetAppFixtures?.refresh();globalThis.BetAppMatchAnalysis?.refresh();if($('analysis-advanced')?.open)refresh();}else if(state.page==='markets')marketAnalysis();else if(state.page==='performance')refreshPerformance();else if(state.page==='lessons')globalThis.BetAppLearning?.refresh();else if(state.page==='coupon')globalThis.BetAppCoupon?.refresh();else globalThis.BetAppDashboard?.refreshMatches();}
function restore(){const r=parseRoute(),changed=state.page!==r.page;state.page=r.page;document.title='Kuponanalizii · '+({home:'Futbol Analiz Merkezi',predictions:'Tahminler',markets:'İstatistikler',matches:'Canlı Skor & Maç Merkezi',bulletin:'Bülten',performance:'Geçmiş Performans',lessons:'Dersler',coupon:'Kupon Analizi'}[state.page]);if(changed){globalThis.BetAppMatchAnalysis?.deactivate();globalThis.BetAppFixtures?.deactivate();}if(changed)for(const [key,c] of state.controllers)if(key!=='leagues')c.abort();document.querySelectorAll('[data-page]').forEach(s=>s.hidden=s.dataset.page!==(state.page));document.querySelectorAll('[data-page-link]').forEach(link=>{if(link.dataset.pageLink===state.page)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');});document.querySelector('.bottom-nav [aria-current=page]')?.scrollIntoView?.({block:'nearest',inline:'center'});if(changed || !state.loaded){state.loaded=true;loadPage();if(changed)document.querySelector(`[data-page="${state.page}"] h1`)?.focus({preventScroll:true});}if(r.match){if(state.lastMatch!==r.match && globalThis.BetAppDashboard){state.lastMatch=r.match;globalThis.BetAppDashboard.selectMatch(r.match,false,false,true);}}else{state.lastMatch=null;globalThis.BetAppDashboard?.dismissDetail?.();}}
function openMatch(id){state.lastMatch=id;globalThis.history?.pushState({betMatch:id},'',`#${state.page}?match=${id}`);}
function closeMatch(){state.lastMatch=null;if(globalThis.history?.state?.betMatch)globalThis.history.back();else{globalThis.history?.replaceState(null,'',`#${state.page}`);restore();}}
function navigate(page){if(!pages.includes(page))return;globalThis.history?.pushState(null,'',`#${page}`);restore();}
function tabs(selector,action){document.querySelectorAll(selector).forEach(b=>{b.onclick=()=>action(b);b.onkeydown=e=>{const items=[...document.querySelectorAll(selector)],i=items.indexOf(b);let next;if(e.key==='ArrowRight')next=(i+1)%items.length;else if(e.key==='ArrowLeft')next=(i+items.length-1)%items.length;else if(e.key==='Home')next=0;else if(e.key==='End')next=items.length-1;else return;e.preventDefault();items[next].focus();action(items[next]);};});}
globalThis.BetAppUI=Object.freeze({active:p=>state.page===p || p==='matches' && state.page==='bulletin',openMatch,closeMatch,restore,navigate,refresh:()=>{leagues();loadPage();},isArctic:true});
$('analysis-advanced')?.addEventListener?.('toggle',()=>{if($('analysis-advanced').open && state.page==='predictions')refresh();});
function acceptRevision(meta){for(const snapshot of [homeSnapshot,picksSnapshot]){const known=snapshot?.cache?.publication_versions;if(!known || !meta.boards || Object.keys(known).length!==Object.values(meta.boards).filter(b=>b.valid).length)continue;if(Object.entries(known).every(([k,v])=>meta.boards[k]?.valid && meta.boards[k].version===v))snapshot.cache.expires_at=new Date(Math.min(...Object.keys(known).map(k=>Date.parse(meta.boards[k].expires_at)))).toISOString();}}
globalThis.BetAppMarket=Object.freeze({refresh,refreshPerformance,activate,oddsReason,refreshHome:home,acceptRevision,homeSnapshot:()=>homeSnapshot});
document.querySelectorAll('[data-page-link],.brand,.primary-button,.secondary-button,.text-link,.coverage-link').forEach(link=>link.onclick=e=>{e.preventDefault();navigate(link.getAttribute('href').slice(1));});
globalThis.addEventListener?.('popstate',restore);globalThis.addEventListener?.('hashchange',restore);
const changed=()=>{state.offset=0;refresh();};for(const id of ['pred-view','pred-date','pred-league','pred-market','prediction-confidence','min-probability','market-sort','pred-bookmaker'])if($(id))$(id).onchange=changed;
if($('performance-wins-sound'))$('performance-wins-sound').onchange=()=>{if($('performance-wins-sound').checked){try{const AC=globalThis.AudioContext || globalThis.webkitAudioContext;if(AC){winsAudio=winsAudio || new AC();winsAudio.resume?.();}}catch(_){}}};
let searchTimer;if($('pred-team'))$('pred-team').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(changed,300);};if($('global-search'))$('global-search').oninput=()=>{if($('pred-team'))$('pred-team').value=value('global-search');clearTimeout(searchTimer);searchTimer=setTimeout(()=>{navigate('predictions');const advanced=$('analysis-advanced');if(advanced){advanced.open=true;advanced.scrollIntoView?.({block:'start'});}changed();},300);};
tabs('[data-market-family]',b=>activate(b.dataset.marketFamily));
for(const id of ['performance-market','performance-exact','performance-league','performance-from','performance-to','performance-confidence'])if($(id))$(id).onchange=()=>{state.performanceOffset=0;refreshPerformance();};
if($('market-previous'))$('market-previous').onclick=()=>{state.offset=Math.max(0,state.offset-20);refresh();};if($('market-next'))$('market-next').onclick=()=>{state.offset+=20;refresh();};if($('performance-previous'))$('performance-previous').onclick=()=>{state.performanceOffset=Math.max(0,state.performanceOffset-50);refreshPerformance();};if($('performance-next'))$('performance-next').onclick=()=>{state.performanceOffset+=50;refreshPerformance();};if($('today-board'))$('today-board').onclick=()=>{$('pred-date').value=today();changed();};if($('upcoming-board'))$('upcoming-board').onclick=()=>{$('pred-date').value='';changed();};
if($('search-toggle'))$('search-toggle').onclick=()=>{const open=$('global-search-label').classList.toggle('search-open');$('search-toggle').setAttribute('aria-expanded',String(open));if(open)$('global-search').focus();};
const toggle=$('sidebar-toggle');
function sidebar(collapsed){document.body?.classList.toggle('sidebar-collapsed',collapsed);toggle?.setAttribute('aria-expanded',String(!collapsed));toggle?.setAttribute('aria-label',collapsed?'Menüyü genişlet':'Menüyü daralt');}
try{sidebar(globalThis.localStorage?.getItem('sidebar-collapsed')==='true');}catch(_){}
if(toggle)toggle.onclick=()=>{const collapsed=toggle.getAttribute('aria-expanded')==='true';sidebar(collapsed);try{globalThis.localStorage?.setItem('sidebar-collapsed',String(collapsed));}catch(_){}};
// Expire visible fixtures even when the next network request is delayed or fails.
function expireVisible(){if(homeSnapshot){const items=homeSnapshot.items.filter(i=>upcoming(i.match));if(items.length!==homeSnapshot.items.length){homeSnapshot={...homeSnapshot,items};renderCards('prediction-cards',items);}}document.querySelectorAll('#market-picks tr[data-kickoff]').forEach(row=>{if(Date.parse(row.dataset.kickoff)<=now())row.remove();});}
let expiredQuotes=new Set();
let day=today();
function tick(){expireVisible();if(homeSnapshot){write('home-prediction-note',snapshotNote(homeSnapshot));if(homeSnapshot.cache?.expires_at && Date.parse(homeSnapshot.cache.expires_at)<=now()){homeSnapshot={...homeSnapshot,items:[]};renderCards('prediction-cards',[],'Analiz güncel değil; yeni veri bekleniyor.');globalThis.BetAppHome?.renderModel(homeSnapshot);}}if(picksSnapshot && state.page==='predictions')write('prediction-window',snapshotNote(picksSnapshot));const quotes=[...(homeSnapshot?.items || []).flatMap(i=>i.recommendations || []),...displayedPicks.map(i=>i.recommendation)];let changed=false;for(const r of quotes){const expiry=r.verified_odds?.expires_at;if(expiry && Date.parse(expiry)<=now() && !expiredQuotes.has(expiry)){expiredQuotes.add(expiry);changed=true;}}if(changed){if(homeSnapshot)renderCards('prediction-cards',homeSnapshot.items);if(state.page==='predictions')renderPicks(displayedPicks.filter(i=>upcoming(i.match)));}const next=today();if(next!==day){day=next;state.controllers.get('home')?.abort();loadPage();}}
document.addEventListener?.('visibilitychange',()=>{if(!document.hidden){tick();if(globalThis.BetAppRevision)globalThis.BetAppRevision.check();else loadPage();}});
leagues();restore();
// Do not refetch and rebuild every visible page each 30 seconds.
// Explicit navigation, user refresh, and the day rollover remain triggers.
// The 1s tick only expires visible evidence; it makes no network requests.
setInterval(tick,1000);
})();
