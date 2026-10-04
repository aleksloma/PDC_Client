/* viewers.js — offline "Show data" + "Show code" new-tab viewers.
 *
 * Vendored locally (Article: on-prem / possibly air-gapped — NO CDN). Renders
 * self-contained HTML documents into a freshly opened browser tab. The code
 * viewer ships a tiny pure-JS Python tokenizer styled like the VS Code dark
 * theme; the data viewer renders a clean, comma-grouped table. Everything is
 * inlined into the written document so the new tab needs no network and no
 * same-origin script load.
 *
 * Public API (window.PDCViewers):
 *   fixPlotlyOffline(html)    — rewrite a chart doc's cdn.plot.ly script tag to the local plotly.js asset.
 *   fitChartTitle(html)       — add the script that keeps a chart's title clear of the Plotly toolbar.
 *   setChartFrame(iframe, html) — THE one way a chart document is put into a frame (sandbox, registered with /api/charts, loaded from /charts/{token}).
 *   openCode(code)            — open a tab and render highlighted Python.
 *   openData(table)           — open a tab and render a {columns, rows} table.
 *   openBlankWindow(title)    — open a tab with a "Loading…" placeholder; returns the handle.
 *   renderData(win, table)    — write the table document into an already-open tab.
 */
(function () {
  "use strict";

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // Comma number-grouping — mirrors dashboard.js _fmtCellValue (years/ids raw).
  // HEAD (last) word match only, so a measure like "Order Value" still groups
  // while "Order ID" / "Year" stay raw.
  var IDENT_HEAD_WORDS = new Set(["year","yr","წელი","id","ids","code","codes","კოდი","იდ","zip","postal","phone","account","iban","invoice","order","ref"]);
  function isIdentifierLabel(name) {
    if (name == null) return false;
    var toks = String(name).match(/\p{L}+/gu);
    if (!toks || !toks.length) return false;
    return IDENT_HEAD_WORDS.has(toks[toks.length - 1].toLowerCase());
  }
  function fmtCell(colName, v) {
    if (typeof v === "number" && isFinite(v) && !isIdentifierLabel(colName)) {
      // Value-driven rounding: integers → 0 dp; non-integers → ≤2 dp (avoids
      // raw floats like "15,018.369999999999"). No column-name literals.
      return Number.isInteger(v)
        ? v.toLocaleString("en-US")
        : v.toLocaleString("en-US", { maximumFractionDigits: 2 });
    }
    return v === null || v === undefined ? "" : String(v);
  }

  // --- Python syntax highlighter (VS Code dark theme) ---
  var PY_KW = new Set(("False None True and as assert async await break class continue def del " +
    "elif else except finally for from global if import in is lambda nonlocal not or pass raise " +
    "return try while with yield match case").split(" "));
  var PY_BUILTIN = new Set(("print len range int float str list dict set tuple bool sum min max abs " +
    "round sorted reversed enumerate zip map filter open type isinstance issubclass super getattr " +
    "setattr hasattr any all repr format pd np plt sns go px df dfs").split(" "));

  function highlightPython(code) {
    var re = /(#[^\n]*)|([rbfuRBFU]{0,2}(?:"""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'))|(\b\d[\d_]*\.?\d*(?:[eE][+-]?\d+)?\b)|(@[A-Za-z_][\w.]*)|([A-Za-z_]\w*)|(\s+)|([^])/g;
    var out = "";
    var m;
    while ((m = re.exec(code)) !== null) {
      if (m[1]) out += '<span class="c">' + esc(m[1]) + "</span>";
      else if (m[2]) out += '<span class="s">' + esc(m[2]) + "</span>";
      else if (m[3]) out += '<span class="n">' + esc(m[3]) + "</span>";
      else if (m[4]) out += '<span class="d">' + esc(m[4]) + "</span>";
      else if (m[5]) {
        var w = m[5];
        if (PY_KW.has(w)) out += '<span class="k">' + esc(w) + "</span>";
        else if (PY_BUILTIN.has(w)) out += '<span class="b">' + esc(w) + "</span>";
        else if (/^\s*\(/.test(code.slice(re.lastIndex))) out += '<span class="f">' + esc(w) + "</span>";
        else out += esc(w);
      } else if (m[6]) out += esc(m[6]);
      else out += esc(m[7]);
    }
    return out;
  }

  // The page's per-response policy nonce (templates set window.__CSP_NONCE__).
  // Documents written into an about:blank tab INHERIT the page policy, so
  // their inline scripts must carry it to run.
  function pageNonce() {
    return String(window.__CSP_NONCE__ || "").replace(/[^A-Za-z0-9_\-+\/=]/g, "");
  }

  function writeDoc(win, html) {
    if (!win) return;
    try {
      win.document.open();
      win.document.write(html);
      win.document.close();
    } catch (e) {
      /* window closed by user — ignore */
    }
  }

  function openBlankWindow(title) {
    var win = window.open("", "_blank");
    if (!win) return null;
    writeDoc(win,
      "<!doctype html><html><head><meta charset='utf-8'><title>" + esc(title || "Loading…") +
      "</title></head><body style='font-family:system-ui,sans-serif;background:#1e1e1e;color:#d4d4d4;padding:24px'>Loading…</body></html>");
    return win;
  }

  function codeDocument(code) {
    var blocks = String(code == null ? "" : code).split("###NEXT_PLOT###")
      .map(function (b) { return b.trim(); })
      .filter(function (b) { return b.length; });
    if (!blocks.length) blocks = [""];
    var multi = blocks.length > 1;

    var sections = blocks.map(function (b, i) {
      var heading = multi ? '<div class="chart-label">Chart ' + (i + 1) + " of " + blocks.length + "</div>" : "";
      return heading +
        '<div class="code-wrap">' +
        '<button class="copy-btn" data-idx="' + i + '">Copy</button>' +
        '<pre><code id="code-' + i + '">' + highlightPython(b) + "</code></pre></div>";
    }).join("");

    // Raw code blocks are stashed in a JSON island so Copy yields the original
    // text. "<" is written as \u003c (the same string once parsed), so no
    // sequence in the code can close the surrounding script element.
    var raw = JSON.stringify(blocks).replace(/</g, "\\u003c");

    return "<!doctype html><html><head><meta charset='utf-8'><title>Generated code</title>" +
      "<style>" +
      "html,body{margin:0;background:#1e1e1e;color:#d4d4d4;font-family:Menlo,Consolas,'Courier New',monospace;}" +
      "body{padding:18px 22px;}" +
      "h1{font:600 15px system-ui,sans-serif;color:#cccccc;margin:0 0 14px;}" +
      ".chart-label{font:600 13px system-ui,sans-serif;color:#9cdcfe;margin:18px 0 6px;}" +
      ".code-wrap{position:relative;background:#1e1e1e;border:1px solid #333;border-radius:6px;margin-bottom:12px;}" +
      "pre{margin:0;padding:16px;overflow-x:auto;}" +
      "code{font:13px/1.55 Menlo,Consolas,'Courier New',monospace;white-space:pre;}" +
      ".copy-btn{position:absolute;top:8px;right:8px;background:#0e639c;color:#fff;border:none;border-radius:4px;" +
        "padding:4px 12px;font:12px system-ui,sans-serif;cursor:pointer;opacity:.85;}" +
      ".copy-btn:hover{opacity:1;}" +
      ".c{color:#6a9955;}.s{color:#ce9178;}.n{color:#b5cea8;}.k{color:#569cd6;}" +
      ".b{color:#4ec9b0;}.f{color:#dcdcaa;}.d{color:#dcdcaa;}" +
      "</style></head><body>" +
      "<h1>Generated Python</h1>" + sections +
      "<script nonce=\"" + pageNonce() + "\">var RAW=" + raw + ";document.querySelectorAll('.copy-btn').forEach(function(btn){" +
      "btn.addEventListener('click',function(){var t=RAW[+btn.dataset.idx]||'';" +
      "navigator.clipboard&&navigator.clipboard.writeText(t).then(function(){btn.textContent='Copied';" +
      "setTimeout(function(){btn.textContent='Copy';},1200);},function(){btn.textContent='Copy failed';});});});" +
      "<\/script></body></html>";
  }

  // Build the HTML for ONE {columns, rows} table (optional heading). "" if empty.
  function tableSection(table, heading) {
    var cols = (table && Array.isArray(table.columns)) ? table.columns : [];
    var rows = (table && Array.isArray(table.rows)) ? table.rows : [];
    if (!cols.length || !rows.length) return "";

    // Right-align columns whose values are predominantly numeric.
    var numericCol = cols.map(function (c) {
      var num = 0, seen = 0;
      for (var i = 0; i < rows.length && seen < 25; i++) {
        var v = rows[i] ? rows[i][c] : undefined;
        if (v === null || v === undefined || v === "") continue;
        seen++;
        if (typeof v === "number") num++;
      }
      return seen > 0 && num / seen >= 0.6;
    });

    var thead = "<tr>" + cols.map(function (c, ci) {
      return "<th class='" + (numericCol[ci] ? "num" : "") + "'>" + esc(c) + "</th>";
    }).join("") + "</tr>";

    var body = rows.map(function (row) {
      return "<tr>" + cols.map(function (c, ci) {
        var v = row ? row[c] : "";
        return "<td class='" + (numericCol[ci] ? "num" : "") + "'>" + esc(fmtCell(c, v)) + "</td>";
      }).join("") + "</tr>";
    }).join("");

    return (heading ? "<h2 class='tbl-title'>" + esc(heading) + "</h2>" : "") +
      "<div class='meta'>" + rows.length + (rows.length === 1 ? " row" : " rows") + " · " +
        cols.length + (cols.length === 1 ? " column" : " columns") + "</div>" +
      "<div class='table-wrap'><table><thead>" + thead + "</thead><tbody>" + body + "</tbody></table></div>";
  }

  function dataDocument(payload) {
    // Accept a single {columns, rows} table OR a multi-subplot {tables:[...]}.
    var tables = [];
    if (payload && Array.isArray(payload.tables)) {
      tables = payload.tables;
    } else if (payload && Array.isArray(payload.columns) && Array.isArray(payload.rows)) {
      tables = [payload];
    }
    var many = tables.length > 1;
    var sections = tables.map(function (t, i) {
      // Per-table heading only when there is more than one (single keeps the
      // original look: just the "Chart data" h1 + meta + table).
      var heading = many ? (t.title || ("Chart " + (i + 1))) : (t.title || "");
      return tableSection(t, heading);
    }).filter(function (s) { return s; }).join("<div style='height:24px'></div>");

    if (!sections) {
      return "<!doctype html><html><head><meta charset='utf-8'><title>Chart data</title></head>" +
        "<body style='font-family:system-ui,sans-serif;padding:32px;color:#374151'>" +
        "<h1 style='font-size:16px'>No chart data available</h1>" +
        "<p>Re-run the question to view the underlying data.</p></body></html>";
    }

    return "<!doctype html><html><head><meta charset='utf-8'><title>Chart data</title><style>" +
      "html,body{margin:0;background:#f5f6f8;color:#1f2937;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;}" +
      "body{padding:24px;}" +
      "h1{font-size:16px;font-weight:600;margin:0 0 14px;}" +
      ".tbl-title{font-size:14px;font-weight:600;margin:0 0 4px;color:#111827;}" +
      ".meta{color:#6b7280;font-size:13px;margin-bottom:8px;}" +
      ".table-wrap{overflow-x:auto;background:#fff;border:1px solid #e5e7eb;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,.06);}" +
      "table{border-collapse:collapse;width:100%;font-size:14px;}" +
      "th,td{padding:9px 14px;border-bottom:1px solid #eef0f2;text-align:left;white-space:nowrap;}" +
      "th{position:sticky;top:0;background:#111827;color:#f9fafb;font-weight:600;}" +
      "td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;}" +
      "th{cursor:pointer;user-select:none;}" +
      "th .si{margin-left:4px;font-size:11px;opacity:.85;}" +
      "tbody tr:nth-child(even){background:#f9fafb;}" +
      "tbody tr:hover{background:#eef2ff;}" +
      "</style></head><body>" +
      "<h1>Chart data</h1>" + sections + sortScript() +
      "</body></html>";
  }

  // Inlined, self-contained click-to-sort for every rendered table (Part B).
  // Numeric columns (server-tagged with the `num` class) sort numerically with
  // commas stripped; others sort locale-aware. Repeated clicks toggle asc/desc
  // and a ▲/▼ indicator marks the active column. No network / no dependencies.
  function sortScript() {
    return "<script nonce=\"" + pageNonce() + "\">(function(){" +
      "function pn(t){t=(t||'').replace(/,/g,'').trim();if(t==='')return NaN;" +
        "var c=t.replace(/[^0-9.\\-eE+]/g,'');if(!/[0-9]/.test(c))return NaN;" +
        "var n=Number(c);return isFinite(n)?n:NaN;}" +
      "Array.prototype.forEach.call(document.querySelectorAll('table'),function(tbl){" +
        "var thead=tbl.tHead,tb=tbl.tBodies[0];if(!thead||!tb||!thead.rows.length)return;" +
        "var hr=thead.rows[thead.rows.length-1];" +
        "var ths=Array.prototype.slice.call(hr.cells);var sc=-1,sd=1;" +
        "ths.forEach(function(th,ci){" +
          "var ind=document.createElement('span');ind.className='si';th.appendChild(ind);" +
          "th.addEventListener('click',function(){" +
            "if(sc===ci)sd=-sd;else{sc=ci;sd=1;}" +
            "var rows=Array.prototype.slice.call(tb.rows);" +
            "var isNum=th.classList.contains('num');" +
            "rows.sort(function(a,b){" +
              "var ta=a.cells[ci]?(a.cells[ci].textContent||'').trim():'';" +
              "var tbv=b.cells[ci]?(b.cells[ci].textContent||'').trim():'';" +
              "if(isNum){var na=pn(ta),nb=pn(tbv);" +
                "return ((isNaN(na)?Infinity:na)-(isNaN(nb)?Infinity:nb))*sd;}" +
              "return ta.localeCompare(tbv,undefined,{numeric:true,sensitivity:'base'})*sd;});" +
            "rows.forEach(function(r){tb.appendChild(r);});" +
            "ths.forEach(function(h){var s=h.querySelector('.si');" +
              "if(s)s.textContent=(h===th)?(sd===1?'\\u25B2':'\\u25BC'):'';});" +
          "});" +
        "});" +
      "});" +
      "})();<\/script>";
  }

  // Rewrite a chart HTML document's cdn.plot.ly script tag to the locally
  // served plotly.js bundle so persisted charts (chat history, dashboard tile
  // snapshots — generated before the offline fix, or by any older build) render
  // on air-gapped / CDN-blocked LANs. A guard script keeps the ORIGINAL CDN url
  // as a fallback: if the local asset is ever missing/404, online networks
  // degrade to the pre-fix behavior instead of failing harder. Idempotent —
  // rewritten HTML has no cdn.plot.ly script tag left, so it passes through.
  var PLOTLY_LOCAL_SRC = "/static/vendor/plotly/plotly.min.js";
  var PLOTLY_CDN_TAG_RE = /<script[^>]*\bsrc=["'](https:\/\/cdn\.plot\.ly\/plotly-[^"']*\.js)["'][^>]*>\s*<\/script>/g;
  function fixPlotlyOffline(html) {
    if (!html || typeof html !== "string" || html.indexOf("cdn.plot.ly") === -1) return html;
    try {
      return html.replace(PLOTLY_CDN_TAG_RE, function (_m, cdnUrl) {
        return '<script src="' + PLOTLY_LOCAL_SRC + '"><\/script>' +
          "<script>window.Plotly||document.write('<script src=\"" + cdnUrl + "\"><\\/script>')<\/script>";
      });
    } catch (e) {
      return html; // never break a render over the rewrite
    }
  }

  // ---- Chart title fit (render time) ----------------------------------------
  // Plotly draws the chart title in the top margin, where the always-visible
  // toolbar (modebar) also sits, so a long title runs under the buttons.
  // `fitChartTitle(doc)` puts ONE inline script into the chart document;
  // once the chart is drawn, and again on resize, it moves the title below
  // the toolbar and word-wraps it to the frame width. The stored HTML and the
  // PNG/PDF exports never see it. `chartTitleFitScript` is NEVER run on this
  // page: its source is serialized into the chart document, where the
  // /charts/ route's policy allows inline script inside the sandboxed frame.
  // The document exposes `window.__pdcTitleFit = {relayouts, done}`.
  function chartTitleFitScript() {
    "use strict";
    var GAP = 8;               // px between the toolbar and the title, and below the title
    var SIDE = 12;             // px kept free at each end of a wrapped line
    var LINE_SPACING = 1.3;    // plotly.js line step, in font sizes
    var CAP_SHIFT = 0.7;       // plotly.js top-anchor shift, in font sizes
    // Deadlines in REAL elapsed time from start() (the chart's own scripts
    // have run): the forced fit lands well before the fallback reveal, so a
    // title is never shown unfitted and then moved. Timer ticks are not
    // counted — a busy page delays them.
    var FORCE_AFTER_MS = 1000;  // fit whatever is drawn by then
    var REVEAL_AFTER_MS = 1500; // nothing stays hidden longer than this
    var SAFETY_REVEAL_MS = 5000; // from the script's own start, should start() never run
    var POLL_MS = 50;
    var state = { relayouts: 0, done: false };
    window.__pdcTitleFit = state;
    // Hidden until the first fit, so the title (and the plot area it pushes
    // down) never visibly jumps.
    var hide = document.createElement("style");
    hide.textContent = ".plotly-graph-div{visibility:hidden}";
    (document.head || document.documentElement).appendChild(hide);
    var gd = null, original = null, baseMarginT = null, singleH = null, busy = false,
      last = null, canvas = null, timer = 0;

    function reveal() { if (hide.parentNode) hide.parentNode.removeChild(hide); }
    function finish() { state.done = true; reveal(); }
    function now() { return (window.performance && performance.now) ? performance.now() : Date.now(); }
    setTimeout(reveal, SAFETY_REVEAL_MS);

    function titleOf(layout) {
      var t = layout && layout.title;
      if (typeof t === "string") return { text: t };
      return t || {};
    }
    function fontOf(full) {
      var f = (full && full.font) || {};
      var style = f.style === "italic" ? "italic " : "";
      var weight = f.weight ? String(f.weight) + " " : "";
      return style + weight + (f.size || 17) + "px " + (f.family || "sans-serif");
    }
    function textWidth(s, font) {
      canvas = canvas || document.createElement("canvas");
      var ctx = canvas.getContext("2d");
      ctx.font = font;
      return ctx.measureText(s).width;
    }
    function wrap(text, avail, font) {
      var out = [];
      text.split(/<br\s*\/?>/i).forEach(function (line) {
        var cur = "";
        line.split(/\s+/).forEach(function (w) {
          if (!w) return;
          var next = cur ? cur + " " + w : w;
          if (cur && textWidth(next, font) > avail) { out.push(cur); cur = w; }
          else { cur = next; }
        });
        out.push(cur);
      });
      return out.join("<br>");
    }
    function availWidth(full) {
      var w = gd.clientWidth || gd.getBoundingClientRect().width;
      var x = typeof full.x === "number" ? full.x : 0.5;
      var xa = full.xanchor || "auto";
      if (xa === "auto") xa = x < 1 / 3 ? "left" : (x > 2 / 3 ? "right" : "center");
      var a = xa === "center" ? 2 * Math.min(x, 1 - x) * w : (xa === "left" ? (1 - x) * w : x * w);
      return Math.max(40, a - 2 * SIDE);
    }
    // The visible toolbar, or null. A chart whose every button is trimmed
    // (pie, sunburst, treemap) still has a `.modebar`, of zero height and
    // without a `.modebar-btn`: that is NO toolbar.
    function toolbar() {
      var mb = gd.querySelector(".modebar");
      if (!mb || !mb.querySelector(".modebar-btn")) return null;
      return mb.getBoundingClientRect().height > 0 ? mb : null;
    }
    function toolbarBottom() {
      var mb = toolbar();
      if (!mb) return 0;
      return Math.max(0, mb.getBoundingClientRect().bottom - gd.getBoundingClientRect().top);
    }
    function lineCount(s) { return String(s || "").split(/<br\s*\/?>/i).length; }
    // Height the title will take with `lines` lines: one line's box (measured
    // ONCE on the title as first drawn, so a re-measure cannot move the
    // margin by a pixel and trigger another relayout) + one line step per
    // extra line.
    function titleHeight(t, lines, size) {
      if (singleH === null) {
        var el = gd.querySelector(".g-gtitle");
        var h0 = el && el.getBBox ? el.getBBox().height : 0;
        singleH = h0 > 0 ? h0 - (lineCount(t.text) - 1) * LINE_SPACING * size : 0;
        if (!(singleH > 0)) singleH = 1.25 * size;
      }
      return singleH + (lines - 1) * LINE_SPACING * size;
    }
    // `force`: fit even before the title and the toolbar are both drawn (the
    // reveal deadline); otherwise a fit waits for them, so the toolbar is
    // never measured before it exists.
    function fit(force) {
      if (busy || !gd || !window.Plotly || !gd._fullLayout) return;
      if (force !== true && !drawn()) return;
      try {
        var t = titleOf(gd.layout);
        if (original === null) original = typeof t.text === "string" ? t.text : "";
        if (!original) { finish(); return; }
        var full = gd._fullLayout.title || {};
        if (baseMarginT === null) {
          var m = gd.layout.margin || {};
          baseMarginT = typeof m.t === "number" ? m.t : ((gd._fullLayout.margin || {}).t || 0);
        }
        // Markup other than <br> is kept as written: the title is only moved.
        var text = /<(?!br\s*\/?>)[^>]*>/i.test(original)
          ? original : wrap(original, availWidth(full), fontOf(full));
        var size = (full.font && full.font.size) || 17;
        var lines = lineCount(text);
        // plotly.js 2.35 anchors a multi-line title's FIRST BASELINE at the
        // top position (its lines carry their own y, which drops the 0.7em
        // cap shift a one-line title gets), so it needs that shift as pad.
        var padT = Math.ceil(toolbarBottom() + GAP + (lines > 1 ? CAP_SHIFT * size : 0));
        // The top margin is reserved here, so title.automargin never has to
        // push it: when it pushes, plotly.js 2.35 also moves a multi-line
        // title UP by its extra lines — back under the toolbar.
        var marginT = Math.max(baseMarginT,
          Math.ceil(padT + titleHeight(t, lines, size) + GAP));
        var pad = t.pad || {};
        var current = t.text === text && pad.t === padT && t.yref === "container" &&
          t.y === 1 && t.yanchor === "top" && t.automargin === true &&
          (gd.layout.margin || {}).t === marginT;
        // `last` also stops a loop should Plotly normalize what it stores.
        if (current || (last && last.text === text && last.padT === padT &&
            last.marginT === marginT)) { finish(); return; }
        busy = true;
        last = { text: text, padT: padT, marginT: marginT };
        state.relayouts += 1;
        var settle = function () { busy = false; finish(); };
        Promise.resolve(window.Plotly.relayout(gd, {
          "title.text": text, "title.yref": "container", "title.y": 1,
          "title.yanchor": "top", "title.pad.t": padT, "title.automargin": true,
          "margin.t": marginT
        })).then(settle, settle);
      } catch (e) { busy = false; finish(); }
    }
    function start() {
      try {
        gd = document.getElementById("plotly-chart") || document.querySelector(".plotly-graph-div");
        if (!gd || !window.Plotly || !titleOf(gd.layout).text) { finish(); return; }
        if (typeof gd.on === "function") gd.on("plotly_afterplot", function () { fit(); });
        window.addEventListener("resize", function () {
          clearTimeout(timer);
          timer = setTimeout(function () { fit(); }, 120);
        });
        // The first draw may already be complete (afterplot fired before
        // this listener existed): poll until the chart is drawn; at the force
        // deadline fit whatever is there.
        var t0 = now();
        var forced = function () { fit(true); if (!busy) finish(); };
        (function poll() {
          if (state.done || busy || last) return;   // a fit ran or is running
          if (drawn() || now() - t0 >= FORCE_AFTER_MS) { forced(); return; }
          setTimeout(poll, POLL_MS);
        })();
        // Fallback reveal: a fit still pending is forced first, so even a
        // delayed timer never shows the title before it is fitted.
        setTimeout(function () {
          if (!state.done && !busy && !last) forced();
          if (!busy) reveal();
        }, REVEAL_AFTER_MS);
      } catch (e) { finish(); }
    }
    // Drawn: the title exists and the toolbar step has run (its container
    // exists, buttons or not), or the chart has no toolbar at all.
    function drawn() {
      if (!gd._fullLayout || !gd.querySelector(".gtitle")) return false;
      if (gd._context && gd._context.displayModeBar === false) return true;
      return !!gd.querySelector(".modebar-container");
    }
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
    else start();
  }

  // Insert the title-fit script into a chart document (right after <head>,
  // else at the very start). Idempotent (the `data-pdc-title-fit` marker);
  // a non-plotly document, or any error, returns the document unchanged.
  function fitChartTitle(doc) {
    try {
      if (!doc || typeof doc !== "string" || doc.indexOf("data-pdc-title-fit") !== -1) return doc;
      if (!/plotly/i.test(doc)) return doc;
      var tag = "<script data-pdc-title-fit>(" + String(chartTitleFitScript) + ")();<\/script>";
      var m = /<head(\s[^>]*)?>/i.exec(doc);
      if (m) return doc.slice(0, m.index + m[0].length) + tag + doc.slice(m.index + m[0].length);
      return tag + doc;
    } catch (e) {
      return doc;
    }
  }

  // Put a chart document (plotly HTML: a full document with inline scripts)
  // into `iframe`. The frame is sandboxed with scripts ONLY, so the document
  // gets an opaque origin of its own: no cookies, no storage, no access to
  // this page, no navigation of it. The document is not written into the
  // frame by this page: it is registered with `POST /api/charts` and the
  // frame loads the `/charts/{token}` URL that comes back. That route serves
  // it under a policy of its own (sandbox, application scripts plus a
  // per-response nonce), so the page policy never has to allow what plotly
  // needs. On a registration failure the frame stays blank and a console
  // warning names the reason. Returns the registration promise.
  // The stored chart HTML is never changed; this is render-time only.
  function setChartFrame(iframe, html) {
    if (!iframe) return Promise.resolve();
    var doc = fixPlotlyOffline(typeof html === "string" ? html : String(html == null ? "" : html));
    doc = fitChartTitle(doc);   // keep the title clear of the toolbar (render time only)
    iframe.setAttribute('sandbox', 'allow-scripts');
    // Per-frame generation: two quick refreshes race, and the first
    // registration may answer last. Only the newest one may set the src.
    var gen = (iframe.__pdcChartGen || 0) + 1;
    iframe.__pdcChartGen = gen;
    return fetch('/api/charts', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ html: doc })
    }).then(function (resp) {
      if (!resp.ok) throw new Error('chart registration failed (' + resp.status + ')');
      return resp.json();
    }).then(function (data) {
      if (iframe.__pdcChartGen !== gen) return;   // a newer chart was set meanwhile
      var url = data && typeof data.url === "string" ? data.url : "";
      if (url.indexOf('/charts/') !== 0) throw new Error('chart registration answered no url');
      iframe.src = url;
    }).catch(function (e) {
      console.warn('[PDCViewers] chart frame left blank:', e && e.message ? e.message : e);
    });
  }

  window.PDCViewers = {
    fixPlotlyOffline: fixPlotlyOffline,
    fitChartTitle: fitChartTitle,
    setChartFrame: setChartFrame,
    openCode: function (code) {
      var win = window.open("", "_blank");
      writeDoc(win, codeDocument(code));
      return win;
    },
    openData: function (table) {
      var win = window.open("", "_blank");
      writeDoc(win, dataDocument(table));
      return win;
    },
    openBlankWindow: openBlankWindow,
    renderData: function (win, table) {
      writeDoc(win, dataDocument(table));
    },
  };
})();
