/* 报表前端：分组 + 手翻分页 + 卡片折叠（对应 docs/DEVELOPMENT.md §7.2 / §7.3）。 */
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
  var pages = {};
  var renderers = [];

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

  function buildCard(item) {
    var flagClass = item.flag === "N" ? "tag-new" : item.flag === "H" ? "tag-equal" : "tag-store";
    var tags = [tag("-" + (item.cut || 0) + "%", "tag-cut")];
    if (item.flag) tags.push(tag(item.flag_label, flagClass));
    if (item.tier === "pending") tags.push(tag("详情待补", "tag-pending"));
    else tags.push(tag(item.tier_label));

    var summary = el("div", { class: "card-summary" }, [
      item.banner ? el("img", { class: "thumb", src: item.banner, alt: "", loading: "lazy" }) : null,
      el("div", { class: "card-main" }, [
        el("h3", { class: "card-title", text: item.title_zh || item.title || "(无标题)" }),
        el("div", { class: "card-sub", text: item.reviews_text || "详情待补" }),
        el("div", { class: "tags" }, tags)
      ]),
      el("div", { class: "card-price" }, [
        el("div", { class: "price-now", text: item.price_text }),
        el("div", { class: "price-regular", text: item.regular_text })
      ])
    ]);

    var links = el("div", { class: "detail-links" }, [
      item.steam_url ? el("a", { href: item.steam_url, target: "_blank", rel: "noopener", text: "Steam 商店页" }) : null,
      item.xiaoheihe_url ? el("a", { href: item.xiaoheihe_url, target: "_blank", rel: "noopener", text: "小黑盒" }) : null,
      item.itad_url ? el("a", { href: item.itad_url, target: "_blank", rel: "noopener", text: "ITAD" }) : null
    ]);

    var compareRows = (item.compare || []).map(function (row) {
      var text = row.price_text;
      if (row.cny_text) text += " " + row.cny_text;
      if (row.diff_text) text += "（" + row.diff_text + "）";
      return detailRow(row.label, text);
    });

    var detail = el("div", { class: "card-detail" }, [
      detailRow("Steam 史低", item.store_low_text),
      detailRow("全周期最低", item.history_low_text),
      detailRow("近一年最低", item.history_low_1y_text),
      detailRow("折扣开始", item.start_text),
      detailRow("折扣结束", item.expiry_text)
    ].concat(compareRows, [links]));

    var card = el("article", { class: "card" }, [summary, detail]);
    summary.addEventListener("click", function () {
      card.classList.toggle("open");
    });
    return card;
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
      pages[groupKey] = current - 1;
      render();
    });
    next.addEventListener("click", function () {
      pages[groupKey] = current + 1;
      render();
    });
    return el("div", { class: "pager" }, [
      prev,
      el("span", { text: "第 " + current + " / " + totalPages + " 页 · 共 " + total + " 条" }),
      next
    ]);
  }

  function buildGroup(group) {
    var collapsed = group.collapsed;
    var cardsBox = el("div", { class: "cards" });
    var pagerBox = el("div", {});
    var arrow = el("span", { class: "arrow", text: "▼" });
    // 分组标题旁直接写上入组条件 —— 光看「好评达标」这类词分不清是什么门槛
    var head = el("div", { class: "group-head" }, [
      arrow,
      el("span", { class: "group-label", text: group.label }),
      group.criteria ? el("span", { class: "group-criteria", text: group.criteria }) : null,
      el("span", { class: "count", text: group.count + " 条" })
    ]);
    var section = el("section", { class: "group" }, [head, cardsBox, pagerBox]);
    if (collapsed) section.classList.add("collapsed");

    function render() {
      var page = pages[group.key] || 1;
      var start = (page - 1) * pageSize;
      cardsBox.textContent = "";
      group.items.slice(start, start + pageSize).forEach(function (item) {
        cardsBox.appendChild(buildCard(item));
      });
      pagerBox.textContent = "";
      var pager = buildPager(group.key, group.items.length, render);
      if (pager) pagerBox.appendChild(pager);
    }

    head.addEventListener("click", function () {
      section.classList.toggle("collapsed");
    });
    renderers.push(render);
    render();
    return section;
  }

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
    renderers.forEach(function (fn) { fn(); });
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