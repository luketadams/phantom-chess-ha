/* Puzzle mode in the bundled card. Run with Node and Playwright installed. */
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
  window.attrs={entry_id:'board-one',full_fen:'r5k1/5ppp/3P4/2p5/1p1q4/3Q3P/5PP1/4R1K1 w - - 1 34',physical_operation:'idle',local_game_active:false,saved_games:[]};
  window.hass={states:{'sensor.board':{state:'x',attributes:attrs},'binary_sensor.connected':{state:'on'},'switch.training':{state:'off',attributes:{}},'select.level':{state:'3',attributes:{options:['1','2','3']}},'select.color':{state:'white',attributes:{options:['white','black']}}},callWS:async c=>{calls.push(c);return {response:{}};}};
  window.update=patch=>{Object.assign(attrs,patch);card.hass={...hass,states:{...hass.states,'sensor.board':{state:String(Math.random()),attributes:{...attrs}}}};};
  card.setConfig(config);card.hass=hass;
 });
 const h2=()=>page.locator('phantom-chess-card h2').first().textContent();
 const btn=name=>page.getByRole('button',{name,exact:true});

 // Starting a puzzle from the game chooser.
 await page.getByText('Choose a game',{exact:true}).click();
 await btn('Daily puzzle').click();
 let call=await page.evaluate(()=>calls.at(-1));
 assert.equal(call.service,'start_puzzle');
 assert.deepEqual(call.service_data,{entry_id:'board-one',source:'daily'});
 assert.equal(call.return_response,true);

 // An active puzzle: status, progress, no themes (they would give it away).
 const active={status:'active',solver:'white',rating:1964,progress:0,length:3,mistakes:0,hints:0,themes:['advancedPawn','mateIn2'],url:'https://lichess.org/training/abc12'};
 await page.evaluate(p=>update({local_game_active:true,side_to_move:'white',our_color:'white',puzzle:p,legal_moves:['d6d7']}),active);
 assert.equal(await h2(),'White to move · find the best move');
 assert.equal(await page.getByText('Lichess puzzle · rated 1964',{exact:true}).count(),1);
 assert.equal(await page.getByText('Move 1 of 2',{exact:true}).count(),1);
 assert.equal(await page.getByText(/advanced pawn/).count(),0);
 assert.equal(await btn('Take back').count(),0);
 assert.equal(await btn('Save & pause').count(),0);

 // Hint.
 await btn('Hint').click();
 assert.equal(await page.evaluate(()=>calls.at(-1).service),'puzzle_hint');
 await page.evaluate(()=>update({puzzle_hint:'d6'}));
 assert.equal(await page.getByText('Hint: move the piece on d6.',{exact:true}).count(),1);

 // A wrong try, then the board's turn.
 await page.evaluate(p=>update({puzzle:{...p,mistakes:1},puzzle_hint:null}),active);
 assert.equal(await page.getByText('Move 1 of 2 · 1 wrong try',{exact:true}).count(),1);
 await page.evaluate(p=>update({side_to_move:'black',puzzle:{...p,progress:1}}),active);
 assert.equal(await h2(),'Board is replying');
 assert.equal(await btn('Hint').isDisabled(),true);
 assert.equal(await btn('Show solution').isDisabled(),true);

 // Show solution and end.
 await page.evaluate(p=>update({side_to_move:'white',puzzle:{...p,progress:2}}),active);
 assert.equal(await page.getByText('Move 2 of 2',{exact:true}).count(),1);
 await btn('Show solution').click();
 assert.equal(await page.evaluate(()=>calls.at(-1).service),'puzzle_show_solution');
 await btn('End puzzle').click();
 assert.match(dialogs.at(-1),/End this puzzle\?/);
 assert.equal(await page.evaluate(()=>calls.at(-1).service),'back_to_modes');

 // Finished: result, themes revealed, Lichess link, another puzzle.
 await page.evaluate(p=>update({local_game_active:false,game_status:'idle',puzzle:{...p,status:'solved',progress:3,mistakes:1}}),active);
 assert.equal(await h2(),'Puzzle solved');
 assert.equal(await page.getByText('Solved in 2 tries.',{exact:true}).count(),1);
 assert.equal(await page.getByText('Themes: advanced pawn, mate in 2',{exact:true}).count(),1);
 assert.equal(await page.getByRole('link',{name:'Open this puzzle on Lichess →'}).getAttribute('href'),'https://lichess.org/training/abc12');
 await btn('Another puzzle').click();
 assert.deepEqual(await page.evaluate(()=>calls.at(-1).service_data),{entry_id:'board-one',source:'random'});
 await page.evaluate(p=>update({puzzle:{...p,status:'solved',mistakes:0,hints:0}}),active);
 assert.equal(await page.getByText('Solved on the first try.',{exact:true}).count(),1);
 await page.evaluate(p=>update({puzzle:{...p,status:'revealed'}}),active);
 assert.equal(await h2(),'Puzzle finished');
 assert.equal(await page.getByText('The solution was shown.',{exact:true}).count(),1);

 // Errors are shown; untrusted text is escaped.
 await page.evaluate(p=>update({puzzle_error:'The board did not confirm the move d4d7. The puzzle has stopped.',puzzle:{...p,status:'solved',themes:['<img src=x onerror=alert(1)>']}}),active);
 assert.equal(await page.getByRole('alert').filter({hasText:'did not confirm the move d4d7'}).count(),1);
 assert.equal(await page.locator('phantom-chess-card img').count(),0);

 assert.deepEqual(errors,[]);
 await browser.close();
 console.log('Frontend puzzle: behavioral assertions passed.');
})().catch(e=>{console.error(e);process.exit(1);});
