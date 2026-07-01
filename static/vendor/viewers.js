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

    // Raw code blocks are stashed in a JSON island so Copy yields the original text.
    var raw = JSON.stringify(blocks);

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
      "<script>var RAW=" + raw + ";document.querySelectorAll('.copy-btn').forEach(function(btn){" +
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
    return "<script>(function(){" +
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

  window.PDCViewers = {
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
