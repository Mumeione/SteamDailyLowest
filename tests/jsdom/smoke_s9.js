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
// 三排（2026-10-07 第二轮定稿）：① 标题 ② 折扣徽章 + Steam ③ 原价 + 现价 + 小黑盒。
// 「史低类型」徽章已删 → 改由**卡片底部 6px 色条**表达（见下一条）
check("大卡三排：标题 / 折扣+图标 / 原价+现价+图标",
  $$("#picks-track .pick .pick-title").length === 5 &&
  $$("#picks-track .pick .pick-low .pick-cut").length === 5 &&
  $$("#picks-track .pick .pick-price-row .pick-was").length === 5 &&
  $$("#picks-track .pick .pick-price-row .pick-price").length === 5,
  $$("#picks-track .pick .pick-low .pick-cut")[0].textContent);
// 用户 2026-10-07：「大卡底部加色条就行了，反正空的地方还多」——
// 每张大卡都要有一条史低类型色条（l-new / l-tie / l-unk 三者之一），且**不再有徽章**
check("大卡底部色条（史低类型）取代了徽章",
  $$("#picks-track .pick.l-new, #picks-track .pick.l-tie, #picks-track .pick.l-unk").length === 5 &&
  $$("#picks-track .pick .hl").length === 0,
  $$("#picks-track .pick.l-new").length + " 张新史低色条");
// 用户 2026-10-07 定稿：小黑盒跟「折扣」同一行、Steam 跟价格同一行，
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
check("每行有价格，且每行都带一个史低类型色条类名",
  $$("#sections .row .row-price .now").length === secRows &&
  $$("#sections .row.l-new, #sections .row.l-tie, #sections .row.l-unk").length === secRows,
  $$("#sections .row.l-new").length + " 行新史低");
// S9 一度把力度条整条弄丢、并把新史低误用成绿色 —— 这两条断言就是防这个再犯
check("每行有折扣力度条（进度条 + 精确百分比）",
  $$("#sections .row .cut-bar").length === secRows &&
  $$("#sections .row .cut-num").length === secRows,
  $$("#sections .row .cut-bar").length + " 条");
// 2026-10-07 第二轮：行卡片的「新史低/平史低」徽章**已删**（靠左侧色条区分），
// 且色条必须是红（新史低）而不是绿（绿专属折扣力度）。这条断言就是防徽章被加回来。
check("行卡片不再有史低徽章（靠左侧色条区分）",
  $$("#sections .row .hl").length === 0 &&
  $$("#sections .row.l-new").length > 0,
  $$("#sections .row.l-new").length + " 行新史低红条");
check("仪表板摘要行（服务端渲染 + S2 结构）",
  /今日新增/.test(doc.getElementById("summary").textContent) &&
  $$("#summary .s-new b").length === 1 &&
  $$("#summary .s-tie b").length === 1 &&
  /\d/.test($$("#summary .s-new b")[0].textContent),
  doc.getElementById("summary").textContent.trim());
// 用户 2026-10-07：「中间不要留很粗的竖线，不好看，可以隔开一点」→ 数字前的小色条已删
check("摘要里没有小色条（靠间距 + 数字颜色区分）", $$("#summary .s-bar").length === 0);
// 用户同一条：「新史低和史低字号比今日新增大一点就行了」
// （jsdom 不跑媒体查询但会算作者样式；这里锁 CSS 文本层面的两档字号）
check("摘要里「新史低/平史低」比「今日新增」大一档",
  /\.summary\s+\.s-cap\s*\{[^}]*font-size:\s*var\(--fs-aux\)/.test(css) &&
  /\.summary\s+\.s-unit\s*\{[^}]*font-size:\s*var\(--fs-sub\)/.test(css));
check("首页可见 / 列表页隐藏",
  !doc.getElementById("home").hidden && doc.getElementById("listview").hidden);
// refs.md §10.7（用户 2026-10-07 定为违规项）：概览摘要**只放首页**，五类标签页不放 ——
// 列表页有自己的条数（lv-count），摘要那句「今日新增…」在那儿是误导
check("首页显示概览摘要", !doc.getElementById("summary").hidden);

// 点导航 → 切到板块完整列表
$$("#nav .nav-item")[0].click();
check("点导航后列表页可见", !doc.getElementById("listview").hidden && doc.getElementById("home").hidden);
check("列表页隐藏概览摘要（refs §10.7）", doc.getElementById("summary").hidden);
check("列表页显示「筛选」按钮",
  !doc.getElementById("filter-open").hidden &&
  dom.window.getComputedStyle(doc.getElementById("filter-open")).display !== "none");
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
// 2026-10-07 第二轮：按钮改成主操作长相（漏斗图标 + 计数 + 折角）
check("筛选按钮带漏斗图标 / 计数 / 折角",
  $$("#filter-open .f-ico").length === 1 &&
  $$("#filter-open .f-badge").length === 1 &&
  $$("#filter-open .f-caret").length === 1);
// E1 的锚点：面板必须是 .filterbar 的**子元素**（≥601px 靠 .filterbar{position:relative}
// 做绝对定位）。挪出去面板就会定位到别处 —— 这条断言锁结构。
check("筛选面板挂在 .filterbar 里（E1 定位锚点）",
  doc.querySelector(".filterbar > #drawer") === drawer,
  drawer.parentNode.className);
check("未选条件时：按钮不带 is-on、没有已选小标签",
  !doc.getElementById("filter-open").classList.contains("is-on") &&
  $$("#active-chips .a-chip").length === 0);
doc.getElementById("filter-open").click();
check("点筛选 → 抽屉打开", !drawer.hidden && !doc.getElementById("drawer-mask").hidden);
// 排序不算「筛选」：改排序不该点亮角标（review 补充审查 #6）
const sortOpts = $$('#drawer .chip.opt[data-group="sort"]');
sortOpts[1].click();
check("只改排序不点亮角标", fBadge.hidden, fBadge.textContent || "(空)");
const cutOpts = $$('#drawer .chip.opt[data-group="cut"]');
// 用户 2026-10-07：「折扣区间去掉 70%」→ 只剩 不限 / ≥50% / ≥80% / ≥90%
check("折扣区间 4 档且没有 ≥70%",
  cutOpts.length === 4 && !cutOpts.some(function (o) { return o.textContent.trim() === "≥ 70%"; }),
  cutOpts.map(function (o) { return o.textContent.trim(); }).join(" / "));
const totalBefore = doc.getElementById("lv-count").textContent;
cutOpts[cutOpts.length - 1].click();          // 选最高那档（≥90%）
check("选了筛选 → 角标亮起", !fBadge.hidden && fBadge.textContent === "1", fBadge.textContent);
check("选了筛选 → 该选项高亮", cutOpts[cutOpts.length - 1].classList.contains("active"));
// B3：按钮整体变蓝 + 右边出现一枚已选条件小标签（文案取自抽屉里的同名选项）
check("选了筛选 → 按钮变 is-on 态", doc.getElementById("filter-open").classList.contains("is-on"));
check("选了筛选 → 出现已选小标签（文案与抽屉一致）",
  $$("#active-chips .a-chip").length === 1 &&
  $$("#active-chips .a-chip")[0].textContent.replace("✕", "").trim() === "≥ 90%",
  $$("#active-chips .a-chip")[0] && $$("#active-chips .a-chip")[0].textContent.trim());
// 每页条数固定，所以看**总条数**变化，不是看这一页的行数
const totalAfter = doc.getElementById("lv-count").textContent;
check("筛选真的作用到列表", totalAfter !== totalBefore, totalBefore + " → " + totalAfter);
// 小标签上的 ✕ 要能单独去掉这个条件（回到默认 = 不限）
$$("#active-chips .a-chip button")[0].click();
check("点小标签的 ✕ → 条件被去掉、标签消失",
  $$("#active-chips .a-chip").length === 0 && fBadge.hidden && cutOpts[0].classList.contains("active"),
  "角标 hidden=" + fBadge.hidden);
cutOpts[cutOpts.length - 1].click();          // 再加回来，供下面「重置」那条断言用
check("再加回来 → 小标签与角标都回来",
  $$("#active-chips .a-chip").length === 1 && !fBadge.hidden);
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

// ---- 2026-10-07 第八轮：板块口径已隐含的选项要置灰 ----
// 用户举例：新史低里还能再选「仅新史低」、大额折扣里还能选「折扣降序」——
// 这些选项在该板块里选了也不会变（板块口径已隐含），要置灰 + 悬停说明原因。
const optBy = function (g, v) {
  return doc.querySelector('#drawer .chip.opt[data-group="' + g + '"][data-value="' + v + '"]');
};
const navTo = function (i) { $$("#nav .nav-item")[i].click(); };
navTo(0);                                    // 新史低
check("新史低板块：仅新史低置灰 + 说明原因（不是删除线）",
  optBy("only_new", "new").disabled &&
  optBy("only_new", "new").classList.contains("is-implied") &&
  !optBy("only_new", "new").classList.contains("is-off") &&
  /新史低/.test(optBy("only_new", "new").title),
  optBy("only_new", "new").title);
check("新史低板块：折扣降序可选（精选已改为按公式打分，不再与它等价）",
  !optBy("sort", "cut").disabled && !optBy("sort", "cut").classList.contains("is-implied"));
check("新史低板块：好评数量仍可选（板块没有评价数门槛）",
  !optBy("reviews", "500").disabled);
navTo(3);                                    // 大额折扣
check("大额折扣板块：折扣降序被隐含（2026-10-08 板块精选改回折扣降序，两者等价）",
  optBy("sort", "cut").classList.contains("is-implied") &&
  !optBy("sort", "cut").classList.contains("is-off") &&
  /折扣降序/.test(optBy("sort", "cut").title || ""),
  optBy("sort", "cut").title || "(无 title)");
check("大额折扣板块：折扣区间 ≥50% / ≥80% 置灰、≥90% 可选",
  optBy("cut", "50").disabled && optBy("cut", "80").disabled && !optBy("cut", "90").disabled);
check("大额折扣板块：史低类型仍可选（板块里有平史低）",
  !optBy("only_new", "new").disabled);
navTo(2);                                    // 热门游戏
check("热门游戏板块：好评数量三档全置灰（板块已要求 ≥1 万条）",
  optBy("reviews", "500").disabled && optBy("reviews", "5000").disabled &&
  optBy("reviews", "10000").disabled);
check("热门游戏板块：折扣区间仍可选", !optBy("cut", "50").disabled);
// 带着失效条件进板块要自动复位：先在「全部折扣」选「仅新史低」，再进「新史低」
navTo(4);                                    // 全部折扣（这里它有效）
optBy("only_new", "new").click();
// 角标 = 2：①仅新史低 ②进「全部折扣」时日期会被切成「全部」（openSection 的既有行为，
// 与 FILTER_DEFAULTS.date="近 7 天" 不同所以计入）—— 与本条无关，别顺手"修"它。
check("全部折扣里能选仅新史低（角标 2：日期切到全部 + 仅新史低）",
  !fBadge.hidden && fBadge.textContent === "2", fBadge.textContent);
navTo(0);                                    // 进新史低 → 该条件失效，应自动复位
check("进新史低板块 → 失效的「仅新史低」自动复位",
  fBadge.hidden && optBy("only_new", "all").classList.contains("active"),
  "角标=" + fBadge.textContent);
navTo(0);

// 点站点名回首页
doc.getElementById("brand").click();
check("点站点名回首页", !doc.getElementById("home").hidden && doc.getElementById("listview").hidden);
check("导航高亮被清空", $$("#nav .nav-item.active").length === 0);
// 首页四板块是服务端算好的预览，不经过筛选项 → 筛选按钮在首页要藏起来，
// 不能"看得见、点了没反应"（review 补充审查 Spec (c)）
// ⚠️ 必须同时断言「属性 + **计算样式**」：2026-10-07 那条 bug 就藏在只查属性里 ——
//    .filter-open{display:inline-flex} 盖掉了 UA 的 [hidden]{display:none}，
//    hidden 属性是 true、按钮照样渲染得出来。jsdom 会解 author 样式表的 [hidden]
//    选择器，补上计算样式就把这条锁死了。
const fBtn = doc.getElementById("filter-open");
check("首页隐藏「筛选」按钮（属性 + 计算样式）",
  fBtn.hidden && dom.window.getComputedStyle(fBtn).display === "none",
  "hidden=" + fBtn.hidden + " · display=" + dom.window.getComputedStyle(fBtn).display);
check("首页显示概览摘要", !doc.getElementById("summary").hidden);

// 大卡翻页 —— 2026-10-08 起默认上限 3 页 15 张（最低 5、能凑 15 凑 15、不凑数）：
// 真实产物（大促满额）走**多页**分支：翻页可用、副标题写页码；
// 单页收起（deck-nav 整块 hidden）的分支在下面用受控数据（切回 5 张）覆盖。
const nextBtn = doc.getElementById("picks-next");
const prevBtn = doc.getElementById("picks-prev");
const picksSubEl = doc.getElementById("picks-sub");
// 期望页数由夹具数据推算（ceil(总张数/列数)），不硬编码 —— 大促夹具换了张数也不脆断
  const mpCols = Number(((doc.getElementById("picks-track").getAttribute("style") || "")
    .match(/repeat\((\d+),/) || [0, 0])[1]);
  const mpTotal = ((dom.window.REPORT_DATA || {}).picks || []).length;
  const mpPages = mpCols && mpTotal ? Math.ceil(mpTotal / mpCols) : 0;
  check("多页大卡：第 1 页上「上一页」禁用、「下一页」可用 + 副标题写页码",
    prevBtn.disabled && !nextBtn.disabled &&
    !doc.querySelector("#picks .deck-nav[hidden]") &&
    mpPages > 1 && new RegExp("第\\s*1\\s*/\\s*" + mpPages + "\\s*页").test(picksSubEl.textContent),
    picksSubEl.textContent + "（期望共 " + mpPages + " 页）");
check("大卡一排 5 张（第一页）、排名从 #1 开始",
  $$("#picks-track .pick").length === 5 &&
  $$("#picks-track .pick-rank")[0].textContent === "#1",
  $$("#picks-track .pick-rank").map(function (e) { return e.textContent; }).join(","));

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
// 这条是 CSS 层面的回归（jsdom 不跑媒体查询，只能查样式文本）。
// ⚠️ 2026-10-07 第三轮中途试过「精简模式窄行隐掉两枚图标」，被用户否掉
//    （手机精简模式的力度条要独占一行铺满，不需要让宽度）→ 现在**任何情况下都不许藏**。
check("图标链接不会被 CSS 藏掉（含精简模式）",
  !/\.row-tags\s+\.links\s*\{[^}]*display:\s*none/.test(css),
  (css.match(/[^{}]*\.row-tags\s+\.links\s*\{[^}]*display:\s*none[^}]*\}/g) || []).join(" | "));

// ---- 2026-10-07 第三轮：手机端「筛选」搬进右下角浮层（桌面上它必须待在筛选行里）----
// jsdom 不跑媒体查询（mq.matches 被桩成 false = 桌面档），所以这里只锁桌面分支：
// 桌面下按钮不能在浮层里，否则手机那份搬动逻辑会把它永久留在浮层。
check("「筛选」按钮全端在右下角浮层、且在「精简」上方（2026-10-08 全端统一）",
  doc.querySelector("#floaters #filter-open") === doc.getElementById("filter-open") &&
  doc.querySelector(".filterbar > #filter-open") === null &&
  (function () {
    const kids = Array.from(doc.getElementById("floaters").children);
    return kids[0].id === "filter-open" && kids[1] && kids[1].id === "btn-compact";
  })(),
  "浮层顺序：" + Array.from(doc.getElementById("floaters").children).map((c) => c.id).join(","));
// 手机上按钮只留漏斗图标 → 「筛选」二字必须有个可隐藏的类名（别写成裸 span）
check("「筛选」二字带可隐藏的 .f-label",
  $$("#filter-open .f-label").length === 1);

// ---- 2026-10-07 第七轮：精简模式的行结构 = **4 列 grid，两行锁死** ----
// 用户终态要求：「卡片的图标和力度条放在剩余天数右侧，卡片右边只显示价格」。
// jsdom 没有布局引擎（量不出行高/换行），所以这几条只能锁 CSS 文本层面的结构 ——
// 少任何一条，那个「评价与标签行同一行、右列只有价格」的版式就会塌。
check("精简模式：两行结构（info 拆开 + 4 列 grid 区域）",
  /body\.compact\s+\.row-info\s*\{\s*display:\s*contents/.test(css) &&
  /body\.compact\s+\.row-main\s*\{[^}]*grid-template-areas/.test(css) &&
  // 第一行「中文名 → 英文名 → 余量 → 价格」，第二行「评价 → 标签行跨后三列」。
  // ⚠️ 中文名列是 `minmax(0, 36%)`（封顶）而不是 `auto`：封顶才能让第二行的
  //    图标/力度条跨行对齐（用户 2026-10-07 的第二条要求）。36% 这个数是按
  //    「竖排原价后的价格列（≈50px）+ 英文名列 + 间距」倒推的上界。
  /"title en\s+\.\s+price"\s*"sub\s+tags\s+tags\s+tags"/.test(css) &&
  /grid-template-columns:\s*minmax\(0,\s*36%\)\s+minmax\(0,\s*auto\)/.test(css) &&
  // 价格在精简模式里**竖排**（现价在上、划线原价在下），省出的宽度给中文名
  /body\.compact\s+\.row-price\s*\{[^}]*flex-direction:\s*column/.test(css));
// 评价行三个数值列必须留固定宽度（用户：「有评价数目显示时，图标和力度条还有折扣日期
// 无法精准对齐…要留一点余量」）—— 少了任何一条，后面的字段就会随「评价数几位数」漂。
check("评价行三个数值列都留了固定宽度（对齐用）",
  /\.row-sub\s+\.rate\s*\{[^}]*min-width/.test(css) &&
  /\.row-sub\s+\.rc\s*\{[^}]*min-width/.test(css) &&
  /\.row-sub\s+\.days\s*\{[^}]*min-width/.test(css));
check("精简模式：窄行隐掉评价条数（连它前面的分隔点一起）",
  /body\.compact\s+\.row-sub\s+\.rc[\s\S]{0,80}\.rc\s*\+\s*\.sp/.test(css));
// 「评价·天数」的 DOM 里，分隔点必须是**元素**（.sp）而不是裸文本节点 ——
// 否则 `:has`/相邻兄弟都没法把它跟评价条数一起隐藏（会剩下「78% · · 剩 2 天」）。
check("评价行：分隔点是 .sp 元素（不是裸文本）",
  (() => {
    const sub = doc.querySelector("#rows .row .row-sub");
    if (!sub) return false;
    const seps = Array.from(sub.children).filter((c) => c.classList.contains("sp"));
    return seps.length >= 1 && seps.every((c) => c.textContent.trim() === "·");
  })(), (() => {
    const sub = doc.querySelector("#rows .row .row-sub");
    return sub ? Array.from(sub.children).map((c) => c.className || "(文本)").join("/") : "(无)";
  })());

// ---- 2026-10-07 第四轮：详情区删「折扣开始」、页脚精简居中 ----
// 用户：「删除详情展开里折扣开始的时间这一行，以后加第三个外区再加回来」
const anyDetail = doc.querySelector("#rows .row .row-detail, #sections .row .row-detail");
const detailText = anyDetail ? anyDetail.textContent : "";
check("详情区不再有「折扣开始」",
  detailText !== "" && !/折扣开始/.test(detailText), detailText.replace(/\s+/g, " ").slice(0, 60));
check("详情区要保留「折扣结束」", /折扣结束/.test(detailText));
// 页脚：居中 + 只留三行，且**不许**出现生成时间 / 最近一次运行（用户 2026-10-07）
const foot = doc.querySelector("footer.footer");
const footText = foot ? foot.textContent : "";
check("页脚不含生成时间 / 最近一次运行",
  footText !== "" && !/生成时间|最近一次运行|状态库/.test(footText),
  footText.replace(/\s+/g, " ").slice(0, 70));
check("页脚保留三条链接（关于网站 / 源码仓库 / Actions）",
  foot && foot.querySelectorAll(".foot-links a").length === 3);
check("页脚居中（CSS text-align: center）",
  /\.footer\s*\{[^}]*text-align:\s*center/.test(css));

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
check("CSS @media 边界只用 payload 下发的断点", (function () {
  const mob = listCfg.breakpoint, tab = listCfg.tablet_breakpoint;
  if (!mob || !tab) return false;
  const nums = new Set();
  for (const m of css.matchAll(/@media[^{]*?(\d{3,4})px/g)) nums.add(Number(m[1]));
  // ① 出现过的每个数值都必须是合法断点（手机/平板及其 +1）—— 挡住「随手写个 900px」
  const legal = [...nums].every((n) => n === mob || n === mob + 1 || n === tab || n === tab + 1);
  // ② 手机断点的两条边界（600 / 601）必须成对出现，否则「布局按手机、逻辑按桌面」会错位。
  // ⚠️ 平板断点 1100/1101 **故意不在 CSS 里**：E1 定案后平板与 PC 共用同一套规则
  //    （行卡片的一行/两行结构由 @container row 按行宽判、板块两列由 @container home
  //    按 .home 内容宽判），三档视口实际只剩两条边界；`tablet_breakpoint` 仍留在
  //    payload 里给预览工具用。
  //    所以这条断言**不再要求 tab+1 出现**，但 url 上仍然不允许多出别的数值。
  return legal && nums.has(mob) && nums.has(mob + 1);
})(), "media=" + [...new Set(Array.from(css.matchAll(/@media[^{]*?(\d{3,4})px/g), (m) => m[1]))].join("/")
    + " payload=" + listCfg.breakpoint + "/" + listCfg.tablet_breakpoint);
// ---- 口径机械校验（2026-10-08）：板块两列的 @container 阈值必须 == 「大卡 4 列」
// 的同一内容宽 4×(PICK_MIN+PICK_GAP)−12。用户定的口径是「两列板块刚好放得下 =
// 大卡从 3 张变 4 张」，两边各写一份数字就会悄悄失配 —— 大卡 3→4 张与板块
// 1→2 列必须在同一宽度发生（见 app.css「四板块」注释）。
(function () {
  const min = (appJs.match(/PICK_MIN\s*=\s*(\d+)/) || [])[1];
  const gap = (appJs.match(/PICK_GAP\s*=\s*(\d+)/) || [])[1];
  const th = (css.match(/@container\s+home\s*\(\s*min-width:\s*(\d+)px\s*\)/) || [])[1];
  check("板块两列阈值 == 大卡 4 列宽（4×(PICK_MIN+PICK_GAP)−12）",
    !!min && !!gap && !!th && Number(th) === 4 * (Number(min) + Number(gap)) - Number(gap),
    "阈值=" + th + "px PICK_MIN=" + min + " PICK_GAP=" + gap);
})();
$$("#nav .nav-item")[0].click();
const firstBatch = $$("#rows .row").length;
check("列表页首批 = 每批 30 条", firstBatch === 30, firstBatch + " 行");
// 2026-10-08 问题1：all.js 卡片瘦身后，列表页（数据来自 all.js）的卡片**没有** steam_url/
// xiaoheihe_url/banner，链接与封面必须由 appid/game_id+art 现拼成功 —— 不然瘦身=毁页面。
// 无 appid 的条目（unlisted/详情待补）本就没有链接，故断言「至少一行有链接」+「凡出现必合规」。
(function () {
  const rows = $$("#rows .row");
  if (!rows.length) return;
  const steamA = rows.map((r) => r.querySelector('.icon-link[aria-label="Steam 商店页"]')).filter(Boolean);
  const xhhA = rows.map((r) => r.querySelector('.icon-link[aria-label="小黑盒"]')).filter(Boolean);
  const imgs = rows.map((r) => r.querySelector(".row-thumb")).filter((t) => t && t.tagName === "IMG");
  const ok = steamA.length > 0 && xhhA.length > 0
    && steamA.every((a) => /^https:\/\/store\.steampowered\.com\/app\/\d+\/$/.test(a.href))
    && xhhA.every((a) => /^https:\/\/www\.xiaoheihe\.cn\/games\/detail\/\d+$/.test(a.href))
    && imgs.every((t) => /^https:\/\/assets\.isthereanydeal\.com\/[0-9a-f-]+\/boxart\.(jpg|png)$/.test(t.src));
  check("列表页链接/封面由 appid/game_id 现拼（瘦身后）", ok,
    "steam " + steamA.length + " · xhh " + xhhA.length + " · img " + imgs.length
      + " · 样例 " + (steamA[0] ? steamA[0].href : "(无)"));
})();
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
// 节日条 3 项**条件执行**（2026-10-08）：announcements.json 不覆盖今天就根本没有
// 节日条 —— 以前无条件下会吃成 3 个假失败。有节日条才验，没有就说明跳过。
if (festBox) {
  check("节日条带季节主题类名 + 活动名",
    /season-(spring|summer|autumn|winter)/.test(festBox.className), festBox.className);
  check("节日条写明起止区间与剩余天数",
    /\d{2}-\d{2} \d{2}:\d{2}.*\d{2}-\d{2} \d{2}:\d{2}/.test(festBox.textContent) &&
    /还有|今天结束/.test(festBox.textContent),
    festBox.textContent.replace(/\s+/g, " ").trim());
  check("节日条与顶栏正文左对齐（内容套了 .wrap）",
    !!festBox.querySelector(".wrap.msg-inner"),
    festBox.querySelector(".msg-inner") ? "有 .wrap.msg-inner" : "没有");
} else {
  check("节日条：当前数据无节日（announcements.json 未覆盖今天），3 项跳过", true, "skip");
}
// ⚠️ 这条原先是直接断言**真实产物**里的 `alertBox.hidden`（即「刚跑过、不该告警」）——
//    但产物的 `generated_at` 是渲染那一刻，它随墙上时钟变旧：本地预览数据放满 26 小时
//    之后这条就会亮告警 → 变成**时间敏感的假失败**（2026-10-07 21:5x 踩到过一次）。
//    现在和下面两条一样用受控数据（generated_at = 1 小时前），测的是同一个行为但不吃时钟。
const freshDoc = buildDom((o) => { o.generated_at = hoursAgo(1); }).window.document;
check("刚更新过的数据不显示陈旧告警", freshDoc.getElementById("msg-alert").hidden);

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

// ② 单页大卡（2026-10-08：默认上限 15 张后，真实产物恒为多页）——
//    用受控数据把大卡切回 5 张，验证「单页时翻页整块收起 + 副标题不带页码」的分支。
const singleDoc = buildDom((o) => { o.picks = (o.picks || []).slice(0, 5); }).window.document;
check("单页大卡（受控 5 张）：翻页整块收起 + 副标题不带页码",
  !!singleDoc.querySelector("#picks .deck-nav[hidden]") &&
  !/第\s*\d+\s*\/\s*\d+\s*页/.test(singleDoc.getElementById("picks-sub").textContent),
  singleDoc.getElementById("picks-sub").textContent);

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
// src 带 ?v=（2026-10-08 起，防 Pages 缓存），用前缀匹配
const loadScript = loadingDom.window.document.querySelector('script[src^="all.js"]');
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
  rateEls.every((e) => /rate-(high|ok|mid|low)/.test(e.className)),
  rateEls.length ? rateEls[0].className + " → " + rateEls[0].textContent : "(无)");
check("剩余天数带分档类名",
  daysEls.length > 0 && daysEls.every((e) => /days-(final|urgent|soon|later)/.test(e.className)),
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
  const style = doc.getElementById("picks-track").getAttribute("style") || "";
  const cm = style.match(/repeat\((\d+),/);
  const cols = cm ? Number(cm[1]) : 0;
  const sub = doc.getElementById("picks-sub").textContent;
  const n = $$("#picks-track .pick").length;
  const total = ((dom.window.REPORT_DATA || {}).picks || []).length;
  // jsdom 没有布局引擎 → clientWidth 恒 0，走「按视口宽度估列数」兜底（1024 → 5 列）。
  // track 上摆的是**当前页**的 cols 张；多页时副标题「第 x/y 页」的 y = ceil(总张数/列数)。
  if (!cols || !total) return false;
  const pg = sub.match(/第\s*(\d+)\s*\/\s*(\d+)\s*页/);
  if (total > cols) {
    return n === cols && !!pg &&
      Number(pg[2]) === Math.ceil(total / cols) && Number(pg[1]) === 1;
  }
  return n === total && n <= cols && !pg;
})(), doc.getElementById("picks-sub").textContent);

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
