// 量真实布局：用 Edge headless 打开预览，量每个 .stat-box 的 boundingRect，
// 按 top 值分行，输出每行几个、是否铺满。这是「不用猜」的验证方式。
const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PREVIEW = path.resolve('output/preview_single.html');
const WIDTHS = [360, 390, 430, 540, 768, 820, 1024, 1440];
const OUT = path.resolve('data/probe/layout_measure.json');

// 断点取**渲染出的 payload**（config.json → report.py → page_size.breakpoint），
// 不在这里再写死 768 —— 否则「布局按一套、量尺按一套」会各说各话（fix-round1 步骤 5.2）
const DATA_JS = path.resolve('output/data.js');
let BREAKPOINT = 768;
if (fs.existsSync(DATA_JS)) {
  const m = /"breakpoint"\s*:\s*(\d+)/.exec(fs.readFileSync(DATA_JS, 'utf-8'));
  if (m) BREAKPOINT = Number(m[1]);
} else {
  console.warn('未找到 output/data.js，断点回落 768 —— 先跑 tools/render_report.py');
}
console.log('断点 =', BREAKPOINT, '（读自 output/data.js，不是写死的）');

// 起一个极简静态服务器（file:// 下 headless 截图/脚本注入有限制，用 http 更稳）
const server = http.createServer((req, res) => {
  const p = decodeURIComponent(req.url.split('?')[0]);
  const f = p === '/' ? PREVIEW : path.resolve('output' + p);
  fs.readFile(f, (err, buf) => {
    if (err) { res.writeHead(404); res.end('nope'); return; }
    const ext = path.extname(f);
    const type = ext === '.html' ? 'text/html' : ext === '.css' ? 'text/css' : 'application/javascript';
    res.writeHead(200, { 'Content-Type': type + '; charset=utf-8' });
    res.end(buf);
  });
});

const MEASURE_JS = `
(() => {
  const boxes = [...document.querySelectorAll('.overview > .stat-box')];
  const ov = document.querySelector('.overview');
  const rows = {};
  boxes.forEach((b, i) => {
    const r = b.getBoundingClientRect();
    const top = Math.round(r.top);
    (rows[top] = rows[top] || []).push({
      i: i + 1,
      label: (b.querySelector('.stat-label') || {}).textContent || '',
      x: Math.round(r.left), w: Math.round(r.width)
    });
  });
  const ovr = ov.getBoundingClientRect();
  return JSON.stringify({
    viewport: window.innerWidth,
    columns: getComputedStyle(ov).gridTemplateColumns,
    count: boxes.length,
    overview: { x: Math.round(ovr.left), w: Math.round(ovr.width) },
    rows: Object.keys(rows).sort((a, b) => a - b).map(t => ({ top: +t, boxes: rows[t] }))
  });
})()
`;

// 浏览器级 WS 不暴露 Runtime/Page 域 —— 要先查 /json/list 拿**页面 target** 的 ws 地址
function fetchTargets(port) {
  return new Promise((resolve, reject) => {
    http.get({ host: '127.0.0.1', port, path: '/json/list' }, res => {
      let buf = '';
      res.on('data', d => buf += d);
      res.on('end', () => {
        try { resolve(JSON.parse(buf)); } catch (e) { reject(e); }
      });
    }).on('error', reject);
  });
}

function cdp(wsUrl, width) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    let id = 0;
    const send = (method, params) => new Promise(res => {
      const myId = ++id;
      const h = ev => {
        const m = JSON.parse(ev.data);
        if (m.id === myId) { ws.removeEventListener('message', h); res(m); }
      };
      ws.addEventListener('message', h);
      ws.send(JSON.stringify({ id: myId, method, params }));
    });
    ws.addEventListener('open', async () => {
      try {
        await send('Page.enable');
        await send('Runtime.enable');
        await send('Emulation.setDeviceMetricsOverride', {
          width, height: 900, deviceScaleFactor: 1, mobile: width <= BREAKPOINT
        });
        await send('Page.navigate', { url: 'http://127.0.0.1:' + PORT + '/' });
        await new Promise(r => setTimeout(r, 1200));
        const res = await send('Runtime.evaluate', { expression: MEASURE_JS, returnByValue: true });
        ws.close();
        if (!res.result || !res.result.result) {
          reject(new Error('evaluate failed: ' + JSON.stringify(res).slice(0, 300)));
          return;
        }
        const val = res.result.result.value;
        if (typeof val !== 'string') {
          reject(new Error('unexpected value: ' + JSON.stringify(val).slice(0, 200)));
          return;
        }
        resolve(JSON.parse(val));
      } catch (e) { try { ws.close(); } catch (_) {} reject(e); }
    });
    ws.addEventListener('error', reject);
  });
}

let PORT = 0;
server.listen(0, '127.0.0.1', async () => {
  PORT = server.address().port;
  const results = [];
  for (const w of WIDTHS) {
    const userDir = fs.mkdtempSync(path.join(require('os').tmpdir(), 'edge-'));
    const dbgPort = 9200 + WIDTHS.indexOf(w);
    const child = spawn(EDGE, [
      '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
      '--remote-debugging-port=' + dbgPort, '--user-data-dir=' + userDir,
      '--window-size=' + w + ',900', 'about:blank'
    ], { stdio: ['ignore', 'pipe', 'pipe'] });
    // 轮询 /json/list 拿页面 target
    let targets = null;
    for (let i = 0; i < 40; i++) {
      await new Promise(r => setTimeout(r, 250));
      try {
        const list = await fetchTargets(dbgPort);
        const page = list.find(t => t.type === 'page');
        if (page && page.webSocketDebuggerUrl) { targets = page; break; }
      } catch (_) { /* 还没起来 */ }
    }
    if (!targets) { results.push({ viewport: w, error: 'no page target' }); child.kill(); continue; }
    try {
      const r = await cdp(targets.webSocketDebuggerUrl, w);
      results.push(r);
    } catch (e) {
      results.push({ viewport: w, error: String(e && e.message || e) });
    }
    child.kill();
  }
  server.close();
  fs.writeFileSync(OUT, JSON.stringify(results, null, 2), 'utf-8');
  console.log('written', OUT);
  process.exit(0);
});
