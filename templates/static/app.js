/* 报表前端：分组 + 手翻分页 + 卡片折叠（对应 docs/DEVELOPMENT.md §7.2 / §7.3）。
 * report-ui spec：R1 组内排序 / R4 图标链接前置 / R6 英文名 / R7 方向键翻页。
 * R2（去 ITAD 链接）与 R8（boxart 小图）在 payload 侧完成；
 * R5 密度切换已取消 —— 默认双列、≤768px 收单列，由 app.css 直接实现。
 * ⚠️ R3（详情左右两栏）经批 E 覆盖后仍是**两栏**：左「距上次史低 / 折扣结束」、
 * 右「比价」；无比价数据时回落单栏（E3）。
 *
 * 批 E（2026-09-24）：左侧史低色条 / 折扣力度条 / 「剩 X 天」/
 * 概览改单行漏斗 + 史低构成色点 / 详情删三行重复史低 —— 详见 .scratch/report-ui/spec.md。
 * 批 F（2026-09-24）：删页面「筛选条件」框（口径移入 README）/ 概览改多框 3 列 grid；
 * 批 F2：分组头拆 title+meta 两段（手机折两行）/ 手机端翻页后滚回列表顶部。
 */
(function () {
  "use strict";

  var data = window.REPORT_DATA;
  if (!data) return;

  // 断点必须与 app.css 的 @media 用同一个值，否则会出现
  // 「布局按手机渲染、每页条数按桌面算」的错位（§7.3）。
  var breakpoint = (data.page_size && data.page_size.breakpoint) || 768;
  var mq = window.matchMedia("(max-width: " + breakpoint + "px)");

  function currentPageSize() {
    return mq.matches ? data.page_size.mobile : data.page_size.desktop;
  }

  var pageSize = currentPageSize();
  var pages = {};          // groupKey -> 当前页码
  var sorts = {};          // groupKey -> { mode: "cut"|"price"|"score", min: 0|500|5000 }
  var groupOrder = [];     // 分组展示顺序（groupKey）
  var groupsByKey = {};
  var sectionsByKey = {};  // groupKey -> <section>
  var renderersByKey = {}; // groupKey -> 该组重画函数
  var activeGroupKey = null; // R7：最近一次被点击翻页按钮/卡片的分组

  // R4：链接图标用 inline SVG 常量内嵌，不下载 favicon、不发外链；
  // 链接语义靠 aria-label / title 文字。
  var ICONS = {
    steam: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M11.979 0C5.678 0 .511 4.86.022 11.037l6.432 2.658c.545-.371 1.203-.59 1.912-.59.063 0 .125.004.188.006l2.861-4.142V8.91c0-2.495 2.028-4.524 4.524-4.524 2.494 0 4.524 2.031 4.524 4.527s-2.03 4.525-4.524 4.525h-.105l-4.076 2.911c0 .052.004.105.004.159 0 1.875-1.515 3.396-3.39 3.396-1.635 0-3.016-1.173-3.331-2.727L.436 15.27C1.862 20.307 6.486 24 11.979 24c6.627 0 11.999-5.373 11.999-12S18.605 0 11.979 0zM7.54 18.21l-1.473-.61c.262.543.714.999 1.314 1.25 1.297.539 2.793-.076 3.332-1.375.263-.63.264-1.319.005-1.949s-.75-1.121-1.377-1.383c-.624-.26-1.29-.249-1.878-.03l1.523.63c.956.4 1.409 1.5 1.009 2.455-.397.957-1.497 1.41-2.454 1.012H7.54zm11.415-9.303c0-1.662-1.353-3.015-3.015-3.015-1.665 0-3.015 1.353-3.015 3.015 0 1.665 1.35 3.015 3.015 3.015 1.663 0 3.015-1.35 3.015-3.015zm-5.273-.005c0-1.252 1.013-2.266 2.265-2.266 1.249 0 2.266 1.014 2.266 2.266 0 1.251-1.017 2.265-2.266 2.265-1.253 0-2.265-1.014-2.265-2.265z"/></svg>',
    // 小黑盒：照官方 logo（cdn.max-c.com/heybox/logo/app_251.png）逐像素重绘。
    // 实际构造不是「六边形挖 H」，而是两块 180° 旋转对称的 Z 形片拼成的棱角 H，
    // 所有斜边均为 30°；顶点由 251px 原图实测推导（IoU 0.956，差异仅抗锯齿边缘）。
    heihe: '<svg viewBox="0 0 24 24" aria-hidden="true">'
      + '<path d="M10.1 0 L21.7 6.6 V17.6 L17.7 19.9 V8.8 L13.9 6.6 V10 H10.1 Z"/>'
      + '<path d="M2.3 6.4 L6.3 4.2 V15.2 L10.1 17.4 V14 H13.9 V24 L2.3 17.4 Z"/>'
      + '</svg>'
  };

  // R1：组内排序三维度 + 好评数量筛选 chips
  var SORT_MODES = [
    { key: "cut", label: "折扣降序" },
    { key: "price", label: "价格升" },
    { key: "score", label: "好评率降" }
  ];
  var REVIEW_CHIPS = [
    { min: 0, label: "全部" },
    { min: 500, label: "≥500" },
    { min: 5000, label: "≥5000" }
  ];

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
    (children || []).forEach(function (child) {
      if (child) node.appendChild(child);
    });
    return node;
  }

  function tag(text, cls) {
    return el("span", { class: "tag " + (cls || ""), text: text });
  }

  function detailRow(label, value) {
    if (!value) return null;
    return el("div", { class: "detail-row" }, [
      el("span", { text: label }),
      el("b", { text: value })
    ]);
  }

  // 批 E spec E2：折扣力度条 —— cut 10% 与 90% 原本长得一模一样，
  // 这里补一条横向条做视觉编码，旁边的小数字才是精确值
  function cutBar(cut) {
    var pct = Math.max(0, Math.min(100, cut || 0));
    var fill = el("i", {});
    fill.style.width = pct + "%";
    return el("span", { class: "cut-wrap" }, [
      el("span", { class: "cut-bar" }, [fill]),
      el("span", { class: "cut-num", text: "-" + (cut || 0) + "%" })
    ]);
  }

  // 批 E spec E2：「剩 X 天」—— 中性灰，是信息不是告警；
  // 真到临期（≤48h）由「即将过期」视图接管，本页不会出现需要变红的情况
  function daysLeftRow(days) {
    if (days === null || days === undefined) return null;
    return el("div", {
      class: "days-left",
      text: days <= 0 ? "今天结束" : "剩 " + days + " 天"
    });
  }

  // 史低天数（§3.6）：主文本就是「N 天」，有具体日期时加虚线下划线标记可交互 ——
  // 桌面悬停（title）看日期，手机点按在天数与日期之间切换。
  // 标签叫「距上次史低」而不是「上次史低」：前者才是这一行的真实含义，
  // 后者会让人以为这行要给的是上次的价格或日期（第二轮修正）。
  function lastLowRow(text, date) {
    if (!text) return null;
    var b = el("b", { text: text });
    if (!date) return el("div", { class: "detail-row" }, [el("span", { text: "距上次史低" }), b]);
    b.classList.add("has-alt");
    b.title = date;
    var row = el("div", { class: "detail-row" }, [el("span", { text: "距上次史低" }), b]);
    b.addEventListener("click", function (e) {
      e.stopPropagation();  // 别触发卡片手风琴
      var showingDate = b.textContent === date;
      b.textContent = showingDate ? text : date;
      // 悬停永远提示「另一种」信息：切到哪边，title 就是另一边
      b.title = showingDate ? date : text;
    });
    return row;
  }

  function iconLink(href, label, svg) {
    var a = el("a", { class: "icon-link", href: href, target: "_blank", rel: "noopener" });
    a.setAttribute("aria-label", label);
    a.title = label;
    a.innerHTML = svg; // 常量字符串，无用户输入
    // 图标在卡片摘要内（标签行），点链接开新标签页时不要触发卡片的展开/收起
    a.addEventListener("click", function (event) { event.stopPropagation(); });
    return a;
  }

  function getSort(groupKey) {
    if (!sorts[groupKey]) sorts[groupKey] = { mode: "cut", min: 0, newOnly: false };
    return sorts[groupKey];
  }

  function hasReviews(item) {
    return !!(item.reviews && item.reviews.score !== null && item.reviews.score !== undefined);
  }

  // R1：组内排序。默认（cut）直接用 payload 里服务端排好的顺序；
  // 无 reviews 的「详情待补」不参与好评率排序，固定排组尾。
  function sortGroupItems(group) {
    var s = getSort(group.key);
    if (s.mode === "cut") return group.items;
    var arr = group.items.slice();
    if (s.mode === "price") {
      arr.sort(function (a, b) {
        var pa = a.price_int == null ? Infinity : a.price_int;
        var pb = b.price_int == null ? Infinity : b.price_int;
        return pa - pb || String(a.title || "").localeCompare(String(b.title || ""));
      });
      return arr;
    }
    var withReviews = [], without = [];
    arr.forEach(function (item) {
      (hasReviews(item) ? withReviews : without).push(item);
    });
    withReviews.sort(function (a, b) {
      return b.reviews.score - a.reviews.score
        || (b.reviews.count || 0) - (a.reviews.count || 0)
        || String(a.title || "").localeCompare(String(b.title || ""));
    });
    return withReviews.concat(without);
  }

  // R1：好评数量 chips 作用于当前组显示集合（只筛有 reviews 的条目；
  // 「详情待补」不参与好评率维度，始终留在组尾）。
  // 「仅新史低」chip：作用于当前组，只留 low_class === "new" 的条目（Steam 口径，非 ITAD flag）。
  function filterGroupItems(group) {
    var s = getSort(group.key);
    var ordered = sortGroupItems(group);
    if (s.newOnly) {
      ordered = ordered.filter(function (item) { return item.low_class === "new"; });
    }
    if (s.mode !== "score" || !s.min) return ordered;
    return ordered.filter(function (item) {
      return !hasReviews(item) || (item.reviews.count || 0) >= s.min;
    });
  }

  function buildCard(item, group) {
    // 批 E spec E1：史低类型只有「新 / 平」两档（Steam 口径），
    // 「店史低」已退役 —— 本页全是 Steam 店史低，用它会产生「那别家呢」的误读
    var lowClass = item.low_class || "unknown";
    var lowTagClass = lowClass === "new" ? "tag-low-new"
      : lowClass === "tie" ? "tag-low-tie" : "tag-low-unknown";
    // 卡片标签里**不再放档位名**（好评达标）：同一分组内必然相同，
    // 分组头已经写明入组条件（批 E spec E2）
    var tags = [tag(item.low_label, lowTagClass)];
    if (item.tier === "pending") tags.push(tag("详情待补", "tag-pending"));

    // Steam / 小黑盒链接：放在**档位标签右侧、同一行内**（批 C2，原「前置到价格区之前」）——
    // 与标签同行天然对齐，不受价格位数影响；点击图标不应触发展开/收起，iconLink 内阻断冒泡
    var links = el("div", { class: "card-links" });
    if (item.steam_url) links.appendChild(iconLink(item.steam_url, "Steam 商店页", ICONS.steam));
    if (item.xiaoheihe_url) links.appendChild(iconLink(item.xiaoheihe_url, "小黑盒", ICONS.heihe));

    // R6：英文名始终显示在中文标题下方（灰色小字）。
    // 没有英文名的卡也保留这一行（空文本 + CSS min-height），
    // 保证双列网格里有/没有中文名的卡片高度对齐。
    var titleEn = el("div", {
      class: "card-title-en",
      text: (item.title_zh && item.title && item.title_zh !== item.title) ? item.title : ""
    });

    // 无封面图也保留占位空块（批 C2）：双列网格不错位，只是不显示图片
    var thumb = item.banner
      ? el("img", { class: "thumb", src: item.banner, alt: "", loading: "lazy" })
      : el("div", { class: "thumb thumb-empty" });

    // 标签行顺序（第二轮调整）：史低 pill → 链接图标 → **折扣力度条**。
    // 力度条挪到链接之后、并加长到 96px：它是「折扣强度」的量化视觉，
    // 挤在 pill 旁边时容易被当成又一个标签
    var tagsEl = el("div", { class: "tags" }, tags);
    tagsEl.appendChild(links);
    tagsEl.appendChild(cutBar(item.cut));

    var summary = el("div", { class: "card-summary" }, [
      thumb,
      el("div", { class: "card-main" }, [
        el("h3", { class: "card-title", text: item.title_zh || item.title || "(无标题)" }),
        titleEn,
        el("div", { class: "card-sub", text: item.reviews_text || "详情待补" }),
        tagsEl
      ]),
      el("div", { class: "card-price" }, [
        el("div", { class: "price-now", text: item.price_text }),
        el("div", { class: "price-regular", text: item.regular_text }),
        daysLeftRow(item.days_left)
      ])
    ]);

    // 批 E spec E3（第二轮改回两栏）：
    // 左栏 = 史低天数（距上次史低多久）+ 折扣结束；右栏 = 跨区比价。
    // 「折扣开始」在全组相同时由分组头承载；右栏没有比价行时不分栏，避免半张空表格
    var leftCol = el("div", { class: "detail-col" }, [
      lastLowRow(item.last_low_text, item.last_low_date),
      detailRow("折扣结束", item.expiry_text)
    ]);
    if (!(group && group.start_text)) {
      // detailRow 在值为空时返回 null，而 insertBefore(null, …) 会抛异常 —— 先判再插
      var startRow = detailRow("折扣开始", item.start_text);
      if (startRow) leftCol.insertBefore(startRow, leftCol.firstChild);
    }

    // 比价行：₴45 ≈ ¥6.75 -30%，±百分比带色（便宜绿 / 贵红 / 同价灰）
    var rightRows = [];
    (item.compare || []).forEach(function (row) {
      var value = el("b", {});
      value.appendChild(document.createTextNode(row.price_text));
      if (row.cny_text) value.appendChild(document.createTextNode(" " + row.cny_text));
      if (row.diff_pct !== null && row.diff_pct !== undefined) {
        value.appendChild(document.createTextNode(" "));
        value.appendChild(el("span", {
          class: row.diff_pct < 0 ? "diff-cheap" : row.diff_pct > 0 ? "diff-dear" : "diff-same",
          text: row.diff_pct < 0 ? "-" + Math.abs(row.diff_pct) + "%"
            : row.diff_pct > 0 ? "+" + row.diff_pct + "%" : "±0%"
        }));
      }
      rightRows.push(el("div", { class: "detail-row" }, [
        el("span", { text: row.label }),
        value
      ]));
    });
    var detail = el("div", { class: "card-detail" },
      rightRows.length
        ? [el("div", { class: "detail-cols" }, [
            leftCol, el("div", { class: "detail-col" }, rightRows)
          ])]
        : [leftCol]);

    var card = el("article", { class: "card low-" + lowClass }, [summary, detail]);
    summary.addEventListener("click", function () {
      // 手风琴：同时最多展开一张 —— 点开新卡先收起其他已展开的，再切换本卡
      var wasOpen = card.classList.contains("open");
      document.querySelectorAll(".card.open").forEach(function (other) {
        other.classList.remove("open");
      });
      if (!wasOpen) card.classList.add("open");
    });
    return card;
  }

  // 翻页后回到列表顶部 —— 一页放不下就得翻好几屏，翻完停在页尾，
  // 得自己往上滑一大段才看得到第 1 张卡。
  // mobileOnly=true 时只在手机断点触发（键盘方向键用，用户要求保持不变）。
  function scrollListTop(mobileOnly) {
    if (mobileOnly && !mq.matches) return;
    var list = document.getElementById("list");
    if (!list) return;
    var top = list.getBoundingClientRect().top + window.pageYOffset - 8;
    window.scrollTo({ top: top, behavior: "smooth" });
  }

  function buildPager(groupKey, total, render) {
    var totalPages = Math.max(1, Math.ceil(total / pageSize));
    pages[groupKey] = Math.min(pages[groupKey] || 1, totalPages);
    var current = pages[groupKey];
    if (totalPages <= 1) return null;
    var prev = el("button", { type: "button", text: "上一页" });
    var next = el("button", { type: "button", text: "下一页" });
    prev.disabled = current <= 1;
    next.disabled = current >= totalPages;
    prev.addEventListener("click", function () {
      activeGroupKey = groupKey; // R7
      pages[groupKey] = current - 1;
      render();
      scrollListTop(false); // 手动翻页：手机 + PC 都回到第一张卡
    });
    next.addEventListener("click", function () {
      activeGroupKey = groupKey; // R7
      pages[groupKey] = current + 1;
      render();
      scrollListTop(false); // 手动翻页：手机 + PC 都回到第一张卡
    });
    return el("div", { class: "pager" }, [
      prev,
      el("span", { text: "第 " + current + " / " + totalPages + " 页 · 共 " + total + " 条" }),
      next
    ]);
  }

  // R1：组内排序工具条。切换排序 / 筛选后该组回到第 1 页。
  function buildGroupToolbar(group, rerender) {
    var s = getSort(group.key);
    var bar = el("div", { class: "group-tools" });
    SORT_MODES.forEach(function (mode) {
      var b = el("button", {
        type: "button", class: "filter sort-btn", text: mode.label,
        onclick: function () {
          if (getSort(group.key).mode === mode.key) return;
          getSort(group.key).mode = mode.key;
          pages[group.key] = 1;
          rerender();
        }
      });
      if (s.mode === mode.key) b.classList.add("active");
      bar.appendChild(b);
    });
    var newOnlyBtn = el("button", {
      type: "button", class: "filter chip", text: "仅新史低",
      onclick: function () {
        getSort(group.key).newOnly = !getSort(group.key).newOnly;
        pages[group.key] = 1;
        rerender();
      }
    });
    if (s.newOnly) newOnlyBtn.classList.add("active");
    bar.appendChild(newOnlyBtn);
    if (s.mode === "score") {
      REVIEW_CHIPS.forEach(function (chip) {
        var b = el("button", {
          type: "button", class: "filter chip", text: chip.label,
          onclick: function () {
            if (getSort(group.key).min === chip.min) return;
            getSort(group.key).min = chip.min;
            pages[group.key] = 1;
            rerender();
          }
        });
        if (s.min === chip.min) b.classList.add("active");
        bar.appendChild(b);
      });
    }
    return bar;
  }

  // 批 E spec E4：史低构成色点。概览用完整文案，分组头（多组时）用短文案
  function lowPointsNode(items, shortForm) {
    var counts = { new: 0, tie: 0, unknown: 0 };
    (items || []).forEach(function (item) {
      var key = item.low_class || "unknown";
      counts[key] = (counts[key] || 0) + 1;
    });
    var box = el("span", { class: "low-points" });
    var newLabel = shortForm ? "新 " + counts.new : "新史低 " + counts.new;
    var tieLabel = shortForm ? "平 " + counts.tie : "平史低 " + counts.tie;
    if (counts.new) box.appendChild(el("span", { class: "point point-new", text: newLabel }));
    if (counts.tie) box.appendChild(el("span", { class: "point point-tie", text: tieLabel }));
    if (counts.unknown) {
      box.appendChild(el("span", {
        class: "point point-unknown",
        text: shortForm ? "待确认 " + counts.unknown : "待确认 " + counts.unknown
      }));
    }
    return box;
  }

  function buildGroup(group) {
    var collapsed = group.collapsed;
    var cardsBox = el("div", { class: "cards" });
    var toolsBox = el("div", { class: "group-tools-box" });
    var pagerBox = el("div", {});
    var arrow = el("span", { class: "arrow", text: "▼" });
    // 分组标题旁直接写上入组条件 —— 光看「好评达标」这类词分不清是什么门槛。
    // 批 E spec E5：全组「折扣开始」相同时上提到这里（卡片里不再每行重复）；
    // 新/平构成只在**多组**时显示 —— 单组时的数字与概览色点完全一样
    // 批 F2：拆成 .group-title（箭头+组名+条件）与 .group-meta（条数+开始+构成）两段，
    // 手机端靠这两段把分组头折成两行，电脑端仍是同一行
    var titleBox = el("span", { class: "group-title" }, [
      arrow,
      el("span", { class: "group-label", text: group.label }),
      group.criteria ? el("span", { class: "group-criteria", text: group.criteria }) : null
    ]);
    var metaBox = el("span", { class: "group-meta" }, [
      el("span", { class: "count", text: group.count + " 条" }),
      group.start_text
        ? el("span", { class: "group-start", text: "折扣开始 " + group.start_text }) : null,
      data.groups.length > 1 ? lowPointsNode(group.items, true) : null
    ]);
    var head = el("div", { class: "group-head" }, [titleBox, metaBox]);
    var section = el("section", { class: "group" }, [head, toolsBox, cardsBox, pagerBox]);
    if (collapsed) section.classList.add("collapsed");

    function render() {
      var ordered = filterGroupItems(group);
      var page = pages[group.key] || 1;
      var totalPages = Math.max(1, Math.ceil(ordered.length / pageSize));
      pages[group.key] = Math.min(page, totalPages);
      var start = (pages[group.key] - 1) * pageSize;
      cardsBox.textContent = "";
      ordered.slice(start, start + pageSize).forEach(function (item) {
        var card = buildCard(item, group);
        card.addEventListener("click", function () { activeGroupKey = group.key; }); // R7
        cardsBox.appendChild(card);
      });
      pagerBox.textContent = "";
      var pager = buildPager(group.key, ordered.length, render);
      if (pager) pagerBox.appendChild(pager);
      toolsBox.textContent = "";
      toolsBox.appendChild(buildGroupToolbar(group, render));
    }

    head.addEventListener("click", function () {
      section.classList.toggle("collapsed");
    });
    groupOrder.push(group.key);
    groupsByKey[group.key] = group;
    sectionsByKey[group.key] = section;
    renderersByKey[group.key] = render;
    render();
    return section;
  }

  // R7：←/→ 对「当前活动分组」翻页；无记录时用第一个未折叠分组
  function activeOrFallbackKey() {
    if (activeGroupKey && renderersByKey[activeGroupKey]) return activeGroupKey;
    for (var i = 0; i < groupOrder.length; i++) {
      var section = sectionsByKey[groupOrder[i]];
      if (section && !section.classList.contains("collapsed")) return groupOrder[i];
    }
    return null;
  }

  document.addEventListener("keydown", function (event) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    var focused = document.activeElement;
    if (focused && /^(INPUT|TEXTAREA|SELECT)$/.test(focused.tagName)) return; // 防御性判断
    var key = activeOrFallbackKey();
    if (!key) return;
    var ordered = filterGroupItems(groupsByKey[key]);
    var totalPages = Math.max(1, Math.ceil(ordered.length / pageSize));
    var current = pages[key] || 1;
    var wanted = event.key === "ArrowRight" ? current + 1 : current - 1;
    if (wanted < 1 || wanted > totalPages) return;
    pages[key] = wanted;
    activeGroupKey = key;
    renderersByKey[key]();
    scrollListTop(true); // 键盘方向键：只在手机端回顶（PC 保持不动）
  });

  var listBox = document.getElementById("list");
  data.groups.forEach(function (group) {
    listBox.appendChild(buildGroup(group));
  });
  if (!data.groups.length) {
    document.getElementById("empty").hidden = false;
  }

  // 断点变化（旋转屏幕 / 改窗口宽度）时重算每页条数并重画，从第 1 页开始。
  function onBreakpointChange() {
    pageSize = currentPageSize();
    pages = {};
    groupOrder.forEach(function (key) { renderersByKey[key](); });
  }
  if (mq.addEventListener) mq.addEventListener("change", onBreakpointChange);
  else if (mq.addListener) mq.addListener(onBreakpointChange);

  document.getElementById("no-image").addEventListener("change", function (event) {
    document.body.classList.toggle("no-image", event.target.checked);
  });

  var generated = new Date(data.generated_at);
  var ageHours = (Date.now() - generated.getTime()) / 3600000;
  if (ageHours > data.stale_banner_hours) {
    document.getElementById("stale-banner").hidden = false;
  }
})();
