/* ==========================================================================
   MCD·GACHA — 下单确认页交互
   职责边界：只做 fetch + DOM 文本；不引入任何库。
   ========================================================================== */
(function () {
  'use strict';

  var btn = document.getElementById('confirm-btn');
  if (!btn) return;

  var err = document.getElementById('order-err');
  var group = document.getElementById('takeway-group');

  function show(msg) {
    if (!err) return;
    err.textContent = msg;
  }

  function selectedTakeWay() {
    if (!group) return 'take-in-store';
    var hit = group.querySelector('input[name="take_way_code"]:checked');
    return hit ? hit.value : 'take-in-store';
  }

  btn.addEventListener('click', function () {
    if (btn.disabled) return;

    var ids = (btn.getAttribute('data-ids') || '')
      .split(',')
      .map(function (s) { return s.trim(); })
      .filter(Boolean);
    if (!ids.length) {
      show('没有可下单的商品，请回到抽卡台重新选择。');
      return;
    }

    // 真实下单必须二次确认——这是唯一会花真钱的动作
    if (btn.getAttribute('data-real') === '1') {
      var ok = window.confirm(
        '⚠️ 当前为 live + 已授权模式。\n\n' +
        '点击「确定」将向真实门店发起真实订单并产生真实费用，且本 demo 没有接入支付流程。\n\n' +
        '确定要继续吗？'
      );
      if (!ok) return;
    }

    btn.disabled = true;
    var original = btn.textContent;
    btn.textContent = '下单中…';
    show('');

    fetch('/api/order', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: ids, take_way_code: selectedTakeWay() })
    })
      .then(function (r) {
        return r.json().then(function (d) { return { status: r.status, d: d }; });
      })
      .then(function (res) {
        if (res.d && res.d.ok) {
          window.location.href = res.d.redirect || '/orders';
          return;
        }
        show((res.d && res.d.error) || '下单失败，请稍后重试。');
      })
      .catch(function () { show('网络异常，下单失败。'); })
      .finally(function () {
        btn.disabled = false;
        btn.textContent = original;
      });
  });
})();
