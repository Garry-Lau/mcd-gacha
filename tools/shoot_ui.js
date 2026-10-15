/* 用 Chrome DevTools Protocol 驱动**真实浏览器**给 UI 拍照 + 做响应式体检。
 *
 * 为什么不用现成的浏览器自动化库：本项目要求「离线可跑、零额外依赖」，
 * 而 Node 22 自带 WebSocket，Chrome 本机就有 —— 于是用原生 CDP 手搓，
 * 不引入 puppeteer / playwright。
 *
 * 用法（先把应用跑起来）：
 *     python app.py --demo --no-reload
 *     node tools/shoot_ui.js --base http://127.0.0.1:5000 --out docs
 *
 * 产出：
 *     docs/shot-index.png    首页
 *     docs/shot-gacha.png    抽卡台（含卡池 / 套餐 / 券）
 *     docs/shot-result.png   抽卡结果（真实交互：勾巨无霸 + 可乐 → 抽卡）
 *     docs/shot-mobile.png   移动端 390px
 * 并在最后打印 390 / 768 / 1440 三个视口的横向溢出检测结果。
 */
const { spawn } = require('child_process');
const os = require('os');
const fs = require('fs');
const path = require('path');
const http = require('http');

// ---------------------------------------------------------------- CLI 参数
function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}
const BASE = arg('base', process.env.MCD_URL || 'http://127.0.0.1:5000').replace(/\/$/, '');
const OUT_DIR = path.resolve(arg('out', 'docs'));
const CHROME =
  arg('chrome', process.env.CHROME_PATH) ||
  [
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/usr/bin/google-chrome',
  ].find((p, _, all) => { void all; return fs.existsSync(p); });
const PORT = 9223;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ------------------------------------------------------------ CDP 最小封装
function getJSON(url) {
  return new Promise((resolve, reject) => {
    http
      .get(url, (res) => {
        let s = '';
        res.on('data', (c) => (s += c));
        res.on('end', () => {
          try { resolve(JSON.parse(s)); } catch (e) { reject(e); }
        });
      })
      .on('error', reject);
  });
}

async function connect() {
  if (!CHROME) throw new Error('找不到 Chrome，请用 --chrome 指定可执行文件路径');

  const chrome = spawn(
    CHROME,
    [
      '--headless=new',
      '--disable-gpu',
      '--hide-scrollbars',
      '--no-first-run',
      `--remote-debugging-port=${PORT}`,
      `--user-data-dir=${path.join(os.tmpdir(), `mcd-cdp-${Date.now()}`)}`,
      'about:blank',
    ],
    { stdio: 'ignore' }
  );

  let targets = null;
  for (let i = 0; i < 40 && !targets; i++) {
    await sleep(250);
    try { targets = await getJSON(`http://127.0.0.1:${PORT}/json/list`); } catch (_) { /* retry */ }
  }
  if (!targets) throw new Error('CDP 端口未就绪（Chrome 启动失败？）');

  const page = targets.find((t) => t.type === 'page');
  if (!page) throw new Error('找不到 page target');

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.onopen = res;
    ws.onerror = () => rej(new Error('WebSocket 连接失败'));
  });

  let id = 0;
  const pending = new Map();
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (!msg.id || !pending.has(msg.id)) return;
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
  };

  const send = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const mid = ++id;
      pending.set(mid, { resolve, reject });
      ws.send(JSON.stringify({ id: mid, method, params }));
    });

  const evaluate = async (expression) => {
    const r = await send('Runtime.evaluate', {
      expression,
      returnByValue: true,
      awaitPromise: true,
    });
    if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
    return r.result && r.result.value;
  };

  await send('Page.enable');
  await send('Runtime.enable');

  return {
    send,
    evaluate,
    async viewport(w, h, mobile = false) {
      await send('Emulation.setDeviceMetricsOverride', {
        width: w,
        height: h,
        deviceScaleFactor: 1,
        mobile,
      });
    },
    async goto(url, settle = 2500) {
      await send('Page.navigate', { url });
      await sleep(settle);
    },
    async shoot(file) {
      const shot = await send('Page.captureScreenshot', { format: 'png' });
      fs.mkdirSync(OUT_DIR, { recursive: true });
      const abs = path.join(OUT_DIR, file);
      fs.writeFileSync(abs, Buffer.from(shot.data, 'base64'));
      const kb = (fs.statSync(abs).size / 1024).toFixed(0);
      console.log(`  ✅ ${file}  ${kb} KB`);
      return abs;
    },
    async close() {
      ws.close();
      chrome.kill();
      await sleep(400);
    },
  };
}

// ---------------------------------------------------------- 响应式溢出体检
async function overflowReport(conn) {
  console.log('\n响应式体检（横向溢出应全为 0）：');
  for (const [w, h] of [[390, 1800], [768, 1800], [1440, 1600]]) {
    await conn.viewport(w, h, w < 700);
    await conn.goto(`${BASE}/gacha`, 1800);
    const r = await conn.evaluate(`(() => {
      const de = document.documentElement;
      let widest = null, max = de.clientWidth;
      document.querySelectorAll('*').forEach((el) => {
        const b = el.getBoundingClientRect();
        if (b.right > max + 1) { max = b.right; widest = el.className || el.tagName; }
      });
      return {
        overflow: Math.max(0, Math.round(de.scrollWidth - de.clientWidth)),
        widest: widest ? String(widest).slice(0, 40) : '-',
      };
    })()`);
    const flag = r.overflow === 0 ? '✅' : '❌';
    console.log(`  ${flag} ${w}px → 溢出 ${r.overflow}px${r.overflow ? `（最宽元素 ${r.widest}）` : ''}`);
  }
}

// ------------------------------------------------------------------- 主流程
async function main() {
  console.log(`目标：${BASE}   输出：${OUT_DIR}`);
  const conn = await connect();
  try {
    // 1) 首页
    console.log('\n静态页截图：');
    await conn.viewport(1440, 1400);
    await conn.goto(`${BASE}/`, 2500);
    await conn.shoot('shot-index.png');

    // 2) 抽卡台
    await conn.viewport(1440, 2400);
    await conn.goto(`${BASE}/gacha`, 2500);
    await conn.shoot('shot-gacha.png');

    // 3) 移动端
    await conn.viewport(390, 1800, true);
    await conn.goto(`${BASE}/gacha`, 2200);
    await conn.shoot('shot-mobile.png');

    // 4) 结果层 —— 需要真实交互
    console.log('\n驱动真实交互（勾选巨无霸 + 可乐 → 抽卡）：');
    await conn.viewport(1440, 1700);
    await conn.goto(`${BASE}/gacha`, 2500);

    const pick = await conn.evaluate(`(() => {
      const picked = [];
      ['1100', '9900008751'].forEach((id) => {
        const card = document.querySelector('.pool-card[data-id="' + id + '"]');
        if (card) { card.click(); picked.push(card.getAttribute('data-name')); }
      });
      return {
        picked,
        count: document.getElementById('picked-count').textContent,
        disabled: document.getElementById('draw-btn').disabled,
      };
    })()`);
    console.log(`  勾选 ${JSON.stringify(pick.picked)}  已选 ${pick.count}`);
    if (pick.disabled) throw new Error('抽卡按钮仍禁用，无法继续');

    await conn.evaluate(`document.getElementById('draw-btn').click()`);
    await sleep(4500); // 依次翻卡（200 + i×260ms）+ 稀有度光效

    const meta = await conn.evaluate(`document.getElementById('result-meta').textContent`);
    const rarities = await conn.evaluate(
      `Array.from(document.querySelectorAll('.result-card')).map((c) => c.getAttribute('data-rarity'))`
    );
    console.log(`  结果：${meta.trim()}  →  ${JSON.stringify(rarities)}`);
    await conn.shoot('shot-result.png');

    // 5) 点击结果卡 → 下单确认页
    console.log('\n点击结果卡进入下单确认页：');
    await conn.evaluate(`document.querySelector('.result-card.is-flipped').click()`);
    await sleep(2400);
    const orderUrl = await conn.evaluate('location.pathname + location.search');
    console.log(`  跳转到 ${orderUrl}`);
    if (!/^\/order\/new/.test(orderUrl)) throw new Error('结果卡没能跳到下单确认页');
    await conn.viewport(1440, 1500);
    await sleep(500);
    await conn.shoot('shot-order-confirm.png');

    // 6) 确认下单 → 订单列表
    console.log('\n提交订单：');
    await conn.evaluate(`document.getElementById('confirm-btn').click()`);
    await sleep(2600);
    const listUrl = await conn.evaluate('location.pathname');
    const cards = await conn.evaluate(`document.querySelectorAll('.order-item').length`);
    console.log(`  跳转到 ${listUrl} | 订单卡片 ${cards} 张`);
    if (listUrl !== '/orders') throw new Error('下单后没有跳到订单列表');
    await conn.shoot('shot-orders.png');

    await overflowReport(conn);
  } finally {
    await conn.close();
  }
  console.log('\n完成。');
}

main()
  .then(() => process.exit(0))
  .catch((e) => {
    console.error('\n失败：', e.message);
    process.exit(1);
  });
