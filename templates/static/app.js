/* 报表前端（S9，2026-10-06）：gg.deals 式首页
 * ================================================================
 * 骨架：吸顶导航 → 筛选胶囊行 → 首页（顶部大卡横排 + 四板块两栏行列表）
 *       → 点导航进「板块完整列表页」（数据来自 all.js 懒加载）
 *
 * 数据来源：
 *   window.REPORT_DATA（data.js）—— 当日新增、四板块预览（各 10 条）、
 *                                    顶部大卡候选 15 张、断点/分页配置
 *   window.ALL_DATA（all.js，首次点导航时 <script> 懒加载）—— 全量卡片，
 *                                    每张带 views（视图成员）与 sections（板块归属）
 *
 * ⚠️ 用 <script> 动态加载而不是 fetch：本地 file:// 直开时 fetch 会被 CORS 拦，
 *    <script> 不受限制（重构 S5 的既有结论，别改回去）。
 * ⚠️ 旧版的分组/卡片网格/视图切换已随 S9 退场 —— 首页四板块 + 行列表是新的骨架。
 */
(function () {
  "use strict";

  var data = window.REPORT_DATA;
  if (!data) return;

  // 断点必须与 app.css 的 @media 用同一个值（payload 里下发，别写死第二份）
  var listCfg = data.list || {};
  var breakpoint = listCfg.breakpoint || 768;
  var mq = window.matchMedia("(max-width: " + breakpoint + "px)");
  // 列表加载参数**从配置来**（payload 的 list 段，见 report.render）——
  // 不在前端再写死一份阈值（review-s9-01 确立的仓库约定）。
  var LIST_BATCH = listCfg.batch || 30;        // 每批追加几条（refs.md §9.2「20~30 条」）
  var LIST_AUTO_MAX = listCfg.auto_max || 300; // 自动追加的总上限（「不做无限追加」）
  var TOP_SHOW_AT = 400;                       // 滚动多少像素后显示「返回顶部」
  var SCROLL_AHEAD = "200px";                  // 提前多远就开始加载下一批

  var ICONS = {
    steam: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11.979 0C5.678 0 .511 4.86.022 11.037l6.432 2.658c.545-.371 1.203-.59 1.912-.59.063 0 .125.004.188.006l2.861-4.142V8.91c0-2.495 2.028-4.524 4.524-4.524 2.494 0 4.524 2.031 4.524 4.527s-2.03 4.525-4.524 4.525h-.105l-4.076 2.911c0 .052.004.105.004.159 0 1.875-1.515 3.396-3.39 3.396-1.635 0-3.016-1.173-3.331-2.727L.436 15.27C1.862 20.307 6.486 24 11.979 24c6.627 0 11.999-5.373 11.999-12S18.605 0 11.979 0zM7.54 18.21l-1.473-.61c.262.543.714.999 1.314 1.25 1.297.539 2.793-.076 3.332-1.375.263-.63.264-1.319.005-1.949s-.75-1.121-1.377-1.383c-.624-.26-1.29-.249-1.878-.03l1.523.63c.956.4 1.409 1.5 1.009 2.455-.397.957-1.497 1.41-2.454 1.012H7.54zm11.415-9.303c0-1.662-1.353-3.015-3.015-3.015-1.665 0-3.015 1.353-3.015 3.015 0 1.665 1.35 3.015 3.015 3.015 1.663 0 3.015-1.35 3.015-3.015zm-5.273-.005c0-1.252 1.013-2.266 2.265-2.266 1.249 0 2.266 1.014 2.266 2.266 0 1.251-1.017 2.265-2.266 2.265-1.253 0-2.265-1.014-2.265-2.265z"/></svg>',
    // ⚠️ viewBox 必须留 **0 0 24 24**，**别再裁到墨迹范围**（曾裁成 "2.3 0 19.4 24"，已修）——
    //    裁到墨迹（x 2.3~21.7）会让图形正好顶到 svg 元素边缘；SVG 默认 overflow:hidden，
    //    在 16px 这种小尺寸 + 小数宽度下，**左右各被切掉约 0.45px**（实测：裁过的墨迹只剩
    //    12.0px 宽，本应 12.9px）→ 右边那条竖直边看着「被砍了一截」（用户 2026-10-07 发现）。
    //    留 24×24 有 2.3 的天然留白 → 图形不贴边、渲染完整。
    //    两枚图标的对齐由「固定宽度的图标列 + 列内居中」保证（见 app.css --icon-col），不靠裁 viewBox。
    heihe: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10.1 0 L21.7 6.6 V17.6 L17.7 19.9 V8.8 L13.9 6.6 V10 H10.1 Z"/><path d="M2.3 6.4 L6.3 4.2 V15.2 L10.1 17.4 V14 H13.9 V24 L2.3 17.4 Z"/></svg>'
  };

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        if (key === "class") node.className = attrs[key];
        else if (key === "text") node.textContent = attrs[key];
        else if (key.indexOf("on") === 0) node.addEventListener(key.slice(2), attrs[key]);
        else if (attrs[key] !== null && attrs[key] !== undefined) node.setAttribute(key, attrs[key]);
      });
    }
    (children || []).forEach(function (child) { if (child) node.appendChild(child); });
    return node;
  }

  // ------------------------------------------------------------------
  // 空状态插画（refs.md B13「没有、加载中、失败都可以加一个小插画，但要符合
  // 网站现有的色彩主题」）—— 自绘 SVG 常量：不引图片文件（页面要能离线双击打开）。
  // 配色只用站点现有色：新史低红 #d92b2b / 主色蓝 #2563eb / 灰 #c9cfd8 · #e3e5e9。
  // ------------------------------------------------------------------
  var EMPTY_ART = {
    // 无内容：卡片框 + 往下探的箭头 + 底部一条红线（呼应站点名那个「价格探底」标记）
    none: '<svg class="empty-art" viewBox="0 0 120 120" aria-hidden="true">'
      + '<rect x="24" y="30" width="72" height="54" rx="9" fill="none" stroke="#c9cfd8"'
      + ' stroke-width="3" stroke-dasharray="8 6"/>'
      + '<path d="M60 42v24" fill="none" stroke="#2563eb" stroke-width="4" stroke-linecap="round"/>'
      + '<path d="M50 58l10 10 10-10" fill="none" stroke="#2563eb" stroke-width="4"'
      + ' stroke-linecap="round" stroke-linejoin="round"/>'
      + '<rect x="30" y="94" width="60" height="5" rx="2.5" fill="#d92b2b" opacity=".5"/></svg>',
    // 加载中：转圈（旋转交给 CSS 的 .is-spin —— 老安卓 WebView 对 SVG 动画支持不稳）
    loading: '<svg class="empty-art is-spin" viewBox="0 0 120 120" aria-hidden="true">'
      + '<circle cx="60" cy="60" r="30" fill="none" stroke="#e3e5e9" stroke-width="9"/>'
      + '<path d="M60 30a30 30 0 0 1 30 30" fill="none" stroke="#2563eb" stroke-width="9"'
      + ' stroke-linecap="round"/></svg>',
    // 加载失败：虚线圈 + 感叹号（红 = 出错，与站点的新史低红同色，不另造一个红）
    fail: '<svg class="empty-art" viewBox="0 0 120 120" aria-hidden="true">'
      + '<circle cx="60" cy="58" r="29" fill="none" stroke="#d92b2b" stroke-width="3.5"'
      + ' stroke-dasharray="9 7" opacity=".85"/>'
      + '<path d="M60 41v23" stroke="#d92b2b" stroke-width="5" stroke-linecap="round"/>'
      + '<circle cx="60" cy="74" r="3.6" fill="#d92b2b"/></svg>'
  };

  var emptyBox = document.getElementById("empty");

  function hideEmpty() { if (emptyBox) emptyBox.hidden = true; }

  /** 空状态三态。``onRetry`` 给了就多一个「重试」按钮（只有「加载失败」用）。 */
  function setEmpty(kind, text, onRetry) {
    if (!emptyBox) return;
    emptyBox.textContent = "";
    var holder = el("div", {});
    holder.innerHTML = EMPTY_ART[kind] || EMPTY_ART.none;   // 常量字符串，无用户输入
    if (holder.firstChild) emptyBox.appendChild(holder.firstChild);
    emptyBox.appendChild(el("p", { class: "empty-text", text: text }));
    if (onRetry) {
      emptyBox.appendChild(el("button", { type: "button", class: "empty-retry",
                                         text: "重试", onclick: onRetry }));
    }
    emptyBox.hidden = false;
  }

  var state = { section: null, limit: LIST_BATCH, filters: {} };
  // 默认值由服务端下发（跟着 home_new_low_days 走）。
  // ⚠️ 兜底**不要再写 date: "d7"** —— 那等于在前端又写死一份窗口，与服务端脱钩
  // （review-s9-01 补充审查 #2）。模板恒发这个变量，取不到就当"全部不筛"，
  // 下面的判据都对 undefined 安全。
  var FILTER_DEFAULTS = window.FILTER_DEFAULTS || {};
  Object.keys(FILTER_DEFAULTS).forEach(function (k) {
    state.filters[k] = FILTER_DEFAULTS[k];
  });
  var SECTION_LABEL = { __all__: "全部折扣" };
  (data.sections || []).forEach(function (s) { SECTION_LABEL[s.key] = s.label; });

  // reviews:{score,count} 是总结伴出现的一组字段（没有详情时整个为 null）——
  // 取值统一走这两个函数，别在各处写 `((x.reviews || {}).count) || 0`（容易漏一层）
  function reviewCount(item) { return ((item.reviews || {}).count) || 0; }
  function reviewScore(item) { return ((item.reviews || {}).score) || 0; }

  function displayTitle(item) { return item.title_zh || item.title || "(无标题)"; }
  function enTitle(item) {
    return (item.title_zh && item.title && item.title_zh !== item.title) ? item.title : "";
  }
  function daysText(item) {
    var d = item.days_left;
    if (d === null || d === undefined) return "";
    return d <= 0 ? "今天结束" : "剩 " + d + " 天";
  }
  function lowClassOf(item) {
    // ⚠️ 这里必须**显式写出三个取值**（含 "unknown"）：check_payload 会核对
    // app.js 里出现过的 low_class 字面量是否与 classify 常量集一致 ——
    // 漏一个就是「Python 改了名、前端静默失配」，不会报错，只是标签和色条全错。
    var lc = item.low_class || "unknown";
    if (lc === "new") return "l-new";
    if (lc === "tie") return "l-tie";
    return "l-unk";                 // unknown：storeLow 缺失 → 灰虚线色条
  }

  /** 子信息行：好评率 · 评价数 · 剩余天数。
   *
   * S9-卡片（用户 2026-10-07）：「60 好评和 90 好评是一个颜色、剩余 7 天和 2 天也是一个颜色」
   * → 两个数值都要分档上色。**档位由服务端给**（`rate_tier` / `days_tier`，阈值在
   * report.py，跟着配置走），前端只挂类名，不在这里写第二份阈值。
   * 文案也在前端拼（服务端原先拼好的 `reviews_text` 已删 —— 要给「94%」单独上色就得拆开）。
   */
  function subLine(item) {
    var box = el("div", { class: "row-sub" });
    var parts = [];
    var rv = item.reviews;
    if (rv && rv.count) {
      parts.push(el("span", { class: "rate" + (item.rate_tier ? " rate-" + item.rate_tier : ""),
                              text: (rv.score === null || rv.score === undefined ? "—" : rv.score + "%") }));
      parts.push(el("span", { text: rv.count.toLocaleString("en-US") + " 条" }));
    } else {
      parts.push(el("span", { text: "详情待补" }));
    }
    var dl = daysText(item);
    if (dl) {
      parts.push(el("span", { class: "days" + (item.days_tier ? " days-" + item.days_tier : ""),
                              text: dl }));
    }
    parts.forEach(function (node, i) {
      if (i) box.appendChild(document.createTextNode(" · "));
      box.appendChild(node);
    });
    return box;
  }

  // ------------------------------------------------------------------
  // 行（一个游戏一行）
  // ------------------------------------------------------------------
  function iconLink(href, label, svg) {
    if (!href) return null;
    var a = el("a", { class: "icon-link", href: href, target: "_blank", rel: "noopener" });
    a.setAttribute("aria-label", label);
    a.title = label;
    a.innerHTML = svg;               // 常量字符串，无用户输入
    a.addEventListener("click", function (e) { e.stopPropagation(); });
    return a;
  }

  function detailRow(label, value) {
    if (!value) return null;
    return el("div", { class: "detail-row" }, [
      el("span", { text: label }), el("b", { text: value })
    ]);
  }

  // 「距上次史低」（§3.6）：主文本就是「N 天」，有具体日期时加虚线下划线标记可交互 ——
  // 电脑悬停（title）看日期，手机点按在「天数 ↔ 日期」之间切换。
  // ⚠️ S9 重写行列表时把这个切换弄丢了（2026-10-07 用户指出），已加回；
  //    别再用 detailRow 直接渲染 last_low_text，那样日期就永远看不到。
  function lastLowRow(text, date) {
    if (!text) return null;
    var b = el("b", { text: text });
    if (!date) return el("div", { class: "detail-row" },
      [el("span", { text: "距上次史低" }), b]);
    b.classList.add("has-alt");
    b.title = date;
    var row = el("div", { class: "detail-row" },
      [el("span", { text: "距上次史低" }), b]);
    b.addEventListener("click", function (e) {
      e.stopPropagation();            // 别触发整行的展开/收起
      var showingDate = b.textContent === date;
      b.textContent = showingDate ? text : date;
      b.title = showingDate ? date : text;   // 悬停永远提示「另一种」
    });
    return row;
  }

  // 折扣力度条（沿用 S9 之前的口径：绿色进度条 + 精确百分比）。
  // ⚠️ S9 重写行列表时一度把它漏掉 —— 见 handoff §4.1，别再删。
  function cutBar(cut) {
    var pct = Math.max(0, Math.min(100, cut || 0));
    var fill = el("i", {});
    fill.style.width = pct + "%";
    return el("span", { class: "cut-wrap" }, [
      el("span", { class: "cut-bar" }, [fill]),
      el("span", { class: "cut-num", text: "-" + (cut || 0) + "%" })
    ]);
  }

  function buildRow(item) {
    var links = el("span", { class: "links" }, [
      iconLink(item.steam_url, "Steam 商店页", ICONS.steam),
      iconLink(item.xiaoheihe_url, "小黑盒", ICONS.heihe)
    ]);
    var tags = el("div", { class: "row-tags" }, [
      el("span", { class: "hl" + (item.low_class === "new" ? " hl-new" : ""),
                   text: item.low_label || "史低" }),
      links,
      cutBar(item.cut)     // 老口径顺序：史低标签 → 图标 → 力度条（力度条自适应铺满）
    ]);

    var thumb = item.banner
      ? el("img", { class: "row-thumb", src: item.banner, alt: "",
                    loading: "lazy", decoding: "async", width: "44", height: "62" })
      : el("div", { class: "row-thumb" });

    var summary = el("div", { class: "row-main" }, [
      thumb,
      el("div", { class: "row-info" }, [
        el("div", { class: "row-title", text: displayTitle(item), title: displayTitle(item) }),
        el("div", { class: "row-en", text: enTitle(item) }),
        subLine(item)                       // 好评率 · 评价数 · 剩余天数（分档上色）
      ]),
      tags,
      el("div", { class: "row-price" }, [
        el("div", { class: "now", text: item.price_text }),
        el("div", { class: "was", text: item.regular_text })
      ])
    ]);

    // 详情（点行展开）：左「距上次史低 / 折扣开始 / 折扣结束」，右「跨区比价」。
    // S9-卡片（2026-10-07）：恒定两栏 —— 之前无比价数据时回落单栏，右半边空着，
    // 用户反馈「展开的布局也没修改好」；现在右栏没有数据就写一行说明，结构对称。
    // 「折扣开始」是批 G 从卡片搬去组头的，S9 之后组头没了，加回详情里（payload 一直有）。
    var left = el("div", { class: "detail-col" }, [
      lastLowRow(item.last_low_text, item.last_low_date),
      detailRow("折扣开始", item.start_text),
      detailRow("折扣结束", item.expiry_text)
    ]);
    var rightRows = (item.compare || []).map(function (row) {
      // 原币价 + ≈¥ 换算包成一段 nowrap：窄栏里宁可让差价百分比换行，也别把价格拆开
      var b = el("b", {}, [el("span", {
        class: "nowrap",
        text: row.price_text + (row.cny_text ? " " + row.cny_text : "")
      })]);
      if (row.diff_pct !== null && row.diff_pct !== undefined) {
        b.appendChild(document.createTextNode(" "));
        b.appendChild(el("span", {
          class: row.diff_pct < 0 ? "diff-cheap" : row.diff_pct > 0 ? "diff-dear" : "diff-same",
          text: row.diff_pct < 0 ? "-" + Math.abs(row.diff_pct) + "%"
            : row.diff_pct > 0 ? "+" + row.diff_pct + "%" : "±0%"
        }));
      }
      return el("div", { class: "detail-row" }, [el("span", { text: row.label }), b]);
    });
    var right = el("div", { class: "detail-col" }, rightRows.length ? rightRows : [
      el("div", { class: "detail-note", text: "跨区比价：本轮未取到数据" })
    ]);
    var detail = el("div", { class: "row-detail" }, [
      el("div", { class: "detail-cols" }, [left, right])
    ]);

    var row = el("article", { class: "row " + lowClassOf(item) }, [summary, detail]);
    summary.addEventListener("click", function () {
      var was = row.classList.contains("open");
      var open = document.querySelectorAll(".row.open");
      for (var i = 0; i < open.length; i++) open[i].classList.remove("open");
      if (!was) row.classList.add("open");
    });
    return row;
  }

  function fillRows(box, items) {
    box.textContent = "";
    items.forEach(function (item) { box.appendChild(buildRow(item)); });
  }

  // ------------------------------------------------------------------
  // 顶部大卡横排（左右翻页）
  // ------------------------------------------------------------------
  // 目标（refs §11.2 + 用户 2026-10-07 的三条纠正）：
  //   · 一排**最多 5 张**（PC 与平板都是 5）—— 之前 auto-fill 让 1100px 变成 6 张，太多；
  //   · 手机 2 列只出一排（张数随列数，**口径只在 picksPerPage 一份**），
  //     且单张不能太宽 —— 之前 768px 走手机档排成 2 列，单张 347px 大得离谱；
  //   · 列数由**这里算出来并写进 inline style**，CSS 不再自己排 ——
  //     避免「公式算 6 列、CSS 排 4 列 → 一排只填 4/6 右边空一块」那种错位。
  // ⚠️ PICK_MIN 要跟 app.css 里 .pick 的观感一致（约 150px 起才放得下封面+价+力度条）。
  var PICK_MIN = 150;
  var PICK_GAP = 12;
  var PICK_MAX_COLS = 5;

  function pickColumns() {
    var width = picksTrack ? picksTrack.clientWidth : 0;
    if (!width) {
      // 还没布局（首屏前 / jsdom 没有布局引擎）→ 拿容器 .wrap 量一下最准；
      // 连 .wrap 都没有就按视口估个大概（宁可估窄一列，也别算出 0 列）。
      // ⚠️ 这里**不要**去复刻 .wrap 的 max-width/padding 公式 —— 那是第二份口径，
      //    CSS 一改就悄悄失配（review-s9-06 提过）。
      // ⚠️ 要判 clientWidth 是否为 0（jsdom 没有布局引擎：元素在、宽度恒 0），
      //    只判元素是否存在会算出 0 列 → 被下面的兜底压到 2 列，页数跟着错。
      var wrap = document.querySelector("main.wrap");
      width = (wrap && wrap.clientWidth) || Math.round((window.innerWidth || 1024) * 0.85);
    }
    var cols = Math.floor((width + PICK_GAP) / (PICK_MIN + PICK_GAP));
    return Math.max(2, Math.min(PICK_MAX_COLS, cols));
  }

  /** 每页张数 = 列数（2026-10-07 收尾，用户拍板）：原来手机 2 列 × 2 排 = 4 张
      （≈570px）把首屏占满、四板块被推到首屏外 → 手机现在只出一排，板块区能在
      首屏露头；平板/PC 不变。
      ⚠️ 全站「每页几张」的具体数字只在本注释这一份，别在别处（CSS/其他注释）
      再抄一份 —— 本次 review 抓过注释数字散布易漂移。也别把这个恒等函数内联掉：
      文档与注释都按这个名字引用口径。 */
  function picksPerPage(cols) { return cols; }

  var pickPage = 0;
  var picksBox = document.getElementById("picks");
  var picksTrack = document.getElementById("picks-track");
  var picksSub = document.getElementById("picks-sub");
  var picksPrev = document.getElementById("picks-prev");
  var picksNext = document.getElementById("picks-next");

  function pickCard(item, rank) {
    var art = el("div", { class: "pick-art" }, [
      item.banner ? el("img", { src: item.banner, alt: "", loading: "lazy",
                                decoding: "async" }) : null,
      el("span", { class: "pick-rank", text: "#" + rank })
    ]);
    // 信息区照 **gg.deals** 的结构（用户 2026-10-07 给的参照图）：
    //   封面 → 标题（最多两行）→「From: 价格 + 折扣徽章」→「划线原价 + 史低徽章 + 商店图标」
    // 用户明确要的：
    //   · 不再用力度条（窄卡里太短，原话：「大卡的力度条太短了」）；
    //   · 两枚商店图标都保留，分别贴折扣行与价格行的右端 → 竖着对齐成一列。
    // 三排（用户 2026-10-07 定稿；2026-10-07 末统一图标顺序为 Steam 在前）：
    // ① 标题　② 史低类型 + 折扣 + **Steam**　③ 原价 + 现价 + **小黑盒**。
    // 顺序与行卡片一致（行卡片也是 Steam→小黑盒）。
    var lowRow = el("div", { class: "pick-low" }, [
      // 我们的「新史低 / 平史低」= gg.deals 里那个 HL 徽章
      el("span", { class: "hl" + (item.low_class === "new" ? " hl-new" : ""),
                   text: item.low_label || "史低" }),
      el("span", { class: "pick-cut", text: "-" + (item.cut || 0) + "%" }),
      el("span", { class: "links" }, [
        iconLink(item.steam_url, "Steam 商店页", ICONS.steam)
      ])
    ]);
    var priceRow = el("div", { class: "pick-price-row" }, [
      el("span", { class: "pick-was", text: item.regular_text }),
      el("span", { class: "pick-price", text: item.price_text }),
      el("span", { class: "links" }, [
        iconLink(item.xiaoheihe_url, "小黑盒", ICONS.heihe)
      ])
    ]);
    var body = el("div", { class: "pick-body" }, [
      el("div", { class: "pick-title", text: displayTitle(item), title: displayTitle(item) }),
      lowRow,
      priceRow
    ]);
    var card = el("article", { class: "pick" }, [art, body]);
    card.addEventListener("click", function () { openSection("new_low"); });
    return card;
  }

  function renderPicks() {
    var list = data.picks || [];
    if (!list.length) return;
    picksBox.hidden = false;              // 先显示，clientWidth 才量得准
    var cols = pickColumns();
    // 列数**由 JS 写进 inline style** —— 与上面算出来的 cols 是同一个数，
    // 不会出现「CSS 排 4 列、我们按 6 列分页」的错位（上次就是这么翻车的）。
    picksTrack.style.gridTemplateColumns = "repeat(" + cols + ", 1fr)";
    var per = picksPerPage(cols);
    var total = Math.ceil(list.length / per);
    if (pickPage >= total) pickPage = total - 1;
    if (pickPage < 0) pickPage = 0;
    var slice = list.slice(pickPage * per, (pickPage + 1) * per);
    picksTrack.textContent = "";
    slice.forEach(function (item, i) {
      picksTrack.appendChild(pickCard(item, pickPage * per + i + 1));
    });
    picksSub.textContent = "本次折扣里最值得买的 " + list.length + " 款（第 "
      + (pickPage + 1) + "/" + total + " 页）";
    picksPrev.disabled = pickPage <= 0;
    picksNext.disabled = pickPage >= total - 1;
    lastPickCols = cols;
  }

  // 同一档内宽度变化（例：900 → 1100，或拖动窗口）也可能改变列数 → 重排。
  // 用**公式**判（不能用「量当前 DOM」：页内只有几张卡，排不满一排时永远量不出更大列数）。
  var lastPickCols = 0;
  window.addEventListener("resize", function () {
    if (!picksTrack || !picksTrack.children.length) return;
    if (pickColumns() !== lastPickCols) renderPicks();
  }, { passive: true });
  picksPrev.addEventListener("click", function () { pickPage--; renderPicks(); });
  picksNext.addEventListener("click", function () { pickPage++; renderPicks(); });

  // ------------------------------------------------------------------
  // 首页四板块（服务端已给每板块前 10 条 + 完整条数）
  // ------------------------------------------------------------------
  var sectionsBox = document.getElementById("sections");

  function buildSection(sec) {
    var head = el("div", { class: "sec-head" }, [
      el("h2", { class: "sec-title", text: sec.label }),
      el("span", { class: "sec-count", text: sec.count + " 条" }),
      el("button", { type: "button", class: "sec-more", text: "查看更多 ›",
                     onclick: function () { openSection(sec.key); } })
    ]);
    var rows = el("div", { class: "rows" });
    fillRows(rows, sec.items || []);
    return el("section", { class: "section" }, [head, rows]);
  }

  // 两列**按估算高度均衡分配**（组头算 1 行高度，每行算 1）：
  // 之前直接交给 CSS grid 两列，某个板块条数少时它下面会留一大块空白，
  // 把下面那个板块顶得老远（1080 视口收到单列时更明显）。
  function renderSections() {
    var sections = data.sections || [];
    sectionsBox.textContent = "";
    if (!sections.length) { setEmpty("none", "今天没有符合条件的折扣。"); return; }
    var cols = [el("div", { class: "section-col" }), el("div", { class: "section-col" })];
    var heights = [0, 0];
    sections.forEach(function (sec, idx) {
      var i = heights[0] <= heights[1] ? 0 : 1;
      var node = buildSection(sec);
      // 单列（≤900px）时 .section-col 变成 display:contents，这 4 个 section 就成了
      // .sections 的网格项 —— 靠 order 把顺序复原成「新史低→即将到期→热门→大额折扣」。
      // 不复原的话会按分栏结果排成「新史低→热门→即将到期→大额折扣」（review 抓到的回归）。
      if (node.style) node.style.order = idx;
      cols[i].appendChild(node);
      heights[i] += 1 + (sec.items || []).length;
    });
    cols.forEach(function (col) { sectionsBox.appendChild(col); });
    hideEmpty();
  }

  // ------------------------------------------------------------------
  // 板块完整列表页：数据来自 all.js（全量卡片，带 sections 标记）
  // ------------------------------------------------------------------
  var allCache = window.ALL_DATA || null;
  var allRequested = false;
  var allCallbacks = [];

  function loadAll(done) {
    if (!allCache && window.ALL_DATA) allCache = window.ALL_DATA;
    if (allCache) { done(allCache, null); return; }
    allCallbacks.push(done);
    if (allRequested) return;
    allRequested = true;
    var script = document.createElement("script");
    script.src = "all.js";
    script.onload = function () {
      var cbs = allCallbacks.splice(0);
      allCache = window.ALL_DATA || null;
      if (!allCache) allRequested = false;   // 数据坏了允许重试，别把回调晾死
      cbs.forEach(function (cb) { cb(allCache, allCache ? null : new Error("all.js 数据为空")); });
    };
    script.onerror = function () {
      allRequested = false;
      var cbs = allCallbacks.splice(0);
      cbs.forEach(function (cb) { cb(null, new Error("all.js 加载失败")); });
    };
    document.head.appendChild(script);
  }

  function allCards() {
    var out = [];
    ((allCache && allCache.groups) || []).forEach(function (g) {
      (g.items || []).forEach(function (c) { out.push(c); });
    });
    return out;
  }

  // ---- 筛选的三个判据（底部抽屉，refs.md §6.7）----
  // 日期值的协议（与 report.filter_specs 的 docstring 同一份，改格式两边必须同步）：
  //   "dN" = 近 N 天 · "N" = 距今第 N 天（0=今天）· "all" = 不限
  function parseDateSpec(value) {
    if (!value || value === "all") return { kind: "all", n: 0 };
    if (value.charAt(0) === "d") return { kind: "days", n: parseInt(value.slice(1), 10) };
    return { kind: "exact", n: parseInt(value, 10) };
  }

  function dateOk(card) {
    var spec = parseDateSpec(state.filters.date);
    if (spec.kind === "all") return true;
    var ago = card.start_days_ago;
    if (ago === null || ago === undefined) return false;   // 没有开始时间 → 不算
    if (isNaN(spec.n)) return true;                        // 值坏了就当不限，别把列表清空
    return spec.kind === "days" ? ago <= spec.n : ago === spec.n;
  }

  // 折扣还在不在期内 —— 服务端筛首页板块时会看（``_is_live``），前端筛列表页也得看，
  // 否则「板块头写的条数」和「查看更多点进去的条数」会对不上（review 补充审查 #3）。
  // ⚠️ 只对四个板块生效；「全部折扣」本来就是要看全量（含过期留存）。
  function liveOk(card) {
    var views = card.views;
    return !views || views.indexOf("active") !== -1;
  }

  function filterOk(card) {
    var f = state.filters;
    if (f.only_new === "new" && card.low_class !== "new") return false;
    if (f.cut !== "all" && f.cut !== undefined
        && (card.cut || 0) < parseInt(f.cut, 10)) return false;
    if (f.reviews !== "all" && f.reviews !== undefined
        && reviewCount(card) < parseInt(f.reviews, 10)) return false;
    return true;
  }

  function sortCards(cards) {
    var mode = state.filters.sort;
    var arr = cards.slice();
    if (mode === "cut") {
      arr.sort(function (a, b) { return (b.cut || 0) - (a.cut || 0); });
    } else if (mode === "price") {
      arr.sort(function (a, b) {
        var pa = (a.price_int === null || a.price_int === undefined) ? 1e9 : a.price_int;
        var pb = (b.price_int === null || b.price_int === undefined) ? 1e9 : b.price_int;
        return pa - pb;
      });
    } else if (mode === "rate") {
      arr.sort(function (a, b) { return reviewScore(b) - reviewScore(a); });
    }
    // featured：保持服务端给的顺序（板块自己的排序），不在这里重排
    return arr;
  }

  /** 板块列表页的顺序：**服务端下发**（all.js 的 `section_order`），前端不复制排序规则。
   *  ⚠️ 首页四板块的顺序是各板块自己一套（新史低→折扣→评价数、热门→评价数…），
   *  而 all.js 的分组顺序是「tier 分组 + 折扣降序」，两者不一样 ——
   *  不按 section_order 排的话，点「查看更多」进去看到的顺序会和首页预览不一致。 */
  function applySectionOrder(cards, key) {
    var order = (allCache && allCache.section_order || {})[key];
    if (!order) return cards;
    var pos = {};
    order.forEach(function (appid, i) { pos[appid] = i; });
    return cards.slice().sort(function (a, b) {
      var pa = pos[a.appid], pb = pos[b.appid];
      return (pa === undefined ? 1e9 : pa) - (pb === undefined ? 1e9 : pb);
    });
  }

  function cardsFor(key) {
    var out = allCards().filter(function (c) {
      if (key !== "__all__") {
        if ((c.sections || []).indexOf(key) === -1) return false;
        // 「即将到期」本来就是按**到期时间**筛的，不再叠加用户选的日期窗口
        // （refs.md §11.3：叠加会把最紧急的老折扣漏掉）
        if (key === "expiring") return filterOk(c) && liveOk(c);
        return dateOk(c) && filterOk(c) && liveOk(c);
      }
      return dateOk(c) && filterOk(c);
    });
    // 「精选」= 服务端给的板块顺序；换成折扣/价格/好评排序时才由前端重排
    if (state.filters.sort === "featured" || state.filters.sort === undefined) {
      return applySectionOrder(out, key);
    }
    return sortCards(out);
  }

  var homeBox = document.getElementById("home");
  var listBox = document.getElementById("listview");
  var rowsBox = document.getElementById("rows");
  var pagerBox = document.getElementById("lv-pager");

  // 滚动加载（refs.md B12 / §9.2）：**只在板块列表页**做，首页不做（"首页不可滑动"）。
  // 每批追加 LIST_BATCH 条；滑到底自动追加，底部按钮同时是手动兜底。
  // 不做虚拟列表 —— 一次最多把该板块全部渲染出来（板块量级几百到一千出头）。
  function renderList(cards) {
    var shown = cards.slice(0, state.limit);
    fillRows(rowsBox, shown);
    pagerBox.textContent = "";
    var left = cards.length - shown.length;
    if (left <= 0) {
      if (cards.length > LIST_BATCH) {
        pagerBox.appendChild(el("div", { class: "pager" }, [
          el("span", { text: "已全部加载 " + cards.length + " 条" })]));
      }
      if (moreObserver) { moreObserver.disconnect(); moreObserver = null; }
      return;
    }
    // 到「自动追加上限」就停止自动追加，只留手动 —— refs.md §9.2「不做无限追加」。
    // 否则「全部折扣」页（7000+ 条）会一直往下接，正是用户担心的「根本滑不到底」。
    var auto = state.limit < LIST_AUTO_MAX;
    var more = el("button", { type: "button", class: "load-more",
      text: (auto ? "加载更多" : "继续加载") + "（还有 " + left + " 条）" });
    more.addEventListener("click", function () {
      state.limit += LIST_BATCH;
      renderList(cards);
    });
    pagerBox.appendChild(el("div", { class: "pager" }, [
      auto ? null : el("span", {
        text: "已自动显示前 " + shown.length + " 条（共 " + cards.length
              + " 条）· 建议用上方「筛选」缩小范围 · " }),
      more
    ]));
    if (auto) observeMore(more);
  }

  // 观察「加载更多」按钮：进视口就自动点它 = 滚到底追加一批。
  // ⚠️ jsdom / 老浏览器没有 IntersectionObserver —— 直接跳过，按钮本身就是兜底。
  var moreObserver = null;
  function observeMore(button) {
    if (typeof window.IntersectionObserver !== "function") return;
    if (moreObserver) moreObserver.disconnect();
    moreObserver = new window.IntersectionObserver(function (entries) {
      for (var i = 0; i < entries.length; i++) {
        if (entries[i].isIntersecting) { moreObserver.disconnect(); button.click(); return; }
      }
    }, { rootMargin: SCROLL_AHEAD });
    moreObserver.observe(button);
  }

  // 用户有没有**手动**改过日期。没有的话，切到「全部折扣」页时日期自动放开成
  // 「全部」（refs §11.5 Q2：「全部折扣」页**不设限**），切回板块再收成默认窗口。
  var dateTouched = false;

  function openSection(key) {
    state.section = key;
    state.limit = LIST_BATCH;
    if (!dateTouched) {
      state.filters.date = (key === "__all__") ? "all" : FILTER_DEFAULTS.date;
    }
    var buttons = document.querySelectorAll("#nav .nav-item");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].classList.toggle("active",
        buttons[i].getAttribute("data-section") === key);
    }
    homeBox.hidden = true;
    listBox.hidden = false;
    // 换板块/改筛选后回到顶部 —— 原来是「翻页后回顶」，改成追加加载后别把这条丢了
    window.scrollTo({ top: 0 });
    syncFilterVisibility();          // 板块页才显示「筛选」按钮
    document.getElementById("lv-title").textContent = SECTION_LABEL[key] || key;
    document.getElementById("lv-count").textContent = "加载中…";
    rowsBox.textContent = "";
    pagerBox.textContent = "";
    setEmpty("loading", "正在加载折扣数据…");
    loadAll(function (_json, err) {
      if (err) {
        document.getElementById("lv-count").textContent = "";
        setEmpty("fail",
          "全部数据加载失败（" + ((err && err.message) || "网络错误") + "），可以点下面的按钮重试。",
          function () { openSection(state.section); });
        return;
      }
      var cards = cardsFor(state.section);
      document.getElementById("lv-count").textContent = cards.length + " 条";
      if (cards.length) hideEmpty();
      else setEmpty("none", "这个板块暂时没有符合条件的折扣。");
      renderList(cards);
    });
  }

  function openHome() {
    state.section = null;
    state.limit = LIST_BATCH;
    var buttons = document.querySelectorAll("#nav .nav-item");
    for (var i = 0; i < buttons.length; i++) buttons[i].classList.remove("active");
    listBox.hidden = true;
    homeBox.hidden = false;
    syncFilterVisibility();          // 首页不显示「筛选」（它不作用于首页板块）
    if ((data.sections || []).length) hideEmpty();
    else setEmpty("none", "今天没有符合条件的折扣。");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  // ------------------------------------------------------------------
  // 事件绑定
  // ------------------------------------------------------------------

  // 手机端导航折叠（refs.md B1）：汉堡按钮 → 竖排浮层。
  // 电脑上按钮 display:none、导航本体始终可见，这段逻辑留着也没有副作用。
  var navEl = document.getElementById("nav");
  var navToggleEl = document.getElementById("nav-toggle");
  function setNavOpen(open) {
    if (!navEl || !navToggleEl) return;
    navEl.classList.toggle("open", open);
    navToggleEl.setAttribute("aria-expanded", open ? "true" : "false");
    navToggleEl.setAttribute("aria-label", open ? "收起分类导航" : "展开分类导航");
  }
  function navIsOpen() { return !!(navEl && navEl.classList.contains("open")); }
  if (navToggleEl) {
    navToggleEl.addEventListener("click", function (e) {
      e.stopPropagation();               // 别让下面那个"点空白收起"立刻把它关掉
      setNavOpen(!navIsOpen());
    });
    // 点浮层以外的任何地方、或按 Esc，都收起（手机上最常用的两种关法）
    document.addEventListener("click", function (e) {
      if (!navIsOpen()) return;
      if ((navEl && navEl.contains(e.target)) || navToggleEl.contains(e.target)) return;
      setNavOpen(false);
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" || e.keyCode === 27) setNavOpen(false);
    });
  }

  document.getElementById("brand").addEventListener("click", function (e) {
    e.preventDefault();
    setNavOpen(false);
    openHome();
  });
  var navButtons = document.querySelectorAll("#nav .nav-item");
  for (var n = 0; n < navButtons.length; n++) {
    (function (btn) {
      btn.addEventListener("click", function () {
        setNavOpen(false);               // 选完就收起，别挡着列表
        openSection(btn.getAttribute("data-section"));
      });
    })(navButtons[n]);
  }
  // ---- 底部抽屉筛选（refs.md §6.7）----
  // 手机上从底部滑出，电脑上同一个面板改成右侧浮层（CSS 切换，逻辑不分叉）。
  var drawer = document.getElementById("drawer");
  var drawerMask = document.getElementById("drawer-mask");
  var filterOpenBtn = document.getElementById("filter-open");
  var filterBadge = document.getElementById("filter-badge");
  var filterOpts = document.querySelectorAll("#drawer .chip.opt");

  /** 「已选 N 项」：**排序不计入** —— 排序不是筛选，改个排序就点亮角标是名不副实
      （review-s9-01 补充审查 #6）。 */
  function activeFilterCount() {
    var n = 0;
    Object.keys(FILTER_DEFAULTS).forEach(function (k) {
      if (k === "sort") return;
      if (state.filters[k] !== FILTER_DEFAULTS[k]) n++;
    });
    return n;
  }

  function syncFilterUI() {
    for (var i = 0; i < filterOpts.length; i++) {
      var opt = filterOpts[i];
      opt.classList.toggle("active",
        state.filters[opt.getAttribute("data-group")] === opt.getAttribute("data-value"));
    }
    var n = activeFilterCount();
    filterBadge.hidden = n === 0;
    filterBadge.textContent = n;
  }

  function setDrawer(open) {
    drawer.hidden = !open;
    drawerMask.hidden = !open;
    filterOpenBtn.setAttribute("aria-expanded", open ? "true" : "false");
  }

  /** 筛选只作用于**板块完整列表页** —— 首页四板块是服务端算好的预览，
      不经过筛选项。所以首页要把「筛选」按钮藏起来，不能"看得见、点了没反应"
      （review-s9-01 补充审查 Spec (c)）。 */
  function syncFilterVisibility() {
    if (filterOpenBtn) filterOpenBtn.hidden = !state.section;
    if (!state.section) setDrawer(false);
  }

  function applyFilters() {
    syncFilterUI();
    if (state.section) { state.limit = LIST_BATCH; openSection(state.section); }
  }

  if (drawer && filterOpenBtn) {
    filterOpenBtn.addEventListener("click", function () { setDrawer(true); });
    document.getElementById("drawer-close").addEventListener("click", function () { setDrawer(false); });
    drawerMask.addEventListener("click", function () { setDrawer(false); });
    document.getElementById("filter-reset").addEventListener("click", function () {
      Object.keys(FILTER_DEFAULTS).forEach(function (k) { state.filters[k] = FILTER_DEFAULTS[k]; });
      dateTouched = false;       // 重置后「全部折扣」页重新自动放开日期
      applyFilters();
    });
    for (var f = 0; f < filterOpts.length; f++) {
      (function (opt) {
        opt.addEventListener("click", function () {
          var group = opt.getAttribute("data-group");
          state.filters[group] = opt.getAttribute("data-value");
          if (group === "date") dateTouched = true;
          applyFilters();        // 选了就立刻生效，不用再点「完成」
        });
      })(filterOpts[f]);
    }
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" || e.keyCode === 27) setDrawer(false);
    });
    syncFilterUI();
    syncFilterVisibility();
  }

  // ------------------------------------------------------------------
  // S9-3：浮动按钮 —— 无图精简模式 + 返回顶部（refs.md C6 / C5）
  // ------------------------------------------------------------------
  var COMPACT_KEY = "sdl.compact";
  var btnCompact = document.getElementById("btn-compact");
  var btnTop = document.getElementById("btn-top");

  function setCompact(on, persist) {
    document.body.classList.toggle("compact", on);
    if (!btnCompact) return;
    btnCompact.classList.add("show");          // 模式开关常驻，不随滚动隐藏
    btnCompact.classList.toggle("on", on);
    btnCompact.setAttribute("aria-pressed", on ? "true" : "false");
    if (!persist) return;                      // 初始化时只应用读到的值，不反写
    try { window.localStorage.setItem(COMPACT_KEY, on ? "1" : "0"); }
    catch (e) { /* file:// 下可能被禁，忽略 */ }
  }

  if (btnCompact) {
    var saved = false;
    try { saved = window.localStorage.getItem(COMPACT_KEY) === "1"; } catch (e) { saved = false; }
    setCompact(saved, false);                  // 用存下来的值初始化，但不回写
    btnCompact.addEventListener("click", function () {
      setCompact(!document.body.classList.contains("compact"), true);
    });
  }
  if (btnTop) {
    btnTop.addEventListener("click", function () {
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
    window.addEventListener("scroll", function () {
      btnTop.classList.toggle("show", window.pageYOffset > TOP_SHOW_AT);
    }, { passive: true });
  }

  // ------------------------------------------------------------------
  // 初始化
  // ------------------------------------------------------------------
  renderPicks();
  renderSections();
  // 「点卡片能点开」不做一次性气泡提示了（2026-10-07 用户裁定：悬停有呼吸/上浮
  // 效果已经能表达可点，气泡多此一举）—— refs.md §6.7 B11 的招 3 整条作废。

  function onBreakpointChange() {
    renderPicks();                     // 每页张数随断点变（口径只在 picksPerPage）
    if (state.section) { state.limit = LIST_BATCH; openSection(state.section); }
  }
  if (mq.addEventListener) mq.addEventListener("change", onBreakpointChange);
  else if (mq.addListener) mq.addListener(onBreakpointChange);

  // ------------------------------------------------------------------
  // S9-3：顶部消息区第二行（refs.md A-3 / B2）
  //   最多一行，优先级：**数据陈旧告警 > 站点通知**（用户 2026-10-07 拍板 ——
  //   数据可不可信比公告更要紧，节日期间也不能把告警压掉）。
  //   告警两档：>26h 黄（Actions 延迟）/>36h 红（今天压根没更新）。
  //   阈值两档都从 payload 来，不在前端写死（review-s9-01 确立的约定）。
  //   ⚠️ 这件事只能在浏览器里算：渲染时刻 ≠ 访客打开时刻，服务端算不了。
  // ------------------------------------------------------------------
  var msgbar = document.getElementById("msgbar");
  var alertBox = document.getElementById("msg-alert");

  /** 画第二行并顺带把整块消息区显示出来 —— 告警与通知走同一条路，
      别再各写一遍「设类名 / 设文案 / 取消 hidden」。 */
  function paintAlert(cls, text) {
    if (!alertBox) return;
    alertBox.className = "msg msg-alert " + cls;
    alertBox.textContent = text;
    alertBox.hidden = false;
    if (msgbar) msgbar.hidden = false;
  }

  function renderAlert() {
    if (!alertBox) return;
    var warnHours = data.stale_warn_hours || 26;
    var redHours = data.stale_banner_hours || 36;
    var hours = (Date.now() - new Date(data.generated_at).getTime()) / 3600000;
    if (hours > redHours) {
      paintAlert("is-stale", "数据已 " + Math.round(hours) + " 小时没更新，今天可能没抓到 —— "
        + "页面上的折扣未必还是这个价。");
      return;
    }
    if (hours > warnHours) {
      paintAlert("is-warn", "数据已 " + Math.round(hours) + " 小时没更新（Actions 延迟），"
        + "可能不是最新的。");
      return;
    }
    // 告警不成立时才轮到站点通知（模板已把文案写在 data-notice 上）
    var notice = alertBox.getAttribute("data-notice");
    if (!notice) return;
    paintAlert("is-notice", notice);
    var url = alertBox.getAttribute("data-notice-url");
    if (url) {
      alertBox.appendChild(document.createTextNode(" "));
      alertBox.appendChild(el("a", { href: url, target: "_blank", rel: "noopener", text: "查看" }));
    }
  }

  renderAlert();
})();
