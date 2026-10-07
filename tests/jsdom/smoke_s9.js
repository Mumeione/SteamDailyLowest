/* S9 首页前端冒烟测（jsdom）—— 无浏览器环境下验证 app.js/app.css 的主力手段。
 * 用法（jsdom 不是本仓库依赖，NODE_PATH 指到装有 jsdom 的 node_modules）：
 *   NODE_PATH=<装有 jsdom 的目录> node tests/jsdom/smoke_s9.js
 * 做法：把 output/ 的 index.html + app.css + data.js + all.js + app.js 内联成一个
 *      自包含页面（同 tools/make_preview.py 的思路），再用 jsdom 跑脚本、断言 DOM。
 * 先跑 `python tools/render_report.py --at <日期>` 产出 output/，再跑本脚本。
 * 2026-10-07 起入库（原来在 %TEMP% 不受版本控制，换机即失传 —— 架构勘察候选 7）。
 */
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const OUT = path.join(__dirname, "..", "..", "output");
const read = (p) => fs.readFileSync(path.join(OUT, p), "utf8");

const index = read("index.html");
const css = read("static/app.css");
const appJs = read("static/app.js");
const dataJs = read("data.js");
const allJs = read("all.js");

// 内联静态资源（all.js 放在 app.js 之前，等价于单文件预览）
let html = index
  .replace(/<link[^>]*app\.css[^>]*>/i, `<style>${css}</style>`)
  .replace(/<script[^>]*data\.js[^>]*><\/script>/i, `<script>${dataJs}</script>`)
  .replace(/<script[^>]*app\.js[^>]*><\/script>/i, `<script>${allJs}</script><script>${appJs}</script>`);

const errors = [];
const vc = new VirtualConsole();
vc.on("jsdomError", (e) => {
  if (/Not implemented/.test(e.message)) return;   // jsdom 对 scrollTo 之类的噪声
  errors.push("[jsdomError] " + e.message);
});
vc.on("error", (m) => errors.push("[console.error] " + m));

const dom = new JSDOM(html, {
  runScripts: "dangerously",
  pretendToBeVisual: true,
  virtualConsole: vc,
  beforeParse(win) {
    win.matchMedia = (q) => ({
      matches: /max-width:\s*768px/.test(q) ? false : false,   // 桌面档
      media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}
    });
    win.scrollTo = () => {};
  }
});

const doc = dom.window.document;
const $$ = (sel) => Array.from(doc.querySelectorAll(sel));
const results = [];
function check(name, ok, extra) {
  results.push({ name, ok, extra: extra === undefined ? "" : String(extra) });
}

check("导航栏 5 项", $$("#nav .nav-item").length === 5, $$("#nav .nav-item").length);
check("顶部大卡渲染 5 张", $$("#picks-track .pick").length === 5, $$("#picks-track .pick").length);
check("大卡有排名徽章", $$("#picks-track .pick-rank").length === 5);
// 大卡照 gg.deals 的结构（2026-10-07 重排）：折扣徽章代替力度条、只留一个 Steam 链接
check("大卡有折扣徽章（不再是力度条）",
  $$("#picks-track .pick .pick-cut").length === 5 && $$("#picks-track .pick .cut-bar").length === 0,
  $$("#picks-track .pick .pick-cut").length + " 个徽章");
// 三排（用户 2026-10-07 定）：① 标题 ② 史低类型 + 折扣 ③ 原价 + 现价 + Steam 图标
check("大卡三排：标题 / 史低+折扣 / 原价+现价+图标",
  $$("#picks-track .pick .pick-title").length === 5 &&
  $$("#picks-track .pick .pick-low .hl").length === 5 &&
  $$("#picks-track .pick .pick-low .pick-cut").length === 5 &&
  $$("#picks-track .pick .pick-price-row .pick-was").length === 5 &&
  $$("#picks-track .pick .pick-price-row .pick-price").length === 5,
  $$("#picks-track .pick .pick-low .pick-cut")[0].textContent);
// 用户 2026-10-07 定稿：小黑盒跟「新史低 + 折扣」同一行、Steam 跟价格同一行，
// 两枚都贴各自右端（竖着对齐成一列）
check("大卡没有 From:、两枚图标分列在第 2/3 排右端",
  $$("#picks-track .pick .pick-from").length === 0 &&
  $$("#picks-track .pick .pick-low .icon-link").length === 5 &&
  $$("#picks-track .pick .pick-price-row .icon-link").length === 5);
// 首页两列按估算高度均衡分配（不再是 CSS grid 直接两列）
check("板块分成左右两列容器", $$("#sections .section-col").length === 2);
check("4 个板块都放进了列里", $$("#sections .section-col .section").length === 4);
check("四板块齐全", $$("#sections .section").length === 4, $$("#sections .section").length);
const secRows = $$("#sections .section .row").length;
check("板块里有行", secRows > 0, secRows + " 行");
check("每行有价格与史低标签",
  $$("#sections .row .row-price .now").length === secRows &&
  $$("#sections .row .row-tags .hl").length === secRows);
// S9 一度把力度条整条弄丢、并把新史低误用成绿色 —— 这两条断言就是防这个再犯
check("每行有折扣力度条（进度条 + 精确百分比）",
  $$("#sections .row .cut-bar").length === secRows &&
  $$("#sections .row .cut-num").length === secRows,
  $$("#sections .row .cut-bar").length + " 条");
check("新史低标签用 hl-new（红，不是绿）",
  $$("#sections .row .hl.hl-new").length > 0,
  $$("#sections .row .hl.hl-new").length + " 个");
check("仪表板摘要行（服务端渲染）",
  /今日新增：新史低/.test(doc.getElementById("summary").textContent),
  doc.getElementById("summary").textContent.trim());
check("首页可见 / 列表页隐藏",
  !doc.getElementById("home").hidden && doc.getElementById("listview").hidden);

// 点导航 → 切到板块完整列表
$$("#nav .nav-item")[0].click();
check("点导航后列表页可见", !doc.getElementById("listview").hidden && doc.getElementById("home").hidden);
check("导航项高亮", $$("#nav .nav-item")[0].classList.contains("active"));
const lvRows = $$("#rows .row").length;
check("列表页渲染出行", lvRows > 0, lvRows + " 行");
check("列表页标题正确", doc.getElementById("lv-title").textContent === "新史低",
  doc.getElementById("lv-title").textContent);
check("列表页条数已填", /\d+ 条/.test(doc.getElementById("lv-count").textContent),
  doc.getElementById("lv-count").textContent);

// 点行 → 展开详情
const firstRow = $$("#rows .row")[0];
firstRow.querySelector(".row-main").click();
check("点行能展开详情", firstRow.classList.contains("open"));
const secondRow = $$("#rows .row")[1];
secondRow.querySelector(".row-main").click();
check("展开是手风琴（同时只开一行）",
  secondRow.classList.contains("open") && !firstRow.classList.contains("open"));

// ---- 底部抽屉筛选（refs.md §6.7）----
const fBadge = doc.getElementById("filter-badge");
const drawer = doc.getElementById("drawer");
check("筛选按钮在、角标默认隐藏", !!doc.getElementById("filter-open") && fBadge.hidden);
doc.getElementById("filter-open").click();
check("点筛选 → 抽屉打开", !drawer.hidden && !doc.getElementById("drawer-mask").hidden);
// 排序不算「筛选」：改排序不该点亮角标（review 补充审查 #6）
const sortOpts = $$('#drawer .chip.opt[data-group="sort"]');
sortOpts[1].click();
check("只改排序不点亮角标", fBadge.hidden, fBadge.textContent || "(空)");
const cutOpts = $$('#drawer .chip.opt[data-group="cut"]');
check("折扣区间有选项", cutOpts.length >= 4, cutOpts.length + " 个");
const totalBefore = doc.getElementById("lv-count").textContent;
cutOpts[cutOpts.length - 1].click();          // 选最高那档（≥90%）
check("选了筛选 → 角标亮起", !fBadge.hidden && fBadge.textContent === "1", fBadge.textContent);
check("选了筛选 → 该选项高亮", cutOpts[cutOpts.length - 1].classList.contains("active"));
// 每页条数固定，所以看**总条数**变化，不是看这一页的行数
const totalAfter = doc.getElementById("lv-count").textContent;
check("筛选真的作用到列表", totalAfter !== totalBefore, totalBefore + " → " + totalAfter);
doc.getElementById("filter-reset").click();
const dateOpts = $$('#drawer .chip.opt[data-group="date"]');
const offDates = dateOpts.filter(function (o) { return o.disabled; });
check("没数据的日期被置灰且不可点",
  dateOpts.length === 5 &&
  offDates.every(function (o) { return o.classList.contains("is-off"); }),
  offDates.length + " 个置灰：" + offDates.map(function (o) { return o.textContent.trim(); }).join("/"));
doc.getElementById("filter-reset").click();
check("重置 → 角标隐藏", fBadge.hidden);
doc.getElementById("drawer-close").click();
check("点 ✕ → 抽屉收起", drawer.hidden);

// 点站点名回首页
doc.getElementById("brand").click();
check("点站点名回首页", !doc.getElementById("home").hidden && doc.getElementById("listview").hidden);
check("导航高亮被清空", $$("#nav .nav-item.active").length === 0);
// 首页四板块是服务端算好的预览，不经过筛选项 → 筛选按钮在首页要藏起来，
// 不能"看得见、点了没反应"（review 补充审查 Spec (c)）
check("首页隐藏「筛选」按钮", doc.getElementById("filter-open").hidden);

// 大卡翻页
const nextBtn = doc.getElementById("picks-next");
check("大卡翻页按钮可用", !nextBtn.disabled);
nextBtn.click();
check("翻页后仍是 5 张", $$("#picks-track .pick").length === 5);
check("翻页后排名从 #6 开始",
  $$("#picks-track .pick-rank")[0].textContent === "#6",
  $$("#picks-track .pick-rank")[0].textContent);

// ---- S9-2：手机端导航折叠（汉堡 → 竖排浮层）----
const navEl = doc.getElementById("nav");
const toggleEl = doc.getElementById("nav-toggle");
check("有汉堡按钮且默认收起",
  !!toggleEl && toggleEl.getAttribute("aria-expanded") === "false" && !navEl.classList.contains("open"));
toggleEl.click();
check("点汉堡 → 展开", navEl.classList.contains("open") && toggleEl.getAttribute("aria-expanded") === "true");
toggleEl.click();
check("再点 → 收起", !navEl.classList.contains("open") && toggleEl.getAttribute("aria-expanded") === "false");
toggleEl.click();
$$("#nav .nav-item")[2].click();          // 展开状态下选一项
check("展开后点导航项 → 自动收起", !navEl.classList.contains("open"));
toggleEl.click();
doc.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
check("按 Esc → 收起", !navEl.classList.contains("open") && toggleEl.getAttribute("aria-expanded") === "false");
// ---- 行内两个图标链接（手机上曾被 CSS 藏掉，2026-10-07 加回）----
check("每行有 Steam / 小黑盒 两个链接",
  $$("#rows .row")[0].querySelectorAll(".icon-link").length === 2,
  $$("#rows .row")[0].querySelectorAll(".icon-link").length + " 个");
// 这条是 CSS 层面的回归（jsdom 不跑媒体查询，只能查样式文本）
check("手机端不再把图标链接 display:none",
  !/\.row-tags\s+\.links\s*\{[^}]*display:\s*none/.test(css));

// ---- 「距上次史低」天数 ↔ 日期 切换（S9 重写时弄丢过）----
const altB = doc.querySelector("b.has-alt");   // 全页找（首页四板块的行也在 DOM 里）
check("「距上次史低」带可切换标记", !!altB);
if (altB) {
  const before = altB.textContent;
  altB.click();
  const after = altB.textContent;
  check("点一下在天数 / 日期之间切换",
    after !== before && /\d{4}-\d{2}-\d{2}/.test(before + after), before + " → " + after);
  altB.click();
  check("再点切回天数", altB.textContent === before, altB.textContent);
}

// 站点名标识（纯 CSS，不引外部字体）：图标 + 双色分段
check("站点名是「图标 + 双色分段」",
  !!doc.querySelector("#brand .brand-mark") &&
  doc.querySelector("#brand .brand-a").textContent === "Steam" &&
  doc.querySelector("#brand .brand-b").textContent === "DailyLowest");

// ---- S9-3：滚动加载 / 返回顶部 / 无图精简模式（重新打开板块列表页验证）----
const listCfg = (dom.window.REPORT_DATA || {}).list || {};
check("列表参数由 payload 下发（batch/auto_max）",
  listCfg.batch === 30 && listCfg.auto_max === 300,
  "batch=" + listCfg.batch + " auto_max=" + listCfg.auto_max);
// ---- 口径机械校验（架构勘察候选 2）：CSS 的 @media 边界必须 == payload 下发的
// 两断点。曾发生真实腐烂：断点 768→600 改版时查询改了、守护注释没跟上 ——
// 注释不会报错，这里会。@media 里出现的值 = 手机断点、+1、平板断点、+1 四种。
check("CSS @media 边界 == payload 下发的两断点", (function () {
  const mob = listCfg.breakpoint, tab = listCfg.tablet_breakpoint;
  if (!mob || !tab) return false;
  const nums = new Set();
  for (const m of css.matchAll(/@media[^{]*?(\d{3,4})px/g)) nums.add(Number(m[1]));
  const ok = [...nums].every((n) => n === mob || n === mob + 1 || n === tab || n === tab + 1);
  return ok && nums.has(mob) && nums.has(tab + 1);
})(), "media=" + [...new Set(Array.from(css.matchAll(/@media[^{]*?(\d{3,4})px/g), (m) => m[1]))].join("/")
    + " payload=" + listCfg.breakpoint + "/" + listCfg.tablet_breakpoint);
$$("#nav .nav-item")[0].click();
const firstBatch = $$("#rows .row").length;
check("列表页首批 = 每批 30 条", firstBatch === 30, firstBatch + " 行");
const moreBtn = doc.querySelector("#lv-pager .load-more");
check("底部有「加载更多」按钮", !!moreBtn);
if (moreBtn) {
  moreBtn.click();
  check("点「加载更多」追加到 60 行", $$("#rows .row").length === 60,
    $$("#rows .row").length + " 行");
}
const compactBtn = doc.getElementById("btn-compact");
const topBtn = doc.getElementById("btn-top");
check("精简模式按钮常驻可见（带 .show）", compactBtn.classList.contains("show"));
check("返回顶部按钮初始不显示", !topBtn.classList.contains("show"));
compactBtn.click();
check("点精简 → body.compact", doc.body.classList.contains("compact"));
check("精简模式 aria-pressed=true", compactBtn.getAttribute("aria-pressed") === "true");
compactBtn.click();
check("再点一次关掉精简模式", !doc.body.classList.contains("compact"));
topBtn.click();
check("点返回顶部不报错", true);

// ---- S9-3：顶部消息区（节日条 / 陈旧告警两档 / 站点通知）----
// 这一段的做法：**拿真实产物当底板**，只替换 data.js 里的 generated_at（或补一个
// data-notice），用同一套 app.js 重建 DOM —— 无浏览器环境下唯一能验证这几条分支的办法。
const dataObj = () => JSON.parse(
  dataJs.replace(/^window\.REPORT_DATA\s*=\s*/, "").replace(/;\s*$/, ""));
const hoursAgo = (h) => new Date(Date.now() - h * 3600000).toISOString();
const patchNotice = (h) => h.replace('id="msg-alert" hidden></div>',
  'id="msg-alert" hidden data-notice="站点改版说明"></div>');

function buildDom(mutate, patchHtml, opts) {
  opts = opts || {};
  const obj = dataObj();
  if (mutate) mutate(obj);
  const patchedData = "window.REPORT_DATA = " + JSON.stringify(obj) + ";";
  let h = index
    .replace(/<link[^>]*app\.css[^>]*>/i, `<style>${css}</style>`)
    .replace(/<script[^>]*data\.js[^>]*><\/script>/i, `<script>${patchedData}</script>`)
    .replace(/<script[^>]*app\.js[^>]*><\/script>/i,
      (opts.withAllJs === false ? "" : `<script>${allJs}</script>`) + `<script>${appJs}</script>`);
  if (patchHtml) h = patchHtml(h);
  const vc2 = new VirtualConsole();
  vc2.on("jsdomError", (e) => {
    if (!/Not implemented/.test(e.message)) errors.push("[variant jsdomError] " + e.message);
  });
  vc2.on("error", (m) => errors.push("[variant console.error] " + m));
  return new JSDOM(h, {
    runScripts: "dangerously",
    pretendToBeVisual: true,
    virtualConsole: vc2,
    beforeParse(win) {
      win.matchMedia = (q) => ({
        matches: false, media: q,
        addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}
      });
      win.scrollTo = () => {};
    }
  });
}

const festBox = doc.getElementById("msg-fest");
const alertBox = doc.getElementById("msg-alert");
check("节日条带季节主题类名 + 活动名",
  !!festBox && /season-(spring|summer|autumn|winter)/.test(festBox.className),
  festBox ? festBox.className : "(没有节日条)");
check("节日条写明起止区间与剩余天数",
  !!festBox && /\d{2}-\d{2} \d{2}:\d{2}.*\d{2}-\d{2} \d{2}:\d{2}/.test(festBox.textContent) &&
  /还有|今天结束/.test(festBox.textContent),
  festBox ? festBox.textContent.replace(/\s+/g, " ").trim() : "");
check("节日条与顶栏正文左对齐（内容套了 .wrap）",
  !!festBox && !!festBox.querySelector(".wrap.msg-inner"),
  festBox ? (festBox.querySelector(".msg-inner") ? "有 .wrap.msg-inner" : "没有") : "");
check("刚更新过的数据不显示陈旧告警", alertBox.hidden);

const warnDoc = buildDom((o) => { o.generated_at = hoursAgo(30); }).window.document;
check(">26 小时 → 黄色告警（Actions 延迟）",
  !warnDoc.getElementById("msg-alert").hidden &&
  warnDoc.getElementById("msg-alert").className.indexOf("is-warn") !== -1,
  warnDoc.getElementById("msg-alert").className);

const redDoc = buildDom((o) => { o.generated_at = hoursAgo(40); }).window.document;
check(">36 小时 → 红色告警", redDoc.getElementById("msg-alert").className.indexOf("is-stale") !== -1);
check("有告警时整块消息区会显示出来", !redDoc.getElementById("msgbar").hidden);

const noticeDoc = buildDom((o) => { o.generated_at = hoursAgo(2); }, patchNotice).window.document;
check("数据新鲜 + 有站点通知 → 第二行显示通知",
  noticeDoc.getElementById("msg-alert").className.indexOf("is-notice") !== -1 &&
  /站点改版说明/.test(noticeDoc.getElementById("msg-alert").textContent),
  noticeDoc.getElementById("msg-alert").textContent);

const bothDoc = buildDom((o) => { o.generated_at = hoursAgo(40); }, patchNotice).window.document;
check("陈旧告警优先于站点通知（两者同时成立时）",
  bothDoc.getElementById("msg-alert").className.indexOf("is-stale") !== -1 &&
  !/站点改版说明/.test(bothDoc.getElementById("msg-alert").textContent),
  bothDoc.getElementById("msg-alert").textContent);

// ---- S9-3：空状态三态插画（refs.md B13）----
// ① 无内容：把 sections 清空
const emptyDoc = buildDom((o) => { o.sections = []; o.picks = []; }).window.document;
const emptyEl = emptyDoc.getElementById("empty");
check("没内容 → 显示空状态（不再只是一句话）",
  !emptyEl.hidden && !!emptyEl.querySelector(".empty-art"),
  emptyEl.textContent.trim());
check("无内容用「探底」那张插画（不是转圈那张）",
  !!emptyEl.querySelector(".empty-art:not(.is-spin)"));

// ② 加载中：不内联 all.js，点导航后停在加载态
const loadingDom = buildDom(null, null, { withAllJs: false });
loadingDom.window.document.querySelector("#nav .nav-item").click();
const loadingEmpty = loadingDom.window.document.getElementById("empty");
check("加载中 → 转圈插画 + 加载文案",
  !loadingEmpty.hidden && !!loadingEmpty.querySelector(".empty-art.is-spin") &&
  /正在加载/.test(loadingEmpty.textContent),
  loadingEmpty.textContent.trim());

// ③ 加载失败：给那个动态 <script> 派发一次 error
const loadScript = loadingDom.window.document.querySelector('script[src="all.js"]');
check("点导航真的去加载 all.js", !!loadScript);
if (loadScript) {
  loadScript.dispatchEvent(new loadingDom.window.Event("error"));
  check("加载失败 → 失败插画 + 重试按钮",
    !loadingEmpty.hidden && !!loadingEmpty.querySelector(".empty-art") &&
    !!loadingEmpty.querySelector(".empty-retry") && /加载失败/.test(loadingEmpty.textContent),
    loadingEmpty.textContent.trim());
  loadingEmpty.querySelector(".empty-retry").click();
  check("点重试 → 回到加载态", !!loadingEmpty.querySelector(".empty-art.is-spin"));
}

// ---- S9-卡片：好评率 / 剩余天数分档上色 + 力度条自适应 ----
const cardRows = $$("#sections .row");
const rateEls = cardRows.map((r) => r.querySelector(".row-sub .rate")).filter(Boolean);
const daysEls = cardRows.map((r) => r.querySelector(".row-sub .days")).filter(Boolean);
check("好评率单独成 span 并带分档类名",
  rateEls.length === cardRows.length &&
  rateEls.every((e) => /rate-(high|ok|low)/.test(e.className)),
  rateEls.length ? rateEls[0].className + " → " + rateEls[0].textContent : "(无)");
check("剩余天数带分档类名",
  daysEls.length > 0 && daysEls.every((e) => /days-(urgent|soon|later)/.test(e.className)),
  daysEls.length ? daysEls[0].className + " → " + daysEls[0].textContent : "(无)");
check("分档有区分度（不是所有卡一个档）",
  new Set(daysEls.map((e) => e.className)).size > 1 ||
  new Set(rateEls.map((e) => e.className)).size > 1,
  "rate=" + new Set(rateEls.map((e) => e.className)).size +
  " days=" + new Set(daysEls.map((e) => e.className)).size);
// 「不要写死太多长度，多用响应式布局」——力度条宽度必须来自 flex，不能再是 96/72px
check("行内力度条宽度改自适应（width:auto + flex，不再写死 96/72px）",
  /\.row-tags\s+\.cut-bar\s*\{[^}]*\bwidth:\s*auto/.test(css) &&
  /\.row-tags\s+\.cut-bar\s*\{[^}]*flex:\s*1/.test(css));
check("大卡列数由 JS 按宽度算并写进 inline style（不再靠 CSS 猜）", (function () {
  const style = doc.getElementById("picks-track").getAttribute("style") || "";
  const m = style.match(/repeat\((\d+),/);
  // 上限 5 列（refs §11.2：一排最多 5 张）；jsdom 没有布局 → 走的是「按视口估算」那条兜底
  return !!m && Number(m[1]) >= 2 && Number(m[1]) <= 5;
})(), doc.getElementById("picks-track").getAttribute("style") || "(无)");
check("大卡每页张数 = 实际列数（不是写死的 5）", (function () {
  const sub = doc.getElementById("picks-sub").textContent;
  const n = $$("#picks-track .pick").length;
  // jsdom 没有布局引擎 → clientWidth 恒 0，走的是「按视口宽度估列数」那条兜底
  // （1024 视口 → 5 列 → 每页 5 张、15 张共 3 页）。真实列数只能靠人工/真机看。
  return /第 \d+\/\d+ 页/.test(sub) && n >= 2 && n <= 8;
})());

// 板块列表页的顺序必须来自服务端下发的 section_order（否则「查看更多」里顺序和首页不一致）
check("all.js 下发了每个板块的顺序 section_order", (function () {
  const so = (dom.window.ALL_DATA || {}).section_order || {};
  const keys = Object.keys(so);
  return keys.length === 4 && keys.every((k) => Array.isArray(so[k]) && so[k].length > 0);
})(), Object.keys((dom.window.ALL_DATA || {}).section_order || {}).join("/"));

// 点进板块列表页后，首条必须是「服务端板块顺序」的第一条（首页预览与列表页一致）
check("板块列表页首条 = 服务端板块顺序第一条", (function () {
  const order = ((dom.window.ALL_DATA || {}).section_order || {}).new_low || [];
  const cards = [];
  ((dom.window.ALL_DATA || {}).groups || []).forEach((g) => (g.items || []).forEach((c) => cards.push(c)));
  const want = cards.find((c) => c.appid === order[0]);
  const got = doc.querySelector("#rows .row .row-title");
  if (!want || !got) return false;
  return got.textContent === (want.title_zh || want.title);
})());

// ---- S9 收尾小件（2026-10-07）：无封面占位 ----
// 一次性气泡做过又删了（用户裁定：悬停呼吸效果已能表达可点，气泡多此一举），
// 留一条断言防止它被「顺手加回来」：
check("一次性气泡已删除（不再渲染 .hint-bubble）", !doc.querySelector(".hint-bubble"));

// 无封面占位（约 9.3% 无 boxart）：无 banner 时渲染同尺寸灰块节点（div.row-thumb /
// .pick-art 空底），CSS 浅灰底兜住 —— 不崩图、不塌高度。
// ⚠️ 不能依赖「当天数据碰巧有无封面卡片」——那会让断言被静默跳过（review 抓过）。
// 做法：用 buildDom 把**所有**条目的 banner 抹成 null 再渲染一遍，占位节点数必须
// 精确等于行数/大卡数，永远可判定。
(function () {
  const phDoc = buildDom((o) => {
    (o.sections || []).forEach((s) => (s.items || []).forEach((it) => { it.banner = null; }));
    (o.picks || []).forEach((p) => { p.banner = null; });
  }).window.document;
  const $$p = (sel) => Array.from(phDoc.querySelectorAll(sel));
  const rowCount = $$p("#sections .row").length;
  const divThumbs = $$p("#sections .row-thumb").filter((e) => e.tagName === "DIV");
  check("无封面行卡片渲染灰块占位（抹 banner 后占位数 == 行数）",
    rowCount > 0 && divThumbs.length === rowCount,
    "占位 " + divThumbs.length + " / 行 " + rowCount);
  const pickArts = $$p("#picks-track .pick-art");
  const noImgPicks = pickArts.filter((a) => !a.querySelector("img"));
  check("无封面大卡 .pick-art 保留灰底占位（不塌高度）",
    pickArts.length > 0 && noImgPicks.length === pickArts.length,
    "占位 " + noImgPicks.length + " / 大卡 " + pickArts.length);
})();

check("运行期无 JS 报错", errors.length === 0, errors.slice(0, 3).join(" | "));

const failed = results.filter((r) => !r.ok);
results.forEach((r) => console.log(`${r.ok ? "  OK  " : "  !!  "}${r.name}${r.extra ? "  → " + r.extra : ""}`));
console.log(`\n${results.length - failed.length}/${results.length} 通过`);
process.exit(failed.length ? 1 : 0);
