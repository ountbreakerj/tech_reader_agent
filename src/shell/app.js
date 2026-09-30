
/* tech-reader shell js — router / toc / search / themes / lightbox / pager */
(function () {
  "use strict";

  /* ========== module registry (TOC_DATA injected by build) ========== */
  /* @generated:module-registry */

  /* ========== dom refs ========== */
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var viewHome = $("#view-home"), viewReader = $("#view-reader"),
      tocTitle = $("#toc-title"), tocSub = $("#toc-sub"), tocList = $("#toc-list"),
      tocProgress = $("#toc-progress"),
      searchInput = $("#search"), searchStatus = $("#searchStatus"),
      progressbar = $("#progressbar"), toTop = $("#toTop"), toast = $("#toast"),
      pagerPrev = $("#prevCh"), pagerNext = $("#nextCh");

  var currentMod = null;       // module key, or null on the home page
  var scrollMemory = {};       // mod -> scrollY
  var spyObserver = null;
  var spyCurrentId = null;
  var lgRevealed = false;
  var llmRevealed = false;
  var readerWidth = 980;
  var spyTick = false;

  /* ========== asset 占位符回填（构建端单份存储，避免重复内联） ========== */
  (function () {
    var reg = window.__TR_ASSETS || {};
    $$("img").forEach(function (img) {
      var src = img.getAttribute("src") || "";
      if (src.indexOf("tr-asset://") === 0) {
        var uri = reg[src.slice(11)];
        if (uri) img.src = uri;
      }
    });
  })();

  /* ========== helpers ========== */
  function escRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }
  function show(el, v) { el.hidden = !v; }
  // TOC 分组标题统一去掉前导 emoji（菜单页保留，作模块识别色载体）
  function stripEmoji(s) { return String(s).replace(/^[\p{Extended_Pictographic}\uFE0F\u200D\u20E3]+\s*/u, ""); }
  function syncTopbarHeight() {
    document.documentElement.style.setProperty("--reader-topbar-height", $("#topbar").getBoundingClientRect().height + "px");
  }
  function toastMsg(msg) {
    toast.textContent = msg;
    toast.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { toast.classList.remove("show"); }, 1800);
  }

  /* ========== theme & font ========== */
  function setTheme(name, save) {
    document.documentElement.setAttribute("data-theme", name);
    $$(".theme-btn").forEach(function (b) { b.classList.toggle("on", b.dataset.themeVal === name); });
    if (save !== false) { try { localStorage.setItem("tr-theme", name); } catch (e) {} }
  }
  function setFont(name, save) {
    document.documentElement.setAttribute("data-font", name);
    $$(".font-btn").forEach(function (b) { b.classList.toggle("on", b.dataset.fontVal === name); });
    if (save !== false) { try { localStorage.setItem("tr-font", name); } catch (e) {} }
  }
  (function initPrefs() {
    var t = "dark", f = "mid", w = 980;
    try {
      t = localStorage.getItem("tr-theme") || "dark";
      f = localStorage.getItem("tr-font") || "mid";
      w = parseInt(localStorage.getItem("tr-width"), 10) || 980;
    } catch (e) {}
    readerWidth = Math.max(720, Math.min(1180, w));
    document.documentElement.style.setProperty("--reader-width", readerWidth + "px");
    setTheme(t, false); setFont(f, false);
    var widthInput = $("#readerWidth");
    if (widthInput) widthInput.value = readerWidth;
    var widthVal = $("#readerWidthVal");
    if (widthVal) widthVal.textContent = readerWidth;
  })();
  $("#readerWidth").addEventListener("input", function (e) {
    readerWidth = parseInt(e.target.value, 10) || 980;
    document.documentElement.style.setProperty("--reader-width", readerWidth + "px");
    var widthVal = $("#readerWidthVal");
    if (widthVal) widthVal.textContent = readerWidth;
    try { localStorage.setItem("tr-width", String(readerWidth)); } catch (e2) {}
  });
  $$(".theme-btn").forEach(function (b) {
    b.addEventListener("click", function () { setTheme(b.dataset.themeVal); });
  });
  $$(".font-btn").forEach(function (b) {
    b.addEventListener("click", function () { setFont(b.dataset.fontVal); });
  });

  /* ========== shortcuts dialog ========== */
  var keysDlg = $("#keysDlg");
  function keysOpen() {
    keysDlg.classList.add("open");
    document.body.style.overflow = "hidden";
    $("#keysClose").focus();
  }
  function keysCloseFn() {
    keysDlg.classList.remove("open");
    document.body.style.overflow = "";
    $("#keysBtn").focus();
  }
  $("#keysBtn").addEventListener("click", keysOpen);
  $("#keysClose").addEventListener("click", keysCloseFn);
  keysDlg.addEventListener("click", function (e) { if (e.target === keysDlg) keysCloseFn(); });

  /* ========== TOC build ========== */
  function flattenToc(entries, out) {
    out = out || [];
    entries.forEach(function (e) {
      if (e.id) out.push(e);
      if (e.c) flattenToc(e.c, out);
    });
    return out;
  }

  function buildToc(mod) {
    var conf = MODULES[mod];
    tocTitle.textContent = conf.title;
    tocSub.textContent = conf.sub;
    tocList.innerHTML = "";
    renderTocEntries(conf.toc, tocList, 0);
  }

  function renderTocEntries(entries, parent, depth) {
    (entries || []).forEach(function (entry) {
      if (entry.g) {
        var gt = document.createElement("div");
        gt.className = "toc-group-title toc-depth-" + depth + (entry.fold ? " collapsible" : "");
        gt.innerHTML = "<span></span><span class='chev'>▼</span>";
        gt.firstChild.textContent = stripEmoji(entry.t);
        var items = document.createElement("div");
        items.className = "toc-items toc-depth-" + depth;
        if (entry.id) items.appendChild(makeTocLink({ id: entry.id, t: "分类目录" }, depth + 1));
        renderTocEntries(entry.c, items, depth + 1);
        if (entry.fold) {
          gt.setAttribute("role", "button");
          gt.tabIndex = 0;
          gt.setAttribute("aria-expanded", "true");
          if (entry.id || depth > 0) {
            gt.classList.add("closed");
            items.classList.add("closed");
            gt.setAttribute("aria-expanded", "false");
          }
          gt.addEventListener("click", function () {
            gt.classList.toggle("closed");
            items.classList.toggle("closed");
            gt.setAttribute("aria-expanded", String(!items.classList.contains("closed")));
          });
          gt.addEventListener("keydown", function (ev) {
            if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); gt.click(); }
          });
        }
        parent.appendChild(gt);
        parent.appendChild(items);
      } else {
        if (entry.id) parent.appendChild(makeTocLink(entry, depth));
        if (entry.c && entry.c.length) {
          var wrap = document.createElement("div");
          wrap.className = "toc-items toc-depth-" + depth;
          renderTocEntries(entry.c, wrap, depth + 1);
          parent.appendChild(wrap);
        }
      }
    });
  }

  function makeTocLink(node, depth) {
    var target = document.getElementById(node.id);
    if (target) target.classList.add("reader-anchor");
    var a = document.createElement("a");
    a.className = "toc-link" + (node.lvl === 2 ? " lvl2" : node.lvl === 3 ? " lvl3" : "") + (depth > 0 ? " toc-depth-" + depth : "");
    a.textContent = stripEmoji(node.t);
    a.dataset.target = node.id;
    a.href = MODULES[currentMod].route + "?section=" + encodeURIComponent(node.id);
    a.addEventListener("click", function (ev) {
      if (ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.altKey) return;
      if (location.hash === a.getAttribute("href")) { ev.preventDefault(); route(); }
      setSidebar(false);
    });
    return a;
  }

  function setActiveToc(id) {
    spyCurrentId = id;
    $$(".toc-link", tocList).forEach(function (a) {
      a.classList.toggle("active", a.dataset.target === id);
    });
    var act = $(".toc-link.active", tocList);
    if (act) {
      // ensure its group is open
      var items = act.closest(".toc-items");
      while (items) {
        if (items.classList.contains("closed")) items.classList.remove("closed");
        var gt = items.previousElementSibling;
        if (gt && gt.classList.contains("toc-group-title")) {
          gt.classList.remove("closed");
          if (gt.hasAttribute("aria-expanded")) gt.setAttribute("aria-expanded", "true");
        }
        items = items.parentElement && items.parentElement.closest(".toc-items");
      }
      if (!act.matches(":hover,:focus")) {
        var sidebar = $("#toc"), box = sidebar.getBoundingClientRect(), ar = act.getBoundingClientRect();
        if (ar.top < box.top + 16) sidebar.scrollTop += ar.top - box.top - 16;
        else if (ar.bottom > box.bottom - 16) sidebar.scrollTop += ar.bottom - box.bottom + 16;
      }
    }
  }

  /* ========== scroll-spy ========== */
  function setupSpy(mod) {
    if (spyObserver) { spyObserver.disconnect(); spyObserver = null; }
    var conf = MODULES[mod];
    var scope = document.getElementById(conf.scopeId);
    var ids = flattenToc(conf.toc).map(function (e) { return e.id; });
    var els = ids.map(function (id) { return document.getElementById(id); }).filter(Boolean);
    if (!els.length) return;
    spyObserver = new IntersectionObserver(function (entries) {
      if (spyTick) return;
      spyTick = true;
      requestAnimationFrame(function () {
        spyTick = false;
        var best = null, bestTop = -Infinity;
        els.forEach(function (el) {
          if (!el.getClientRects().length) return;
          var top = el.getBoundingClientRect().top;
          var offset = Math.max(120, parseFloat(getComputedStyle(el).scrollMarginTop) || 0);
          if (top <= offset + 2 && top > bestTop) { best = el; bestTop = top; }
        });
        if (!best) { bestTop = Infinity; els.forEach(function (el) { if (!el.getClientRects().length) return; var top = el.getBoundingClientRect().top; if (top >= 0 && top < bestTop) { best = el; bestTop = top; } }); }
        if (best) setActiveToc(best.id);
      });
    }, { rootMargin: "-72px 0px -65% 0px", threshold: [0, 0.01] });
    els.forEach(function (el) { spyObserver.observe(el); });
    if (els[0]) setActiveToc(els[0].id);
  }

  /* ========== progress / toTop ========== */
  function onScroll(ev) {
    var h = document.documentElement;
    var max = h.scrollHeight - h.clientHeight;
    var p = max > 0 ? (h.scrollTop / max) * 100 : 0;
    progressbar.style.width = p + "%";
    if (tocProgress) tocProgress.textContent = "阅读进度 " + Math.round(p) + "%";
    toTop.classList.toggle("show", h.scrollTop > 600);
    if (currentMod) scrollMemory[currentMod] = h.scrollTop;
    // ev 为空表示是启动时的手工调用（此时还没恢复位置），不能拿它覆盖已存进度
    if (ev) recordPos();
  }
  window.addEventListener("scroll", onScroll, { passive: true });
  toTop.addEventListener("click", function () { window.scrollTo({ top: 0, behavior: "smooth" }); });

  /* ========== drawer ========== */
  function setSidebar(open) {
    document.body.classList.toggle("sidebar-open", open);
    var mb = $("#menuBtn");
    mb.setAttribute("aria-expanded", String(open));
    mb.setAttribute("aria-label", open ? "关闭导航" : "打开导航");
  }
  $("#menuBtn").addEventListener("click", function () {
    setSidebar(!document.body.classList.contains("sidebar-open"));
  });
  $("#backdrop").addEventListener("click", function () {
    setSidebar(false);
  });
  window.addEventListener("resize", function () {
    if (window.innerWidth > 820) {
      setSidebar(false);
    }
  });

  /* ========== router ========== */
  function showLearningMenu(mod, sectionId) {
    var conf = MODULES[mod];
    var panels = $$("[data-learning-menu]", document.getElementById(conf.scopeId));
    if (!panels.length) return;
    var home = panels[0];
    var selected = sectionId || home.id;
    panels.forEach(function (panel) { show(panel, panel.id === selected); });
    var group = conf.toc.find(function (entry) {
      return entry.g && entry.id && entry.id !== selected && flattenToc(entry.c || []).some(function (child) { return child.id === selected; });
    });
    var parent = $("#tocParent");
    parent.href = conf.route + "?section=" + encodeURIComponent(group ? group.id : home.id);
    parent.textContent = "← " + (group ? group.t : "模块目录");
    show(parent, selected !== home.id);
  }

  function route() {
    var h = location.hash || "#/";
    var routePath = h.split("?")[0];
    var mod = ROUTE_TO_MOD[routePath];
    var sectionId = new URLSearchParams(h.indexOf("?") === -1 ? "" : h.slice(h.indexOf("?") + 1)).get("section");
    var changed = currentMod !== mod;
    flushPos();
    clearTimeout(searchTimer);
    clearSearch();
    if (settleTimer) { clearTimeout(settleTimer); settleTimer = null; }
    restoring = false;
    setSidebar(false);
    $("#tocParent").href = "#/harness";
    $("#tocParent").textContent = "← Harness";
    show($("#tocParent"), mod === "dsh" || mod === "codex-harness");
    if (!mod) {
      flushPos();                 // 回首页前先把位置写盘
      currentMod = null;
      show(viewReader, false);
      show(viewHome, true);
      // 返回首页时同时隐藏上一次打开的模块，避免首页与旧模块叠加可见。
      Object.keys(MODULES).forEach(function (k) {
        show(document.getElementById(MODULES[k].scopeId), false);
      });
      renderHomeProgress();
      setSidebar(false);
      window.scrollTo({ top: 0, behavior: "instant" });
      return;
    }
    show(viewHome, false);
    show(viewReader, true);
    Object.keys(MODULES).forEach(function (k) {
      show(document.getElementById(MODULES[k].scopeId), k === mod);
    });
    showLearningMenu(mod, sectionId);
    syncTopbarHeight();
    if (currentMod !== mod) {
      currentMod = mod;
      buildToc(mod);
      setupSpy(mod);
      clearSearch();
      updatePager();
      if (!sectionId && pendingRestore && pendingRestore.mod === mod) {
        var pr = pendingRestore;
        pendingRestore = null;
        restoreTo(pr.rec);
      } else {
        // 瞬移：否则从深处切模块会被 html{scroll-behavior:smooth} 拖成一段长动画
        try { window.scrollTo({ top: 0, left: 0, behavior: "instant" }); }
        catch (e) { window.scrollTo(0, 0); }
      }
      if (mod === "lg" && !lgRevealed) {
        lgRevealed = true;
        setupLgReveal();
        // mermaid fallback: render only after module is visible (getBBox needs layout)
        if (window.__mmRender) { setTimeout(window.__mmRender, 60); }
      }
      if (mod === "llm" && !llmRevealed) {
        llmRevealed = true;
        if (window.__mmRender) { setTimeout(window.__mmRender, 60); }
      }
    }
    var section = sectionId && document.getElementById(sectionId);
    var scope = document.getElementById(MODULES[mod].scopeId);
    if (section && scope.contains(section)) {
      pendingRestore = null;
      section.setAttribute("tabindex", "-1");
      section.focus({ preventScroll: true });
      section.scrollIntoView({ behavior: "instant", block: "start" });
      setActiveToc(sectionId);
    } else if (!sectionId && !changed) {
      window.scrollTo({ top: 0, behavior: "instant" });
      setActiveToc(flattenToc(MODULES[mod].toc)[0].id);
    }
  }
  window.addEventListener("hashchange", route);

  /* ========== pager ========== */
  function chapterList(mod) {
    var list = [];
    MODULES[mod].toc.forEach(function (e) {
      var first = firstTocLeaf(e);
      if (first) {
        list.push({ id: first.id, t: e.t || first.t, entry: e });
      }
    });
    return list;
  }
  function firstTocLeaf(entry) {
    if (entry.id) return entry;
    for (var i = 0; entry.c && i < entry.c.length; i++) {
      var first = firstTocLeaf(entry.c[i]);
      if (first) return first;
    }
    return null;
  }
  function chapterIndex(mod) {
    var list = chapterList(mod);
    if (!spyCurrentId) return 0;
    var inChapter = -1;
    for (var i = 0; i < list.length; i++) {
      var ids = flattenToc([list[i].entry]).map(function (x) { return x.id; });
      if (ids.indexOf(spyCurrentId) !== -1) { inChapter = i; break; }
    }
    return inChapter === -1 ? 0 : inChapter;
  }
  function updatePager() {
    var list = chapterList(currentMod);
    var idx = chapterIndex(currentMod);
    var prev = list[idx - 1], next = list[idx + 1];
    setPgBtn(pagerPrev, prev, "← 上一章");
    setPgBtn(pagerNext, next, "下一章 →");
    pagerPrev.disabled = !prev; pagerNext.disabled = !next;
  }
  function setPgBtn(btn, ch, label) {
    if (ch) { btn.innerHTML = ""; btn.appendChild(document.createTextNode(label));
      var s = document.createElement("small"); s.textContent = ch.t; btn.appendChild(s); }
    else { btn.textContent = label; }
  }
  function gotoChapter(delta) {
    if (!currentMod) return;
    var list = chapterList(currentMod);
    var idx = chapterIndex(currentMod) + delta;
    if (idx < 0 || idx >= list.length) return;
    var href = MODULES[currentMod].route + "?section=" + encodeURIComponent(list[idx].id);
    if (location.hash === href) route();
    else location.hash = href;
  }
  pagerPrev.addEventListener("click", function () { gotoChapter(-1); });
  pagerNext.addEventListener("click", function () { gotoChapter(1); });
  // scroll-spy drives pager refresh (cheap)
  var _setActiveToc = setActiveToc;
  setActiveToc = function (id) { _setActiveToc(id); if (currentMod) updatePager(); };

  /* ========== keyboard ========== */
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && document.body.classList.contains("sidebar-open")) {
      setSidebar(false);
      $("#menuBtn").focus();
      return;
    }
    if (keysDlg && keysDlg.classList.contains("open")) {
      if (e.key === "Escape") keysCloseFn();
      else if (e.key === "Enter" && !(e.target.closest && e.target.closest("button, a, input, select, [role='button']"))) { e.preventDefault(); keysCloseFn(); }
      return;
    }
    if (tpPop && tpPop.classList.contains("open")) {
      if (e.key === "Escape") tpClose();
      else if (e.key === "Enter" && !(e.target.closest && e.target.closest("button, a, input, select, [role='button']"))) { e.preventDefault(); tpGotoChapter(); }
      return;
    }
    if (resumeDlg && resumeDlg.classList.contains("open")) {
      if (e.key === "Escape") resumeClose();
      else if (e.key === "Enter" && !(e.target.closest && e.target.closest("button, a, input, select, [role='button']"))) { e.preventDefault(); resumeGo(); }
      return;
    }
    if (lightbox.classList.contains("open")) {
      if (e.key === "Escape") lbCloseFn();
      else if (e.key === "ArrowLeft") lbNav(-1);
      else if (e.key === "ArrowRight") lbNav(1);
      return;
    }
    var tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea") {
      if (e.key === "Escape") e.target.blur();
      return;
    }
    if (e.target.closest("button, a, summary, select, [role='button']")) return;
    if (e.key === "?") { e.preventDefault(); keysOpen(); return; }
    if (!currentMod) return;
    if (e.key === "ArrowLeft") { e.preventDefault(); gotoChapter(-1); }
    else if (e.key === "ArrowRight") { e.preventDefault(); gotoChapter(1); }
    else if (e.key === "j" || e.key === "J") { window.scrollBy({ top: window.innerHeight * 0.38, behavior: "smooth" }); }
    else if (e.key === "k" || e.key === "K") { window.scrollBy({ top: -window.innerHeight * 0.38, behavior: "smooth" }); }
    else if (e.key === " ") {
      e.preventDefault();
      window.scrollBy({ top: (e.shiftKey ? -0.85 : 0.85) * window.innerHeight, behavior: "smooth" });
    }
    else if (e.key === "/") { e.preventDefault(); searchInput.focus(); }
  });

  /* ========== search: shared hit-marking engine ========== */
  var hitMarks = [];
  var hitCursor = -1;

  function unwrapMarks() {
    hitMarks.forEach(function (m) {
      var p = m.parentNode;
      if (!p) return;
      p.replaceChild(document.createTextNode(m.textContent), m);
      p.normalize();
    });
    hitMarks = [];
    hitCursor = -1;
  }

  function markIn(root, q) {
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        if (!node.nodeValue || node.nodeValue.toLowerCase().indexOf(q) === -1) return NodeFilter.FILTER_REJECT;
        var p = node.parentNode;
        while (p && p !== root) {
          var t = p.tagName;
          if (t === "SCRIPT" || t === "STYLE" || t === "MARK") return NodeFilter.FILTER_REJECT;
          if (p.hasAttribute("data-learning-menu") && p.hidden) return NodeFilter.FILTER_REJECT;
          p = p.parentNode;
        }
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    var nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    var ql = q.length;
    nodes.forEach(function (node) {
      var text = node.nodeValue;
      var low = text.toLowerCase();
      var frag = document.createDocumentFragment();
      var pos = 0, idx;
      while ((idx = low.indexOf(q, pos)) !== -1) {
        if (idx > pos) frag.appendChild(document.createTextNode(text.slice(pos, idx)));
        var mk = document.createElement("mark");
        mk.className = "hit";
        mk.textContent = text.slice(idx, idx + ql);
        frag.appendChild(mk);
        hitMarks.push(mk);
        pos = idx + ql;
      }
      frag.appendChild(document.createTextNode(text.slice(pos)));
      node.parentNode.replaceChild(frag, node);
    });
  }

  function gotoHit(n) {
    if (!hitMarks.length) return;
    hitCursor = ((n % hitMarks.length) + hitMarks.length) % hitMarks.length;
    hitMarks.forEach(function (m, i) { m.classList.toggle("hit-cur", i === hitCursor); });
    var parent = hitMarks[hitCursor].parentElement;
    while (parent) {
      if (parent.tagName === "DETAILS") parent.open = true;
      parent = parent.parentElement;
    }
    hitMarks[hitCursor].scrollIntoView({ block: "center", behavior: "smooth" });
  }

  /* ---- toc filter for prose modules ---- */
  function filterTocByHits() {
    var links = $$(".toc-link", tocList);
    if (!hitMarks.length) {
      links.forEach(function (a) { a.classList.remove("hidden"); });
      return 0;
    }
    // assign each mark to nearest preceding toc target
    var targets = links.map(function (a) { return document.getElementById(a.dataset.target); });
    var has = targets.map(function () { return false; });
    hitMarks.forEach(function (m) {
      for (var i = targets.length - 1; i >= 0; i--) {
        var t = targets[i];
        if (!t) continue;
        if (t.compareDocumentPosition(m) & Node.DOCUMENT_POSITION_FOLLOWING) { has[i] = true; break; }
      }
    });
    links.forEach(function (a, i) { a.classList.toggle("hidden", !has[i]); });
    return has.filter(Boolean).length;
  }

  /* ---- per-module adapters ---- */
  var searchAdapters = {
    book: makeProseAdapter("mod-book"),
    lg: makeProseAdapter("mod-lg"),
    llm: makeProseAdapter("mod-llm"),
    harness: makeProseAdapter("mod-harness"),
    "codex-harness": makeProseAdapter("mod-codex-harness"),
    dsh: makeDshAdapter()
  };

  function makeProseAdapter(scopeId) {
    return {
      apply: function (q) {
        var scope = document.getElementById(scopeId);
        markIn(scope, q);
        var secs = filterTocByHits();
        if (!hitMarks.length) {
          searchStatus.textContent = "无结果 · 换个关键词试试";
        } else {
          searchStatus.innerHTML = "<b>" + hitMarks.length + "</b> 处命中 · " + secs + " 个小节 · Enter 跳转";
          gotoHit(0);
        }
      },
      clear: function () {
        unwrapMarks();
        filterTocByHits();
      }
    };
  }

  function makeDshAdapter() {
    var scope = function () { return document.getElementById("mod-dsh"); };
    var origTitles = new WeakMap();
    return {
      apply: function (q) {
        var cards = $$(".card", scope());
        var visibles = 0;
        var directory = $(".harness-scope", scope());
        if (directory) markIn(directory, q);
        var directoryHits = hitMarks.length;
        cards.forEach(function (c) {
          var kw = (c.getAttribute("data-kw") || "") + " " + c.textContent.toLowerCase();
          var hit = kw.indexOf(q) !== -1;
          c.classList.toggle("hidden", !hit);
          var t = c.querySelector(".tname");
          if (t) {
            if (!origTitles.has(t)) origTitles.set(t, t.textContent);
            var orig = origTitles.get(t);
            t.innerHTML = hit ? orig.replace(new RegExp("(" + escRe(q) + ")", "gi"), "<mark class='hit'>$1</mark>") : orig;
          }
          if (hit) { visibles++; c.classList.add("open"); markIn(c, q); }
        });
        // hide sections with no visible card
        $$("section", scope()).forEach(function (sec) {
          var any = $$(".card", sec).some(function (c) { return !c.classList.contains("hidden"); });
          var headHit = (sec.querySelector(".sec-head") || {}).textContent &&
                        sec.querySelector(".sec-head").textContent.toLowerCase().indexOf(q) !== -1;
          sec.classList.toggle("hidden", !any && !headHit && $$(".card", sec).length > 0);
        });
        searchStatus.innerHTML = "<b>" + visibles + "</b> / " + cards.length + " 个模块";
        if (directoryHits) searchStatus.appendChild(document.createTextNode(" · 目录 " + directoryHits + " 处命中"));
        if (!visibles && !directoryHits) searchStatus.textContent = "无结果 · 换个关键词试试";
      },
      clear: function () {
        unwrapMarks();
        $$(".card", scope()).forEach(function (c) {
          c.classList.remove("hidden");
          var t = c.querySelector(".tname");
          if (t && origTitles.has(t)) t.textContent = origTitles.get(t);
        });
        $$("section.hidden", scope()).forEach(function (s) { s.classList.remove("hidden"); });
      }
    };
  }

  function clearSearch() {
    searchInput.value = "";
    searchStatus.textContent = "";
    if (currentMod && searchAdapters[currentMod]) searchAdapters[currentMod].clear();
    $$(".toc-link", tocList).forEach(function (a) { a.classList.remove("hidden"); });
  }

  var searchTimer = null;
  searchInput.addEventListener("input", function () {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(function () {
      var q = searchInput.value.trim().toLowerCase();
      if (!currentMod) return;
      searchAdapters[currentMod].clear();
      if (!q) { searchStatus.textContent = ""; return; }
      searchAdapters[currentMod].apply(q);
    }, 160);
  });
  searchInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      e.preventDefault();
      if (hitMarks.length) gotoHit(hitCursor + (e.shiftKey ? -1 : 1));
    }
  });

  /* ========== copy buttons ========== */
  function copyText(text, btn) {
    function done() {
      toastMsg("已复制到剪贴板");
      if (btn) {
        var old = btn.textContent;
        btn.textContent = "已复制";
        btn.classList.add("copied");
        setTimeout(function () { btn.textContent = old; btn.classList.remove("copied"); }, 1600);
      }
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { legacyCopy(text); done(); });
    } else { legacyCopy(text); done(); }
  }
  function legacyCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.cssText = "position:fixed;opacity:0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) {}
    document.body.removeChild(ta);
  }

  function enhanceCopyButtons() {
    // book codeblocks
    $$("#mod-book .codeblock, #mod-harness .codeblock").forEach(function (wrap) {
      var lang = wrap.getAttribute("data-lang") || "";
      if (lang && lang !== "text") {
        var tag = document.createElement("span");
        tag.className = "code-lang-tag";
        tag.textContent = lang;
        wrap.insertBefore(tag, wrap.firstChild);
      }
      var btn = document.createElement("button");
      btn.className = "code-copy";
      btn.textContent = "复制";
      btn.addEventListener("click", function () {
        var pre = $("pre", wrap);
        copyText(pre ? pre.innerText : "", btn);
      });
      wrap.appendChild(btn);
    });
    // dsh code blocks (.blk.code contains pre)
    $$("#mod-dsh .blk.code").forEach(function (blk) {
      var pre = $("pre", blk);
      if (!pre) return;
      var btn = document.createElement("button");
      btn.className = "copy-fab";
      btn.textContent = "复制";
      btn.addEventListener("click", function () { copyText(pre.innerText, btn); });
      blk.appendChild(btn);
    });
    // lg / llm code blocks: insert into existing .code-header
    $$("#mod-lg .code-block-wrapper, #mod-llm .code-block-wrapper, #mod-codex-harness .code-block-wrapper").forEach(function (wrap) {
      var pre = $("pre", wrap);
      if (!pre) return;
      var header = $(".code-header", wrap);
      var btn = document.createElement("button");
      btn.className = "copy-btn";
      btn.textContent = "复制";
      btn.addEventListener("click", function () { copyText(pre.innerText, btn); });
      if (header) header.appendChild(btn);
      else {
        btn.classList.add("copy-fab");
        wrap.appendChild(btn);
      }
      // horizontal-scroll hint
      if (pre.scrollWidth > pre.clientWidth + 4) wrap.classList.add("scrollable");
    });
    // MiniMind full-source appendix: keep closed files cheap, add copy on first open.
    $$("#mod-llm .source-file").forEach(function (details) {
      details.addEventListener("toggle", function () {
        if (!details.open || details.dataset.enhanced) return;
        var pre = $("pre", details), summary = $("summary", details);
        if (!pre || !summary) return;
        var btn = document.createElement("button");
        btn.type = "button"; btn.className = "source-copy"; btn.textContent = "复制";
        btn.addEventListener("click", function (ev) { ev.preventDefault(); ev.stopPropagation(); copyText(pre.innerText, btn); });
        summary.appendChild(btn); details.dataset.enhanced = "1";
      });
    });
  }

  /* ========== DSH card collapse (event delegation) ========== */
  document.getElementById("mod-dsh").addEventListener("click", function (e) {
    var head = e.target.closest(".card-head");
    if (head && document.getElementById("mod-dsh").contains(head)) {
      head.parentElement.classList.toggle("open");
    }
  });

  /* ========== LG reveal animations ========== */
  function setupLgReveal() {
    var scope = document.getElementById("mod-lg");
    var secs = $$(".section", scope);
    var obs = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) { en.target.classList.add("visible"); obs.unobserve(en.target); }
      });
    }, { threshold: 0.03 });
    secs.forEach(function (s) { obs.observe(s); });
    // steps: keep it simple — reveal all with stagger
    $$(".step", scope).forEach(function (st, i) {
      st.style.opacity = "0";
      st.style.transform = "translateX(-14px)";
      st.style.transition = "opacity .45s ease " + (i % 8) * 0.05 + "s, transform .45s ease " + (i % 8) * 0.05 + "s";
    });
    var sobs = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) {
          en.target.style.opacity = "1";
          en.target.style.transform = "translateX(0)";
          sobs.unobserve(en.target);
        }
      });
    }, { threshold: 0.1 });
    $$(".step", scope).forEach(function (st) { sobs.observe(st); });
  }

  /* ========== lightbox ========== */
  var lightbox = $("#lightbox"), lbImgWrap = $(".lb-imgwrap", lightbox),
      lbCap = $(".lb-cap", lightbox), lbCount = $(".lb-count", lightbox);
  var lbItems = [], lbIndex = 0;

  function collectMedia() {
    lbItems = [];
    $$("#mod-book .fig-img, #mod-codex-harness .fig-img, #mod-llm .fig-img, #mod-llm .happy-gallery-item img").forEach(function (img) {
      lbItems.push({ kind: "img", src: img.src, cap: img.alt || "" });
    });
    $$(".diagram", document.getElementById("mod-lg")).forEach(function (d, i) {
      var svg = $("svg", d);
      var cap = $(".caption", d);
      lbItems.push({ kind: "svg", node: svg, cap: cap ? cap.textContent : "流程图 " + (i + 1), diag: d });
    });
  }
  var lbTrigger = null;
  function lbOpen(item) {
    lbTrigger = document.activeElement;
    lbIndex = lbItems.indexOf(item);
    lbRender();
    lightbox.classList.add("open");
    document.body.style.overflow = "hidden";
    $("#lbClose").focus();
  }
  function lbRender() {
    var item = lbItems[lbIndex];
    lbImgWrap.innerHTML = "";
    if (item.kind === "img") {
      var im = document.createElement("img");
      im.src = item.displaySrc || item.src;
      im.alt = item.cap || "";
      lbImgWrap.appendChild(im);
    } else {
      var theme = document.documentElement.getAttribute("data-theme");
      var variant = $(".mm-variant." + (theme === "dark" ? "mm-dark" : "mm-light") + " svg", item.diag) || $("svg", item.diag);
      lbImgWrap.appendChild(variant.cloneNode(true));
    }
    lbCap.textContent = item.cap || "";
    lbCount.textContent = (lbIndex + 1) + " / " + lbItems.length;
    $("#lbPrev").style.visibility = lbIndex > 0 ? "visible" : "hidden";
    $("#lbNext").style.visibility = lbIndex < lbItems.length - 1 ? "visible" : "hidden";
  }
  function lbNav(d) {
    var n = lbIndex + d;
    if (n < 0 || n >= lbItems.length) return;
    lbIndex = n;
    lbRender();
  }
  function lbCloseFn() {
    lightbox.classList.remove("open");
    document.body.style.overflow = "";
    if (lbTrigger) lbTrigger.focus({ preventScroll: true });
  }
  $("#lbPrev").addEventListener("click", function (e) { e.stopPropagation(); lbNav(-1); });
  $("#lbNext").addEventListener("click", function (e) { e.stopPropagation(); lbNav(1); });
  $("#lbClose").addEventListener("click", lbCloseFn);
  lightbox.addEventListener("click", function (e) { if (e.target === lightbox) lbCloseFn(); });

  document.getElementById("content").addEventListener("click", function (e) {
    var routeLink = e.target.closest("a[href]");
    if (routeLink && !e.ctrlKey && !e.metaKey && !e.shiftKey && !e.altKey &&
        routeLink.getAttribute("href") === location.hash && location.hash.indexOf("?section=") !== -1) {
      e.preventDefault(); route(); return;
    }
    var tpm = e.target.closest(".tp-mark");
    if (tpm) { e.preventDefault(); e.stopPropagation(); tpOpen(tpm.getAttribute("data-tp"), tpm); return; }
    // cross-module goto links (llm feature cards / 模块互链)
    var goto = e.target.closest("[data-goto]");
    if (goto) {
      var gt = document.getElementById(goto.getAttribute("data-goto"));
      if (gt) { gt.scrollIntoView({ behavior: "smooth", block: "start" }); setActiveToc(gt.id); }
      return;
    }
    var jump = e.target.closest && e.target.closest("a.quick-jump");
    if (jump) {
      var targetId = (jump.getAttribute("href") || "").slice(1), target = document.getElementById(targetId);
      if (target) {
        var details = target.closest("details");
        if (details) details.open = true;
        target.classList.add("jump-target");
        setTimeout(function () { target.classList.remove("jump-target"); }, 1600);
      }
    }
    var imageButton = e.target.closest(".cx-diagram-open");
    var img = imageButton ? $(".fig-img", imageButton) : e.target.closest(".fig-img, .happy-gallery-item img");
    if (img) {
      var items = lbItems.filter(function (x) { return x.kind === "img"; });
      var found = null;
      items.forEach(function (x) { if (x.src === img.src) found = found || x; });
      if (found) {
        found.displaySrc = img.currentSrc || img.src;
        lbOpen(found); return;
      }
    }
    var diag = e.target.closest(".diagram");
    if (diag) {
      var ditems = lbItems.filter(function (x) { return x.kind === "svg" && x.diag === diag; });
      if (ditems.length) lbOpen(ditems[0]);
    }
  });

  /* ========== print ========== */
  $("#printBtn").addEventListener("click", function () { window.print(); });
  window.addEventListener("beforeprint", function () { document.body.classList.add("printing"); });
  window.addEventListener("afterprint", function () { document.body.classList.remove("printing"); });

  /* ========== 技术点角标（req #2） ========== */
  // 注册表由内容脚本注入；key -> { t 标题, cat 分类, tgt 目标章节 id, sum 摘要 HTML, dia Mermaid 源 }
  var TECH_POINTS = window.TECH_POINTS || {};
  var tpPop = null, tpBack = null, tpCur = null, tpReturn = null;

  function tpFindMod(id) {
    var el = document.getElementById(id);
    var m = el && el.closest(".module");
    if (!m) return null;
    var found = null;
    Object.keys(MODULES).forEach(function (k) { if (MODULES[k].scopeId === m.id) found = k; });
    return found;
  }
  function tpClose() {
    if (!tpPop) return;
    tpPop.classList.remove("open");
    document.body.style.overflow = "";
    tpCur = null;
  }
  function tpOpen(key, mark) {
    var tp = TECH_POINTS[key];
    if (!tp) { toastMsg("这个技术点还没有详情"); return; }
    tpCur = key;
    $("#tpCat").textContent = tp.cat || "技术点";
    $("#tpTitle").textContent = tp.t || key;
    var html = tp.sum || "<p>暂无摘要。</p>";
    if (tp.dia) {
      html += '<div class="diagram" data-mm-src="' + String(tp.dia).replace(/&/g, "&amp;").replace(/"/g, "&quot;") +
              '"><pre class="mermaid">' + String(tp.dia).replace(/&/g, "&amp;").replace(/</g, "&lt;") + "</pre></div>";
    }
    $("#tpBody").innerHTML = html;
    $("#tpBody").scrollTop = 0;
    var goto = $("#tpGoto");
    var tgtOk = tp.tgt && document.getElementById(tp.tgt);
    goto.disabled = !tgtOk;
    goto.textContent = tgtOk ? "查看完整章节 →" : "暂无对应章节";
    $("#tpSrc").textContent = tgtOk ? (MODULES[tpFindMod(tp.tgt)] || {}).title || "" : "";
    tpPop.classList.add("open");
    document.body.style.overflow = "hidden";
    if (mark) mark.classList.add("seen");
    // 弹窗不在 .module 内且带 .llm-scope，现成的 Mermaid 渲染器会认领它
    if (tp.dia && window.__mmRender) setTimeout(window.__mmRender, 30);
    setTimeout(function () { goto.focus(); }, 30);
  }
  function tpShowBack(label) {
    if (!tpBack) return;
    $("#tpBackWhere").textContent = label ? "· " + label : "";
    tpBack.classList.add("show");
  }
  function tpHideBack() { if (tpBack) tpBack.classList.remove("show"); tpReturn = null; }
  function tpGotoChapter() {
    var tp = TECH_POINTS[tpCur];
    if (!tp || !tp.tgt) return;
    var mod = tpFindMod(tp.tgt);
    if (!mod) { toastMsg("找不到对应章节"); return; }
    // 记下当前位置，供「返回原处」使用
    var h = document.documentElement;
    tpReturn = {
      mod: currentMod, hash: location.hash || "#/",
      ch: spyCurrentId, top: Math.round(h.scrollTop),
      off: spyCurrentId && document.getElementById(spyCurrentId)
           ? Math.round(-document.getElementById(spyCurrentId).getBoundingClientRect().top) : 0,
      label: currentMod ? chapterTitle(currentMod, spyCurrentId) || MODULES[currentMod].title : "首页"
    };
    tpClose();
    var rec = { ch: tp.tgt, off: -12, top: 0 };
    if (currentMod === mod) { restoreTo(rec); }
    else {
      pendingRestore = { mod: mod, rec: rec };
      if (location.hash === MODULES[mod].route) route();
      else location.hash = MODULES[mod].route;
    }
    tpShowBack(tpReturn.label);
  }
  function tpGoBack() {
    var r = tpReturn;
    if (!r) return;
    tpHideBack();
    if (!r.mod) { location.hash = r.hash || "#/"; return; }
    var rec = { ch: r.ch, off: r.off, top: r.top };
    if (currentMod === r.mod) { restoreTo(rec); }
    else {
      pendingRestore = { mod: r.mod, rec: rec };
      if (location.hash === MODULES[r.mod].route) route();
      else location.hash = MODULES[r.mod].route;
    }
  }

  /* ========== 续看 / 阅读进度（req #1） ========== */
  var PROG_KEY = "tr-progress";   // { mod: { pct, top, ch, off, at } }
  var LAST_KEY = "tr-last";       // 最后阅读的模块
  var progData = {};
  var pendingRestore = null;      // 路由切换完成后要恢复的位置
  var saveTimer = null;
  var pendingLastMod = null;
  var restoring = false;          // 恢复进行中：抑制 recordPos
  var settleTimer = null;
  var resumeDlg = null;

  function progLoad() {
    try {
      var raw = localStorage.getItem(PROG_KEY);
      progData = raw ? JSON.parse(raw) : {};
    } catch (e) { progData = {}; }
    if (!progData || typeof progData !== "object") progData = {};
  }
  function progSave() {
    try { localStorage.setItem(PROG_KEY, JSON.stringify(progData)); } catch (e) {}
  }
  function progCommit() {
    if (pendingLastMod) { progSetLastMod(pendingLastMod); pendingLastMod = null; }
    progSave();
  }
  function progLastMod() {
    try { return localStorage.getItem(LAST_KEY) || null; } catch (e) { return null; }
  }
  function progSetLastMod(mod) {
    try {
      if (mod) localStorage.setItem(LAST_KEY, mod);
      else localStorage.removeItem(LAST_KEY);
    } catch (e) {}
  }
  function chapterTitle(mod, id) {
    var conf = MODULES[mod];
    if (!conf || !id) return "";
    var hit = null;
    flattenToc(conf.toc).forEach(function (e) { if (e.id === id && !hit) hit = e; });
    return hit ? hit.t : "";
  }
  function scheduleSave() {
    if (saveTimer) return;
    saveTimer = setTimeout(function () { saveTimer = null; progCommit(); }, 600);
  }
  function recordPos() {
    if (!currentMod || restoring) return;   // 恢复期间不记录，避免覆盖目标位置
    var h = document.documentElement;
    var max = h.scrollHeight - h.clientHeight;
    var pct = max > 0 ? (h.scrollTop / max) * 100 : 0;
    if (pct < 0) pct = 0; else if (pct > 100) pct = 100;
    var rec = progData[currentMod] || (progData[currentMod] = {});
    rec.pct = Math.round(pct * 10) / 10;
    rec.top = Math.round(h.scrollTop);
    rec.at = Date.now();
    if (spyCurrentId) {
      rec.ch = spyCurrentId;
      var el = document.getElementById(spyCurrentId);
      // 存「章节 + 章内偏移」而非纯 scrollTop：图渲染后整体高度会变，章节锚点更稳
      rec.off = el ? Math.round(-el.getBoundingClientRect().top) : 0;
    }
    pendingLastMod = currentMod;
    scheduleSave();
  }
  function flushPos() {
    recordPos();
    if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
    progCommit();
  }
  function targetOf(rec) {
    var h = document.documentElement;
    var target = null;
    if (rec.ch) {
      var el = document.getElementById(rec.ch);
      // 用「章节锚点 + 章内偏移」而非绝对 scrollTop：图渲染完高度会变，锚点才稳
      if (el) target = el.getBoundingClientRect().top + h.scrollTop + (rec.off || 0);
    }
    if (target === null) target = rec.top || 0;
    var max = h.scrollHeight - h.clientHeight;
    if (target < 0) target = 0; else if (target > max) target = max;
    return Math.round(target);
  }
  function applyRestore(rec) {
    if (!rec) return;
    // instant：html{scroll-behavior:smooth} 会让 scrollTo 变成动画，
    // 动画期间的中间位置会污染后续计算，这里必须瞬移
    try { window.scrollTo({ top: targetOf(rec), left: 0, behavior: "instant" }); }
    catch (e) { window.scrollTo(0, targetOf(rec)); }
  }
  function restoreTo(rec) {
    if (!rec) return;
    showLearningMenu(currentMod, rec.ch);
    // 拷一份快照：progData[mod] 可能被后续写入改动，恢复过程不能跟着变
    var snap = { ch: rec.ch, off: rec.off, top: rec.top };
    restoring = true;
    if (settleTimer) { clearTimeout(settleTimer); settleTimer = null; }
    if (snap.ch) setActiveToc(snap.ch);
    var stable = 0, last = -1, deadline = Date.now() + 6000;
    (function tick() {
      var t = targetOf(snap);
      var cur = Math.round(document.documentElement.scrollTop);
      // 两个条件都要满足才算稳：目标算出来不再变，且上一次真的滚到位了
      // （只看目标不够——若有别的逻辑在我们之后又滚了一下，误差会被当成收敛）
      var settled = Math.abs(t - last) <= 2 && Math.abs(cur - t) <= 2;
      applyRestore(snap);
      last = t;
      stable = settled ? stable + 1 : 0;
      if (stable >= 3 || Date.now() > deadline) {
        settleTimer = null;
        restoring = false;
        onScroll();
        return;
      }
      settleTimer = setTimeout(tick, 110);
    })();
  }
  function fmtWhen(ts) {
    if (!ts) return "";
    var d = Date.now() - ts;
    if (d < 60000) return "刚刚";
    if (d < 3600000) return Math.floor(d / 60000) + " 分钟前";
    if (d < 86400000) return Math.floor(d / 3600000) + " 小时前";
    if (d < 2592000000) return Math.floor(d / 86400000) + " 天前";
    var dt = new Date(ts);
    return dt.getFullYear() + "-" + ("0" + (dt.getMonth() + 1)).slice(-2) + "-" + ("0" + dt.getDate()).slice(-2);
  }
  function renderHomeProgress() {
    $$(".home-card").forEach(function (card) {
      var mod = ROUTE_TO_MOD[card.getAttribute("href")];
      if (!mod) return;
      var box = card.querySelector(".prog");
      if (!box) {
        box = document.createElement("div");
        box.className = "prog";
        box.innerHTML = '<div class="bar"><i></i></div><div class="txt"></div>';
        // 必须插在 .go 之前：.go{margin-top:auto} 负责把【开始阅读 →】压到卡片底部对齐（req #3）
        card.insertBefore(box, card.querySelector(".go"));
      }
      var rec = progData[mod];
      var pct = rec && rec.pct ? rec.pct : 0;
      if (pct < 0.5) { box.classList.remove("on"); return; }
      box.classList.add("on");
      box.querySelector("i").style.width = pct + "%";
      var ch = chapterTitle(mod, rec.ch);
      box.querySelector(".txt").textContent = "已读 " + Math.round(pct) + "%" + (ch ? " · " + ch : "");
    });
  }
  function resumeClose() {
    if (!resumeDlg) return;
    resumeDlg.classList.remove("open");
    document.body.style.overflow = "";
  }
  function resumeGo() {
    var mod = progLastMod();
    resumeClose();
    if (!mod || !MODULES[mod]) return;
    var rec = progData[mod] || {};
    if (currentMod === mod) {
      restoreTo(rec);   // 已在该模块内，route 不会走切换分支，直接恢复
      return;
    }
    pendingRestore = { mod: mod, rec: rec };
    if (location.hash === MODULES[mod].route) route();
    else location.hash = MODULES[mod].route;
  }
  function resumeAsk() {
    var mod = progLastMod();
    if (!mod || !MODULES[mod]) return false;
    var rec = progData[mod];
    if (!rec || !(rec.pct > 0.5)) return false;
    $("#resumeMod").textContent = MODULES[mod].title;
    var ch = chapterTitle(mod, rec.ch);
    $("#resumeCh").textContent = ch ? " · " + ch : "";
    $("#resumeMeta").textContent = "上次阅读：" + fmtWhen(rec.at) + " · 已读约 " + Math.round(rec.pct) + "%";
    resumeDlg.classList.add("open");
    document.body.style.overflow = "hidden";
    setTimeout(function () { var b = $("#resumeYes"); if (b) b.focus(); }, 30);
    return true;
  }

  /* ========== boot ========== */
  // 浏览器自带的滚动恢复会在我们定位之后再抢一次，必须关掉
  try { if ("scrollRestoration" in history) history.scrollRestoration = "manual"; } catch (e) {}
  progLoad();
  resumeDlg = $("#resumeDlg");
  tpPop = $("#tpPop");
  tpBack = $("#tpBack");
  $("#tpClose").addEventListener("click", tpClose);
  $("#tpGoto").addEventListener("click", tpGotoChapter);
  tpPop.addEventListener("click", function (e) { if (e.target === tpPop) tpClose(); });
  tpBack.addEventListener("click", function (e) {
    if (e.target && e.target.id === "tpBackX") { e.stopPropagation(); tpHideBack(); return; }
    tpGoBack();
  });
  // 角标也可用键盘触发
  document.addEventListener("keydown", function (e) {
    if ((e.key === "Enter" || e.key === " ") && e.target && e.target.classList
        && e.target.classList.contains("tp-mark")) {
      e.preventDefault();
      tpOpen(e.target.getAttribute("data-tp"), e.target);
    }
  });
  $("#resumeNo").addEventListener("click", resumeClose);
  $("#resumeYes").addEventListener("click", resumeGo);
  resumeDlg.addEventListener("click", function (e) { if (e.target === resumeDlg) resumeClose(); });
  window.addEventListener("beforeunload", flushPos);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") flushPos();
  });

  collectMedia();
  var topbarObserver = new ResizeObserver(syncTopbarHeight);
  topbarObserver.observe($("#topbar"));
  enhanceCopyButtons();
  renderHomeProgress();
  route();
  onScroll();
  // 只在停留首页时询问续看；直接带 #/xxx 打开视为用户已指定去处
  if (!location.hash || location.hash === "#/") resumeAsk();
})();
