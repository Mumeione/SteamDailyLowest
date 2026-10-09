/* 报表前端（S9，2026-10-06）：gg.deals 式首页
 * ================================================================
 * 骨架：吸顶导航 → 筛选胶囊行 → 首页（顶部大卡横排 + 四板块两栏行列表）
 *       → 点导航进「板块完整列表页」（数据来自分片懒加载）
 *
 * 数据来源：
 *   window.REPORT_DATA（data.js）—— 四板块预览（栏位数 K **随池量动态**，不写死 10）、
 *                                    顶部大卡候选、断点/分页配置
 *                                    （⚠️ 2026-10-08 起不含分组卡片；2026-10-09 起
 *                                     不含 overview / fx / steam / sweep / low_points
 *                                     / filter_defaults —— 概览与汇率是服务端渲染进
 *                                     index/about 的，筛选默认值走模板的
 *                                     window.FILTER_DEFAULTS，前端一个都不读）
 *   window.ALL_S["<板块>_<片号>"]（all/<key>_<n>.js，按需懒加载）—— 板块完整列表，
 *                                    每张带 views（视图成员）与 sections（板块归属）。
 *                                    2026-10-08 起取代原先的单文件 all.js
 *                                    （5.81MB / gzip 729KB，分类页要等 13~18 秒）。
 *
 * ⚠️ 用 <script> 动态加载而不是 fetch：本地 file:// 直开时 fetch 会被 CORS 拦，
 *    <script> 不受限制（重构 S5 的既有结论，别改回去）。
 * ⚠️ 旧版的分组/卡片网格/视图切换已随 S9 退场 —— 首页四板块 + 行列表是新的骨架。
 */
(function () {
  "use strict";

  var data = window.REPORT_DATA;
  if (!data) return;

  // 断点必须与 app.css 的 @media 用同一个值（payload 里下发，别写死第二份）。
  // ⚠️ **刻意不给兜底数字**（2026-10-09，卡片 08）：这里曾写 `|| 768`，与全仓库的
  //    600 矛盾，是个纯粹的僵尸常量 —— 真出现 768 只会让「跨档重排」静默按错的
  //    边界走。取不到就干脆不挂这个行为（见文件末尾的 mq 判空）。
  var listCfg = data.list || {};
  var breakpoint = listCfg.breakpoint;
  var mq = breakpoint ? window.matchMedia("(max-width: " + breakpoint + "px)") : null;
  // 列表加载参数**从配置来**（payload 的 list 段，见 report.render）——
  // 不在前端再写死一份阈值（review-s9-01 确立的仓库约定）。**刻意不给兜底数字**
  // （code-audit-2026-10-09 #18，与上方断点同口径 2026-10-09 卡片 08）：取不到就按
  // 自然降级走 —— batch 缺 → 不分批（全量展示、无分页条）；auto_max 缺 → 只留手动「加载更多」。
  var LIST_BATCH = listCfg.batch;        // 每批追加几条（refs.md §9.2「20~30 条」）
  var LIST_AUTO_MAX = listCfg.auto_max;  // 自动追加的总上限（「不做无限追加」）
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

  /** 读一个 CSS 变量的**计算值**（色板不在这里再抄一份 —— 架构检查卡片 10）。
   *  ⚠️ 读不到用第二个参数兜底：:root 每次都随页面产出（同一份渲染），真出问题
   *     只会是「变量被改名」，那种情况宁可回落一个具体的颜色，也不要 stroke 变成空串
   *     （空串 = 不描边，插画直接消失，比配色不对更难发现）。
   *  ⚠️ 不能在 SVG 里直接写 `stroke="var(--x)"` —— 表现属性上的 var() 支持面很窄，
   *     读出来插值才是稳的。 */
  function cssVar(name, fallback) {
    var value = window.getComputedStyle(document.documentElement)
      .getPropertyValue(name);
    return (value && value.trim()) || fallback;
  }

  // ------------------------------------------------------------------
  // 空状态插画（refs.md B13「没有、加载中、失败都可以加一个小插画，但要符合
  // 网站现有的色彩主题」）—— 自绘 SVG 常量：不引图片文件（页面要能离线双击打开）。
  // 配色**全部从 CSS 变量读**（--accent / --new-low / --line / --faint），
  // 不再在 JS 里维护第二份色值表（2026-10-09，卡片 10）。
  // ------------------------------------------------------------------
  var EMPTY_ART = (function () {
    var accent = cssVar("--accent", "#2563eb");      // 主色蓝
    var newLow = cssVar("--new-low", "#d92b2b");     // 新史低红（也是「出错」红）
    var line = cssVar("--line", "#e3e5e9");
    var faint = cssVar("--faint", "#9aa1ab");
    return {
      // 无内容：卡片框 + 往下探的箭头 + 底部一条红线（呼应站点名那个「价格探底」标记）
      none: '<svg class="empty-art" viewBox="0 0 120 120" aria-hidden="true">'
        + '<rect x="24" y="30" width="72" height="54" rx="9" fill="none" stroke="' + faint + '"'
        + ' stroke-width="3" stroke-dasharray="8 6"/>'
        + '<path d="M60 42v24" fill="none" stroke="' + accent + '" stroke-width="4" stroke-linecap="round"/>'
        + '<path d="M50 58l10 10 10-10" fill="none" stroke="' + accent + '" stroke-width="4"'
        + ' stroke-linecap="round" stroke-linejoin="round"/>'
        + '<rect x="30" y="94" width="60" height="5" rx="2.5" fill="' + newLow + '" opacity=".5"/></svg>',
      // 加载中：转圈（旋转交给 CSS 的 .is-spin —— 老安卓 WebView 对 SVG 动画支持不稳）
      loading: '<svg class="empty-art is-spin" viewBox="0 0 120 120" aria-hidden="true">'
        + '<circle cx="60" cy="60" r="30" fill="none" stroke="' + line + '" stroke-width="9"/>'
        + '<path d="M60 30a30 30 0 0 1 30 30" fill="none" stroke="' + accent + '" stroke-width="9"'
        + ' stroke-linecap="round"/></svg>',
      // 加载失败：虚线圈 + 感叹号（红 = 出错，与站点的新史低红同色，不另造一个红）
      fail: '<svg class="empty-art" viewBox="0 0 120 120" aria-hidden="true">'
        + '<circle cx="60" cy="58" r="29" fill="none" stroke="' + newLow + '" stroke-width="3.5"'
        + ' stroke-dasharray="9 7" opacity=".85"/>'
        + '<path d="M60 41v23" stroke="' + newLow + '" stroke-width="5" stroke-linecap="round"/>'
        + '<circle cx="60" cy="74" r="3.6" fill="' + newLow + '"/></svg>'
    };
  })();

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
    setFilter(k, undefined, { auto: true });   // 初始化：不算「用户手动改过」
  });
  var SECTION_LABEL = { __all__: "全部折扣" };
  (data.sections || []).forEach(function (s) { SECTION_LABEL[s.key] = s.label; });

  // reviews:{score,count} 是总结伴出现的一组字段（没有详情时整个为 null）——
  // 取值统一走这两个函数，别在各处写 `((x.reviews || {}).count) || 0`（容易漏一层）
  function reviewCount(item) { return ((item.reviews || {}).count) || 0; }
  function reviewScore(item) { return ((item.reviews || {}).score) || 0; }

  // 评价数紧凑写法（2026-10-08 用户定案：千位以上用「千 / 万」，真实数字放 title 悬停看）。
  // 起因：`.row-sub` 是单行 nowrap+overflow:hidden，6~7 位数（「228,750 条」≈63px）会把
  // 行尾的「剩余天数」挤出可视区（PC/平板一行结构行窄时尤其明显）。紧凑后最长「22.9万」
  // ≈35px，落在 `.rc` 的预留宽（3.4em，见 app.css）内，天数不再被裁。
  function compactCount(n) {
    n = n || 0;
    if (n >= 10000) {
      var w = n / 10000;
      return (w >= 100 ? Math.round(w) : Math.round(w * 10) / 10) + "万";
    }
    if (n >= 1000) {
      var k = Math.round(n / 100) / 10;
      return k >= 10 ? "1万" : k + "千";   // 9999 四舍五入到 10 千会别扭，归到「1万」
    }
    return String(n);
  }

  // 商店 / 小黑盒链接（2026-10-08，问题1）：all.js 不再下发这两个 URL —— 由 appid 现拼，
  // 口径与 src/report.py 原先拼的一致。data.js 的卡片仍带现成值，这里统一走现拼。
  function steamUrl(item) { return item.appid ? "https://store.steampowered.com/app/" + item.appid + "/" : null; }
  function xhhUrl(item) { return item.appid ? "https://www.xiaoheihe.cn/games/detail/" + item.appid : null; }

  // 封面 URL（2026-10-08，问题1）：all.js 不再下发 banner，改下发紧凑的 `art` 扩展名码
  // （见 src/report.py 的 boxart_code）—— 由 game_id（= ITAD 资产 uuid）现拼。
  //   · data.js 卡片仍带 item.banner（现成 URL）→ 直接用；
  //   · all.js 卡片：art 为 "jpg"/"png" → 拼 assets.isthereanydeal.com/<game_id>/boxart.<ext>；
  //     art 为 null（无封面，约 9%）→ null，渲染灰块占位（buildRow / pickCard）。
  function bannerUrl(item) {
    if (item.banner) return item.banner;
    if (item.art === undefined) return null;      // 既无 banner 也无 art = 真的没有封面
    if (!item.art || !item.game_id) return null;
    return "https://assets.isthereanydeal.com/" + item.game_id + "/boxart." + item.art;
  }

  function displayTitle(item) { return item.title_zh || item.title || "(无标题)"; }
  function enTitle(item) {
    return (item.title_zh && item.title && item.title_zh !== item.title) ? item.title : "";
  }
  function daysText(item) {
    var d = item.days_left;
    if (d === null || d === undefined) return "";
    return d <= 0 ? "今天结束" : "剩 " + d + " 天";
  }
  /** 史低类型 —— 2026-10-07 第二轮起**不再渲染徽章**（原来的 lowBadge 已删），
   *  只给卡片挂一个类名：
   *  行卡片 → `.row.l-new / .l-tie / .l-unk` 的**左侧 6px 色条**；
   *  大卡   → `.pick.l-new / …` 的**底部 6px 色条**。
   *  用户原话：「删掉新史低标签，靠色条区分，精简元素」+「大卡底部加色条就行了」。
   *  「这俩颜色代表什么」由**首页摘要行**那两枚同色小色条当图例（见 index.html.j2）。
   *  ⚠️ 别在卡片里再把徽章加回来：一卡同时有色条 + 徽章 = 同一件事涂两遍，
   *    那正是用户说的「一张卡最多同时出现 6 种颜色」的根源。
   *  ⚠️ 这里必须**显式写出三个取值**（含 "unknown"）：check_payload 会核对 app.js 里
   *    出现过的 low_class 字面量是否与 classify 常量集一致 ——
   *    漏一个就是「Python 改了名、前端静默失配」，不会报错，只是色条全错。 */
  function lowClassOf(item) {
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
      // ⚠️ 评价数这一格带 `.rc`：精简模式在**窄行**（≤460 容器）会把它连同后面那个
      //    分隔点一起隐藏 —— 那一行要同时塞下「评价·剩余天数 + 图标 + 力度条 + 折扣%」，
      //    实测 390px 宽放不下（143+8+182=333 刚好顶满，窄一点就折行成三行）。
      //    评价数是最不要紧的一格（好评率与「剩 X 天」都在），让位给它。
      parts.push(el("span", { class: "rc", text: compactCount(rv.count),
                              title: rv.count.toLocaleString("en-US") + " 条" }));
    } else {
      parts.push(el("span", { text: "详情待补" }));
    }
    var dl = daysText(item);
    if (dl) {
      parts.push(el("span", { class: "days" + (item.days_tier ? " days-" + item.days_tier : ""),
                              text: dl }));
    }
    parts.forEach(function (node, i) {
      // 分隔点包成 .sp 元素（原来是裸文本节点）—— 这样 `.rc + .sp` 能跟着 .rc 一起隐藏
      if (i) box.appendChild(el("span", { class: "sp", text: " · " }));
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

  // 「上次新史低」（§3.6，2026-10-09 用户定案改名，原「距上次史低」）：
  // 主文本就是「N 天」，有具体日期时加虚线下划线标记可交互 ——
  // 电脑悬停（title）看日期，手机点按在「天数 ↔ 日期」之间切换。
  // 新史低 = 距上一次史低期（自有记忆）；平史低 = 距上次同价（ITAD）；
  // 「本次新史低」（该游戏首次史低/记忆未建立）没有日期，不显示切换。
  // ⚠️ S9 重写行列表时把这个切换弄丢了（2026-10-07 用户指出），已加回；
  //    别再用 detailRow 直接渲染 last_low_text，那样日期就永远看不到。
  function lastLowRow(text, date) {
    if (!text) return null;
    var b = el("b", { text: text });
    if (!date) return el("div", { class: "detail-row" },
      [el("span", { text: "上次新史低" }), b]);
    b.classList.add("has-alt");
    b.title = date;
    var row = el("div", { class: "detail-row" },
      [el("span", { text: "上次新史低" }), b]);
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
      iconLink(steamUrl(item), "Steam 商店页", ICONS.steam),
      iconLink(xhhUrl(item), "小黑盒", ICONS.heihe)
    ]);
    var tags = el("div", { class: "row-tags" }, [
      links,
      cutBar(item.cut)     // 史低类型已改由 .row 的**左侧色条**表达，标签行不再放徽章
    ]);

    var thumbSrc = bannerUrl(item);
    var thumb = thumbSrc
      ? el("img", { class: "row-thumb", src: thumbSrc, alt: "",
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

    // 详情（点行展开）：左「上次新史低 / 折扣结束」，右「跨区比价」。
    // S9-卡片（2026-10-07）：恒定两栏 —— 之前无比价数据时回落单栏，右半边空着，
    // 用户反馈「展开的布局也没修改好」；现在右栏没有数据就写一行说明，结构对称。
    // ⚠️ 「折扣开始」这一行**已删**（用户 2026-10-07：「删除详情展开里折扣开始的时间这一行，
    //    以后加第三个外区再加回来」）—— 它的位置留给将来的第 3 个比价区，别顺手加回来。
    //    数据本身还在 payload 里（`start_text`），要用随时能取。
    var left = el("div", { class: "detail-col" }, [
      lastLowRow(item.last_low_text, item.last_low_date),
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

    // data-gid：行卡与大卡共用的身份（点大卡跳转后靠它定位并展开这一行，见 renderList）
    var row = el("article", {
      class: "row " + lowClassOf(item),
      "data-gid": item.game_id || ""
    }, [summary, detail]);
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
  //   · 一排**最多几张 = payload 的 pick_page**（服务端 HOME_PICKS_PAGE，与「取数按
  //     整数页」同一份口径；这里带 5 兜底，payload 缺字段时行为不变）——
  //     之前 auto-fill 让 1100px 变成 6 张，太多；
  //   · 手机 2 列只出一排（张数随列数，**口径只在 picksPerPage 一份**），
  //     且单张不能太宽 —— 之前 768px 走手机档排成 2 列，单张 347px 大得离谱；
  //   · 列数由**这里算出来并写进 inline style**，CSS 不再自己排 ——
  //     避免「公式算 6 列、CSS 排 4 列 → 一排只填 4/6 右边空一块」那种错位。
  // ⚠️ PICK_MIN 要跟 app.css 里 .pick 的观感一致（约 150px 起才放得下封面+价+力度条）。
  var PICK_MIN = 150;
  var PICK_GAP = 12;
  // 一排最多几张 = payload 的 pick_page（口径唯一来源）。**不再写 5 兜底**
  // （code-audit-2026-10-09 #18）：取不到就不设上限（见下面 pickColumns 的判空）。
  var PICK_MAX_COLS = (data && data.pick_page) || null;

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
    // 取不到 pick_page → 不设上限（code-audit-2026-10-09 #18：别静默按 5 跑）
    return PICK_MAX_COLS ? Math.max(2, Math.min(PICK_MAX_COLS, cols)) : Math.max(2, cols);
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
  var picksNav = document.querySelector("#picks .deck-nav");

  // 点大卡后待定位的行：``{gid, triedAll}``；null = 没有待办。
  // 见 renderList 末尾：先在新史低板块找，找不到再退到全部折扣。
  var pendingPick = null;
  var LOCATE_STEP = 300;    // 定位时每次多加载几条（分片 200 一片，约一片多）
  var LOCATE_MAX = 1500;    // 自动加载上限：够覆盖新史低板块，又不至于把全部折扣铺满 DOM

  function pickCard(item, rank) {
    var pickSrc = bannerUrl(item);
    var art = el("div", { class: "pick-art" }, [
      pickSrc ? el("img", { src: pickSrc, alt: "", loading: "lazy",
                            decoding: "async" }) : null,
      el("span", { class: "pick-rank", text: "#" + rank })
    ]);
    // 信息区照 **gg.deals** 的结构（用户 2026-10-07 给的参照图）。
    // 用户明确要的：
    //   · 不再用力度条（窄卡里太短，原话：「大卡的力度条太短了」）；
    //   · 两枚商店图标都保留，分别贴折扣行与价格行的右端 → 竖着对齐成一列。
    // 三排（2026-10-07 第二轮定稿）：
    // ① 标题　② 折扣徽章 + **Steam**　③ 原价 + 现价 + **小黑盒**。
    // 「史低类型」徽章已删 —— 改用**卡片底部 6px 色条**（`.pick.l-*`，见 app.css），
    // 顺序与行卡片一致（行卡片也是 Steam→小黑盒）。
    var lowRow = el("div", { class: "pick-low" }, [
      el("span", { class: "pick-cut", text: "-" + (item.cut || 0) + "%" }),
      el("span", { class: "links" }, [
        iconLink(steamUrl(item), "Steam 商店页", ICONS.steam)
      ])
    ]);
    var priceRow = el("div", { class: "pick-price-row" }, [
      el("span", { class: "pick-was", text: item.regular_text }),
      el("span", { class: "pick-price", text: item.price_text }),
      el("span", { class: "links" }, [
        iconLink(xhhUrl(item), "小黑盒", ICONS.heihe)
      ])
    ]);
    var body = el("div", { class: "pick-body" }, [
      el("div", { class: "pick-title", text: displayTitle(item), title: displayTitle(item) }),
      lowRow,
      priceRow
    ]);
    // 底部色条走 lowClassOf（与行卡片同一个函数，改口径只改那一处）
    var card = el("article", { class: "pick " + lowClassOf(item) }, [art, body]);
    // 点大卡 → 跳到「新史低」板块，**并记住这张卡**：落地后自动滚到对应行、展开详情
    // （2026-10-09 用户定案 A —— 原来只跳转，用户「不能一眼找到刚才点击的大卡详情」）。
    card.addEventListener("click", function () {
      pendingPick = item.game_id ? { gid: item.game_id, triedAll: false } : null;
      openSection("new_low");
    });
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
    picksSub.textContent = "本次折扣里最值得买的 " + list.length + " 款"
      + (total <= 1 ? "" : "（第 " + (pickPage + 1) + "/" + total + " 页）");
    picksPrev.disabled = pickPage <= 0;
    picksNext.disabled = pickPage >= total - 1;
    // 只有一页时翻页按钮没有任何意义 → 整块收起（2026-10-07 起大卡默认只放一页，
    // 平时就是这么个状态；手机一页 2~3 张时按钮照旧显示）。
    picksNav.hidden = total <= 1;
    lastPickCols = cols;
  }

  // 同一档内宽度变化（例：900 → 1100，或拖动窗口）也可能改变列数 → 重排。
  // 用**公式**判（不能用「量当前 DOM」：页内只有几张卡，排不满一排时永远量不出更大列数）。
  var lastPickCols = 0;
  window.addEventListener("resize", function () {
    if (!picksTrack || !picksTrack.children.length) return;
    if (pickColumns() !== lastPickCols) renderPicks();
  }, { passive: true });
  // 面板开着时窗口尺寸变了（手机横竖屏、PC 拉窗口）要重算它贴按钮的位置 ——
  // 手机端那个 bottom 是 JS 量的，不重算就会飘。
  window.addEventListener("resize", function () {
    if (drawer && !drawer.hidden) placeDrawer();
  }, { passive: true });
  picksPrev.addEventListener("click", function () { pickPage--; renderPicks(); });
  picksNext.addEventListener("click", function () { pickPage++; renderPicks(); });

  // ------------------------------------------------------------------
  // 首页四板块（服务端已给每板块前 K 条预览 + 完整条数；K 由服务端按池量动态定）
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
  // 板块完整列表页：按板块顺序切好的分片（2026-10-08）
  // ------------------------------------------------------------------
  // 原先是一整个 all.js（5.81MB raw / 729KB gzip），分类页必须**整份下完**才肯渲染
  // 第一条。国内到 github.io 实测 40~60KB/s，729KB 就是 13~18 秒，占满用户报的
  // 「接近二十秒」的 95%（剩下是解析：5.81MB 对象字面量约 200ms，手机上翻倍）。
  // 现在切成 all/<key>_<n>.js，每片 200 条 ≈ 20KB gzip，首屏只要 1 片。
  //
  // ⚠️ **顺序 = 分片的物理顺序**：片号 0,1,2… 依次读出来就是板块顺序，所以不再需要
  //   服务端单独下发 section_order（排序口径仍然只在服务端一份，前端不复制规则）。
  // ⚠️ **必须按板块自己的顺序切片**，不能做「共享池 + 板块索引」：池顺序（tier 分组
  //   + 折扣降序）跟板块排序不相关，实测「热门游戏」前 30 条会散落在 18 个分片里，
  //   等于没优化。卡片因此**在板块间重复存储**（发布体积 5.8MB → 13.1MB raw），
  //   这是可重建产物，换首屏速度值得。
  var ALL_GLOBAL = "ALL_S";
  var shardPending = {};   // "<key>_<n>" -> [回调]，合并并发的重复请求
  /** 一次最多并发取几片。默认路径（精选排序）只用到 1 片，这个上限只影响
   *  筛选很严 / 换排序时的补片速度。 */
  var SHARD_CONCURRENCY = 4;

  function shardGlobal() {
    if (!window[ALL_GLOBAL]) window[ALL_GLOBAL] = {};
    return window[ALL_GLOBAL];
  }

  function slotOf(key, n) { return key + "_" + n; }

  /** 取一个分片。已加载的直接回调（同步），正在取的合并进同一批回调。 */
  function loadShard(slot, cb) {
    var got = shardGlobal()[slot];
    if (got) { cb(got, null); return; }
    var pend = shardPending[slot];
    if (pend) { pend.push(cb); return; }
    shardPending[slot] = [cb];
    var script = document.createElement("script");
    // 与 data.js / app.js 同源的 ?v=（2026-10-08 加的）：否则会吃 Pages 的
    // ~10 分钟缓存，刚发布的新数据可能拿不到。
    script.src = "all/" + slot + ".js?v=" + encodeURIComponent((data && data.assets_version) || "");
    script.onload = function () { fireShard(slot, shardGlobal()[slot] || null); };
    script.onerror = function () { fireShard(slot, null); };
    document.head.appendChild(script);
  }

  function fireShard(slot, payload) {
    var cbs = shardPending[slot] || [];
    delete shardPending[slot];      // 失败也清掉 ⇒ 下次 openSection 可以重试
    for (var i = 0; i < cbs.length; i++) {
      cbs[i](payload, payload ? null : new Error("分片数据为空"));
    }
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

  // ---- 分片顺序即板块顺序（不再需要服务端下发 section_order）----
  // 「精选」排序 = 服务端给的板块顺序 = **分片的物理顺序**，按片号 0,1,2… 依次读出来
  // 就对了，前端仍然不复制任何排序规则。

  /** 单卡在当前板块 + 当前筛选下要不要留下（判据只有这一份，服务端 `section_agg`
   *  用的是同一套，改这里必须同步改那边）。 */
  function cardPredicate(key) {
    return function (card) {
      if (key !== "__all__") {
        if ((card.sections || []).indexOf(key) === -1) return false;
        if (!liveOk(card)) return false;
        // 「即将到期」本来就是按**到期时间**筛的，不再叠加用户选的日期窗口
        // （refs.md §11.3：叠加会把最紧急的老折扣漏掉）
        if (key !== "expiring" && !dateOk(card)) return false;
      } else if (!dateOk(card)) {
        return false;
      }
      return filterOk(card);
    };
  }

  /** 第 0 片带的**预聚合计数表**：只加载一片也能给出精确的「共 N 条」。
   *  查不到（服务端没这张表 / 选项对不上）返回 null，调用方退回已加载条数。 */
  function aggCount(shard0) {
    var agg = shard0 && shard0.agg;
    if (!agg || !agg.counts) return null;
    var f = state.filters;
    var parts = [f.date, f.cut, f.reviews, f.only_new].map(function (v) {
      return (v === undefined || v === null) ? "all" : String(v);
    });
    var n = agg.counts[parts.join("|")];
    return (n === undefined || n === null) ? null : n;
  }

  /** 换「折扣/价格/好评」排序是**全局重排**，必须拿到该板块全部卡片 ⇒ 拉全部分片。
   *  默认「精选」不需要 —— 实测按折扣排序的前 30 条仍落在 1~3 片内，
   *  但前端事先不知道，只能全取。代价与改动前「点一次分类」相同，属次要路径。 */
  function isGlobalSort() {
    return !!state.filters.sort && state.filters.sort !== "featured";
  }

  /**
   * 按分片顺序累积卡片，够 `want` 条就停（`all` = true 时取满整个板块）。
   * 回调 `done(cards, meta, err)`，meta = {total, shards, size, count}。
   */
  function collectCards(key, want, all, done) {
    var pred = cardPredicate(key);
    var acc = [];
    var meta = null;
    var next = 1;              // 片 0 由下面单独发起（它带 total/shards/agg）
    var flying = 0;
    var stopped = false;
    var failed = 0;            // 中途加载失败的分片数（第 0 片失败直接走 err 路径）

    function finish(err) {
      if (stopped) return;
      stopped = true;
      if (meta && failed) meta.failed = failed;
      done(acc, meta, err || null);
    }

    function grab(shard) {
      var items = (shard && shard.items) || [];
      for (var i = 0; i < items.length; i++) {
        if (pred(items[i])) acc.push(items[i]);
      }
    }

    function pump() {
      if (stopped) return;
      if (flying === 0 && (acc.length >= want || next >= meta.shards)) { finish(null); return; }
      var size = meta.size || 200;
      var batch;
      if (all) {
        batch = Math.min(SHARD_CONCURRENCY, meta.shards - next);
      } else {
        // 按已加载部分的命中率外推还要几片 —— 别一股脑并发拉一堆用不上的片
        var rate = acc.length / Math.max(1, next * size);
        var need = rate > 0 ? Math.ceil((want - acc.length) / rate / size) : (meta.shards - next);
        batch = Math.max(1, Math.min(SHARD_CONCURRENCY, need, meta.shards - next));
      }
      for (var k = 0; k < batch; k++) {
        flying++;
        loadShard(slotOf(key, next++), function (shard, err) {
          flying--;
          if (stopped) return;
          // ⚠️ 中间片失败不能静默吞掉：记下失败数、继续拿其余片（别让一片失败
          // 饿死整张列表），完成后 meta.failed 交给 renderList 挂「重试」入口 ——
          // 失败片的 pending 已被 fireShard 清掉，重试会真正重新拉取。
          if (err || !shard) failed++;
          else grab(shard);
          pump();
        });
      }
    }

    loadShard(slotOf(key, 0), function (shard0, err0) {
      if (err0 || !shard0) { finish(err0 || new Error("分片数据为空")); return; }
      meta = {
        total: shard0.total,
        shards: shard0.shards,
        size: shard0.size,
        count: aggCount(shard0),
      };
      grab(shard0);
      if (!all && acc.length >= want) { finish(null); return; }
      pump();
    });
  }

  /** 列表页当前该展示的卡片（异步）。done(cards, meta, err) */
  function sectionCards(key, done) {
    collectCards(key, state.limit, isGlobalSort(), function (acc, meta, err) {
      if (err) { done(null, null, err); return; }
      done(sortCards(acc), meta, null);
    });
  }

  var homeBox = document.getElementById("home");
  var listBox = document.getElementById("listview");
  var rowsBox = document.getElementById("rows");
  var pagerBox = document.getElementById("lv-pager");

  // 列表页当前这批已取到的卡片与板块元信息（分片是异步来的，渲染要能重入）。
  var listState = { key: null, cards: [], meta: null };

  // 滚动加载（refs.md B12 / §9.2）：**只在板块列表页**做，首页不做（"首页不可滑动"）。
  // 每批追加 LIST_BATCH 条；滑到底自动追加，底部按钮同时是手动兜底。
  // 不做虚拟列表 —— 一次最多把该板块全部渲染出来（板块量级几百到一千出头）。
  //
  // 2026-10-08：数据改成分片后，「加载更多」可能要去取下一片（已取到的片直接命中
  // 缓存，不会重复下载），所以这里不再吃一份固定的 cards，而是读 listState。
  function renderList() {
    var cards = listState.cards || [];
    var meta = listState.meta || {};
    var shown = cards.slice(0, state.limit);
    fillRows(rowsBox, shown);
    pagerBox.textContent = "";
    // 「共 N 条」用预聚合表的精确值；没有这张表就退回已加载条数（不会更好，但不会错）
    var total = (meta.count === null || meta.count === undefined) ? cards.length : meta.count;
    document.getElementById("lv-count").textContent = total + " 条";
    // 点大卡跳进来 → 定位并展开那一行（2026-10-09 用户定案 A）：
    //   ① 命中当前批次 → 滚动到它 + 展开详情；
    //   ② 还没渲染到 → 分步多加载（封顶 LOCATE_MAX，别把大板块一次铺满 DOM）；
    //      ⚠️ 有分片失败时**不再加量**（否则 cards.length 恒 < total 会反复重拉 = 死循环）；
    //   ③ 「新史低」板块找不到（大卡可能是**平史低补位**，该板块不含平史低）→ 退到
    //      「全部折扣」再找一遍；④ 仍找不到 → 放弃定位，别卡住。
    if (pendingPick) {
      var hit = pendingPick.gid
        ? rowsBox.querySelector('.row[data-gid="' + pendingPick.gid + '"]') : null;
      if (hit) {
        hit.classList.add("open");
        if (hit.scrollIntoView) hit.scrollIntoView({ block: "center" });
        pendingPick = null;
      } else if (cards.length < total && !meta.failed && state.limit < LOCATE_MAX) {
        state.limit = Math.min(total, state.limit + LOCATE_STEP);
        refreshList();
        return;
      } else if (!pendingPick.triedAll) {
        pendingPick.triedAll = true;
        openSection("__all__");
        return;
      } else {
        pendingPick = null;
      }
    }
    // 中间有分片加载失败 ⇒ 列表可能缺一段（「共 N 条」仍是 agg 的精确值）。
    // 不清空已加载的内容，只在底部挂一条提示 + 重试按钮，让用户可以自愈。
    if (meta.failed) {
      pagerBox.appendChild(el("div", { class: "pager" }, [
        el("span", { text: "有 " + meta.failed + " 个数据分片加载失败，内容可能不完整 · " }),
        (function () {
          var retry = el("button", { type: "button", class: "load-more", text: "重试" });
          retry.addEventListener("click", function () { refreshList(); });
          return retry;
        })(),
      ]));
    }
    var left = Math.max(0, total - shown.length);
    // 无分页参数（payload 缺 batch）→ 不分批：按「全部已加载」收尾，不挂分页条。
    // （code-audit-2026-10-09 #18 判空跳过；否则 state.limit += undefined 得 NaN，
    //   点「加载更多」会把列表清空 —— 这是 review 抓到的退化，必须在这里兜住。）
    if (left <= 0 || !LIST_BATCH) {
      if (total > (LIST_BATCH || 0)) {
        pagerBox.appendChild(el("div", { class: "pager" }, [
          el("span", { text: "已全部加载 " + total + " 条" })]));
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
      refreshList();                 // 可能要补取下一片；已取到的片直接命中缓存
    });
    pagerBox.appendChild(el("div", { class: "pager" }, [
      auto ? null : el("span", {
        text: "已自动显示前 " + shown.length + " 条（共 " + total
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
  // ⚠️ 这个标记**只由 setFilter 维护**（见下）—— 别在别处赋值（漏更新 =
  //    「全部折扣页日期意外收紧/放开」的静默回归）。
  var dateTouched = false;
  function touchDate() { dateTouched = true; }
  function clearDateTouched() { dateTouched = false; }

  /** 筛选项的**唯一写入口**（2026-10-09，架构检查卡片 10）。
   *
   *  `state.filters` 从前有 6 条写入路径（初始化 / 抽屉点击 / 已选小标签的 ✕ /
   *  resetFilters / openSection 的隐式日期改写 / resetImpliedFilters），`dateTouched`
   *  也散在 3 处 —— 注释里记录的两条静默回归都出在这块。现在全部走这里：
   *
   *  · `value === undefined` = **恢复该项的默认值**（服务端下发的 FILTER_DEFAULTS）；
   *  · 日期维度的「用户手动改过」标记自动处理：给了具体值 = 用户改过（touch）；
   *    恢复默认 = 不算改过（clear）。程序自动调整（openSection 的放开/收紧）要显式
   *    传 `{auto:true}`，否则会被误记成「用户改过」；
   *  · 返回**是否真的变了**，调用方可据此决定要不要重渲染。
   *
   *  ⚠️ 新增筛选维度不需要改这里（遍历 FILTER_DEFAULTS + 模板渲染选项即可）；
   *     只有当新维度带**附加状态**时才在这里加一个显式选项，别去外面直接赋值。
   */
  function setFilter(group, value, opts) {
    opts = opts || {};
    var next = (value === undefined) ? FILTER_DEFAULTS[group] : value;
    var changed = state.filters[group] !== next;
    state.filters[group] = next;
    if (group === "date" && opts.auto !== true) {
      if (value === undefined) clearDateTouched();
      else touchDate();
    }
    return changed;
  }

  function openSection(key) {
    state.section = key;
    state.limit = LIST_BATCH;
    if (!dateTouched) {
      // 程序自动放开/收紧日期窗口：**不算**用户手动改过（auto:true）
      setFilter("date", (key === "__all__") ? "all" : undefined, { auto: true });
    }
    // 进板块先复位「在当前板块里没有意义」的已选条件 ——
    // 不然角标会挂着一个筛不掉任何东西的条件（例：带着「仅新史低」进新史低板块）。
    resetImpliedFilters();
    // ⚠️ 这里必须手动刷一次筛选 UI（含「本板块已隐含」选项的置灰）——
    // openSection 自己不调 applyFilters（它在 refreshList 里直接 renderList），
    // 漏了这步的话，切板块后筛选项的可点状态会停在**上一个板块**的状态
    // （2026-10-07 冒烟抓到：从新史低切到大额折扣，「折扣降序」还是灰的）。
    syncFilterUI();
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
    listState.key = key;
    listState.cards = [];
    listState.meta = null;
    refreshList();
  }

  var listSeq = 0;   // 只认最后一次请求的结果，避免慢回来的旧结果盖掉新的
  /** 重新取一遍当前板块该展示的卡片（分片已缓存的话不发网络请求）再渲染。 */
  function refreshList() {
    var seq = ++listSeq;
    var key = state.section;
    sectionCards(key, function (cards, meta, err) {
      // 期间用户切走了 / 又有更新的请求 → 丢掉这次结果，别把旧数据画上去
      if (seq !== listSeq || listState.key !== key) return;
      if (err) {
        pendingPick = null;   // 这一板块取不到数 → 别再等定位，免得状态挂到别的板块上
        document.getElementById("lv-count").textContent = "";
        setEmpty("fail",
          "折扣数据加载失败（" + ((err && err.message) || "网络错误") + "），可以点下面的按钮重试。",
          function () { openSection(state.section); });
        return;
      }
      listState.cards = cards;
      listState.meta = meta;
      if (cards.length) hideEmpty();
      else setEmpty("none", "这个板块暂时没有符合条件的折扣。");
      renderList();
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
  var summaryEl = document.getElementById("summary");
  var chipsBox = document.getElementById("active-chips");
  var filterOpts = document.querySelectorAll("#drawer .chip.opt");
  // 选项的**初始**可点状态先记下来：那是「数据驱动」的（哪天没数据，服务端渲染时已写进
  // disabled 属性）；后面 syncOptionAvailability 把它和「板块隐含」（随板块变）两部分合并。
  for (var f0 = 0; f0 < filterOpts.length; f0++) {
    filterOpts[f0].dataset.baseOff = filterOpts[f0].disabled ? "1" : "0";
  }

  /** 按当前板块刷新每个筛选项的可点状态（用户 2026-10-07：
   *  「新史低里面还能再选新史低选项，大额折扣里面还能选折扣降序，置灰」）。
   *  一个选项在某个板块里「选了也不会变」（板块口径已经隐含了它）就置灰 + 悬停说明原因 ——
   *  否则用户会以为筛选坏了。原因文本由服务端 `filter_specs` 的 `implied_note` 下发。 */
  function syncOptionAvailability() {
    var sec = state.section || "";
    for (var i = 0; i < filterOpts.length; i++) {
      var opt = filterOpts[i];
      var implied = (opt.dataset.implied || "").split(",");
      var hit = !!sec && implied.indexOf(sec) >= 0;
      opt.disabled = opt.dataset.baseOff === "1" || hit;
      opt.classList.toggle("is-implied", hit);
      if (hit) {
        opt.title = opt.dataset.impliedNote || "本板块已按此条件筛选";
      } else if (opt.dataset.baseOff === "1") {
        opt.title = "这一天没有数据";
      } else {
        opt.removeAttribute("title");
      }
    }
  }

  /** 进板块时，把「在当前板块里没有意义」的已选条件复位 ——
   *  不然角标会挂着一个**筛选不到任何东西**的条件，用户还以为板块里没货。 */
  function resetImpliedFilters() {
    var sec = state.section || "";
    for (var i = 0; i < filterOpts.length; i++) {
      var opt = filterOpts[i];
      if ((opt.dataset.implied || "").split(",").indexOf(sec) < 0) continue;
      var g = opt.getAttribute("data-group");
      if (state.filters[g] === opt.getAttribute("data-value")) {
        setFilter(g, undefined);          // 程序复位：不算用户手动改过
      }
    }
  }


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
    syncOptionAvailability();      // 「本板块已隐含」的选项置灰（随板块变）
    var n = activeFilterCount();
    filterBadge.hidden = n === 0;
    filterBadge.textContent = n;
    // 按钮整体变蓝（「有没有筛过」不靠那个 20px 小圆点表达）+ 刷新已选条件小标签
    filterOpenBtn.classList.toggle("is-on", n > 0);
    renderActiveChips();
  }

  /** 把所有筛选恢复成服务端给的默认值（抽屉的「重置」与标签行的「清空」共用一份）。 */
  function resetFilters() {
    // setFilter(k, undefined) = 恢复默认；对 date 会自动 clearDateTouched
    // （重置后「全部折扣」页重新自动放开日期）
    Object.keys(FILTER_DEFAULTS).forEach(function (k) { setFilter(k, undefined); });
    applyFilters();
  }

  /** 从抽屉里那颗**同名选项**上读它的展示文案（「≥ 80%」「近 7 天」「仅新史低」…）。
   *  ⚠️ 不在前端另存一份 label 映射 —— 服务端 report.filter_specs 渲染的那份就是
   *  唯一来源（阈值改了、选项文案跟着变），这边自动跟着走，不会出现两边说法不一致。 */
  function optLabel(group, value) {
    var node = drawer.querySelector(
      '.chip.opt[data-group="' + group + '"][data-value="' + value + '"]');
    return node ? node.textContent.trim() : value;
  }

  /** 已选条件小标签（B3）：PC/平板显示在筛选按钮右边，每个能单独 ✕ 掉，末尾一个「清空」。
   *  ⚠️ 手机档由 CSS（`.active-chips{display:none}`）整条隐藏 —— 用户 2026-10-07：
   *  「我选 B3，但是手机端还是默认 B1」。所以这里**不判断点**，隐藏只归 CSS 管。 */
  function renderActiveChips() {
    if (!chipsBox) return;
    chipsBox.textContent = "";
    var picked = [];
    Object.keys(FILTER_DEFAULTS).forEach(function (k) {
      if (k === "sort") return;                       // 排序不算筛选（与角标同一口径）
      if (state.filters[k] === FILTER_DEFAULTS[k]) return;
      picked.push(k);
    });
    if (!picked.length) return;
    picked.forEach(function (k) {
      var value = state.filters[k];
      chipsBox.appendChild(el("span", { class: "a-chip" }, [
        el("span", { text: optLabel(k, value) }),
        el("button", {
          type: "button", text: "✕", title: "去掉这个条件",
          "aria-label": "去掉条件 " + optLabel(k, value),
          onclick: function (e) {
            e.stopPropagation();
            setFilter(k, undefined);      // 去掉这个条件 = 恢复默认（date 会自动清标记）
            applyFilters();
          }
        })
      ]));
    });
    chipsBox.appendChild(el("button", {
      type: "button", class: "a-clear", text: "清空", onclick: resetFilters
    }));
  }

  function setDrawer(open) {
    drawer.hidden = !open;
    drawerMask.hidden = !open;
    filterOpenBtn.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) placeDrawer();          // 面板贴着浮层按钮上沿，bottom 要按按钮当前位置算
  }

  // ------------------------------------------------------------------
  // 「筛选」按钮在右下角浮层 + 面板贴着它上沿展开（2026-10-08 起全端统一）
  // 用户：「PC 端滑动页面时筛选也放到右侧精简模式框上方，和手机端布局一致」
  // —— 原先只有手机搬浮层、桌面挂在筛选行下拉开，两套布局；现在只有一套。
  // ------------------------------------------------------------------

  /** 把**同一个** `#filter-open` 节点放进右下角浮层（全端统一，只搬一次）。
      为什么搬节点而不是写两份 markup：文案、角标、已选态都只有一份实现，
      两份按钮迟早会出现「角标数对不上」这种漂移。
      插到浮层最前面 → 顺序是 [筛选, 精简, 返回顶部]，「筛选」正好在「精简」上方 ✓。 */
  function placeFilterButton() {
    if (!filterOpenBtn) return;
    var floaters = document.getElementById("floaters");
    if (!floaters) return;
    if (filterOpenBtn.parentNode !== floaters) {
      floaters.insertBefore(filterOpenBtn, floaters.firstChild);
    }
  }

  /** 面板贴着浮层按钮的**上沿**展开（全端统一）—— `bottom` 只能等打开那一刻量：
      浮动按钮的堆叠高度会随「返回顶部」是否出现而变化（±50px），纯 CSS 算不出来。
      CSS 里只写 64px 兜底（app.js 没跑时的估计值），打开瞬间以这里量的为准。 */
  function placeDrawer() {
    if (!drawer || !filterOpenBtn) return;
    var r = filterOpenBtn.getBoundingClientRect();
    drawer.style.bottom = Math.max(8, Math.round(window.innerHeight - r.top + 8)) + "px";
  }

  /** 顶栏以下那两件「只属于首页 / 只属于列表页」的东西一起切：
   *  · 「筛选」按钮 —— 只作用于**板块完整列表页**；首页四板块是服务端算好的预览，
   *    不经过筛选项。所以首页要把按钮藏起来，不能"看得见、点了没反应"
   *    （review-s9-01 补充审查 Spec (c)）。
   *  · 「今日新增：新史低 X · 平史低 Y」摘要 —— refs.md §10.7 用户定的是
   *    **只放首页**，五类标签页不放（列表页有自己的条数 lv-count）。
   *    2026-10-08 起摘要挪进 #picks（「最值得买」标题上方，用户定的位置），
   *    随 #picks 一起显隐；这里的 hidden 是双保险（summary 自带
   *    display:inline-flex，hidden 必须显式写回，见 app.css 那条坑）。
   */
  function syncFilterVisibility() {
    if (filterOpenBtn) filterOpenBtn.hidden = !state.section;
    if (chipsBox) chipsBox.hidden = !state.section;   // 已选条件同理，只属于列表页
    if (summaryEl) summaryEl.hidden = !!state.section;
    if (!state.section) setDrawer(false);
  }

  function applyFilters() {
    syncFilterUI();
    if (state.section) openSection(state.section);   // limit 重置由 openSection 负责，别在这再重置一次
  }

  if (drawer && filterOpenBtn) {
    filterOpenBtn.addEventListener("click", function () { setDrawer(true); });
    document.getElementById("drawer-close").addEventListener("click", function () { setDrawer(false); });
    drawerMask.addEventListener("click", function () { setDrawer(false); });
    document.getElementById("filter-reset").addEventListener("click", resetFilters);
    for (var f = 0; f < filterOpts.length; f++) {
      (function (opt) {
        opt.addEventListener("click", function () {
          var group = opt.getAttribute("data-group");
          setFilter(group, opt.getAttribute("data-value"));   // 用户手动改：date 会打标记
          applyFilters();        // 选了就立刻生效，不用再点「完成」
        });
      })(filterOpts[f]);
    }
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" || e.keyCode === 27) setDrawer(false);
    });
    syncFilterUI();
    syncFilterVisibility();
    placeFilterButton();          // 按钮全端都在右下角浮层（见 placeFilterButton）
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

  // ---- 首页空闲时偷跑各板块的第 0 片（2026-10-08）----
  // 用户看首页的几秒足够下完 5×20KB，点进分类时就只剩「读缓存」，近乎瞬开。
  // ⚠️ 一片一片串行、每片都排在下一个空闲时段里，不跟首屏渲染抢主线程；
  //    用户已经进了某个板块（说明预取没必要了）就立刻停手。
  // ⚠️ 代价是只看首页不点分类的用户也会多下 ~100KB —— 用「近乎瞬开」换的，
  //    觉得不划算就把下面这段删掉，分类页仍然只比现在快 33 倍。
  (function prefetchShards() {
    var keys = ["new_low", "expiring", "popular", "big_cut", "__all__"];
    var i = 0;
    function step() {
      if (state.section) return;                 // 已经进板块了，不用预取
      if (i >= keys.length) return;
      var key = keys[i++];
      loadShard(slotOf(key, 0), function () {
        if (typeof window.requestIdleCallback === "function") {
          window.requestIdleCallback(step, { timeout: 2000 });
        } else {
          window.setTimeout(step, 200);
        }
      });
    }
    if (typeof window.requestIdleCallback === "function") {
      window.requestIdleCallback(step, { timeout: 3000 });
    } else {
      window.setTimeout(step, 1000);
    }
  })();

  function onBreakpointChange() {
    placeFilterButton();               // 幂等兜底：按钮永远在浮层（正常在 init 已就位）
    setDrawer(false);                  // 搬完按钮面板位置就变了，直接收起更省事
    renderPicks();                     // 每页张数随断点变（口径只在 picksPerPage）
    if (state.section) { state.limit = LIST_BATCH; openSection(state.section); }
  }
  // payload 没给断点就不挂这个行为（宁可不重排，也不按猜的边界重排）
  if (mq && mq.addEventListener) mq.addEventListener("change", onBreakpointChange);
  else if (mq && mq.addListener) mq.addListener(onBreakpointChange);

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
    // 两档陈旧阈值由 payload 下发，**不留兜底数字**（code-audit-2026-10-09 #18）：
    // 取不到则该档告警不成立（`hours > undefined` 恒 false）→ 自然跳过，轮到站点通知。
    var warnHours = data.stale_warn_hours;
    var redHours = data.stale_banner_hours;
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
