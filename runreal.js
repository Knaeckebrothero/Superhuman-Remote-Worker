const { chromium, firefox } = require('playwright');
(async () => {
  for (const [name,l] of [['chromium',chromium],['firefox',firefox]]) {
    let b; try {
      b=await l.launch(); const p=await b.newPage();
      await p.goto('http://127.0.0.1:8972/real-css.html',{waitUntil:'load'});
      const r=await p.evaluate(()=>new Promise(res=>{const t0=Date.now();
        const poll=()=>(window.__done||Date.now()-t0>20000)?res({e:window.__error,R:window.__results}):setTimeout(poll,80);poll();}));
      console.log('\n############ '+name.toUpperCase()+' ############');
      if (r.e) { console.log('ERR', r.e); continue; }
      for (const s of r.R.steps) {
        console.log('append '+String(s.appended).padEnd(6)+' | '+s.REGIME.padEnd(28)
          +' innerH '+String(s.before.innerH).padEnd(6)+'->'+String(s.after.innerH).padEnd(6)
          +' | scrollH '+String(s.before.scrollH).padEnd(5)+'->'+String(s.after.scrollH).padEnd(5)
          +' | maxScrollTop '+String(s.after.maxScrollTop).padEnd(5)
          +' | roCalls='+s.roCalls+'  '+s.VERDICT);
      }
      console.log('\njump-latest pill:', r.R.jumpLatestPill.VERDICT,
                  '| innerH', r.R.jumpLatestPill.before.innerH, '->', r.R.jumpLatestPill.after.innerH,
                  '| calls', r.R.jumpLatestPill.roCalls);
      console.log('streaming pin  :', JSON.stringify(r.R.streamingPin));
    } catch(e){ console.log(name,'FAIL',String(e).split('\n')[0]); }
    finally { if(b) await b.close().catch(()=>{}); }
  }
})();
