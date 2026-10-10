/* 跨语言判据对拍（code-audit-2026-10-09 #7）：Python section_agg ↔ 前端 cardPredicate。
 * ================================================================
 * 目的：列表页有两套「等价」判据 —— 服务端预聚合计数表（src/report.py::section_agg，
 * 写进第 0 片 agg.counts）与前端 app.js 的 cardPredicate（板块归属 + liveOk +
 * 条件 dateOk + filterOk）。两者以前只对拍了「键序 / 字面量」，判据逻辑本身没有
 * 跨语言等价性测试。本脚本把 app.js 的**真判据**跑起来，交给 Python 侧逐组合比对。
 *
 * 用法：node tests/jsdom/parity_filters.js <fixture.json>
 *   fixture = {
 *     payload: {...},   // 塞进 window.REPORT_DATA（含 sections[].date_window）
 *     jobs: [{
 *       section,        // 板块 key（含 "__all__"）
 *       filters: {date, cut, reviews, only_new},
 *       cards: [...]    // **该板块自己的成员集** —— 与生产分片一致（JS 读一片就只看到
 *                       // 其成员）。每个 job 自带，故 "__all__"（池 = pool_items，
 *                       // 不是四板块的并集）也能忠实对拍。
 *     }, ...]
 *   }
 * 结果：与 jobs 同序的计数数组，**只以 JSON 打到 stdout**；任何日志走 stderr。
 *
 * jsdom 不在仓库依赖里：解析顺序 = SDL_NODE_MODULES / NODE_PATH / 全局 npm 目录 /
 * 仓库根 node_modules（唯一实现在 _jsdom_loader.js）——全局装了就无需任何环境变量。
 */
"use strict";

const fs = require("fs");
const path = require("path");

const REPO_ROOT = path.join(__dirname, "..", "..");
const { loadJsdom } = require("./_jsdom_loader.js");

const fixturePath = process.argv[2];
if (!fixturePath) {
  console.error("[parity_filters] 用法：node tests/jsdom/parity_filters.js <fixture.json>");
  process.exit(2);
}
const fixture = JSON.parse(fs.readFileSync(fixturePath, "utf8"));
const appSource = fs.readFileSync(
  path.join(REPO_ROOT, "templates", "static", "app.js"), "utf8");

// app.js 加载期会做一批 getElementById / querySelectorAll / addEventListener —— 这里给
// 一份「满足加载期所需元素」的最小 DOM（渲染相关分支命中空数据后自然早退，不产真实卡片）。
const HTML = `<!doctype html><html><head></head><body>
  <main class="wrap">
    <div id="floaters"></div>
    <div id="msgbar" hidden><div id="msg-alert" hidden></div></div>
    <div id="home">
      <div id="summary" hidden></div>
      <div id="picks" hidden>
        <div id="picks-track"></div>
        <div class="deck-nav" hidden></div>
        <div id="picks-sub"></div>
        <button id="picks-prev" type="button"></button>
        <button id="picks-next" type="button"></button>
      </div>
      <div id="sections"></div>
    </div>
    <div id="listview" hidden>
      <div id="lv-title"></div><div id="lv-count"></div>
      <button id="filter-open" type="button"></button><span id="filter-badge" hidden></span>
      <div id="active-chips"></div>
      <div id="rows"></div><div id="lv-pager"></div>
    </div>
    <div id="empty" hidden></div>
    <div id="drawer" hidden>
      <button id="drawer-close" type="button"></button>
      <button id="filter-reset" type="button"></button>
    </div>
    <div id="drawer-mask" hidden></div>
    <button id="btn-compact" type="button"></button><button id="btn-top" type="button"></button>
    <nav id="nav" hidden></nav>
    <button id="nav-toggle" type="button" aria-controls="nav" aria-expanded="false"></button>
    <a id="brand" href="#"></a>
  </main>
</body></html>`;

const { JSDOM, VirtualConsole } = loadJsdom();
const vc = new VirtualConsole();
vc.on("jsdomError", (e) => {
  if (/Not implemented/.test(e.message)) return;      // jsdom 对 scrollTo 之类的噪声
  console.error("[jsdomError] " + e.message);
});

const dom = new JSDOM(HTML, {
  runScripts: "outside-only",
  pretendToBeVisual: true,
  url: "https://example.com/",
  virtualConsole: vc,
  beforeParse(win) {
    win.matchMedia = (q) => ({
      matches: false, media: q,
      addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}
    });
    win.scrollTo = () => {};
  }
});

const win = dom.window;
win.REPORT_DATA = fixture.payload;
win.__SDL_TEST_HOOK__ = true;               // 显式开启 seam，app.js 末尾才会挂 API
win.eval(appSource);                        // 跑真判据（同浏览器执行的同一份源码）
const api = win.__SDL_TEST_API__;
if (!api || typeof api.cardPredicate !== "function") {
  console.error("[parity_filters] seam 未挂载 —— app.js 是否正常加载？（REPORT_DATA 缺失会早退）");
  process.exit(3);
}

const out = fixture.jobs.map(function (job) {
  api.setFilters(job.filters);
  const pred = api.cardPredicate(job.section);
  // 每个 job 用**自己的成员集** —— 与生产里「打开某板块只加载该板块分片」一致。
  // （不能用四板块的并集：`__all__` 的池是 pool_items，与并集不是同一批卡片。）
  return (job.cards || []).filter(pred).length;
});

process.stdout.write(JSON.stringify(out));
dom.window.close();
process.exit(0);
