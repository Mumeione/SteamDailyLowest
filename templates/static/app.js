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
  var breakpoint = (data.page_size && data.page_size.breakpoint) || 768;
  var mq = window.matchMedia("(max-width: " + breakpoint + "px)");
  function pageSize() {
    return mq.matches ? data.page_size.mobile : data.page_size.desktop;
  }

  var ICONS = {
    steam: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11.979 0C5.678 0 .511 4.86.022 11.037l6.432 2.658c.545-.371 1.203-.59 1.912-.59.063 0 .125.004.188.006l2.861-4.142V8.91c0-2.495 2.028-4.524 4.524-4.524 2.494 0 4.524 2.031 4.524 4.527s-2.03 4.525-4.524 4.525h-.105l-4.076 2.911c0 .052.004.105.004.159 0 1.875-1.515 3.396-3.39 3.396-1.635 0-3.016-1.173-3.331-2.727L.436 15.27C1.862 20.307 6.486 24 11.979 24c6.627 0 11.999-5.373 11.999-12S18.605 0 11.979 0zM7.54 18.21l-1.473-.61c.262.543.714.999 1.314 1.25 1.297.539 2.793-.076 3.332-1.375.263-.63.264-1.319.005-1.949s-.75-1.121-1.377-1.383c-.624-.26-1.29-.249-1.878-.03l1.523.63c.956.4 1.409 1.5 1.009 2.455-.397.957-1.497 1.41-2.454 1.012H7.54zm11.415-9.303c0-1.662-1.353-3.015-3.015-3.015-1.665 0-3.015 1.353-3.015 3.015 0 1.665 1.35 3.015 3.015 3.015 1.663 0 3.015-1.35 3.015-3.015zm-5.273-.005c0-1.252 1.013-2.266 2.265-2.266 1.249 0 2.266 1.014 2.266 2.266 0 1.251-1.017 2.265-2.266 2.265-1.253 0-2.265-1.014-2.265-2.265z"/></svg>',
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

  var state = { section: null, page: 1, filters: {} };
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
    var lc = item.low_class;
    return lc === "new" ? "l-new" : (lc === "tie" ? "l-tie" : "l-unk");
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
    var sub = [item.reviews_text || "详情待补"];
    var dl = daysText(item);
    if (dl) sub.push(dl);

    var links = el("span", { class: "links" }, [
      iconLink(item.steam_url, "Steam 商店页", ICONS.steam),
      iconLink(item.xiaoheihe_url, "小黑盒", ICONS.heihe)
    ]);
    var tags = el("div", { class: "row-tags" }, [
      el("span", { class: "hl" + (item.low_class === "new" ? " hl-new" : ""),
                   text: item.low_label || "史低" }),
      links,
      cutBar(item.cut)     // 老口径顺序：史低标签 → 图标 → 力度条
    ]);

    var thumb = item.banner
      ? el("img", { class: "row-thumb", src: item.banner, alt: "",
                    loading: "lazy", decoding: "async", width: "40", height: "56" })
      : el("div", { class: "row-thumb" });

    var summary = el("div", { class: "row-main" }, [
      thumb,
      el("div", { class: "row-info" }, [
        el("div", { class: "row-title", text: displayTitle(item), title: displayTitle(item) }),
        el("div", { class: "row-en", text: enTitle(item) }),
        el("div", { class: "row-sub", text: sub.join(" · ") })
      ]),
      tags,
      el("div", { class: "row-price" }, [
        el("div", { class: "now", text: item.price_text }),
        el("div", { class: "was", text: item.regular_text })
      ])
    ]);

    // 详情（点行展开）：左「距上次史低 / 折扣结束」，右「跨区比价」
    var left = el("div", { class: "detail-col" }, [
      lastLowRow(item.last_low_text, item.last_low_date),
      detailRow("折扣结束", item.expiry_text)
    ]);
    var rightRows = (item.compare || []).map(function (row) {
      var b = el("b", {}, [document.createTextNode(row.price_text)]);
      if (row.cny_text) b.appendChild(document.createTextNode(" " + row.cny_text));
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
    var detail = el("div", { class: "row-detail" },
      rightRows.length
        ? [el("div", { class: "detail-cols" }, [
            left, el("div", { class: "detail-col" }, rightRows)])]
        : [left]);

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
  // 顶部大卡横排（每页 5 张，左右翻页）
  // ------------------------------------------------------------------
  // 手机上每页 4 张（2 列 × 2 排）—— 5 张在手机上会排成 3 排（2+2+1），
  // 最后一排孤零零一张很难看，而且整块占了近两屏高（用户 2026-10-07 反馈）。
  // 电脑上仍是 5 张（一行正好排满）。
  function picksPerPage() { return mq.matches ? 4 : 5; }
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
    var body = el("div", { class: "pick-body" }, [
      el("div", { class: "pick-title", text: displayTitle(item), title: displayTitle(item) }),
      el("div", { class: "pick-foot" }, [
        el("span", { class: "pct", text: "-" + (item.cut || 0) + "%" }),
        el("span", { class: "pick-from", text: "现价" }),
        el("span", { class: "pick-price", text: item.price_text })
      ])
    ]);
    var card = el("article", { class: "pick" }, [art, body]);
    card.addEventListener("click", function () { openSection("new_low"); });
    return card;
  }

  function renderPicks() {
    var list = data.picks || [];
    if (!list.length) return;
    var per = picksPerPage();
    var total = Math.ceil(list.length / per);
    if (pickPage >= total) pickPage = total - 1;
    if (pickPage < 0) pickPage = 0;
    var slice = list.slice(pickPage * per, (pickPage + 1) * per);
    picksBox.hidden = false;
    picksTrack.textContent = "";
    slice.forEach(function (item, i) {
      picksTrack.appendChild(pickCard(item, pickPage * per + i + 1));
    });
    picksSub.textContent = "本次折扣里最值得买的 " + list.length + " 款（第 "
      + (pickPage + 1) + "/" + total + " 页）";
    picksPrev.disabled = pickPage <= 0;
    picksNext.disabled = pickPage >= total - 1;
  }
  picksPrev.addEventListener("click", function () { pickPage--; renderPicks(); });
  picksNext.addEventListener("click", function () { pickPage++; renderPicks(); });

  // ------------------------------------------------------------------
  // 首页四板块（服务端已给每板块前 10 条 + 完整条数）
  // ------------------------------------------------------------------
  var sectionsBox = document.getElementById("sections");

  function renderSections() {
    sectionsBox.textContent = "";
    (data.sections || []).forEach(function (sec) {
      var head = el("div", { class: "sec-head" }, [
        el("h2", { class: "sec-title", text: sec.label }),
        el("span", { class: "sec-count", text: sec.count + " 条" }),
        el("button", { type: "button", class: "sec-more", text: "查看更多 ›",
                       onclick: function () { openSection(sec.key); } })
      ]);
      var rows = el("div", { class: "rows" });
      fillRows(rows, sec.items || []);
      sectionsBox.appendChild(el("section", { class: "section" }, [head, rows]));
    });
    document.getElementById("empty").hidden = !!(data.sections || []).length;
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
    return sortCards(out);
  }

  var homeBox = document.getElementById("home");
  var listBox = document.getElementById("listview");
  var rowsBox = document.getElementById("rows");
  var pagerBox = document.getElementById("lv-pager");
  var emptyBox = document.getElementById("empty");

  function renderList(cards) {
    var size = pageSize();
    var totalPages = Math.max(1, Math.ceil(cards.length / size));
    if (state.page > totalPages) state.page = totalPages;
    if (state.page < 1) state.page = 1;
    var start = (state.page - 1) * size;
    fillRows(rowsBox, cards.slice(start, start + size));

    pagerBox.textContent = "";
    if (totalPages > 1) {
      var prev = el("button", { type: "button", text: "上一页" });
      var next = el("button", { type: "button", text: "下一页" });
      prev.disabled = state.page <= 1;
      next.disabled = state.page >= totalPages;
      prev.addEventListener("click", function () {
        state.page--; renderList(cards); scrollTop();
      });
      next.addEventListener("click", function () {
        state.page++; renderList(cards); scrollTop();
      });
      pagerBox.appendChild(el("div", { class: "pager" }, [
        prev,
        el("span", { text: "第 " + state.page + " / " + totalPages + " 页 · 共 "
          + cards.length + " 条" }),
        next
      ]));
    }
  }

  function scrollTop() {
    var top = listBox.getBoundingClientRect().top + window.pageYOffset - 70;
    window.scrollTo({ top: top, behavior: "smooth" });
  }

  // 用户有没有**手动**改过日期。没有的话，切到「全部折扣」页时日期自动放开成
  // 「全部」（refs §11.5 Q2：「全部折扣」页**不设限**），切回板块再收成默认窗口。
  var dateTouched = false;

  function openSection(key) {
    state.section = key;
    state.page = 1;
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
    syncFilterVisibility();          // 板块页才显示「筛选」按钮
    document.getElementById("lv-title").textContent = SECTION_LABEL[key] || key;
    document.getElementById("lv-count").textContent = "加载中…";
    rowsBox.textContent = "";
    pagerBox.textContent = "";
    emptyBox.hidden = true;
    loadAll(function (_json, err) {
      if (err) {
        document.getElementById("lv-count").textContent = "";
        emptyBox.hidden = false;
        emptyBox.textContent = "全部数据加载失败（" + ((err && err.message) || "网络错误")
          + "），可点站点名回首页后重试。";
        return;
      }
      var cards = cardsFor(state.section);
      document.getElementById("lv-count").textContent = cards.length + " 条";
      emptyBox.hidden = !!cards.length;
      emptyBox.textContent = "这个板块暂时没有符合条件的折扣。";
      renderList(cards);
    });
  }

  function openHome() {
    state.section = null;
    state.page = 1;
    var buttons = document.querySelectorAll("#nav .nav-item");
    for (var i = 0; i < buttons.length; i++) buttons[i].classList.remove("active");
    listBox.hidden = true;
    homeBox.hidden = false;
    syncFilterVisibility();          // 首页不显示「筛选」（它不作用于首页板块）
    emptyBox.hidden = !!(data.sections || []).length;
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
    if (state.section) { state.page = 1; openSection(state.section); }
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
  // 初始化
  // ------------------------------------------------------------------
  renderPicks();
  renderSections();

  function onBreakpointChange() {
    renderPicks();                     // 每页张数随断点变（手机 4 / 电脑 5）
    if (state.section) { state.page = 1; openSection(state.section); }
  }
  if (mq.addEventListener) mq.addEventListener("change", onBreakpointChange);
  else if (mq.addListener) mq.addListener(onBreakpointChange);

  var generated = new Date(data.generated_at);
  if ((Date.now() - generated.getTime()) / 3600000 > data.stale_banner_hours) {
    document.getElementById("stale-banner").hidden = false;
  }
})();
