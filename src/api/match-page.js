/* Independent, read-only dossier. All source strings are rendered as text. */
(() => {
'use strict';
const $=id=>document.getElementById(id),id=Number(location.pathname.split('/').pop());
const node=(tag,text='',cls='')=>{const e=document.createElement(tag);e.textContent=text;e.className=cls;return e;};
const number=v=>v==null?'Veri yok':Number.isFinite(v)?v.toLocaleString('tr-TR',{maximumFractionDigits:2}):String(v);
const pct=v=>v==null?'Veri yok':number(v*100)+'%';
const date=v=>v?new Date(v).toLocaleString('tr-TR',{timeZone:'Europe/Istanbul'}):'Zaman bilinmiyor';
const score=(a,b)=>a==null||b==null?'—':`${a} : ${b}`;
const status={scheduled:'Başlamadı',live:'Canlı',finished:'Tamamlandı',postponed:'Ertelendi',cancelled:'İptal',abandoned:'Yarıda kaldı'};
const archiveQuery=new URLSearchParams(location.search);
const archiveSuffix=archiveQuery.get('history_source')==='archive'?'?history_source=archive&history_generation='+encodeURIComponent(archiveQuery.get('history_generation')||''):'';
let data,active='overview',generation=0;
const tabs=[['overview','Genel bakış'],['odds','Oranlar'],['teams','Takımlar & H2H'],['counts','Korner & kart'],['predictions','Kayıtlı tahminler']];
function card(parent,title){const c=node('article','','card');c.append(node('h2',title));parent.append(c);return c;}
function note(parent,text){parent.append(node('p',text,'notice'));}
function metric(parent,label,value){const e=node('div','','metric');e.append(node('span',label),node('strong',value));parent.append(e);}
function table(parent,head,rows){const wrap=node('div','','table-scroll');wrap.tabIndex=0;wrap.setAttribute('role','region');wrap.setAttribute('aria-label',head.join(', '));const t=node('table'),h=node('tr');head.forEach(x=>h.append(node('th',x)));const thead=node('thead');thead.append(h);t.append(thead);const body=node('tbody');rows.forEach(row=>{const tr=node('tr');row.forEach(value=>{const td=node('td');td.append(value instanceof Node?value:document.createTextNode(String(value)));tr.append(td);});body.append(tr);});t.append(body);wrap.append(t);parent.append(wrap);}
function link(m){const a=node('a',`${m.home_team} — ${m.away_team}`);a.href=`/match/${m.id}`+(m.archive_generation?'?history_source=archive&history_generation='+encodeURIComponent(m.archive_generation):archiveSuffix);a.target='_blank';a.rel='noopener noreferrer';return a;}
function svgEl(tag,attrs){const e=document.createElementNS('http://www.w3.org/2000/svg',tag);Object.entries(attrs).forEach(([k,v])=>e.setAttribute(k,v));return e;}
function chart(parent,title,points){
 const valid=points.filter(p=>Number.isFinite(p.value));parent.append(node('h3',title));
 if(!valid.length){note(parent,'Grafik için doğrulanmış veri yok.');return;}
 const svg=svgEl('svg',{viewBox:'0 0 600 220',role:'img','aria-label':title});const desc=svgEl('title',{});desc.textContent=points.map(p=>`${p.label}: ${number(p.value)}`).join('; ');svg.append(desc);
 const max=Math.max(1,...valid.map(p=>p.value));const step=550/points.length;
 points.forEach((p,i)=>{if(!Number.isFinite(p.value))return;const h=140*p.value/max,x=35+i*step;const r=svgEl('rect',{x,y:170-h,width:Math.min(45,step-8),height:h,rx:4,fill:i%2?'#999':'#171717'});svg.append(r);const v=svgEl('text',{x,y:160-h,'font-size':12,fill:'#333'});v.textContent=number(p.value);svg.append(v);const t=svgEl('text',{x,y:193,'font-size':11,fill:'#555'});t.textContent=p.label;svg.append(t);});parent.append(svg);
}
function overview(section){
 const grid=node('div','','grid');section.append(grid);const m=data.match;
 const info=card(grid,'Maç kaydı');metric(info,'Lig / Sezon',`${m.league} / ${m.season}`);metric(info,'Hafta',m.round_label||number(m.round));metric(info,'Başlama',date(m.kickoff_at));metric(info,'İlk yarı',score(m.ht_home,m.ht_away));metric(info,'Maç skoru',score(m.ft_home,m.ft_away));metric(info,'Durum gözlemi',date(m.status_observed_at));
 const form=card(grid,'Gol performansı · son 10');chart(form,'Atılan / yenilen gol ortalaması',['home','away'].flatMap((s,i)=>['goals_for','goals_against'].map((k,j)=>({label:`${i?'Dep.':'Ev'} ${j?'yenen':'atılan'}`,value:data.profiles[s]['10'][k].value}))));form.append(node('p','Yalnızca bu maçtan önce tamamlanmış kayıtlar. Her istatistiğin örneklemi Takımlar sekmesinde.','chart-caption'));
 const venue=card(grid,'İç saha / deplasman');chart(venue,'Son 10 saha maçı · atılan gol',[{label:'Ev sahibi iç saha',value:data.profiles.home_venue['10'].goals_for.value},{label:'Deplasman dış saha',value:data.profiles.away_venue['10'].goals_for.value}]);
 const coverage=card(grid,'Veri kapsamı');note(coverage,data.histories.home.coverage);metric(coverage,'Kontrol zamanı',date(data.as_of));metric(coverage,'Önbellek',`${data.cache_seconds} saniye`);note(coverage,data.prediction_note);
}
function odds(section){
 const names={home:'1 / Ev',draw:'X',away:'2 / Dep.',over:'Üst',under:'Alt',line:'Çizgi'},stages={opening:'Açılış',latest:'Son gözlem',closing:'Kapanış'},markets={'1x2':'1X2',ah:'Asya handikapı',ou:'Asya toplam gol'};
 note(section,'Sütunlar kaydedilmiş fiyat noktalarıdır; aradaki hareketler bilinmiyor. Gözlem zamanı, fiyatın doğrulandığı veya oynanabilir olduğu anlamına gelmez. Kapanış kaydı olmayan yerde son fiyat kapanış olarak gösterilmez.');
 for(const bookmaker of ['Crown','Bet365','Sbobet']){
  const c=card(section,bookmaker);
  for(const market of ['1x2','ah','ou']){
   const o=data.odds.find(x=>x.bookmaker===bookmaker&&x.market===market&&x.period==='full_time');c.append(node('h3',markets[market]));
   if(!o){note(c,'Kaynakta kayıtlı oran yok.');continue;}
   c.append(node('p',`Kaynak: ${o.source||'Kaynak adresi kaydedilmemiş'} · Güncelleme: ${date(o.updated_at)} · Doğrulanmış fiyat uygunluğu yok`,'muted'));
   const sides=market==='1x2'?['home','draw','away']:market==='ah'?['line','home','away']:['line','over','under'];
   table(c,['Seçim',...Object.values(stages)],sides.map(side=>[names[side],...Object.keys(stages).map(stage=>{const el=node('span',number(o[stage][side]));if(side!=='line'){const stamp=o.field_observed_at?.[stage+'_'+side],age=Date.parse(data.as_of)-Date.parse(stamp);el.append(node('small',date(stamp)));el.append(node('small',!stamp||!Number.isFinite(age)||age<0?'Doğrulanmamış zaman':age<=900000?'Son 15 dakikada gözlendi':'Eski gözlem'));}return el;})]));
   for(const side of sides.filter(s=>s!=='line'))chart(c,`${markets[market]} · ${names[side]}`,Object.entries(stages).map(([stage,label])=>({label,value:o.field_observed_at?.[stage+'_'+side]?o[stage][side]:null})));
  }
 }
}
const metrics={corners_against:'Rakip korner',corners_total:'Toplam korner',yellow_cards_against:'Rakip sarı kart',yellow_cards_total:'Toplam sarı kart',red_cards_against:'Rakip kırmızı kart',red_cards_total:'Toplam kırmızı kart',goals_for:'Atılan gol',goals_against:'Yenilen gol',first_half_for:'İlk yarı atılan',first_half_against:'İlk yarı yenilen',btts:'KG Var',over_25:'2.5 Üst',corners:'Korner',yellow_cards:'Sarı kart',red_cards:'Kırmızı kart'};
function profile(parent,scope,title,counts=false){const c=card(parent,title);for(const n of ['5','10']){const p=data.profiles[scope][n];c.append(node('h3',`Son ${n} · ${p.matches} kayıt`));const keys=Object.keys(metrics).filter(k=>counts?['corners','yellow_cards','red_cards'].some(m=>k.startsWith(m)):!['corners','yellow_cards','red_cards'].some(m=>k.startsWith(m)));table(c,['İstatistik','Ortalama / oran','Örneklem'],keys.map(k=>[metrics[k],['btts','over_25'].includes(k)?pct(p[k].value):number(p[k].value),`${p[k].sample}/${p.matches}`]));if(!counts){metric(c,'Form · en yeni önce',p.form.join(' · ')||'Veri yok');metric(c,'G / B / M',`${p.wins} / ${p.draws} / ${p.losses}`);metric(c,'KG Yok',pct(p.btts.value==null?null:1-p.btts.value));metric(c,'2.5 Alt',pct(p.over_25.value==null?null:1-p.over_25.value));}}}
/* Historical comparisons: every value comes from already stored, pre-kickoff matches. */
const validCount=v=>typeof v==='number'&&Number.isFinite(v)&&v>=0?v:null;
const recent=(scope,n)=>(data.histories?.[scope]?.items||[]).slice(0,n);
const scopeTeam=scope=>scope.startsWith('home')?data.team_ids.home:data.team_ids.away;
function resultFor(m,team){const h=validCount(m.ft_home),a=validCount(m.ft_away);if(h==null||a==null)return '?';const own=m.home_team_id===team?h:a,other=m.home_team_id===team?a:h;return own>other?'G':own<other?'M':'B';}
function evidence(scope,n){
 const items=recent(scope,n),team=scopeTeam(scope),buckets={};let wins=0,draws=0,losses=0;
 const add=(key,v,kind)=>{if(v==null)return;const b=buckets[key]||(buckets[key]={total:0,count:0,kind});b.total+=v;b.count++;};
 for(const m of items){
  const side=m.home_team_id===team?'home':'away',other=side==='home'?'away':'home';
  const gf=validCount(m['ft_'+side]),ga=validCount(m['ft_'+other]);
  if(gf!=null&&ga!=null){
   if(gf>ga)wins++;else if(gf===ga)draws++;else losses++;
   for(const [key,value,kind] of [
    ['wins',Number(gf>ga),'rate'],['gf',gf,'avg'],['ga',ga,'avg'],
    ['clean',Number(ga===0),'rate'],['scored',Number(gf>0),'rate'],
    ['btts',Number(gf>0&&ga>0),'rate'],['over15',Number(gf+ga>=2),'rate'],
    ['over25',Number(gf+ga>=3),'rate'],['over35',Number(gf+ga>=4),'rate']
   ])add(key,value,kind);
  }
  const htH=validCount(m.ht_home),htA=validCount(m.ht_away);
  if(htH!=null&&htA!=null){add('ht05',Number(htH+htA>=1),'rate');add('ht15',Number(htH+htA>=2),'rate');}
  const stats=m.statistics||{};
  if(stats.is_final)for(const kind of ['corners','yellow_cards','red_cards']){
   add(kind,validCount(stats[side+'_'+kind]),'avg');
   add(kind+'_against',validCount(stats[other+'_'+kind]),'avg');
  }
 }
 const metrics={};for(const [k,v] of Object.entries(buckets))metrics[k]={value:v.total/v.count,hits:v.total,n:v.count,kind:v.kind};
 return {matches:items.length,wins,draws,losses,decided:wins+draws+losses,metrics};
}
const evidenceText=m=>!m?'— · 0 gözlem':m.kind==='rate'?number(m.value*100)+'% · '+m.hits+'/'+m.n:number(m.value)+' · '+m.n+' maç';
function compareRow(parent,label,left,right){
 const line=node('div','','compare-row'),sides=[left,right];
 const cell=(m,i)=>{const v=node('div','','compare-value '+(i?'away':'home'));v.append(node('strong',evidenceText(m)));
  const track=node('span','','evidence-track'),bar=node('span','','evidence-fill');
  const other=sides[1-i],max=m?.kind==='rate'?1:Math.max(1,m?.value||0,other?.value||0);
  bar.style.width=m?Math.max(0,Math.min(100,Math.round(m.value/max*100)))+'%':'0%';track.append(bar);v.append(track);return v;};
 line.append(cell(left,0),node('span',label,'compare-label'),cell(right,1));parent.append(line);
}
function compareCard(parent,title,scopeA,scopeB,n){
 const c=card(parent,title),a=evidence(scopeA,n),b=evidence(scopeB,n),head=node('div','','compare-head');
 head.append(node('strong',data.match.home_team),node('span','Son '+n),node('strong',data.match.away_team));c.append(head);
 for(const [label,key] of [
  ['Galibiyet oranı','wins'],['Atılan gol / maç','gf'],['Yenilen gol / maç','ga'],
  ['Gol yemeden biten','clean'],['Gol atılan maç','scored'],['KG Var','btts'],
  ['Toplam 1.5 üst','over15'],['Toplam 2.5 üst','over25'],['Toplam 3.5 üst','over35'],
  ['İlk yarı 0.5 üst','ht05'],['İlk yarı 1.5 üst','ht15'],
  ['Korner / maç','corners'],['Sarı kart / maç','yellow_cards'],['Kırmızı kart / maç','red_cards']
 ])compareRow(c,label,a.metrics[key],b.metrics[key]);
 c.append(node('p',data.match.home_team+': '+a.matches+' kayıt / '+a.decided+' sonuç · '+data.match.away_team+': '+b.matches+' kayıt / '+b.decided+' sonuç. Örneklem sayıları her istatistiğin yanındadır.','comparison-foot'));
}
function formStrip(parent,scope,title,n){
 const c=card(parent,title),team=scopeTeam(scope),items=recent(scope,n);c.append(node('p','En yeni maç solda · G: galibiyet · B: beraberlik · M: mağlubiyet','muted'));
 if(!items.length){note(c,'Bu kapsamda geçmiş maç kaydı yok.');return;}
 const strip=node('div','','team-form-strip');for(const m of items){const value=resultFor(m,team),a=link(m);a.className='form-result form-'+value;a.textContent=value;
 a.title=date(m.kickoff_at)+' · '+m.home_team+' — '+m.away_team+' · '+score(m.ft_home,m.ft_away)+' · '+value;
 a.setAttribute('aria-label',a.title);strip.append(a);}c.append(strip);
 const e=evidence(scope,n);metric(c,'G / B / M',e.wins+' / '+e.draws+' / '+e.losses);
 c.append(node('p',e.matches+' kayıt · '+e.decided+' skoru bulunan sonuç.','muted'));
}
function h2hSummary(parent){
 const c=card(parent,'Önceki derbiler · H2H');c.id='h2h-evidence';const page=data.histories.h2h,items=page.items||[];
 let gs=0,fb=0,draws=0,total=0,btts=0,over=0;
 for(const m of items){const h=validCount(m.ft_home),a=validCount(m.ft_away);if(h==null||a==null)continue;
 total++;const our=m.home_team_id===data.team_ids.home?h:a,opp=m.home_team_id===data.team_ids.home?a:h;
 if(our>opp)gs++;else if(our<opp)fb++;else draws++;btts+=Number(h>0&&a>0);over+=Number(h+a>=3);}
 const summary=node('div','','h2h-summary');for(const [label,value] of [[data.match.home_team+' galibiyeti',gs],['Beraberlik',draws],[data.match.away_team+' galibiyeti',fb]]){
 const el=node('div','','h2h-stat');el.append(node('strong',String(value)),node('span',label));summary.append(el);}c.append(summary);
 metric(c,'KG Var',total?btts+'/'+total+' · '+number(btts/total*100)+'%':'Veri yok');
 metric(c,'2.5 üst',total?over+'/'+total+' · '+number(over/total*100)+'%':'Veri yok');
 note(c,total+' kayıtlı skorlu karşılaşma değerlendirildi'+(page.has_more?' (son 10, daha eski kayıtlar olabilir)':' (erişilebilir aktif veritabanı kapsamında)')+'. Kayıtsız derbiler dahil değildir.');
}
function teams(section){
 note(section,'Yalnızca başlama saatinden önce bitmiş, aktif veritabanında kayıtlı maçlar karşılaştırılır. Eksik gözlemler sıfır sayılmaz; yeni tahmin üretilmez. Yüzdeler gerçekleşmiş maçların tarihsel oranlarıdır.');
 const controls=node('div','','team-period-control');controls.append(node('strong','İstatistik aralığı'));let limit=10;
 const buttons={},comparisons=node('div');
 for(const n of [5,10]){const b=node('button','Son '+n+' maç');b.type='button';b.dataset.teamPeriod=String(n);b.onclick=()=>{limit=n;draw();};buttons[n]=b;controls.append(b);}
 section.append(controls,comparisons);
 function draw(){for(const n of [5,10])buttons[n].setAttribute('aria-pressed',String(n===limit));comparisons.replaceChildren();
 const form=node('div','','grid');formStrip(form,'home',data.match.home_team+' · genel form',limit);formStrip(form,'away',data.match.away_team+' · genel form',limit);comparisons.append(form);
 const metricsGrid=node('div','','grid');compareCard(metricsGrid,'Genel performans','home','away',limit);compareCard(metricsGrid,'Saha etkisi · iç saha / deplasman','home_venue','away_venue',limit);comparisons.append(metricsGrid);
 const venue=node('div','','grid');formStrip(venue,'home_venue',data.match.home_team+' · iç saha formu',limit);formStrip(venue,'away_venue',data.match.away_team+' · deplasman formu',limit);comparisons.append(venue);}
 draw();h2hSummary(section);section.append(node('h2','Kaynak maç geçmişleri','history-heading'));
 const list=node('div','','grid');section.append(list);
 for(const [scope,title] of [['home',data.match.home_team+' · son maçlar'],['away',data.match.away_team+' · son maçlar'],
 ['home_venue',data.match.home_team+' · iç saha'],['away_venue',data.match.away_team+' · deplasman'],['h2h','Önceki derbiler · maç bazında']])historyCard(list,scope,title);
}
function historyCard(parent,scope,title){
 const c=card(parent,title);let page=data.histories[scope],busy=false;const content=node('div');c.append(content);
 const stat=(m,key)=>{const s=m.statistics||{},a=validCount(s['home_'+key]),b=validCount(s['away_'+key]);return s.is_final&&a!=null&&b!=null?a+' - '+b:'—';};
 function draw(){content.replaceChildren();if(!page.items.length)note(content,'Bu kapsamda geçmiş maç kaydı yok.');
 else table(content,['Tarih','Maç','Skor','İlk yarı','Korner','Sarı kart','Kırmızı kart'],page.items.map(m=>[
 date(m.kickoff_at),link(m),score(m.ft_home,m.ft_away),score(m.ht_home,m.ht_away),stat(m,'corners'),stat(m,'yellow_cards'),stat(m,'red_cards')]));
 const nav=node('div','','pagination');for(const [label,delta,disabled] of [['Önceki',-10,page.offset===0],['Sonraki',10,!page.has_more||page.offset>=1000]]){
 const b=node('button',label);b.disabled=disabled||busy;b.onclick=async()=>{busy=true;draw();try{const response=await fetch('/api/matches/'+id+'/history?scope='+scope+'&offset='+(page.offset+delta),{signal:AbortSignal.timeout(archiveSuffix?90000:15000)});
 if(!response.ok)throw Error();page=await response.json();}catch(_){note(c,'Geçmiş alınamadı. Yeniden deneyin.');}finally{busy=false;draw();}};nav.append(b);}
 nav.append(node('small','Sayfa '+(page.offset/10+1)+' · '+page.items.length+' kayıt'));content.append(nav);}
 draw();
}
function counts(section){const o=data.observations,c=card(section,'Maçın gözlenen değerleri');table(c,['Ölçüm',data.match.home_team,data.match.away_team,'Toplam'],['corners','yellow_cards','red_cards'].map(k=>[metrics[k],number(o['home_'+k]),number(o['away_'+k]),number(o['home_'+k]==null||o['away_'+k]==null?null:o['home_'+k]+o['away_'+k])]));note(c,`Kaynak: ${o.source||'Bilinmiyor'} · ${date(o.observed_at)} · ${o.is_final?'Kesinleşmiş istatistik':'Kesinleşmemiş / eksik istatistik'}`);const grid=node('div','','grid');section.append(grid);for(const side of ['home','away']){profile(grid,side,data.match[side+'_team'],true);const c=card(grid,`${data.match[side+'_team']} · son 10`);chart(c,'Maç başına ortalama',['corners','yellow_cards','red_cards'].map(k=>({label:metrics[k],value:data.profiles[side]['10'][k].value})));}note(section,'Ortalamalar yalnızca kesinleşmiş kaynak istatistiklerinden hesaplanır. Eksik kart veya korner verisi sıfır sayılmaz.');}
function predictions(section){
 const published=Array.isArray(data.current_recommendations)?data.current_recommendations:[];
 const saved=Array.isArray(data.predictions)?data.predictions.map(x=>x.pick):[];
 const savedIds=new Set(saved.map(x=>x.id));
 const currentIds=new Set(published.map(x=>x.id));
 const keyOf=pick=>[pick.market||'',pick.selection||'',pick.id||''].join('|');
 const currentKeys=new Set(published.map(keyOf));
 const savedKeys=new Set(saved.map(keyOf));
 const differs=published.length&&saved.length&&(
   published.some(p=>!savedKeys.has(keyOf(p)))||saved.some(p=>!currentKeys.has(keyOf(p))));
 if(differs){
   const comparison=card(section,'Tahmin farkı · neden aynı değil?');
   note(comparison,'GÜNCEL ÖNERİ ile İLK KAYDEDİLEN TAHMİN farklı. Ana sayfa yeni model yayınını gösterir; geçmiş başarı hesabında yalnızca maç öncesi dondurulmuş kayıt kullanılır. Eski kayıt değiştirilmez.');
   note(comparison,'Ana sayfada şimdi: '+published.map(p=>p.label||p.selection||'Bilinmiyor').join(' · '));
   note(comparison,'İlk kaydedilen: '+saved.map(p=>p.label||p.selection||'Bilinmiyor').join(' · '));
   if(data.captured_at)note(comparison,'İlk kayıt zamanı: '+date(data.captured_at));
   if(data.current_generated_at)note(comparison,'Güncel yayın zamanı: '+date(data.current_generated_at));
 }
 const currentCard=card(section,'Güncel öneriler · ana sayfa');
 note(currentCard,data.current_note||'Güncel lig yayını bulunamadı.');
 if(data.current_generated_at)currentCard.append(node('p','Güncel lig yayını: '+date(data.current_generated_at)+' · Geçerlilik bitişi: '+date(data.current_expires_at),'muted'));
 if(!published.length)note(currentCard,'Şu anda gösterilecek geçerli güncel öneri yok.');
 for(const pick of published){
   const row=node('div','','current-pick');
   row.append(node('h3',pick.label||pick.selection||'Seçim adı bilinmiyor'));
   metric(row,'Model olasılığı',pct(pick.probability));
   metric(row,'Güvenilirlik',pct(pick.reliability));
   metric(row,'Örneklem',number(pick.sample_size));
   row.append(node('p',savedIds.has(pick.id)?'İlk kayıtta da vardı.':'İlk kaydedilmiş tahmin setinde yok; yeni lig yayınında öne çıkmış.','muted'));
   currentCard.append(row);
 }
 const missing=saved.filter(p=>!currentIds.has(p.id));
 if(published.length&&missing.length)note(currentCard,'İlk kayıtta olup güncel ilk iki öneride bulunmayan seçimler: '+missing.map(p=>p.label||p.selection).join(' · ')+'. İlk kayıt değiştirilmedi.');
 section.append(node('h2','İlk kaydedilen maç öncesi tahminler','history-heading'));
 note(section,'Aşağıdaki seçimler ilk kez kaydedildiği tarihte dondurulmuştur. Güncel ana sayfa önerilerinden farklı olabilir; geçmiş tahminler yeniden yazılmaz.');
 note(section,data.prediction_note);
 if(data.captured_at)section.append(node('p','İlk kayıt: '+date(data.captured_at)+' · Model: '+data.model_version,'muted'));const families={result:'1X2',btts:'KG Var / Yok',goals:'Toplam gol',asian_handicap:'Handikap',corners:'Korner',cards:'Kart',yellow_cards:'Sarı kart',red_cards:'Kırmızı kart',first_half_goals:'İlk yarı gol',second_half_goals:'İkinci yarı gol',double_chance:'Çifte şans'};const outcomes={won:'Kazandı · doğrulandı',lost:'Kaybetti · doğrulandı',void:'Geçersiz · doğrulandı',disputed:'Kanıt çelişkisi · doğrulanamadı',awaiting_data:'Doğrulanmış sonuç bekleniyor'};
 for(const [market,title] of Object.entries(families)){const c=card(section,title),items=data.predictions.filter(x=>x.pick.market===market);if(!items.length){note(c,'Bu markette kayıtlı maç öncesi seçim yok.');continue;}for(const item of items){const p=item.pick;c.append(node('h3',p.label||p.selection));metric(c,'Model olasılığı',pct(p.probability));metric(c,'Güvenilirlik',pct(p.reliability));metric(c,'Örneklem',number(p.sample_size));metric(c,'Sonuç',outcomes[item.outcome]||'Veri yok');if(item.recorded_at)metric(c,'Sonuç kayıt zamanı',date(item.recorded_at));const e=p.evidence||{};const labels={home_sample:'Ev sahibi örneklemi',away_sample:'Deplasman örneklemi',league_sample:'Lig örneklemi',home_rate:'Ev sahibi oranı',away_rate:'Deplasman oranı',league_rate:'Lig oranı'};let count=0;for(const [k,label] of Object.entries(labels))if(e[k]!=null){metric(c,label,k.endsWith('rate')?pct(e[k]):number(e[k]));count++;}if(!count)note(c,'Ek istatistiksel kanıt alanı kaydedilmemiş.');}}
}
const renderers={overview,odds,teams,counts,predictions};
function show(key){active=key;for(const b of $('tabs').children){const selected=b.dataset.key===key;b.setAttribute('aria-selected',String(selected));b.tabIndex=selected?0:-1;}const section=node('section');section.id='panel-'+key;section.setAttribute('role','tabpanel');section.setAttribute('aria-labelledby','tab-'+key);renderers[key](section);$('analysis').replaceChildren(section);history.replaceState(null,'','#'+key);}
function render(){const m=data.match;document.title=`${m.home_team} — ${m.away_team} · Kuponanalizii`;$('hero').replaceChildren();$('hero').append(node('p',`${m.league} · ${m.season} · ${m.round_label||m.round+'. hafta'}`,'eyebrow'));const line=node('div','','scoreline');line.append(node('h1',m.home_team),node('strong',score(m.ft_home,m.ft_away),'score'),node('h2',m.away_team));$('hero').append(line,node('p',`${status[m.status]||m.status}${m.live_minute?' · '+m.live_minute+'′':''} · ${date(m.kickoff_at)}`),node('p',`İlk yarı ${score(m.ht_home,m.ht_away)}`,'muted'));$('tabs').replaceChildren();for(const [key,label] of tabs){const b=node('button',label);b.id='tab-'+key;b.dataset.key=key;b.setAttribute('role','tab');b.setAttribute('aria-controls','panel-'+key);b.onclick=()=>show(key);b.onkeydown=e=>{const keys=tabs.map(t=>t[0]),i=keys.indexOf(key),n=e.key==='ArrowRight'?(i+1)%keys.length:e.key==='ArrowLeft'?(i+keys.length-1)%keys.length:e.key==='Home'?0:e.key==='End'?keys.length-1:null;if(n!=null){e.preventDefault();show(keys[n]);$('tab-'+keys[n]).focus();}};$('tabs').append(b);}show(tabs.some(t=>t[0]===location.hash.slice(1))?location.hash.slice(1):active);}
async function load(){const run=++generation;$('feedback').textContent='Maç kayıtları yükleniyor…';$('retry').hidden=true;try{const r=await fetch(`/api/matches/${id}/dossier${archiveSuffix}`,{signal:AbortSignal.timeout(archiveSuffix?90000:15000)});if(!r.ok)throw Error(r.status===404?'Maç bulunamadı.':'Veri bağlantısı kurulamadı.');const result=await r.json();if(run!==generation)return;data=result;render();$('feedback').textContent='Kayıtlar yüklendi · '+date(data.as_of);}catch(e){$('feedback').textContent=e.name==='TimeoutError'?'Bağlantı zaman aşımına uğradı.':e.message;$('retry').hidden=false;}}
$('retry').onclick=load;load();
})();
