
(function () {
  if (!window.mermaid) return;
  // disable auto-start BEFORE DOMContentLoaded (we render lazily on first lg activation)
  mermaid.initialize({ startOnLoad: false });
  function isDark() { return document.documentElement.getAttribute("data-theme") === "dark"; }
  function vars(dark) {
    return dark ? {
      primaryColor: "#1e3a5f", primaryBorderColor: "#60a5fa", primaryTextColor: "#e5e7eb",
      lineColor: "#60a5fa", secondaryColor: "#2a1215", tertiaryColor: "#14261a",
      background: "#111318", mainBkg: "#111318", nodeBorder: "#60a5fa",
      clusterBkg: "#1e2028", clusterBorder: "#374151",
      fontFamily: "-apple-system, BlinkMacSystemFont, 'PingFang SC', sans-serif"
    } : {
      primaryColor: "#e3eafd", primaryBorderColor: "#1d4ed8", primaryTextColor: "#111827",
      lineColor: "#1d4ed8", secondaryColor: "#fce5e4", tertiaryColor: "#d1fae4",
      background: "#ffffff",
      fontFamily: "-apple-system, BlinkMacSystemFont, 'PingFang SC', sans-serif"
    };
  }
  // stash sources so we can re-render on theme change
  document.querySelectorAll(".lg-scope .diagram, .llm-scope .diagram").forEach(function (d) {
    var pre = d.querySelector("pre.mermaid");
    if (pre && !d.getAttribute("data-mm-src")) d.setAttribute("data-mm-src", pre.textContent);
  });
  function renderAll() {
    if (!window.mermaid) return;
    var dark = isDark();
    mermaid.initialize({
      startOnLoad: false, theme: dark ? "dark" : "base", themeVariables: vars(dark),
      flowchart: { useMaxWidth: true, htmlLabels: true, curve: "basis" }
    });
    document.querySelectorAll(".lg-scope .diagram, .llm-scope .diagram").forEach(function (d) {
      // skip diagrams in hidden modules: getBBox fails in display:none
      var holder = d.closest(".module");
      if (holder && holder.hidden) return;
      var src = d.getAttribute("data-mm-src");
      if (!src) return;
      var svg = d.querySelector("svg");
      var pre = d.querySelector("pre.mermaid");
      if (svg) {
        // 重建容器而不是 replaceChild：querySelector("svg") 会匹配任意深度的后代，
        // 而 replaceChild 只接受直接子节点，渲染结果有包装层时会抛 NotFoundError。
        // data-mm-src 里存着源码，整体重建是安全的。
        d.innerHTML = "";
        pre = document.createElement("pre");
        pre.className = "mermaid";
        pre.textContent = src;
        d.appendChild(pre);
      } else if (pre) {
        pre.removeAttribute("data-processed");
        pre.textContent = src;
      }
    });
    var pres = [];
    document.querySelectorAll(".lg-scope pre.mermaid, .llm-scope pre.mermaid").forEach(function (p) {
      var holder = p.closest(".module");
      if (holder && holder.hidden) return;
      pres.push(p);
    });
    if (pres.length) mermaid.run({ nodes: pres });
  }
  window.__mmRender = renderAll;
  document.querySelectorAll(".theme-btn").forEach(function (b) {
    b.addEventListener("click", function () {
      var lg = document.getElementById("mod-lg");
      var llm = document.getElementById("mod-llm");
      if ((lg && !lg.hidden) || (llm && !llm.hidden)) setTimeout(renderAll, 40);
    });
  });
})();

