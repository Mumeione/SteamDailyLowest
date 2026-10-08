/* 报表前端（S9，2026-10-06）：gg.deals 式首页
 * ================================================================
 * 骨架：吸顶导航 → 筛选胶囊行 → 首页（顶部大卡横排 + 四板块两栏行列表）
 *       → 点导航进「板块完整列表页」（数据来自 all.js 懒加载）
 *
 * 数据来源：
 *   window.REPORT_DATA（data.js）—— 当日新增摘要（low_points）、四板块预览
 *                                    （各 10 条）、顶部大卡候选、断点/分页配置
 *                                    （⚠️ 2026-10-08 起不含分组卡片，见下）
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

    // 详情（点行展开）：左「距上次史低 / 折扣结束」，右「跨区比价」。
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
  var PICK_MAX_COLS = (data && data.pick_page) || 5;

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
  var picksNav = document.querySelector("#picks .deck-nav");

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
    // all.js 也带 ?v=（2026-10-08）：否则会吃 Pages 的 ~10 分钟缓存，刚发布的新数据
    // 可能拿不到。版本从 payload 取，与 data.js/app.js 的 ?v= 同源（report.render 的 assets_version）。
    script.src = "all.js?v=" + encodeURIComponent(data.assets_version || "");
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
  // ⚠️ 写点只有下面 touchDate / clearDateTouched 两个（架构整理时收敛的）——
  //    新增改筛选的入口必须走它们，别直接赋值（漏更新 = 「全部折扣页日期意外
  //    收紧/放开」的静默回归）。
  var dateTouched = false;
  function touchDate() { dateTouched = true; }
  function clearDateTouched() { dateTouched = false; }

  function openSection(key) {
    state.section = key;
    state.limit = LIST_BATCH;
    if (!dateTouched) {
      state.filters.date = (key === "__all__") ? "all" : FILTER_DEFAULTS.date;
    }
    // 进板块先复位「在当前板块里没有意义」的已选条件 ——
    // 不然角标会挂着一个筛不掉任何东西的条件（例：带着「仅新史低」进新史低板块）。
    resetImpliedFilters();
    // ⚠️ 这里必须手动刷一次筛选 UI（含「本板块已隐含」选项的置灰）——
    // openSection 自己不调 applyFilters（它在 loadAll 回调里直接 renderList），
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
        state.filters[g] = FILTER_DEFAULTS[g];
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
    Object.keys(FILTER_DEFAULTS).forEach(function (k) { state.filters[k] = FILTER_DEFAULTS[k]; });
    clearDateTouched();        // 重置后「全部折扣」页重新自动放开日期
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
            state.filters[k] = FILTER_DEFAULTS[k];
            if (k === "date") clearDateTouched();
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
    if (open) placeDrawer();          // 手机端要按按钮当前位置算 bottom，见 placeDrawer
  }

  // ------------------------------------------------------------------
  // 手机端：「筛选」按钮搬进右下角浮层 + 面板贴着它上沿展开
  // 用户 2026-10-07：「手机的筛选放到右边精简模式上方，也是点开就可以选，
  // 这样才符合手机的操作逻辑」
  // ------------------------------------------------------------------

  /** 是否手机档 —— 与 app.css 的 `@media (max-width: mobile_breakpoint_px)` 同一条断点
      （数值从 payload 来，见本文件开头的 `mq`；别在这里另写一个像素数）。 */
  function isMobileView() { return !!mq.matches; }

  /** 把**同一个** `#filter-open` 节点在「筛选行」与「右下角浮层」之间搬一次。
      为什么搬节点而不是写两份 markup：文案、角标、已选态都只有一份实现，
      两份按钮迟早会出现「角标数对不上」这种漂移。
      插到浮层最前面 → 顺序是 [筛选, 精简, 返回顶部]，「筛选」正好在「精简」上方 ✓。 */
  function placeFilterButton() {
    if (!filterOpenBtn) return;
    var floaters = document.getElementById("floaters");
    var bar = document.getElementById("filterbar");
    if (!floaters || !bar) return;
    if (isMobileView()) {
      if (filterOpenBtn.parentNode !== floaters) {
        floaters.insertBefore(filterOpenBtn, floaters.firstChild);
      }
    } else if (filterOpenBtn.parentNode !== bar) {
      bar.insertBefore(filterOpenBtn, bar.firstChild);
    }
  }

  /** 手机端面板贴着浮层按钮的**上沿**展开 —— `bottom` 只能等打开那一刻量：
      浮动按钮的堆叠高度会随「返回顶部」是否出现而变化（±50px），纯 CSS 算不出来。
      桌面端面板是 `position:absolute` 挂在 `.filterbar` 上，这里什么都不用做
      （顺手清掉手机端写过的 inline 值，免得残留）。 */
  function placeDrawer() {
    if (!drawer || !filterOpenBtn) return;
    if (!isMobileView()) { drawer.style.bottom = ""; return; }
    var r = filterOpenBtn.getBoundingClientRect();
    drawer.style.bottom = Math.max(8, Math.round(window.innerHeight - r.top + 8)) + "px";
  }

  /** 顶栏以下那两件「只属于首页 / 只属于列表页」的东西一起切：
   *  · 「筛选」按钮 —— 只作用于**板块完整列表页**；首页四板块是服务端算好的预览，
   *    不经过筛选项。所以首页要把按钮藏起来，不能"看得见、点了没反应"
   *    （review-s9-01 补充审查 Spec (c)）。
   *  · 「今日新增：新史低 X · 平史低 Y」摘要 —— refs.md §10.7 用户定的是
   *    **只放首页**，五类标签页不放（列表页有自己的条数 lv-count）。
   *    原先这个函数只管按钮，摘要就跟着留在列表页上了（用户 2026-10-07 报的违规）。
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
          state.filters[group] = opt.getAttribute("data-value");
          if (group === "date") touchDate();
          applyFilters();        // 选了就立刻生效，不用再点「完成」
        });
      })(filterOpts[f]);
    }
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" || e.keyCode === 27) setDrawer(false);
    });
    syncFilterUI();
    syncFilterVisibility();
    placeFilterButton();          // 手机档要把按钮搬进右下角浮层（见 placeFilterButton）
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
    placeFilterButton();               // 跨 600px 时「筛选」要在浮层与筛选行之间搬
    setDrawer(false);                  // 搬完按钮面板位置就变了，直接收起更省事
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
