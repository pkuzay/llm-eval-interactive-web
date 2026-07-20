/* ============================================================
   animations.js — Balatro 风格动效辅助
   核心还原点：
   1. 手牌整体呈弧形展开 + 每张牌轻微浮动/摆动
   2. 计分时逐张弹跳，弹出 +筹码 / ×倍率 飘字
   3. 数字滚动累加（chips / mult / round score）
   ============================================================ */

const FX = {
  sleep: (ms) => new Promise(r => setTimeout(r, ms)),

  // 手牌弧形布局：中间高、两侧低、带旋转
  layoutFan(handEl) {
    const cards = [...handEl.querySelectorAll('.card')];
    const n = cards.length;
    if (n === 0) return;
    const mid = (n - 1) / 2;
    const maxRot = Math.min(4, 18 / n);   // 每张的角度步进
    const arc = Math.min(6, 40 / n);      // 中间下沉幅度
    cards.forEach((c, i) => {
      const off = i - mid;
      const rot = off * maxRot;
      const lift = -Math.pow(off, 2) * arc * 0.12; // 弧线
      c.style.setProperty('--rot', rot.toFixed(2) + 'deg');
      c.dataset.baseLift = lift.toFixed(1);
      if (!c.classList.contains('selected')) {
        c.style.setProperty('--lift', lift.toFixed(1) + 'px');
      }
    });
  },

  // 数字滚动累加动画
  countTo(el, from, to, ms = 500) {
    const start = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - start) / ms);
      const eased = 1 - Math.pow(1 - t, 3);
      const val = Math.round(from + (to - from) * eased);
      el.textContent = val.toLocaleString();
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  },

  bump(el) {
    el.classList.remove('bump');
    void el.offsetWidth;
    el.classList.add('bump');
  },

  // 从某张牌弹出飘字（+chips 或 xmult）
  popText(cardEl, text, type) {
    const fx = document.createElement('div');
    fx.className = type === 'chip' ? 'chip-fx' : 'mult-fx';
    fx.textContent = text;
    cardEl.appendChild(fx);
    setTimeout(() => fx.remove(), 750);
  },

  scoreBounce(cardEl) {
    cardEl.classList.remove('scoring');
    void cardEl.offsetWidth;
    cardEl.classList.add('scoring');
  },

  jokerTrigger(jokerEl) {
    jokerEl.classList.remove('trigger');
    void jokerEl.offsetWidth;
    jokerEl.classList.add('trigger');
  },

  shake(el) {
    el.classList.remove('shake');
    void el.offsetWidth;
    el.classList.add('shake');
  },
};

window.FX = FX;
