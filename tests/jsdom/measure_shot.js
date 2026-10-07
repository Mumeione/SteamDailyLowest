/* 卡片布局实测 + 截图（Edge headless + CDP，零依赖，node 直跑）
 * 用法：node tests/jsdom/measure_shot.js [index.html 路径] [截图输出目录]
 *   省略参数 = 量 output/index.html，截图落在 %TEMP%/s9_shots。
 * 产出：<输出目录>/shot-<宽>.png（首屏 + 展开一行后的样子）
 * ⚠️ 这是**自检与量数**，不是替代用户的 UI 验收。
 * 2026-10-07 起入库（原来在 %TEMP%，换机失传）。
 */
const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");
const os = require("os");

const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const PORT = 9334;
const PAGE = process.argv[2] || path.join(__dirname, "..", "..", "output", "index.html");
const OUT = process.argv[3] || path.join(os.tmpdir(), "s9_shots");
const FILE_URL = "file:///" + PAGE.replace(/\\/g, "/");

const WIDTHS = [[390, 844], [430, 932], [768, 1024], [900, 700], [1100, 800], [1440, 900], [1920, 1080]];

const MEASURE = `(() => {
  const R = (el) => { if (!el) return null; const r = el.getBoundingClientRect();
    return {w: Math.round(r.width), h: Math.round(r.height), t: Math.round(r.top)}; };
  const rowsOf = (els) => { const tops = [];
    els.forEach((e) => { const t = Math.round(e.getBoundingClientRect().top);
      if (!tops.some((x) => Math.abs(x - t) <= 3)) tops.push(t); });
    return tops.length ? tops.sort((a,b)=>a-b).map((t) =>
      els.filter((e) => Math.abs(Math.round(e.getBoundingClientRect().top) - t) <= 3).length) : []; };
  const picks = Array.from(document.querySelectorAll("#picks-track .pick"));
  const rows = Array.from(document.querySelectorAll("#sections .row"));
  const one = rows[0];
  return {
    perPage: picks.length,
    pickRows: rowsOf(picks).join("+"),
    pick: R(picks[0]),
    sections: rowsOf(Array.from(document.querySelectorAll("#sections .section"))).join("+"),
    row: R(one),
    thumb: one ? R(one.querySelector(".row-thumb")) : null,
    bar: one ? R(one.querySelector(".cut-bar")) : null,
    tagsTop: one ? Math.round(one.querySelector(".row-tags").getBoundingClientRect().top - one.getBoundingClientRect().top) : 0,
    priceFont: one ? getComputedStyle(one.querySelector(".row-price .now")).fontSize : "-",
    overflowX: document.documentElement.scrollWidth - window.innerWidth,
    detailCols: (() => { const d = document.querySelector("#sections .row.open .detail-cols");
      return d ? rowsOf(Array.from(d.children)).join("+") : "-"; })(),
  };
})()`;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const child = spawn(EDGE, [
    "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${path.join(os.tmpdir(), "edge-shot-profile")}`,
    "--allow-file-access-from-files", "--hide-scrollbars", "about:blank",
  ], { stdio: "ignore" });

  let target = null;
  for (let i = 0; i < 40 && !target; i++) {
    await sleep(250);
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      target = list.find((t) => t.type === "page");
    } catch (e) { /* 还没起来 */ }
  }
  if (!target) { console.error("连不上 Edge 调试端口"); child.kill(); process.exit(1); }

  const ws = new WebSocket(target.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  const send = (method, params) => new Promise((resolve) => {
    const mid = ++id; pending.set(mid, resolve);
    ws.send(JSON.stringify({ id: mid, method, params: params || {} }));
  });
  ws.addEventListener("message", (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg.result); pending.delete(msg.id); }
  });
  await new Promise((r) => ws.addEventListener("open", r));
  await send("Page.enable");
  await send("Runtime.enable");

  const evalJs = async (expr) => (await send("Runtime.evaluate",
    { expression: expr, returnByValue: true })).result.value;

  await send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url: FILE_URL });
  await sleep(1600);

  // 不带 clip = 截当前**视口**（滚动后 clip 的 y=0 已不在可视区，会截出空白）
  const shot = async (file) => {
    const res = await send("Page.captureScreenshot", { format: "png" });
    if (res && res.data) fs.writeFileSync(file, Buffer.from(res.data, "base64"));
  };

  console.log("视口        每页大卡  大卡排布   大卡尺寸    行卡片(宽×高)  封面   力度条  标签行y  板块  详情栏  溢出");
  for (const [w, h] of WIDTHS) {
    await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile: false });
    await sleep(450);
    let v = await evalJs(MEASURE);
    const line = (v2, tag) =>
      `${String(w).padStart(4)}×${String(h).padEnd(4)} ${String(v2.perPage).padStart(5)}  ` +
      `${v2.pickRows.padEnd(7)} ${String(v2.pick ? v2.pick.w + "×" + v2.pick.h : "-").padEnd(12)}` +
      `${String(v2.row ? v2.row.w + "×" + v2.row.h : "-").padEnd(15)}` +
      `${String(v2.thumb ? v2.thumb.w + "×" + v2.thumb.h : "-").padEnd(9)}` +
      `${String(v2.bar ? v2.bar.w : "-").padStart(5)}px  ` +
      `y=${String(v2.tagsTop).padStart(3)}  ${String(v2.sections).padEnd(6)}` +
      `${String(v2.detailCols).padEnd(7)}` +
      `${v2.overflowX > 0 ? "⚠ " + v2.overflowX + "px" : "无"}${tag || ""}`;
    console.log(line(v));

    // ① 首屏（大卡）② 滚到四板块（看行卡片）③ 展开一行（看详情两栏）
    await evalJs("window.scrollTo(0, 0); 1");   // 上一档滚过页面了，先回顶再截
    await sleep(200);
    await shot(path.join(OUT, `shot-${w}.png`));
    await evalJs('document.getElementById("sections").scrollIntoView({block:"start"}); 1');
    await sleep(250);
    await shot(path.join(OUT, `shot-${w}-rows.png`));
    await evalJs(`(function(){
      // 展开**第一个有真比价数据**的卡片：本地预览只有 cache.json 覆盖到的 appid 有比价行
      // （其余显示一行说明）；截图要展示的是「有数据时」的版式
      var rows = Array.from(document.querySelectorAll("#sections .row"));
      for (var i = 0; i < rows.length; i++) {
        rows[i].querySelector(".row-main").click();
        var opened = rows[i].classList.contains("open");
        if (opened && !rows[i].querySelector(".detail-note")) {
          rows[i].scrollIntoView({block: "center"});
          return i;
        }
        if (opened) rows[i].querySelector(".row-main").click();
      }
      rows[0].querySelector(".row-main").click();
      rows[0].scrollIntoView({block: "center"});
      return -1;
    })()`);
    await sleep(250);
    v = await evalJs(MEASURE);
    await shot(path.join(OUT, `shot-${w}-detail.png`));
    console.log(line(v, "   ← 已展开一行"));
    await evalJs(`(function(){var r=document.querySelector("#sections .row.open"); if(r){r.querySelector(".row-main").click();} return 1;})()`);
    await sleep(150);
  }

  ws.close();
  child.kill();
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
