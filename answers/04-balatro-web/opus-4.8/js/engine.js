/* ============================================================
   engine.js — 小丑牌核心规则引擎（自研实现，纯逻辑）
   - 扑克牌型识别
   - Balatro 风格 筹码(chips) × 倍率(mult) 计分
   ============================================================ */

const SUITS = [
  { key: 'S', name: '黑桃', symbol: '♠', color: 'black' },
  { key: 'H', name: '红桃', symbol: '♥', color: 'red' },
  { key: 'D', name: '方块', symbol: '♦', color: 'red' },
  { key: 'C', name: '梅花', symbol: '♣', color: 'black' },
];

// rank: 内部数值 2..14 (A=14)。展示与筹码另算。
const RANKS = [
  { r: 2, label: '2', chips: 2 },
  { r: 3, label: '3', chips: 3 },
  { r: 4, label: '4', chips: 4 },
  { r: 5, label: '5', chips: 5 },
  { r: 6, label: '6', chips: 6 },
  { r: 7, label: '7', chips: 7 },
  { r: 8, label: '8', chips: 8 },
  { r: 9, label: '9', chips: 9 },
  { r: 10, label: '10', chips: 10 },
  { r: 11, label: 'J', chips: 10 },
  { r: 12, label: 'Q', chips: 10 },
  { r: 13, label: 'K', chips: 10 },
  { r: 14, label: 'A', chips: 11 },
];

// 牌型基础分值（Level 1），沿用原作数值设计
const HAND_TABLE = {
  'FLUSH_FIVE':    { name: '同花五条', chips: 160, mult: 16 },
  'FLUSH_HOUSE':   { name: '同花葫芦', chips: 140, mult: 14 },
  'FIVE_KIND':     { name: '五条',     chips: 120, mult: 12 },
  'STRAIGHT_FLUSH':{ name: '同花顺',   chips: 100, mult: 8  },
  'FOUR_KIND':     { name: '四条',     chips: 60,  mult: 7  },
  'FULL_HOUSE':    { name: '葫芦',     chips: 40,  mult: 4  },
  'FLUSH':         { name: '同花',     chips: 35,  mult: 4  },
  'STRAIGHT':      { name: '顺子',     chips: 30,  mult: 4  },
  'THREE_KIND':    { name: '三条',     chips: 30,  mult: 3  },
  'TWO_PAIR':      { name: '两对',     chips: 20,  mult: 2  },
  'PAIR':          { name: '对子',     chips: 10,  mult: 2  },
  'HIGH_CARD':     { name: '高牌',     chips: 5,   mult: 1  },
};

function makeDeck() {
  const deck = [];
  let id = 0;
  for (const s of SUITS) {
    for (const rk of RANKS) {
      deck.push({
        id: id++,
        suit: s.key, suitName: s.name, symbol: s.symbol, color: s.color,
        rank: rk.r, label: rk.label, chips: rk.chips,
      });
    }
  }
  return deck;
}

function shuffle(arr) {
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

// 统计点数出现次数
function rankCounts(cards) {
  const m = new Map();
  for (const c of cards) m.set(c.rank, (m.get(c.rank) || 0) + 1);
  return m;
}

function isFlush(cards) {
  if (cards.length < 5) return false;
  return cards.every(c => c.suit === cards[0].suit);
}

// 顺子判断，支持 A-2-3-4-5 与 10-J-Q-K-A
function isStraight(cards) {
  if (cards.length < 5) return false;
  const rs = [...new Set(cards.map(c => c.rank))].sort((a, b) => a - b);
  if (rs.length !== 5) return false;
  if (rs[4] - rs[0] === 4) return true;
  // wheel: A,2,3,4,5
  const wheel = [2, 3, 4, 5, 14];
  return wheel.every((v, i) => rs[i] === v);
}

/**
 * 识别打出的牌（1-5 张）构成的最佳牌型。
 * 返回 { key, name, scoringCards }
 * scoringCards = 真正参与计分的牌（原作里只有构成牌型的牌加筹码）
 */
function evaluateHand(cards) {
  const counts = rankCounts(cards);
  const countVals = [...counts.values()].sort((a, b) => b - a);
  const flush = isFlush(cards);
  const straight = isStraight(cards);
  const n = cards.length;

  const byRankDesc = [...cards].sort((a, b) => b.rank - a.rank);
  const cardsOfCount = (cnt) => {
    const ranks = [...counts.entries()].filter(([, v]) => v === cnt).map(([r]) => r);
    return cards.filter(c => ranks.includes(c.rank));
  };

  let key;
  let scoringCards;

  if (n === 5 && countVals[0] === 5 && flush) { key = 'FLUSH_FIVE'; scoringCards = cards; }
  else if (n === 5 && countVals[0] === 3 && countVals[1] === 2 && flush) { key = 'FLUSH_HOUSE'; scoringCards = cards; }
  else if (countVals[0] === 5) { key = 'FIVE_KIND'; scoringCards = cardsOfCount(5); }
  else if (n === 5 && straight && flush) { key = 'STRAIGHT_FLUSH'; scoringCards = cards; }
  else if (countVals[0] === 4) { key = 'FOUR_KIND'; scoringCards = cardsOfCount(4); }
  else if (countVals[0] === 3 && countVals[1] === 2) { key = 'FULL_HOUSE'; scoringCards = cards; }
  else if (flush) { key = 'FLUSH'; scoringCards = cards; }
  else if (straight) { key = 'STRAIGHT'; scoringCards = cards; }
  else if (countVals[0] === 3) { key = 'THREE_KIND'; scoringCards = cardsOfCount(3); }
  else if (countVals[0] === 2 && countVals[1] === 2) { key = 'TWO_PAIR'; scoringCards = cardsOfCount(2); }
  else if (countVals[0] === 2) { key = 'PAIR'; scoringCards = cardsOfCount(2); }
  else { key = 'HIGH_CARD'; scoringCards = [byRankDesc[0]]; }

  return { key, name: HAND_TABLE[key].name, scoringCards };
}

// 手牌等级（可被行星牌升级），保存每种牌型的 level
function makeHandLevels() {
  const lv = {};
  for (const k of Object.keys(HAND_TABLE)) lv[k] = 1;
  return lv;
}

// 每升 1 级增量（简化版，近似原作幅度）
const LEVEL_GAIN = {
  'FLUSH_FIVE':    { chips: 50, mult: 3 },
  'FLUSH_HOUSE':   { chips: 40, mult: 4 },
  'FIVE_KIND':     { chips: 35, mult: 3 },
  'STRAIGHT_FLUSH':{ chips: 40, mult: 4 },
  'FOUR_KIND':     { chips: 30, mult: 3 },
  'FULL_HOUSE':    { chips: 25, mult: 2 },
  'FLUSH':         { chips: 15, mult: 2 },
  'STRAIGHT':      { chips: 30, mult: 3 },
  'THREE_KIND':    { chips: 20, mult: 2 },
  'TWO_PAIR':      { chips: 20, mult: 1 },
  'PAIR':          { chips: 15, mult: 1 },
  'HIGH_CARD':     { chips: 10, mult: 1 },
};

function baseHandScore(key, level) {
  const b = HAND_TABLE[key];
  const g = LEVEL_GAIN[key];
  const lv = level - 1;
  return { chips: b.chips + g.chips * lv, mult: b.mult + g.mult * lv };
}

// 盲注目标分数：基于底注(ante)增长曲线
const BLINDS = [
  { key: 'small', name: '小盲注', mult: 1,   reward: 3 },
  { key: 'big',   name: '大盲注', mult: 1.5, reward: 4 },
  { key: 'boss',  name: 'Boss盲注', mult: 2, reward: 5 },
];

const ANTE_BASE = [300, 800, 2000, 5000, 11000, 20000, 35000, 50000];

function blindGoal(anteIndex, blindIndex) {
  const base = ANTE_BASE[Math.min(anteIndex, ANTE_BASE.length - 1)];
  return Math.round(base * BLINDS[blindIndex].mult);
}

window.BALATRO = {
  SUITS, RANKS, HAND_TABLE, LEVEL_GAIN, BLINDS, ANTE_BASE,
  makeDeck, shuffle, evaluateHand, makeHandLevels, baseHandScore, blindGoal,
};
