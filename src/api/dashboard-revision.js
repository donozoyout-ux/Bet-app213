/* Small change checks; never rebuild an unchanged page or mutate publications. */
(() => {
  'use strict';
  let previous=null,pending=false;const retry=new Set();
  async function check(){
    if(document.hidden || pending)return;
    pending=true;
    try{
      const response=await fetch('/api/dashboard/revision',{signal:AbortSignal.timeout(10000)});
      if(!response.ok)return;
      const current=await response.json();
      if(!current.predictions || !current.coupons || !current.day)return;
      const old=previous;previous=current;
      if(!old || old.predictions===current.predictions){globalThis.BetAppMatchAnalysis?.acceptRevision?.(current);globalThis.BetAppMarket?.acceptRevision?.(current);}
      if(!old || old.coupons===current.coupons){globalThis.BetAppCoupon?.acceptRevision?.(current);globalThis.BetAppHome?.acceptRevision?.(current);}
      if(!old)return; // Initial page load already reads persisted publications.
      const ui=globalThis.BetAppUI;
      if(old.predictions!==current.predictions || old.day!==current.day || retry.has('predictions')){
        retry.delete('predictions');
        if(ui?.active('home'))globalThis.BetAppMarket?.refreshHome?.();
        if(ui?.active('predictions')){
          globalThis.BetAppMatchAnalysis?.refresh();
          if(document.getElementById('analysis-advanced')?.open)globalThis.BetAppMarket?.refresh();
        }
      }
      if(old.coupons!==current.coupons || old.day!==current.day || retry.has('coupons')){
        retry.delete('coupons');
        if(ui?.active('home'))globalThis.BetAppHome?.refreshCoupon?.();
        if(ui?.active('coupon'))globalThis.BetAppCoupon?.refreshDaily?.();
      }
    }catch(_){/* An unavailable revision service must not erase valid evidence. */}
    finally{pending=false;}
  }
  globalThis.BetAppRevision=Object.freeze({check,failed:key=>retry.add(key)});
  setInterval(check,30000);check();
})();
