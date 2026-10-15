/* Read-only presentation of immutable learning editions; no statistical recomputation. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const el=(tag,text='',cls='')=>{const n=document.createElement(tag);n.textContent=text;n.className=cls;return n;};
  const put=(id,text)=>{$(id).textContent=text;};
  const num=v=>Number.isFinite(v)?v.toLocaleString('tr-TR',{maximumFractionDigits:3}):'—';
  const pct=v=>Number.isFinite(v)?'%'+(v*100).toLocaleString('tr-TR',{maximumFractionDigits:1}):'—';
  const date=v=>v && Number.isFinite(Date.parse(v))?new Date(v).toLocaleString('tr-TR',{timeZone:'Europe/Istanbul',dateStyle:'medium',timeStyle:'short'}):'Tarih bilinmiyor';
  const markets={goals:'Gol',first_half_goals:'İlk yarı gol',second_half_goals:'İkinci yarı gol',corners:'Korner',cards:'Kart',yellow_cards:'Sarı kart',red_cards:'Kırmızı kart',result:'Maç sonucu',btts:'Karşılıklı gol',asian_handicap:'Asya handikap',double_chance:'Çifte şans'};
  const statuses={insufficient_sample:'Örneklem yetersiz · Gözlem',supported_candidate:'Kanıt destekli ders adayı',inconclusive:'Kesinleşmemiş gözlem'};
  const confidence={low:'Düşük',moderate:'Orta',medium:'Orta',high:'Yüksek'};
  const state={data:null,controller:null,generation:0,offset:0,reportsOffset:0,leagues:new Map()};
  const size=10;
  const kind=r=>r.scope_key.split(':')[0];
  const label=r=>r.league_id!=null?(state.leagues.get(String(r.league_id)) || 'Lig #'+r.league_id):r.market?(markets[r.market] || r.market):r.model_version || 'Genel';
  function option(box,value,text){const n=el('option',text);n.value=value;box.append(n);}
  async function get(path,signal){const r=await fetch('/api/'+path,{signal:AbortSignal.any([signal,AbortSignal.timeout(15000)])});if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}
  async function pages(path,id,signal){
    const rows=[];let offset=0;
    while(true){const d=await get(`learning/${path}${path.includes('?')?'&':'?'}report_id=${id}&limit=100&offset=${offset}`,signal);
      if(d.status!=='ready' || d.report_id!==id || !Array.isArray(d.items) || !Number.isInteger(d.total))throw new Error('Rapor şeması uyuşmuyor');
      rows.push(...d.items);offset+=d.items.length;if(offset>=d.total)return rows;
      if(!d.items.length || offset>100000)throw new Error('Rapor tamamlanamadı');
    }
  }
  function metric(label,value){const n=el('article','','learning-metric');n.append(el('span',label),el('strong',value));$('learning-summary').append(n);}
  function renderSummary(){const {summary:s,coverage:c,meta:m}=state.data;$('learning-summary').replaceChildren();
    for(const [name,value] of [['Kaydedilen tahmin',num(c.saved_predictions)],['Sonuçlanan',num(c.recorded_terminal)],['Gerçekleşen başarı',pct(s.success_rate)],['Beklenen başarı',pct(s.mean_probability)],['Brier skoru',num(s.brier_score)],['Öğrenme durumu',state.data.lessons.some(r=>r.status==='supported_candidate')?'Ders adayı var':'Gözlem aşaması']])metric(name,value);
    put('learning-coverage',`${num(s.predictions)} geçerli sonuç · ${num(s.matches)} maç · Maç bekleyen: ${num(c.match_pending)} · Sonuç verisi bekleyen: ${num(c.result_data_pending)} · Geçersiz: ${num(c.exclusions?.void)} · Log loss: ${num(s.log_loss)}`);
    put('learning-status',`${m.stale?'Eski rapor · Güncelleme bekleniyor. ':''}${$('learning-report').value?'Arşiv raporu · ':''}Rapor #${m.report_id} · ${date(m.created_at)} · Son kaynak kontrolü: ${date(m.checked_at)}`);
  }
  function scopes(){const box=$('learning-scope');box.replaceChildren();option(box,'','Tümü');const filter=$('learning-kind').value;
    for(const r of state.data?.lessons || [])if(filter==='all' || kind(r)===filter)option(box,r.scope_key,`${label(r)}${r.league_id!=null && r.market?' · '+(markets[r.market] || r.market):''}`);
    box.disabled=!state.data;
  }
  function card(r){const n=el('article','','learning-card');const head=el('div','','card-meta');const time=el('time',date(r.date));if(r.date)time.dateTime=r.date;
    head.append(el('span',statuses[r.status] || 'İnceleme bekleyen gözlem','status-pill'),time);n.append(head,el('h2',r.title));
    n.append(el('p',`Lig: ${r.league_id!=null?state.leagues.get(String(r.league_id)) || '#'+r.league_id:'Tüm ligler'} · Market: ${r.market?markets[r.market] || r.market:'Tümü'} · Model: ${r.model_version || 'Tümü'}`,'muted'));
    const facts=el('dl','','learning-facts');for(const [name,value] of [['Örneklem',`${num(r.predictions)} tahmin / ${num(r.matches)} maç`],['Beklenen başarı',pct(r.expected_success)],['Gerçekleşen başarı',pct(r.observed_success)],['Güven seviyesi',confidence[r.confidence] || 'Belirtilmedi']])facts.append(el('dt',name),el('dd',value));n.append(facts);
    n.append(el('p',r.status==='insufficient_sample'?'Yeterli veri birikiyor. Bu kayıt doğrulanmış bir ders değildir.':r.status==='supported_candidate'?'Bu aday kayıtlı istatistiksel kanıtla desteklenir; doğrulanmış ders değildir ve model değişikliği uygulanmaz.':'Kanıt henüz kesin bir sonuca ulaşmıyor. Bu gözlem doğrulanmış bir ders değildir.','learning-evidence'));
    const details=el('details'),e=r.evidence || {};details.append(el('summary','Ayrıntıları Gör'));
    for(const text of [`Hesaplama yöntemi: gerçekleşen sonuç ile model olasılığı farkının ortalaması; maç kümeleriyle ağırlıklı Hoeffding sınırı ve çoklu karşılaştırma için Bonferroni düzeltmesi.`, `Kayıtlı yöntem: ${e.calculation || 'Yöntem kaydı bulunmuyor'}`,`Başarı farkı güven aralığı: ${Array.isArray(e.gap_interval)?e.gap_interval.map(pct).join(' – '):'Veri yok'}`,`Aile hata sınırı: ${pct(e.family_alpha)} · Karşılaştırma: ${num(e.family_tests)}`,`Etkin maç örneklemi: ${num(e.effective_matches)} · Gerekli: ${num(e.minimum_effective_matches)}`,`Aynı maçtaki tahminler birlikte değerlendirilir. Tekrarlayan takım ve zaman bağımlılığı kalabilir.`,`Rapor #${state.data.meta.report_id} · Sürüm: ${r.method_version || state.data.meta.method_version}`,`Kanıt kimliği: ${r.data_fingerprint || state.data.meta.data_fingerprint}`])details.append(el('p',text));n.append(details);return n;
  }
  function render(){if(!state.data)return;const filter=$('learning-kind').value,scope=$('learning-scope').value;
    const rows=state.data.lessons.filter(r=>(filter==='all' || kind(r)===filter) && (!scope || r.scope_key===scope));const box=$('learning-lessons');box.replaceChildren();
    for(const r of rows.slice(state.offset,state.offset+size))box.append(card(r));if(!rows.length)box.append(el('p','Bu kapsamda ders veya gözlem kaydı bulunmuyor.','panel-empty'));
    put('learning-page',`${rows.length} kayıt · Sayfa ${Math.floor(state.offset/size)+1} / ${Math.max(1,Math.ceil(rows.length/size))} · Rapor #${state.data.meta.report_id}`);
    $('learning-previous').disabled=state.offset===0;$('learning-next').disabled=state.offset+size>=rows.length;
    const stats=state.data.statistics[filter] || [],selected=stats.find(r=>r.scope_key===scope.split(':').slice(1).join(':'));
    put('learning-statistics',selected?`Seçili kapsam: ${num(selected.predictions)} tahmin · Başarı ${pct(selected.success_rate)} · Brier ${num(selected.brier_score)} · Log loss ${num(selected.log_loss)}`:filter==='all' || filter==='overall'?'Tarih sırasıyla, en yeni gözlemler önce.':`${stats.length} ${filter==='league'?'lig':filter==='market'?'market':'model sürümü'} için kayıtlı istatistik. Ayrıntılı sonuç için ilgili kaydı seçin.`);
  }
  async function reports(signal,append=false){const d=await get(`learning/reports?limit=100&offset=${append?state.reportsOffset:0}`,signal);const select=$('learning-report'),before=select.value;if(!append){select.replaceChildren();option(select,'','Güncel rapor');state.reportsOffset=0;}
    for(const r of d.items)option(select,String(r.report_id),`#${r.report_id} · ${date(r.created_at)} · ${r.method_version}`);state.reportsOffset+=d.items.length;select.value=before;$('learning-more-reports').hidden=state.reportsOffset>=d.total;
  }
  async function refresh(force=false){if(!globalThis.BetAppUI?.active('lessons'))return;if(state.controller && !force)return;
    state.controller?.abort();const controller=new AbortController(),signal=controller.signal;state.controller=controller;const generation=++state.generation;
    $('learning-lessons').setAttribute('aria-busy','true');put('learning-status','Öğrenme verileri yükleniyor…');
    try{const selected=$('learning-report').value;const summary=await get('learning/summary'+(selected?'?report_id='+selected:''),signal);
      if(summary.status==='warming'){state.data=null;$('learning-summary').replaceChildren();$('learning-lessons').replaceChildren();put('learning-status','Yeterli veri birikiyor. İlk öğrenme raporu hazırlanıyor.');put('learning-coverage','');put('learning-page','');put('learning-statistics','');scopes();return;}
      const id=summary.report_id;if(!Number.isInteger(id) || !summary.summary)throw new Error('Geçersiz özet');
      if(!force && state.data?.meta.report_id===id){state.data.meta=summary;renderSummary();return;}
      const [coverage,lessons,leagueStats,marketStats,modelStats,leagues]=await Promise.all([
        get('learning/coverage?report_id='+id,signal),pages('lessons',id,signal),pages('statistics?kind=league',id,signal),pages('statistics?kind=market',id,signal),pages('statistics?kind=model',id,signal),get('leagues',signal)]);
      if(coverage.report_id!==id || !coverage.coverage)throw new Error('Kapsam raporu uyuşmuyor');await reports(signal);
      if(generation!==state.generation)return;state.leagues=new Map(leagues.map(r=>[String(r.id),r.name]));
      lessons.sort((a,b)=>(Date.parse(b.date)||0)-(Date.parse(a.date)||0) || a.scope_key.localeCompare(b.scope_key));
      state.data={meta:summary,summary:summary.summary,coverage:coverage.coverage,lessons,statistics:{league:leagueStats,market:marketStats,model:modelStats}};state.offset=0;scopes();renderSummary();render();
    }catch(e){if(generation===state.generation && !signal.aborted){state.data=null;$('learning-summary').replaceChildren();$('learning-lessons').replaceChildren();put('learning-status','Öğrenme API verileri alınamadı. Raporu Yenile ile yeniden deneyin.');put('learning-coverage','');put('learning-statistics','');put('learning-page','');scopes();}}
    finally{if(generation===state.generation){state.controller=null;$('learning-lessons').setAttribute('aria-busy','false');if(!state.data){$('learning-previous').disabled=true;$('learning-next').disabled=true;}}}
  }
  $('learning-refresh').onclick=()=>refresh(true);$('learning-report').onchange=()=>refresh(true);
  $('learning-kind').onchange=()=>{state.offset=0;scopes();render();};$('learning-scope').onchange=()=>{state.offset=0;render();};
  for(const [id,step] of [['learning-previous',-size],['learning-next',size]])$(id).onclick=()=>{state.offset=Math.max(0,state.offset+step);render();$('learning-lessons').scrollIntoView({block:'start'});};
  $('learning-more-reports').onclick=async()=>{const controller=new AbortController();try{await reports(controller.signal,true);}catch(_){put('learning-status','Eski raporlar alınamadı. Yeniden deneyin.');}};
  globalThis.BetAppLearning=Object.freeze({refresh});
})();
