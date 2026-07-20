/* ============================================================
   game.js — 游戏主循环 / 状态机 / UI 绑定
   ============================================================ */
const B = window.BALATRO;

// ---------- 小丑牌定义（自研的简化效果集合） ----------
const JOKER_POOL = [
  { id:'joker',      emoji:'🤡', name:'小丑',       desc:'+4 倍率',                  hook:'flatMult', val:4,  price:2 },
  { id:'greedy',     emoji:'💎', name:'贪婪小丑',   desc:'每张方块 +3 倍率',         hook:'suitMult', suit:'D', val:3, price:5 },
  { id:'lusty',      emoji:'❤️', name:'好色小丑',   desc:'每张红桃 +3 倍率',         hook:'suitMult', suit:'H', val:3, price:5 },
  { id:'wrathful',   emoji:'♠️', name:'愤怒小丑',   desc:'每张黑桃 +3 倍率',         hook:'suitMult', suit:'S', val:3, price:5 },
  { id:'gluttonous', emoji:'♣️', name:'暴食小丑',   desc:'每张梅花 +3 倍率',         hook:'suitMult', suit:'C', val:3, price:5 },
  { id:'jolly',      emoji:'😄', name:'欢乐小丑',   desc:'打出对子时 +8 倍率',       hook:'handMult', need:'PAIR', val:8, price:4 },
  { id:'zany',       emoji:'🤪', name:'滑稽小丑',   desc:'打出三条时 +12 倍率',      hook:'handMult', need:'THREE_KIND', val:12, price:4 },
  { id:'crazy',      emoji:'🤯', name:'疯狂小丑',   desc:'打出顺子时 +12 倍率',      hook:'handMult', need:'STRAIGHT', val:12, price:4 },
  { id:'droll',      emoji:'😏', name:'诙谐小丑',   desc:'打出同花时 +10 倍率',      hook:'handMult', need:'FLUSH', val:10, price:4 },
  { id:'sly',        emoji:'🧐', name:'狡猾小丑',   desc:'打出对子时 +50 筹码',      hook:'handChips', need:'PAIR', val:50, price:3 },
  { id:'clever',     emoji:'🤓', name:'聪明小丑',   desc:'打出两对时 +80 筹码',      hook:'handChips', need:'TWO_PAIR', val:80, price:4 },
  { id:'even',       emoji:'2️⃣', name:'偶数小丑',   desc:'每张偶数点 +4 倍率',      hook:'parityMult', parity:0, val:4, price:4 },
  { id:'odd',        emoji:'1️⃣', name:'奇数小丑',   desc:'每张奇数点 +4 倍率',      hook:'parityMult', parity:1, val:4, price:4 },
  { id:'scholar',    emoji:'📚', name:'学者',       desc:'每张A +20筹码 +4倍率',     hook:'aceBonus', chips:20, mult:4, price:5 },
  { id:'bull',       emoji:'🐂', name:'公牛',       desc:'每有 $1 则 +2 筹码',       hook:'moneyChips', val:2, price:5 },
  { id:'mult8',      emoji:'✖️', name:'加倍卡',     desc:'倍率 ×1.5（结算最后）',    hook:'xmult', val:1.5, price:6 },
];

// ---------- 游戏状态 ----------
const S = {
  deck: [], drawPile: [], hand: [], played: [],
  selected: new Set(),
  jokers: [],
  handLevels: B.makeHandLevels(),
  money: 4,
  ante: 0,         // 0-based -> 底注 1
  blindIdx: 0,     // 0 small,1 big,2 boss
  roundScore: 0,
  goal: 0,
  handsLeft: 4, discardsLeft: 3,
  handSize: 8,
  maxHands: 4, maxDiscards: 3,
  busy: false,
};

const $ = (id) => document.getElementById(id);
const els = {
  hand: $('hand'), play: $('playArea'), jokers: $('jokersRow'),
  chips: $('chipsBox'), mult: $('multBox'), handLabel: $('handLabel'),
  roundScore: $('roundScore'), blindName: $('blindName'), blindGoal: $('blindGoal'),
  blindReward: $('blindReward'), blindToken: $('blindToken'),
  statHands: $('statHands'), statDiscards: $('statDiscards'), statMoney: $('statMoney'),
  statAnte: $('statAnte'), statRound: $('statRound'), statHandSize: $('statHandSize'),
  deckCount: $('deckCount'),
  btnPlay: $('btnPlay'), btnDiscard: $('btnDiscard'),
  btnSortRank: $('btnSortRank'), btnSortSuit: $('btnSortSuit'),
  overlay: $('overlay'), overlayTitle: $('overlayTitle'), overlayText: $('overlayText'), overlayBtn: $('overlayBtn'),
  shop: $('shop'), shopItems: $('shopItems'), shopMoney: $('shopMoney'), shopNext: $('shopNext'),
};

// ---------- 卡牌 DOM ----------
function cardEl(card) {
  const el = document.createElement('div');
  el.className = `card ${card.color} floating`;
  el.dataset.id = card.id;
  el.innerHTML = `
    <div class="face">
      <div class="corner tl"><span>${card.label}</span><span class="pip">${card.symbol}</span></div>
      <div class="center">${card.symbol}</div>
      <div class="corner br"><span>${card.label}</span><span class="pip">${card.symbol}</span></div>
    </div>`;
  el.addEventListener('click', () => toggleSelect(card, el));
  return el;
}

function renderHand(deal = false) {
  els.hand.innerHTML = '';
  S.hand.forEach((card, i) => {
    const el = cardEl(card);
    if (S.selected.has(card.id)) el.classList.add('selected');
    if (deal) {
      el.classList.add('dealing');
      el.style.animationDelay = (i * 55) + 'ms';
    }
    els.hand.appendChild(el);
  });
  requestAnimationFrame(() => FX.layoutFan(els.hand));
  updateControls();
  els.deckCount.textContent = S.drawPile.length;
}

function toggleSelect(card, el) {
  if (S.busy) return;
  if (S.selected.has(card.id)) {
    S.selected.delete(card.id);
    el.classList.remove('selected');
  } else {
    if (S.selected.size >= 5) { FX.shake(el); return; }
    S.selected.add(card.id);
    el.classList.add('selected');
  }
  FX.layoutFan(els.hand);
  updateControls();
  previewHand();
}

function selectedCards() {
  return S.hand.filter(c => S.selected.has(c.id));
}

function previewHand() {
  const sel = selectedCards();
  if (sel.length === 0) { els.handLabel.innerHTML = '&nbsp;'; setFormula(0, 0); return; }
  const { key, name } = B.evaluateHand(sel);
  const base = B.baseHandScore(key, S.handLevels[key]);
  els.handLabel.textContent = `${name} Lv.${S.handLevels[key]}`;
  setFormula(base.chips, base.mult);
}

function setFormula(chips, mult) {
  els.chips.textContent = chips;
  els.mult.textContent = mult;
}

function updateControls() {
  const k = S.selected.size;
  els.btnPlay.disabled = S.busy || k === 0 || S.handsLeft <= 0;
  els.btnDiscard.disabled = S.busy || k === 0 || S.discardsLeft <= 0;
}

// ---------- 小丑牌渲染 ----------
function renderJokers() {
  els.jokers.innerHTML = '';
  S.jokers.forEach(j => {
    const el = document.createElement('div');
    el.className = 'joker';
    el.dataset.id = j.id;
    el.innerHTML = `<div class="j-emoji">${j.emoji}</div>
      <div class="j-name">${j.name}</div>
      <div class="tip"><b>${j.name}</b><br>${j.desc}</div>`;
    els.jokers.appendChild(el);
  });
}

// ---------- 抽牌 ----------
function draw(n) {
  for (let i = 0; i < n; i++) {
    if (S.drawPile.length === 0) break;
    S.hand.push(S.drawPile.pop());
  }
  sortHand('rank');
}

function sortHand(mode) {
  if (mode === 'suit') {
    const order = { S:0, H:1, D:2, C:3 };
    S.hand.sort((a, b) => order[a.suit] - order[b.suit] || b.rank - a.rank);
  } else {
    S.hand.sort((a, b) => b.rank - a.rank || a.suit.localeCompare(b.suit));
  }
}

// ---------- 计分序列（核心动效） ----------
async function playHand() {
  if (S.busy) return;
  const sel = selectedCards();
  if (sel.length === 0) return;
  S.busy = true; updateControls();
  S.handsLeft--;
  refreshStats();

  const orderIds = S.hand.filter(c => S.selected.has(c.id)).map(c => c.id);
  // 从手牌移除并放到 play 区
  S.played = orderIds.map(id => S.hand.find(c => c.id === id));
  S.hand = S.hand.filter(c => !S.selected.has(c.id));
  S.selected.clear();

  // 渲染 play 区
  els.play.innerHTML = '';
  const playEls = {};
  S.played.forEach((card) => {
    const el = cardEl(card);
    el.classList.remove('floating');
    el.classList.add('played');
    el.style.cursor = 'default';
    els.play.appendChild(el);
    playEls[card.id] = el;
  });
  renderHand();
  await FX.sleep(280);

  const { key, name, scoringCards } = B.evaluateHand(S.played);
  const base = B.baseHandScore(key, S.handLevels[key]);
  let chips = base.chips;
  let mult = base.mult;
  els.handLabel.textContent = `${name} Lv.${S.handLevels[key]}`;
  setFormula(chips, mult);

  const scoringSet = new Set(scoringCards.map(c => c.id));

  // 1) 逐张计入筹码
  for (const card of S.played) {
    if (!scoringSet.has(card.id)) continue;
    const el = playEls[card.id];
    FX.scoreBounce(el);
    chips += card.chips;
    FX.popText(el, `+${card.chips}`, 'chip');
    els.chips.textContent = chips;
    FX.bump(els.chips);
    await FX.sleep(230);
  }

  // 2) 小丑牌逐个触发
  for (const j of S.jokers) {
    const jEl = els.jokers.querySelector(`[data-id="${j.id}"]`);
    const before = { chips, mult };
    ({ chips, mult } = applyJoker(j, { chips, mult, key, scoringCards, played: S.played }));
    if (chips !== before.chips || mult !== before.mult) {
      if (jEl) FX.jokerTrigger(jEl);
      if (mult !== before.mult) {
        FX.popText(jEl || els.mult, mult > before.mult ? `+${(mult - before.mult).toFixed(0)}` : '', 'mult');
        els.mult.textContent = Math.round(mult);
        FX.bump(els.mult);
      }
      if (chips !== before.chips) {
        els.chips.textContent = Math.round(chips);
        FX.bump(els.chips);
      }
      await FX.sleep(260);
    }
  }

  // 3) 最终结算：chips × mult，数字滚动
  const gained = Math.round(chips * mult);
  await FX.sleep(150);
  FX.bump(els.chips); FX.bump(els.mult);
  const from = S.roundScore;
  S.roundScore += gained;
  FX.countTo(els.roundScore, from, S.roundScore, 650);
  await FX.sleep(700);

  // play 区飞出
  S.played.forEach((card, i) => {
    const el = playEls[card.id];
    el.classList.add('flyout');
    el.style.animationDelay = (i * 40) + 'ms';
  });
  await FX.sleep(450);
  els.play.innerHTML = '';
  S.played = [];

  // 判定
  if (S.roundScore >= S.goal) {
    return winRound();
  }
  if (S.handsLeft <= 0) {
    return loseRound();
  }

  draw(S.handSize - S.hand.length);
  renderHand(true);
  S.busy = false;
  els.handLabel.innerHTML = '&nbsp;';
  setFormula(0, 0);
  updateControls();
}

// 小丑牌效果计算
function applyJoker(j, ctx) {
  let { chips, mult } = ctx;
  const { key, scoringCards, played } = ctx;
  switch (j.hook) {
    case 'flatMult': mult += j.val; break;
    case 'xmult': mult *= j.val; break;
    case 'suitMult':
      for (const c of scoringCards) if (c.suit === j.suit) mult += j.val;
      break;
    case 'parityMult':
      for (const c of scoringCards) {
        const isFace = c.rank > 10 && c.rank < 14;
        if (isFace) continue;
        const v = c.rank === 14 ? 1 : c.rank; // A 当奇
        if (v % 2 === j.parity) mult += j.val;
      }
      break;
    case 'handMult': if (key === j.need) mult += j.val; break;
    case 'handChips': if (key === j.need) chips += j.val; break;
    case 'aceBonus':
      for (const c of scoringCards) if (c.rank === 14) { chips += j.chips; mult += j.mult; }
      break;
    case 'moneyChips': chips += S.money * j.val; break;
  }
  return { chips, mult };
}

// ---------- 弃牌 ----------
async function discard() {
  if (S.busy || S.discardsLeft <= 0 || S.selected.size === 0) return;
  S.busy = true; updateControls();
  S.discardsLeft--;
  refreshStats();

  const ids = [...S.selected];
  ids.forEach((id, i) => {
    const el = els.hand.querySelector(`[data-id="${id}"]`);
    if (el) { el.classList.add('flyout'); el.style.animationDelay = (i * 40) + 'ms'; }
  });
  await FX.sleep(420);

  S.hand = S.hand.filter(c => !S.selected.has(c.id));
  S.selected.clear();
  draw(S.handSize - S.hand.length);
  renderHand(true);
  S.busy = false;
  els.handLabel.innerHTML = '&nbsp;';
  setFormula(0, 0);
  updateControls();
}

// ---------- 回合流转 ----------
function refreshStats() {
  els.statHands.textContent = S.handsLeft;
  els.statDiscards.textContent = S.discardsLeft;
  els.statMoney.textContent = '$' + S.money;
  els.statAnte.textContent = (S.ante + 1) + '/8';
  els.statRound.textContent = S.ante * 3 + S.blindIdx + 1;
  els.statHandSize.textContent = S.handSize;
}

function loadBlind() {
  const blind = B.BLINDS[S.blindIdx];
  S.goal = B.blindGoal(S.ante, S.blindIdx);
  S.roundScore = 0;
  S.handsLeft = S.maxHands;
  S.discardsLeft = S.maxDiscards;
  els.blindName.textContent = blind.name;
  els.blindGoal.textContent = S.goal.toLocaleString();
  els.blindReward.textContent = '奖励 ' + '$'.repeat(blind.reward);
  els.blindToken.classList.remove('beaten');
  els.roundScore.textContent = '0';
  setFormula(0, 0);
  els.handLabel.innerHTML = '&nbsp;';

  // 洗牌发牌
  S.drawPile = B.shuffle(S.deck);
  S.hand = [];
  S.selected.clear();
  draw(S.handSize);
  renderHand(true);
  refreshStats();
  S.busy = false;
  updateControls();
}

function winRound() {
  els.blindToken.classList.add('beaten');
  const blind = B.BLINDS[S.blindIdx];
  const reward = blind.reward + Math.min(5, Math.floor(S.money / 5)) + S.handsLeft; // 利息 + 剩余出牌
  S.money += reward;
  refreshStats();
  showOverlay('回合通过！',
    `目标 ${S.goal.toLocaleString()} 达成，本轮得分 ${S.roundScore.toLocaleString()}。\n` +
    `奖励 $${blind.reward} + 利息 $${Math.min(5, Math.floor((S.money - reward + blind.reward) / 5))} + 剩余出牌 $${S.handsLeft} → 现有 $${S.money}`,
    '前往商店', () => { hideOverlay(); openShop(); });
}

function loseRound() {
  showOverlay('游戏结束',
    `未能在出牌用尽前达到目标分数 ${S.goal.toLocaleString()}（本轮 ${S.roundScore.toLocaleString()}）。\n再来一局？`,
    '重新开始', () => { hideOverlay(); newGame(); });
}

function nextBlind() {
  S.blindIdx++;
  if (S.blindIdx > 2) { S.blindIdx = 0; S.ante++; }
  if (S.ante >= 8) {
    return showOverlay('通关！🎉',
      '你击败了全部 8 个底注，恭喜通关小丑牌！',
      '再玩一次', () => { hideOverlay(); newGame(); });
  }
  loadBlind();
}

// ---------- 商店 ----------
function openShop() {
  els.shop.hidden = false;
  els.shopMoney.textContent = '$' + S.money;
  const picks = B.shuffle(JOKER_POOL).slice(0, 3);
  els.shopItems.innerHTML = '';
  picks.forEach(j => {
    const slot = document.createElement('div');
    slot.className = 'shop-slot';
    const owned = S.jokers.some(x => x.id === j.id);
    slot.innerHTML = `
      <div class="joker" style="animation:none">
        <div class="j-emoji">${j.emoji}</div>
        <div class="j-name">${j.name}</div>
        <div class="tip" style="opacity:1;position:static;transform:none;width:auto;margin-top:6px">${j.desc}</div>
      </div>
      <div class="price">$${j.price}</div>
      <button class="btn btn-play">${owned ? '已拥有' : '购买'}</button>`;
    const btn = slot.querySelector('button');
    if (owned || S.jokers.length >= 5) btn.disabled = true;
    btn.addEventListener('click', () => {
      if (S.money < j.price) { FX.shake(slot); return; }
      if (S.jokers.length >= 5) return;
      S.money -= j.price;
      S.jokers.push(j);
      renderJokers();
      els.shopMoney.textContent = '$' + S.money;
      els.statMoney.textContent = '$' + S.money;
      btn.textContent = '已拥有'; btn.disabled = true;
    });
    els.shopItems.appendChild(slot);
  });
}

// ---------- overlay ----------
function showOverlay(title, text, btn, onClick) {
  els.overlayTitle.textContent = title;
  els.overlayText.textContent = text;
  els.overlayBtn.textContent = btn;
  els.overlayBtn.onclick = onClick;
  els.overlay.hidden = false;
}
function hideOverlay() { els.overlay.hidden = true; }

// ---------- 新游戏 ----------
function newGame() {
  S.deck = B.makeDeck();
  S.jokers = [];
  S.handLevels = B.makeHandLevels();
  S.money = 4;
  S.ante = 0;
  S.blindIdx = 0;
  S.maxHands = 4; S.maxDiscards = 3; S.handSize = 8;
  renderJokers();
  loadBlind();
}

// ---------- 事件绑定 ----------
els.btnPlay.addEventListener('click', playHand);
els.btnDiscard.addEventListener('click', discard);
els.btnSortRank.addEventListener('click', () => { sortHand('rank'); renderHand(); });
els.btnSortSuit.addEventListener('click', () => { sortHand('suit'); renderHand(); });
els.shopNext.addEventListener('click', () => { els.shop.hidden = true; nextBlind(); });
window.addEventListener('resize', () => FX.layoutFan(els.hand));
document.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !els.btnPlay.disabled) playHand();
  if (e.key === 'Backspace' && !els.btnDiscard.disabled) discard();
});

newGame();

// 调试/测试用：暴露状态（不影响正常游玩）
window.__GAME = { S, els, loadBlind, openShop };
