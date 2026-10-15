/* Isolated DOM/HTTP fixtures for homepage safety, never a production data source. */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element {
  constructor(){this.children=[];this.textContent='';this.style={setProperty(){}};}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(){this.children=[];this.textContent='';}
  setAttribute(k,v){this[k]=v;}
  text(){return [this.textContent,...this.children.map(n=>n.text())].join(' ');}
}
async function run(mode){
  let now=Date.UTC(2026,9,8,15),active=true;
  class Clock extends Date {static now(){return now;}}
  const iso=offset=>new Date(now+offset).toISOString();
  const items=Array.from({length:4},(_,i)=>({match:{id:i+1,status:'scheduled',kickoff_at:iso(3600000),home_team:'Home '+i,away_team:'Away '+i,league:'Fixture league'},
    recommendation:{id:'under_9_5_corners',label:'Under 9.5',qualifies:true,probability:.70,reliability:.80,score:.80},
    quote:{bookmaker:'Bet365',price:2.2,observed_at:iso(-60000),expires_at:iso(840000)}}));
  if(mode==='empty')items.length=0;
  if(mode==='stale')items[0].quote.observed_at=iso(-900001);
  if(mode==='future')items[0].quote.observed_at=iso(1);
  if(mode==='expired')items[0].quote.expires_at=iso(-1);
  if(mode==='duplicate')items[1].match.id=items[0].match.id;
  if(mode==='weak-probability')items[0].recommendation.probability=.699;
  if(mode==='weak-reliability')items[0].recommendation.reliability=.799;
  if(mode==='weak-score')items[0].recommendation.score=.799;
  if(mode==='unknown-bookmaker')items[0].quote.bookmaker='Unknown';
  if(mode==='invalid-price')items[0].quote.price=NaN;
  if(mode==='started')items[0].match.status='live';
  const html=fs.readFileSync('src/api/dashboard.html','utf8');
  const nodes=new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>[m[1],new Element()]));
  const calls=[],timers=[];
  const market={bookmaker:'Bet365',market:'1x2',opening:{home:1.8},latest:{home:1.9},field_observed_at:{latest_home:iso(-60000)}};
  if(mode==='stale-movement')market.field_observed_at.latest_home=iso(-900001);
  if(mode==='changed-line'){market.market='ou';market.opening={line:2.5,over:1.8};market.latest={line:3.5,over:1.9};market.field_observed_at={latest_over:iso(-60000)};}
  const sandbox={console,Intl,Date:Clock,AbortSignal,document:{hidden:mode==='hidden',getElementById:id=>nodes.get(id),createElement:()=>new Element()},
    setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearTimeout(){},
    BetAppUI:{active:()=>active},BetAppMarket:{homeSnapshot:()=>null},BetAppOdds:{stage:()=> 'latest',liveStates:['live']},
    fetch:async(path,options)=>{calls.push({path,options});if(mode==='failed')return {ok:false,status:503};
      const body=path.includes('/coupons/')?{items,combined_odds:2.2**4,coupon_date:'2026-10-08',message:'Source prices pending'}:
        path.includes('view=live')?{items:[],total:0}:{items:[{id:99,home_team:'Home',away_team:'Away',odds:[market]}]};
      return {ok:true,json:async()=>structuredClone(body)};}};
  vm.createContext(sandbox);vm.runInContext(fs.readFileSync('src/api/dashboard-home.js','utf8'),sandbox);
  const settle=async()=>{for(let i=0;i<8;i++)await new Promise(resolve=>setImmediate(resolve));};
  await settle();
  if(mode==='hidden'){assert.equal(calls.length,0);return;}
  assert(!calls.some(c=>c.path.includes('telegram')));
  const text=id=>nodes.get(id).text();
  if(mode==='failed'){assert(text('home-coupon-list').includes('Veri bağlantısı'));assert.equal(text('home-coupon-total'),'—');return;}
  const valid=['valid','stale-movement','changed-line'].includes(mode);
  assert.equal(text('home-coupon-total'),valid?'23,43':'—',mode);
  if(valid){assert.equal(nodes.get('home-coupon-list').children.length,4);assert.equal(calls.find(c=>c.path.includes('coupons')).options.method,'POST');}
  if(mode==='stale-movement'||mode==='changed-line')assert(!text('home-movement-list').includes('1,90'));
  if(mode==='valid'){
    assert(text('home-movement-list').includes('1,90'));
    now+=840001;timers.at(-1).fn();assert.equal(text('home-coupon-total'),'—');
    assert(text('home-coupon-list').includes('tazelik süresi doldu'));
    active=false;const count=calls.length;sandbox.BetAppHome.refresh();await settle();assert.equal(calls.length,count);
  }
}
(async()=>{const modes=['valid','empty','stale','future','expired','duplicate','weak-probability','weak-reliability','weak-score','unknown-bookmaker','invalid-price','started','stale-movement','changed-line','failed','hidden'];for(const mode of modes)await run(mode);console.log(`home runtime: ${modes.length} scenarios passed`);})().catch(e=>{console.error(e);process.exitCode=1;});
