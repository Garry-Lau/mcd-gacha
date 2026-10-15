/* ==========================================================================
   MCD·GACHA — 交互脚本
   职责边界（严格遵守）：只做 fetch 与 DOM 切换 / 文本填充。
   所有视觉动效（翻卡、粒子、光晕、进度条）全部由 CSS 承担，不引入任何动画库。
   ========================================================================== */
(function () {
  'use strict';

  var MAX_ITEMS = 8;
  var MIN_ITEMS = 2;

  var selected = new Map(); // id -> { name, cal }

  var el = {
    pool: document.getElementById('pool-form'),
    count: document.getElementById('picked-count'),
    cal: document.getElementById('picked-cal'),
    err: document.getElementById('picked-err'),
    draw: document.getElementById('draw-btn'),
    layer: document.getElementById('result-layer'),
    grid: document.getElementById('result-grid'),
    meta: document.getElementById('result-meta'),
    close: document.getElementById('close-result'),
    upBlock: document.getElementById('upgrade-block'),
    upGrid: document.getElementById('upgrade-grid'),
    tpl: document.getElementById('tpl-card'),
    pill: document.getElementById('mode-pill')
  };

  if (!el.pool || !el.draw) return;

  // ---------------------------------------------------------------- 卡池选择
  el.pool.addEventListener('click', function (ev) {
    var card = ev.target.closest('.pool-card');
    if (!card) return;
    var id = card.getAttribute('data-id');
    var on = card.getAttribute('aria-pressed') === 'true';

    if (!on && selected.size >= MAX_ITEMS) {
      flash('最多勾选 ' + MAX_ITEMS + ' 件');
      return;
    }
    if (on) {
      selected.delete(id);
      card.setAttribute('aria-pressed', 'false');
    } else {
      selected.set(id, {
        name: card.getAttribute('data-name'),
        cal: parseInt(card.getAttribute('data-cal') || '0', 10)
      });
      card.setAttribute('aria-pressed', 'true');
    }
    syncBar();
  });

  function flash(msg) {
    if (!el.err) return;
    el.err.textContent = msg;
    window.clearTimeout(flash._t);
    flash._t = window.setTimeout(function () { el.err.textContent = ''; }, 2600);
  }

  function syncBar() {
    var n = selected.size;
    var cal = 0;
    selected.forEach(function (v) { cal += v.cal; });
    el.count.textContent = String(n);
    el.cal.textContent = cal + ' kcal';
    el.draw.disabled = n < MIN_ITEMS || n > MAX_ITEMS;
    if (n > 0 && n < MIN_ITEMS) {
      el.err.textContent = '再选 ' + (MIN_ITEMS - n) + ' 件即可抽卡';
    } else if (el.err.textContent.indexOf('再选') === 0) {
      el.err.textContent = '';
    }
  }

  // ------------------------------------------------------------------ 抽卡
  el.draw.addEventListener('click', function () {
    if (el.draw.disabled) return;
    el.draw.disabled = true;
    el.draw.classList.add('is-loading');

    fetch('/api/recommend', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: Array.from(selected.keys()), limit: 5 })
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (!res.ok || !res.d.ok) {
          flash((res.d && res.d.error) || '抽卡失败');
          return;
        }
        render(res.d);
      })
      .catch(function () { flash('网络异常，抽卡失败'); })
      .finally(function () {
        el.draw.classList.remove('is-loading');
        syncBar();
      });
  });

  // ------------------------------------------------------------ 渲染结果
  function render(data) {
    el.grid.innerHTML = '';
    el.upGrid.innerHTML = '';
    el.upBlock.hidden = true;

    var results = data.results || [];
    var upgrades = data.upgrades || [];

    el.meta.textContent =
      '单餐预算 ' + Math.round(data.target) + ' kcal · 勾选 ' + data.selected_count +
      ' 件 · 契合度 ≥ 50 的组合 ' + results.length + ' 个' +
      (upgrades.length ? ' · 补齐建议 ' + upgrades.length + ' 条' : '');

    if (!results.length) {
      var note = document.createElement('p');
      note.className = 'empty-note';
      note.textContent = '没有组合达到 50 分。试试多选几件，或让总热量更接近单餐预算 ' +
        Math.round(data.target) + ' kcal。';
      el.grid.appendChild(note);
    }

    results.forEach(function (c, i) { el.grid.appendChild(buildCard(c, i, false)); });

    if (upgrades.length) {
      el.upBlock.hidden = false;
      upgrades.forEach(function (c, i) {
        el.upGrid.appendChild(buildCard(c, results.length + i, true));
      });
    }

    el.layer.hidden = false;
    document.body.style.overflow = 'hidden';
    el.close.focus();

    // 依次翻开：每张延迟 260ms（动画本体在 CSS 里）
    var cards = el.layer.querySelectorAll('.result-card');
    Array.prototype.forEach.call(cards, function (card, i) {
      window.setTimeout(function () {
        card.classList.add('is-flipped');
      }, 200 + i * 260);
    });
  }

  // -------------------------------------------------- 结果卡 → 下单确认页
  function openOrder(card) {
    // 翻牌动画进行中不响应，否则容易误触
    if (!card || !card.classList.contains('is-flipped')) return;
    var ids = card.getAttribute('data-ids');
    if (!ids) return;
    var sm = card.getAttribute('data-sm') || '';
    var url = '/order/new?ids=' + encodeURIComponent(ids) +
      (sm ? '&sm=' + encodeURIComponent(sm) : '');
    window.location.href = url;
  }

  function bindCardActivation(container) {
    if (!container) return;
    container.addEventListener('click', function (ev) {
      var card = ev.target.closest && ev.target.closest('.result-card');
      if (card) openOrder(card);
    });
    // 键盘可访问：Enter / Space 等同于点击（卡片带 role=button + tabindex）
    container.addEventListener('keydown', function (ev) {
      if (ev.key !== 'Enter' && ev.key !== ' ') return;
      var card = ev.target.closest && ev.target.closest('.result-card');
      if (!card) return;
      ev.preventDefault();
      openOrder(card);
    });
  }

  function buildCard(c, index, isUpgrade) {
    var node = el.tpl.content.firstElementChild.cloneNode(true);
    node.setAttribute('data-rarity', c.rarity);
    node.style.animationDelay = (index * 70) + 'ms';

    // 下单所需的最小信息：商品 id 列表（+ 命中的套餐 id 便于后端核对）
    node.setAttribute('data-ids', (c.item_ids || []).join(','));
    node.setAttribute('data-sm', c.setmeal_id || '');
    node.setAttribute('role', 'button');
    node.setAttribute('tabindex', '0');
    node.setAttribute(
      'aria-label',
      '下单：' + (c.skill_name || c.name) + '，' + (c.item_names || []).join('、') +
      '，应付 ¥' + c.combo_price + '，按回车进入下单确认页'
    );

    node.querySelector('.rarity-badge').textContent = c.rarity_label || c.rarity;
    node.querySelector('.card-skill').textContent = c.skill_name || c.name;
    node.querySelector('.card-who').textContent = (c.item_names || []).join(' + ');

    // 单品清单（"原 N 张卡"的价格）
    var ul = node.querySelector('.card-items');
    (c.items || []).forEach(function (it) {
      var li = document.createElement('li');
      var nm = document.createElement('span');
      nm.textContent = it.name;
      var pr = document.createElement('b');
      pr.textContent = '¥' + it.price;
      li.appendChild(nm);
      li.appendChild(pr);
      ul.appendChild(li);
    });

    // 指标
    var stats = node.querySelector('.card-stats');
    [
      ['总热量', Math.round(c.total_calories) + ' kcal'],
      ['蛋白', c.total_protein + ' g'],
      ['脂肪', c.total_fat + ' g'],
      ['碳水', c.total_carb + ' g']
    ].forEach(function (pair) {
      var box = document.createElement('div');
      var dt = document.createElement('dt');
      dt.textContent = pair[0];
      var dd = document.createElement('dd');
      dd.textContent = pair[1];
      box.appendChild(dt);
      box.appendChild(dd);
      stats.appendChild(box);
    });

    // 套餐匹配度
    var smLine = node.querySelector('.card-setmeal');
    if (c.setmeal_name) {
      var pct = c.setmeal_ratio_pct;
      smLine.textContent = '套餐匹配度 ' + pct.toFixed(1) + '% · ' + c.setmeal_name +
        (c.missing_items && c.missing_items.length ? '（还差 ' + c.missing_items.join('、') + '）' : '（已凑满）');
      var bar = document.createElement('span');
      bar.className = 'bar';
      var fill = document.createElement('i');
      fill.style.width = pct.toFixed(1) + '%';
      bar.appendChild(fill);
      smLine.appendChild(bar);
    } else {
      smLine.remove();
    }

    // 推荐理由
    node.querySelector('.card-reason').textContent = c.reason || '';

    // 价格对比：原价 vs 组合技价
    var price = node.querySelector('.card-price');
    if (parseFloat(c.saved) > 0) {
      var was = document.createElement('span');
      was.className = 'was';
      was.textContent = '原价 ¥' + c.original_price;
      var now = document.createElement('span');
      now.className = 'now';
      now.textContent = '¥' + c.combo_price;
      var off = document.createElement('span');
      off.className = 'off';
      off.textContent = '省 ¥' + c.saved + '（' + Math.round(c.saved_pct * 100) + '%）';
      price.appendChild(was);
      price.appendChild(now);
      price.appendChild(off);
    } else {
      var plain = document.createElement('span');
      plain.className = 'plain';
      plain.textContent = '¥' + c.original_price;
      price.appendChild(plain);
    }
    if (isUpgrade && c.upgrade_note) {
      var un = document.createElement('p');
      un.className = 'card-upgrade-note';
      un.textContent = '补齐建议：' + c.upgrade_note;
      price.parentNode.insertBefore(un, price);
    }

    // SSR / SR / R 粒子占位（动画由 CSS 的 nth-child 控制）
    if (c.rarity === 'SSR' || c.rarity === 'SR' || c.rarity === 'R') {
      var sparks = node.querySelector('.sparks');
      for (var i = 0; i < 12; i++) sparks.appendChild(document.createElement('i'));
    }

    return node;
  }

  // ---------------------------------------------------------------- 关闭
  function closeLayer() {
    el.layer.hidden = true;
    document.body.style.overflow = '';
  }
  el.close.addEventListener('click', closeLayer);
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape' && !el.layer.hidden) closeLayer();
  });

  // ------------------------------------------------- /health 反映运行模式
  bindCardActivation(el.grid);
  bindCardActivation(el.upGrid);

  fetch('/health')
    .then(function (r) { return r.json(); })
    .then(function (h) {
      if (el.pill && h && h.mcp === 'connected') {
        el.pill.textContent = 'MCP 已连接';
        el.pill.classList.remove('pill-demo');
      }
    })
    .catch(function () { /* /health 不可用时保持"演示数据"角标 */ });

  syncBar();
})();
