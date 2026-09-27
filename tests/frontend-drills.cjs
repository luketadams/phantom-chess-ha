/* Endgame drills in the bundled card. Run with Node and Playwright installed. */
const {chromium}=require('playwright');
const path=require('path');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
 const page=await browser.newPage({viewport:{width:1200,height:900}});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const dialogs=[];page.on('dialog',d=>{dialogs.push(d.message());d.accept();});
 await page.setContent('<phantom-chess-card></phantom-chess-card>');
 await page.evaluate(()=>customElements.define('home-assistant',class extends HTMLElement {}));
 await page.addScriptTag({type:'module',path:path.resolve('custom_components/phantom_chess/www/phantom-chess-card.js')});
 await page.evaluate(()=>{
  window.calls=[];window.card=document.querySelector('phantom-chess-card');
  window.config={entity:'sensor.board',connected:'binary_sensor.connected',paused:'switch.paused',voice:'switch.voice',training:'switch.training',level:'select.level',color:'select.color',view:'play'};
  window.drills=[{id:'queen_mate',title:'Queen and king mate',level:'beginner',goal:'checkmate',move_limit:15,instructions:'Checkmate with queen and king.',solver:'white'},{id:'hold_the_draw',title:'King and pawn: hold the draw',level:'intermediate',goal:'draw',move_limit:30,instructions:'Defend with Black.',solver:'black'}];
  window.attrs={entry_id:'board-one',full_fen:'8/8/8/4k3/8/8/8/3QK3 w - - 0 1',physical_operation:'idle',local_game_active:false,saved_games:[],drills};
  window.hass={states:{'sensor.board':{state:'x',attributes:attrs},'binary_sensor.connected':{state:'on'},'switch.training':{state:'off',attributes:{}},'select.level':{state:'3',attributes:{options:['1','2','3']}},'select.color':{state:'white',attributes:{options:['white','black']}}},callWS:async c=>{calls.push(c);return {response:{}};}};
  window.update=patch=>{Object.assign(attrs,patch);card.hass={...hass,states:{...hass.states,'sensor.board':{state:String(Math.random()),attributes:{...attrs}}}};};
  card.setConfig(config);card.hass=hass;
 });
 const h2=()=>page.locator('phantom-chess-card h2').first().textContent();
 const btn=name=>page.getByRole('button',{name,exact:true});

 await page.getByText('Choose a game',{exact:true}).click();
 assert.equal(await page.getByText('Endgame drills',{exact:true}).count(),1);
 await btn('Queen and king mate · beginner').click();
 assert.deepEqual(await page.evaluate(()=>calls.at(-1).service_data),{entry_id:'board-one',drill:'queen_mate'});
 assert.equal(await page.evaluate(()=>calls.at(-1).service),'start_drill');

 const active={...(await page.evaluate(()=>drills[0])),status:'active',moves:3,reason:null};
 await page.evaluate(d=>update({local_game_active:true,side_to_move:'white',our_color:'white',drill:d,move_history_moves:[]}),active);
 assert.equal(await page.getByText('Endgame drill · beginner',{exact:true}).count(),1);
 assert.equal(await page.getByText('Checkmate with queen and king.',{exact:true}).count(),1);
 assert.equal(await page.getByText('Move 3 of 15',{exact:true}).count(),1);
 assert.equal(await btn('Save & pause').count(),0);
 assert.equal(await btn('Take back').count(),1);
 await btn('End drill').click();
 assert.match(dialogs.at(-1),/End this drill\?/);

 await page.evaluate(d=>update({local_game_active:false,drill:{...d,status:'failure',reason:'Stalemate: the king had no legal move but was not in check.'}}),active);
 assert.equal(await h2(),'Drill failed');
 assert.equal(await page.getByText(/Stalemate: the king had no legal move/).count(),1);
 await btn('Try again').click();
 assert.deepEqual(await page.evaluate(()=>calls.at(-1).service_data),{entry_id:'board-one',drill:'queen_mate'});
 await page.evaluate(d=>update({drill:{...d,status:'success',reason:'Checkmate.'}}),active);
 assert.equal(await h2(),'Drill complete');

 assert.deepEqual(errors,[]);
 await browser.close();
 console.log('Frontend drills: behavioral assertions passed.');
})().catch(e=>{console.error(e);process.exit(1);});
