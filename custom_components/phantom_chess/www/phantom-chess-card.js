/* Phantom Chess: packaged, authenticated Lovelace application. No external assets. */
// HA installs its scoped custom-element registry during bootstrap. Defining
// a card before that replaces the native registry can lose the registration.
// Wait before capturing HTMLElement as the card's base class as well.
await customElements.whenDefined('home-assistant');
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pieces = {K:'♔',Q:'♕',R:'♖',B:'♗',N:'♘',P:'♙',k:'♚',q:'♛',r:'♜',b:'♝',n:'♞',p:'♟'};
const names = {k:'king',q:'queen',r:'rook',b:'bishop',n:'knight',p:'pawn'};
// Rounded CCRL benchmark bands from Stockfish 18's published Elo-to-skill
// curve, inverted for this integration's skills [0,1,2,3,7,11,15,20].
// These are reference bands, not calibrated ratings of our depth-limited play.
const levelStrength = {
  '1':'≈1,300–1,400', '2':'≈1,400–1,500', '3':'≈1,500–1,600',
  '4':'≈1,700–1,800', '5':'≈2,500–2,600', '6':'≈2,800–2,900',
  '7':'≈3,000–3,100', '8':'Maximum strength',
};
const badgeSpecs={best:['✓','Best','#386b32'],excellent:['!','Excellent','#386b32'],good:['!','Good','#486b25'],brilliant:['!!','Brilliant','#087a89'],book:['▤','Book','#745331'],inaccuracy:['?!','Inaccuracy','#856000'],mistake:['?','Mistake','#a64e00'],blunder:['??','Blunder','#ba2929'],unknown:['…','Not analyzed','#636b68']};
const levelLabel = value => levelStrength[value]
  ? `Level ${value} · ${levelStrength[value]}${value==='8'?'':' benchmark Elo'}` : value;
class PhantomChessCard extends HTMLElement {
  constructor() { super(); this.attachShadow({mode:'open'}); this.ply=0; this.query=''; this.reviewRequest=0; this.reviewFailures=0; this.showAdvantage=false; try { this.showAdvantage=localStorage.getItem('phantom-show-advantage')==='true'; } catch {} }
  setConfig(config) { if (!config.entity) throw new Error('Choose a Phantom live position entity'); this.config=config; this.render(); this.scheduleReview(); }
  getCardSize() { return 10; }
  set hass(hass) {
    this._hass=hass;
    const next=JSON.stringify([hass.states[this.config?.entity], ...['paused','voice','training','level','color','connected','battery','firmware'].map(k=>hass.states[this.config?.[k]])]);
    if (next!==this.snapshot) { this.snapshot=next; this.render(); }
  }
  get a() { return this._hass?.states[this.config?.entity]?.attributes || {}; }
  get active() { return this.a.local_game_active || this.a.lichess_active || this.a.two_player_active || this.a.ai_vs_ai_active || this.a.sculpture_active; }
  get blocked() { return this.pending || ['moving','undoing','uncertain'].includes(this.a.physical_operation); }
  state(key) { return this._hass?.states[this.config?.[key]]?.state ?? 'unavailable'; }
  button(label, action, disabled=false, extra='') { return `<button data-action="${action}" ${disabled?'disabled':''} ${extra}>${esc(label)}</button>`; }
  async service(service, data={}, response=false, domain='phantom_chess') {
    if (domain==='phantom_chess') {
      if (!this.a.entry_id) throw new Error('The board integration is still loading');
      data={entry_id:this.a.entry_id,...data};
    }
    const result=await this._hass.callWS({type:'call_service',domain,service,service_data:data,...(response?{return_response:true}:{})});
    return result?.response ?? result;
  }
  async run(fn) {
    if (this.pending) return;
    this.pending=true; this.error=''; this.notice=''; this.render();
    try { await fn(); } catch(e) { this.error=e.message || String(e); }
    finally { this.pending=false; this.render(); }
  }
  control(key,label) {
    const entity=this._hass?.states[this.config?.[key]];
    if (!entity) return `<p>${esc(label)}: unavailable</p>`;
    if (this.config[key].startsWith('select.')) return `<label>${esc(label)}<select data-select="${key}" ${key==='level'?'aria-describedby="strength-guide"':''} ${this.active?'disabled':''}>${(entity.attributes.options||[]).map(o=>`<option value="${esc(o)}" ${o===entity.state?'selected':''}>${esc(key==='level'?levelLabel(o):o)}</option>`).join('')}</select></label>${key==='level'?'<p class="caption" id="strength-guide">Approximate local Stockfish benchmark guide. Actual strength at these search depths is uncalibrated; these are not FIDE or Chess.com ratings. Lichess levels use different settings.</p>':''}`;
    return `<label class="toggle"><input type="checkbox" data-toggle="${key}" ${entity.state==='on'?'checked':''} ${this.pending?'disabled':''}>${esc(label)}</label>`;
  }
  board(fen, interactive=false) {
    const squares=[];
    for(const row of (fen||'8/8/8/8/8/8/8/8').split(' ')[0].split('/')) for(const c of row) {
      if(/[1-8]/.test(c)) squares.push(...Array(Number(c)).fill('')); else squares.push(c);
    }
    if(squares.length!==64) return '<p>Waiting for a board position.</p>';
    const flipped=interactive && this.a.our_color==='black';
    const order=Array.from({length:64},(_,i)=>flipped?63-i:i);
    return `<div class="board" role="group" aria-label="Chessboard">${order.map(i=>{
      const p=squares[i], sq='abcdefgh'[i%8]+(8-Math.floor(i/8));
      const legal=(this.a.legal_moves||[]).filter(m=>m.startsWith(this.selected||'-'));
      const target=interactive && legal.some(m=>m.slice(2,4)===sq);
      const last=(this.a.move_history_moves||[]).at(-1);
      const feedback=this.state('training')==='on' && !this.a.lichess_active && ['play','learn'].includes(this.config.view||'play') && last?.uci?.slice(2,4)===sq;
      const [glyph,grade,color]=badgeSpecs[last?.classification]||badgeSpecs.unknown;
      const label=`${sq}${p?`, ${p===p.toUpperCase()?'white':'black'} ${names[p.toLowerCase()]}`:', empty'}`;
      return `<button class="square ${(Math.floor(i/8)+i%8)%2?'dark':'light'} ${sq===this.selected?'selected':''} ${target?'target':''}" data-square="${sq}" aria-label="${esc(label)}" ${!interactive||this.blocked||!(this.a.legal_moves||[]).length?'disabled':''}><span class="piece ${p===p.toUpperCase()?'white':'black'}">${pieces[p]||''}</span>${feedback?`<span class="move-badge square-badge" style="background:${color}" title="${grade}" aria-label="${grade}">${glyph}</span>`:''}<small>${sq}</small></button>`;
    }).join('')}</div>`;
  }
  advantage(data=this.a, live=true) {
    const toggle=`<label class="toggle"><input type="checkbox" data-advantage ${this.showAdvantage?'checked':''}>Show advantage bar</label>`;
    if(!this.showAdvantage) return toggle;
    if(live && this.a.lichess_active) return `${toggle}<p class="caption">Live evaluation is hidden during online play.</p>`;
    const fresh=data.eval_fen && data.eval_fen===data.full_fen;
    const cp=data.eval_cp, mate=data.eval_mate;
    if(!fresh || mate===0 || !(Number.isFinite(cp)||Number.isFinite(mate))) return `${toggle}<p class="caption" role="status">Evaluation pending or unavailable for this position.</p>`;
    const white=Number.isFinite(mate)?(mate>0?100:0):100/(1+Math.exp(-0.00368208*cp));
    const score=Number.isFinite(mate)?`Mate ${Math.abs(mate)}`:`${cp>=0?'+':''}${(cp/100).toFixed(1)}`;
    const side=Number.isFinite(mate)?(mate>0?'White':'Black'):(cp>0?'White':cp<0?'Black':'Neither side');
    return `${toggle}<div class="advantage" role="img" aria-label="${esc(side)} advantage, ${esc(score)}"><div style="width:${white}%"></div></div><p class="caption">${esc(score)} · ${esc(side)}${cp===0&&!Number.isFinite(mate)?' has an advantage':' favored'} · depth ${esc(data.eval_depth??'unknown')}. Engine estimate, not a guaranteed outcome.</p>`;
  }
  moveBadges() {
    if(this.state('training')!=='on' || this.a.lichess_active) return '';

    return `<div class="move-badges" aria-label="Move coaching">${(this.a.move_history_moves||[]).slice(-12).map(m=>{const [glyph,label,color]=badgeSpecs[m.classification]||badgeSpecs.unknown;return `<span class="move-feedback"><strong>${esc(m.san)}</strong> <span class="move-badge" style="background:${color}" aria-label="${label}">${glyph}</span> ${label}${m.best_san&&['inaccuracy','mistake','blunder'].includes(m.classification)?` · Try ${esc(m.best_san)}`:''}</span>`;}).join('')||'<p class="caption">Move feedback will appear as analysis finishes.</p>'}</div>`;
  }
  play() {
    const unavailable=this.state('connected')!=='on';
    const ended=!this.active && ['checkmate','stalemate','draw'].includes(this.a.game_status);
    const side=this.a.side_to_move==='black'?'Black':'White';
    const result=this.a.game_status==='checkmate'?`Checkmate — ${side==='White'?'Black':'White'} wins`:this.a.game_status==='stalemate'?'Draw — stalemate':'Game drawn';
    const status=this.blocked?this.a.physical_operation==='uncertain'?'Check the board':'Board is moving':this.active?this.a.paused?'Game paused':this.a.game_status==='check'?`${side} is in check`:`${side} to move`:ended?result:'Ready when you are';
    return `<div class="columns"><section>${this.board(this.a.full_fen,true)}${this.advantage()}${this.moveBadges()}<p class="caption">${this.active?'Move a piece on the board, or select a piece and its destination here.':ended?'This game is complete. Open Review to revisit the moves.':'Your next game starts here.'}</p>${this.promotion?`<div role="group" aria-label="Promote pawn"><p>Promote to</p>${['q','r','b','n'].map(p=>this.button(names[p],`promote-${p}`)).join('')}</div>`:''}</section><section class="panel"><p class="eyebrow">${esc(this.a.opening_name||'Your chess table')}</p><h2>${esc(status)}</h2>${ended?`<div class="notice" role="status"><p>${this.a.game_status==='checkmate'?`${side} is in check with no legal escape. No further moves can be played.`:'The game has ended in a draw.'}</p><a href="/phantom-chess/review">Open game review →</a></div>`:''}${unavailable?'<p>Connect the board and its Bluetooth adapter to play. Saved games remain available in Review.</p>':''}${!this.active?`<p>A game at your pace, with your usual color and difficulty.</p>${this.button('Play my usual game','start_local_game',this.blocked||unavailable,'class="primary"')}${this.a.recovery_available?this.button('Resume saved game','recover',this.pending||unavailable):''}<details><summary>Choose a game</summary>${this.control('level','Computer level')}${this.control('color','Play as')}<div class="actions">${this.button('Two people at the board','start_two_player_game',this.blocked||unavailable)}${this.button('Play online','start_game',this.blocked||unavailable)}${this.button('Watch computers play','start_ai_vs_ai_game',this.blocked||unavailable)}</div></details>`:`<div class="actions">${this.button(this.a.paused?'Continue':'Pause','pause',this.blocked)}${this.a.local_game_active?this.button('Save & pause','save_game',this.blocked):''}${this.button('Take back','takeback',this.blocked)}${this.button('End game','end',this.pending)}</div>`}<div class="history"><h3>Moves</h3><p>${esc((this.a.move_history_moves||[]).slice(-16).map(m=>`${m.side==='white'?m.move_num+'. ':''}${m.san}`).join(' ')||'The first move is yours to make.')}</p></div></section></div>`;
  }
  learn() {
    return `<div class="columns"><section>${this.board(this.a.full_fen)}${this.advantage()}${this.moveBadges()}<p class="caption">Learning follows the current game.</p></section><section class="panel"><p class="eyebrow">Learn at the board</p><h2>Understand the next move</h2>${this.control('training','Show coaching during play')}<p>Turn coaching on for hints and move badges. When it is off, spoken mistake alerts can still play if voice announcements are enabled.</p><dl><dt>Opening</dt><dd>${esc(this.a.opening_name||'No opening identified')}</dd><dt>Suggested move</dt><dd>${esc(this.state('training')==='on'?(this.a.best_move_san||'Analysis is not available yet'):'Coaching is off')}</dd><dt>Watch for</dt><dd>${esc(this.state('training')==='on'?(this.a.threat_san||'No threat identified'):'Coaching is off')}</dd></dl><h3>Practice a position</h3><p>Open a saved game in Review, move to any position, and choose “Practice from here.” The original game stays intact.</p><a href="/phantom-chess/review">Open Review →</a></section></div>`;
  }
  disconnectedCallback() { clearTimeout(this.reviewTimer); this.reviewRequest++; }
  connectedCallback() {
    if(this.reviewGame && this.config?.view==='review') { this.reviewFailures=0; this.fetchReview(); }
  }
  scheduleReview() {
    clearTimeout(this.reviewTimer);
    const retry=this.reviewFetchError && this.reviewFailures<5;
    if(this.config?.view==='review' && this.reviewGame && this.isConnected &&
       (retry || (!this.reviewFetchError && this.reviewReport?.status==='running'))) {
      const delay=this.reviewFetchError?Math.min(30000,2000*2**this.reviewFailures):2000;
      this.reviewTimer=setTimeout(()=>this.fetchReview(),delay);
    }
  }
  async fetchReview() {
    const id=this.reviewGame?.game_id;
    if(!id) return;
    clearTimeout(this.reviewTimer);
    const request=++this.reviewRequest;
    const current=()=>this.reviewGame?.game_id===id && request===this.reviewRequest && this.isConnected;
    try {
      const report=await this.service('game_library',{action:'review',game_id:id},true);
      if(!current()) return;
      this.reviewReport=report; this.reviewFailures=0; this.reviewFetchError='';
    } catch(e) {
      if(!current()) return;
      this.reviewFailures++;
      this.reviewFetchError=this.reviewFailures<5
        ?'Analysis updates were interrupted. Retrying automatically; the last received result is shown.'
        :'Analysis updates are unavailable. Check your Home Assistant connection, then choose Refresh analysis.';
    }
    if(current()) { this.render(); this.scheduleReview(); }
  }
  reviewCoaching() {
    if(!this.reviewGame) return '';
    const r=this.reviewReport, current=r?.moves?.find(m=>m.ply===this.ply);
    const errors=r?.moves?.filter(m=>['inaccuracy','mistake','blunder'].includes(m.classification))||[];
    const ev=r?.evaluations?.[this.ply], fen=this.reviewGame.positions[this.ply];
    const countLabel=(n,word)=>`${n} ${word}${n===1?'':'s'}`;
    const statusLabel={not_started:'Ready',running:'Analyzing',paused:'Paused',complete:'Complete',failed:'Needs attention'};
    const counts=['white','black'].map(side=>{const own=r?.moves?.filter(m=>m.side===side)||[];return `${side==='white'?'White':'Black'}: ${countLabel(own.filter(m=>m.classification==='blunder').length,'blunder')}, ${countLabel(own.filter(m=>m.classification==='mistake').length,'mistake')}`;}).join(' · ');
    const meter=this.advantage({full_fen:fen,eval_fen:ev?fen:null,eval_cp:ev?.cp,eval_mate:ev?.mate,eval_depth:ev?.depth},false);
    return `${meter}<section class="panel review-coaching"><h3>Game coaching</h3>${this.reviewFetchError?`<p class="error" role="alert">${esc(this.reviewFetchError)}</p>`:''}${r?.analyzed?`<p class="caption">${esc(counts)} · analyzed moves only</p>`:''}<p role="status">${r?`${this.reviewFetchError?'Last received: ':''}${esc(statusLabel[r.status]||'Unavailable')} · ${r.analyzed} of ${countLabel(r.total,'move')} analyzed`:'Ready to analyze this game'}</p>${r?.error?`<p class="error" role="alert">${esc(r.error)}</p>`:''}<div class="actions">${r?.status==='running'?this.button('Cancel analysis','cancel-analysis',this.pending):this.button(r?.status==='complete'?'Reanalyze game':r?.analyzed?'Continue analysis':'Analyze game','analyze',this.pending||this.active)}${this.button('Refresh analysis','refresh-analysis',this.pending)}</div>${current?`<p><strong>${esc(current.san)} · ${esc(current.label)}</strong></p><p>${esc(current.coaching)}</p><p>${esc(current.before)} → ${esc(current.after)} (White's perspective)</p>${current.variation?.length?`<p>Suggested line: ${esc(current.variation.join(' '))}</p>`:''}${this.button('Practice before this move','practice-coached',this.pending||this.active)}`:'<p>Select an analyzed move to see its feedback.</p>'}${errors.length?`<details open><summary>${countLabel(errors.length,'moment')} to revisit</summary><div class="actions">${errors.map(m=>`<button data-review-ply="${m.ply}">${m.move_num}${m.side==='white'?'.':'…'} ${esc(m.san)} · ${esc(m.label)}</button>`).join('')}</div></details>`:''}<p class="caption">${esc(r?.method||'Analysis runs locally and never moves the board. You can leave this page while it works.')}</p></section>`;
  }
  review() {
    const games=(this.a.saved_games||[]).filter(g=>`${g.white} ${g.black} ${g.game_id}`.toLowerCase().includes(this.query.toLowerCase()));
    return `<div class="columns"><section>${this.board(this.reviewGame?.positions?.[this.ply]||this.a.full_fen)}${this.reviewGame?`<label>Position ${this.ply} of ${this.reviewGame.sans.length}<input aria-label="Replay position" data-replay type="range" min="0" max="${this.reviewGame.sans.length}" value="${this.ply}"></label><p>${esc(this.ply?this.reviewGame.sans[this.ply-1]:'Starting position')}</p><div class="actions">${this.button('Export PGN','download',this.pending)}${this.button('Practice from here','practice',this.pending||this.active)}${this.button('Resume this game','resume-selected',this.pending||this.active)}${this.button('Delete saved game','delete',this.pending||this.active)}</div>`:'<p class="caption">Choose a game to replay every position.</p>'}${this.reviewCoaching()}</section><section class="panel"><p class="eyebrow">Your games</p><h2>A little better each time</h2><label>Search games<input data-search type="search" value="${esc(this.query)}" placeholder="Player or game"></label><div class="games">${games.length?games.map(g=>`<button data-game="${esc(g.game_id)}"><strong>${esc(g.white)} · ${esc(g.black)}</strong><span>${esc(g.status)} · ${g.plies} plies · ${esc(g.result)}<br>${esc(new Date(g.updated).toLocaleString())}</span></button>`).join(''):'<p>No matching saved games. Local games are saved automatically.</p>'}</div><details><summary>Import a PGN</summary><label>Paste one game<textarea data-pgn rows="7" maxlength="256000" placeholder="[Event &quot;My game&quot;]"></textarea></label>${this.button('Import game','import',this.pending)}</details></section></div>`;
  }
  settings() {
    const engine=this.a.engine_health||{};
    const engineLabel={not_checked:'Not checked yet',verifying:'Verifying engine',downloading:'Downloading engine',installing:'Installing verified engine',ready:'Ready',unavailable:'Unavailable',error:'Needs attention'};
    return `<div class="columns"><section class="panel"><p class="eyebrow">Board</p><h2>Ready for a real game</h2><dl><dt>Connection</dt><dd>${esc(this.state('connected')==='on'?'Connected':'Unavailable')}</dd><dt>Battery</dt><dd>${esc(this.state('battery'))}%</dd><dt>Firmware</dt><dd>${esc(this.a.firmware_version||'Unknown')}</dd><dt>Movement</dt><dd>${esc(this.a.physical_operation||'idle')}</dd><dt>Chess engine</dt><dd>${esc(engineLabel[engine.status]||engineLabel.not_checked)}${engine.version?` · ${esc(engine.version)}`:''}</dd></dl>${engine.error?`<p class="error">${esc(engine.error)}</p>`:''}${this.button('Check chess engine','check_engine',this.pending||this.active)}<p class="caption">Checks local analysis without moving pieces. The first check may download the engine.</p><p>Resetting moves the pieces back to the starting position. Keep the board clear while it works.</p>${this.button('Reset board','reset',this.pending||['moving','undoing'].includes(this.a.physical_operation))}</section><section class="panel"><p class="eyebrow">Your preferences</p><h2>Make it yours</h2>${this.control('level','Usual computer level')}${this.control('color','Usual color')}${this.control('voice','Spoken move announcements')}${this.control('training','Coaching during play')}<p>Speech routing and provider settings are available in the integration options.</p><a href="/config/integrations/integration/phantom_chess">Open integration settings →</a></section></div>`;
  }
  render() {
    if(!this.config||!this._hass) return;
    const view=this.config.view||'play';
    const focus=this.shadowRoot.activeElement;
    const focusKey=focus?.hasAttribute('data-search')?'[data-search]':focus?.hasAttribute('data-pgn')?'[data-pgn]':null;
    const draft=this.shadowRoot.querySelector('[data-pgn]')?.value;
    const selection=focusKey?[focus.selectionStart,focus.selectionEnd]:null;
    this.shadowRoot.innerHTML=`<style>
      .square-badge{position:absolute;right:0;top:0;font-size:14px;z-index:1}.advantage{height:24px;border:1px solid #647269;border-radius:6px;background:#202d26;overflow:hidden}.advantage>div{height:100%;background:#fffef4;transition:width .3s}.move-badges{display:flex;flex-wrap:wrap;gap:8px}.move-feedback{font-size:14px;padding:6px;border:1px solid var(--divider-color,#ccc);border-radius:6px}.move-badge{display:inline-grid;place-items:center;border-radius:50%;width:28px;height:28px;color:white;font-weight:bold}:host{display:block;color:var(--primary-text-color,#24342d);font:16px/1.55 var(--paper-font-body1_-_font-family,system-ui);background:var(--primary-background-color,#f4f3ed);min-height:100%}*{box-sizing:border-box}main{max-width:1200px;margin:auto;padding:32px}header{display:flex;justify-content:space-between;align-items:center;gap:20px;margin-bottom:28px}h1{font:500 29px Georgia,serif;margin:0}h2{font:500 30px/1.2 Georgia,serif;margin:8px 0 20px}h3{font-size:16px}nav{display:flex;gap:6px;flex-wrap:wrap}nav a{color:inherit;text-decoration:none;padding:8px 12px;border-radius:24px}nav a[aria-current=page]{background:#244d3c;color:white}.columns{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(280px,1fr);gap:36px}.panel{padding:26px;border:1px solid var(--divider-color,#dadfd8);border-radius:20px;background:var(--card-background-color,#fff)}.eyebrow{font-size:12px;text-transform:uppercase;letter-spacing:.15em;color:var(--secondary-text-color,#61756a)}.board{display:grid;grid-template-columns:repeat(8,1fr);aspect-ratio:1;border:8px solid #315541;border-radius:8px;overflow:hidden}.square{position:relative;min-height:0;min-width:0;border:0;border-radius:0;padding:0;aspect-ratio:1;font-size:clamp(22px,4vw,52px);display:grid;place-items:center;opacity:1!important}.square.light{background:#e9e5d6}.square.dark{background:#7e9a84}.piece.white{color:#fffef4;text-shadow:0 1px 1px #152c20,-1px 0 #152c20,1px 0 #152c20}.piece.black{color:#172a21;text-shadow:0 1px #eff3eb}.square small{position:absolute;bottom:1px;left:3px;font:9px system-ui;color:#203d2c}.square.selected{box-shadow:inset 0 0 0 4px #f7c553}.square.target{box-shadow:inset 0 0 0 4px #d9bb45}.caption{font-size:13px;color:var(--secondary-text-color,#61756a)}button,select,input,textarea{font:inherit}button{cursor:pointer;border:1px solid var(--divider-color,#d3dcd4);border-radius:10px;padding:10px 16px;background:var(--card-background-color,#fff);color:inherit;min-height:44px}button:disabled{cursor:default;opacity:.5}button.primary{background:#244d3c;color:#fff;width:100%;margin-bottom:12px}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid #d79921;outline-offset:3px}.actions{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0}details{margin-top:24px}summary{cursor:pointer;padding:10px 0}label{display:block;margin:16px 0}input:not([type=checkbox]),select,textarea{display:block;width:100%;margin-top:6px;padding:10px;border:1px solid var(--divider-color,#cdd5cf);border-radius:8px;background:var(--card-background-color,#fff);color:inherit}.toggle{display:flex;gap:12px;align-items:center;min-height:44px}.toggle input{width:22px;height:22px}a{color:var(--primary-color,#2c6549)}dl{display:grid;grid-template-columns:1fr 1fr;gap:12px}dt{color:var(--secondary-text-color,#657268)}dd{margin:0;text-align:right}.history{border-top:1px solid var(--divider-color,#dde2dc);margin-top:28px}.error,.notice{padding:14px;border-radius:10px;margin:12px 0}.error{background:#fff0d8;color:#633b00}.notice{background:#e4f0e6;color:#244d3c}.games{max-height:430px;overflow:auto}.games button{width:100%;text-align:left;margin-bottom:8px}.games span{display:block;font-size:13px;color:var(--secondary-text-color,#61756a)}@media(max-width:760px){main{padding:16px}.columns{grid-template-columns:1fr;gap:18px}header{align-items:flex-start;flex-direction:column}.panel{padding:20px}.square{font-size:clamp(25px,8vw,52px)}nav{gap:0}nav a{padding:8px 10px}}
      </style><main><header><h1>♞ Phantom Chess</h1><nav aria-label="Chess navigation">${[['play','main','Play'],['learn','learn','Learn'],['review','review','Review'],['settings','board','Board & settings']].map(([v,path,label])=>`<a href="/phantom-chess/${path}" ${view===v?'aria-current="page"':''}>${label}</a>`).join('')}</nav></header><div role="status" aria-live="polite">${this.pending?'<p>Working…</p>':''}${this.notice?`<p class="notice">${esc(this.notice)}</p>`:''}</div>${[this.error,this.a.game_reviews?.error,this.a.speech_error,this.a.engine_error,this.a.journal_error,this.a.library_error,this.a.physical_operation==='uncertain'?'The last movement was not confirmed. Check the board, then resume the saved game or reset the position.':''].filter(Boolean).map(e=>`<p class="error" role="alert">${esc(e)}</p>`).join('')}${view==='learn'?this.learn():view==='review'?this.review():view==='settings'?this.settings():this.play()}</main>`;
    const textarea=this.shadowRoot.querySelector('[data-pgn]'); if(textarea&&draft!==undefined) textarea.value=draft;
    if(focusKey) { const el=this.shadowRoot.querySelector(focusKey);el?.focus();if(selection&&el?.setSelectionRange&&el.type!=='search') el.setSelectionRange(...selection); }
    this.shadowRoot.querySelectorAll('[data-action]').forEach(b=>b.onclick=()=>this.action(b.dataset.action));
    this.shadowRoot.querySelectorAll('[data-square]').forEach(b=>b.onclick=()=>this.square(b.dataset.square));
    this.shadowRoot.querySelectorAll('[data-game]').forEach(b=>b.onclick=()=>this.run(async()=>{this.reviewReport=null;this.reviewFetchError='';this.reviewFailures=0;this.reviewGame=await this.service('game_library',{action:'export',game_id:b.dataset.game},true);this.ply=0;await this.fetchReview();}));
    this.shadowRoot.querySelectorAll('[data-toggle]').forEach(el=>el.onchange=()=>this.run(()=>this.service(el.checked?'turn_on':'turn_off',{entity_id:this.config[el.dataset.toggle]},false,'switch')));
    this.shadowRoot.querySelectorAll('[data-select]').forEach(el=>el.onchange=()=>{const option=el.value;this.run(()=>this.service('select_option',{entity_id:this.config[el.dataset.select],option},false,'select'));});
    const advantage=this.shadowRoot.querySelector('[data-advantage]');if(advantage) advantage.onchange=()=>{this.showAdvantage=advantage.checked;try{localStorage.setItem('phantom-show-advantage',String(this.showAdvantage));}catch{}this.render();};
    const search=this.shadowRoot.querySelector('[data-search]');if(search) search.oninput=()=>{this.query=search.value;this.render();};
    this.shadowRoot.querySelectorAll('[data-review-ply]').forEach(el=>el.onclick=()=>{this.ply=Number(el.dataset.reviewPly);this.render();});
    const replay=this.shadowRoot.querySelector('[data-replay]');if(replay) replay.oninput=()=>{this.ply=Number(replay.value);this.render();this.shadowRoot.querySelector('[data-replay]')?.focus();};
  }
  square(sq) {
    const matches=(this.a.legal_moves||[]).filter(m=>m.startsWith(this.selected||'-')&&m.slice(2,4)===sq);
    if(matches.length>1) {this.promotion=this.selected+sq;this.render();return;}
    if(matches.length===1) { const move=matches[0];this.selected=null;this.run(()=>this.service('execute_move',{move}));return; }
    this.selected=(this.a.legal_moves||[]).some(m=>m.startsWith(sq))?sq:null;this.render();
  }
  action(action) {
    const pgn=this.shadowRoot.querySelector('[data-pgn]')?.value;
    if(action==='end'&&!confirm('End this game? Your local game will remain in Review.')) return;
    if(action==='reset'&&!confirm('End local play and move the pieces to their starting position?')) return;
    if(action==='delete'&&!confirm('Delete this saved game permanently? Export it first if you want a copy.')) return;
    this.run(async()=>{
      if(action.startsWith('promote-')) {const move=this.promotion+action.slice(-1);this.promotion=null;this.selected=null;await this.service('execute_move',{move});}
      else if(action==='analyze'||action==='cancel-analysis') {await this.service('game_library',{action:action==='analyze'?(this.reviewReport?.status==='complete'?'reanalyze':'analyze'):'cancel_review',game_id:this.reviewGame.game_id},true);await this.fetchReview();}
      else if(action==='refresh-analysis') {this.reviewFailures=0;await this.fetchReview();}
      else if(action==='check_engine') {const result=await this.service('check_engine',{},true);this.notice=result.status==='ready'?'The chess engine is ready.':result.error||'Engine check finished.';}
      else if(action==='takeback') {
        // On your turn in a local game the last ply is the computer's reply;
        // undo the pair so your own move comes back, not just the reply.
        const pair=this.a.local_game_active&&this.a.side_to_move===this.a.our_color&&(this.a.move_history_moves||[]).length>=2;
        await this.service('takeback',pair?{count:2}:{});
      }
      else if(action==='pause') await this.service(this.a.paused?'turn_off':'turn_on',{entity_id:this.config.paused},false,'switch');
      else if(action==='end') await this.service('back_to_modes');
      else if(action==='reset') await this.service('reset_position');
      else if(action==='recover') await this.service('resume_game');
      else if(action==='resume-selected') await this.service('resume_game',{game_id:this.reviewGame.game_id});
      else if(action==='import') {await this.service('game_library',{action:'import',pgn},true);this.notice='Game imported.';}
      else if(action==='delete') {await this.service('game_library',{action:'delete',game_id:this.reviewGame.game_id},true);this.reviewGame=null;this.reviewReport=null;clearTimeout(this.reviewTimer);}
      else if(action==='practice'||action==='practice-coached') {const saved=await this.service('game_library',{action:'practice',game_id:this.reviewGame.game_id,ply:action==='practice-coached'?Math.max(0,this.ply-1):this.ply},true);this.notice='Practice position saved. Select it below and choose Resume this game when the board is ready.';this.reviewGame=await this.service('game_library',{action:'export',game_id:saved.game_id},true);this.ply=0;this.reviewReport=null;clearTimeout(this.reviewTimer);}
      else if(action==='download') {const url=URL.createObjectURL(new Blob([this.reviewGame.pgn],{type:'application/x-chess-pgn'}));const a=document.createElement('a');a.href=url;a.download=`phantom-${this.reviewGame.game_id}.pgn`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
      else {await this.service(action);if(action==='save_game') this.notice='Saved. Your game is paused.';}
    });
  }
}
if(!customElements.get('phantom-chess-card')) customElements.define('phantom-chess-card',PhantomChessCard);
window.customCards=window.customCards||[];
window.customCards.push({type:'phantom-chess-card',name:'Phantom Chess',description:'Play, learn, review and manage your Phantom board.'});
