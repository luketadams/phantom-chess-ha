/* Deterministic connection-loss and late-response checks on the bundled card. */
const {chromium}=require('playwright');
const path=require('path');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
 try {
  const page=await browser.newPage();
  await page.clock.install({time:new Date('2026-09-05T12:00:00Z')});
  await page.clock.pauseAt(new Date('2026-09-05T12:00:01Z'));
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.setContent('<phantom-chess-card></phantom-chess-card>');
  await page.evaluate(()=>customElements.define('home-assistant',class extends HTMLElement {}));
  await page.addScriptTag({type:'module',path:path.resolve('custom_components/phantom_chess/www/phantom-chess-card.js')});
  await page.evaluate(()=>{
   window.card=document.querySelector('phantom-chess-card');
   window.reads=0;window.fail=true;
   window.report={status:'running',analyzed:0,total:1,moves:[],evaluations:[]};
   window.hass={states:{'sensor.board':{attributes:{entry_id:'one',saved_games:[]}}},callWS:async()=>{reads++;if(fail)throw Error('Connection lost');return {response:report};}};
   card.setConfig({entity:'sensor.board',view:'review'});card.hass=hass;
   card.reviewGame={game_id:'g1',positions:['8/8/8/8/8/8/8/8'],sans:[]};
   card.reviewReport=report;
  });
  await page.evaluate(()=>card.fetchReview());
  assert.equal(await page.getByRole('alert').filter({hasText:'Retrying automatically'}).count(),1);
  await page.evaluate(()=>{fail=false;report={...report,status:'complete'};});
  await page.clock.fastForward(4000);
  assert.equal(await page.evaluate(()=>reads),2);
  assert.equal(await page.getByRole('alert').count(),0);
  assert.equal(await page.getByRole('button',{name:'Reanalyze game',exact:true}).count(),1);
  await page.clock.fastForward(60000);
  assert.equal(await page.evaluate(()=>reads),2); // Completed analysis stops polling.

  await page.evaluate(()=>{fail=true;reads=0;return card.fetchReview();});
  for(const delay of [4000,8000,16000,30000]) await page.clock.fastForward(delay);
  assert.equal(await page.evaluate(()=>reads),5);
  assert.equal(await page.getByRole('alert').filter({hasText:'choose Refresh analysis'}).count(),1);
  await page.clock.fastForward(120000);
  assert.equal(await page.evaluate(()=>reads),5); // Bounded retry budget.
  await page.evaluate(()=>{fail=false;card.shadowRoot.querySelector('[data-action="refresh-analysis"]').click();});
  await page.waitForFunction(()=>!card.pending);
  assert.equal(await page.getByRole('alert').count(),0);

  // A response from a previously selected game cannot overwrite the new one.
  await page.evaluate(()=>{
   window.normal=hass.callWS;
   hass.callWS=()=>new Promise(resolve=>window.resolveOld=resolve);
   window.oldRead=card.fetchReview();
   card.reviewGame={...card.reviewGame,game_id:'g2'};
   hass.callWS=normal;report={...report,status:'paused'};
   return card.fetchReview();
  });
  await page.evaluate(async()=>{resolveOld({response:{...report,status:'complete'}});await oldRead;});
  assert.equal(await page.evaluate(()=>card.reviewReport.status),'paused');

  // Overlapping reads of the same game also keep the newest response.
  await page.evaluate(()=>{
   hass.callWS=()=>new Promise(resolve=>window.resolveOld=resolve);
   window.oldRead=card.fetchReview();hass.callWS=normal;
   report={...report,status:'complete'};return card.fetchReview();
  });
  await page.evaluate(async()=>{resolveOld({response:{...report,status:'running'}});await oldRead;});
  assert.equal(await page.evaluate(()=>card.reviewReport.status),'complete');
  await page.evaluate(()=>{report={...report,status:'paused'};return card.fetchReview();});

  // Removing the card ignores an in-flight result and stops background reads.
  await page.evaluate(()=>{
   hass.callWS=()=>new Promise(resolve=>window.resolveOld=resolve);
   window.oldRead=card.fetchReview();card.remove();
  });
  await page.evaluate(async()=>{resolveOld({response:{...report,status:'complete'}});await oldRead;});
  assert.equal(await page.evaluate(()=>card.reviewReport.status),'paused');
  await page.evaluate(()=>{hass.callWS=normal;report={...report,status:'complete'};document.body.append(card);});
  await page.waitForFunction(()=>card.reviewReport.status==='complete');
  assert.equal(await page.getByRole('button',{name:'Reanalyze game',exact:true}).count(),1);
  await page.evaluate(()=>{report={...report,status:'running'};return card.fetchReview();});
  const before=await page.evaluate(()=>reads);
  await page.evaluate(()=>card.setConfig({entity:'sensor.board',view:'learn'}));
  await page.clock.fastForward(60000);
  assert.equal(await page.evaluate(()=>reads),before); // Leaving Review clears its timer.
  assert.deepEqual(errors,[]);
  console.log('Review recovery: 15 behavioral assertions passed.');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
