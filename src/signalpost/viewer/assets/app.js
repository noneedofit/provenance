/* Signalpost static viewer — client behaviour only. No network calls, no external libraries.
 * All data is read from data-json attributes / inline <script type="application/json"> tags already
 * present in the server-rendered HTML, so every page works from a plain file:// URL.
 */
(function () {
  "use strict";

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function parseCards(root) {
    return Array.prototype.slice.call(root.querySelectorAll("[data-json]")).map(function (el) {
      var d = {};
      try {
        d = JSON.parse(el.getAttribute("data-json"));
      } catch (e) {
        /* leave d empty; card stays inert rather than breaking the page */
      }
      return { el: el, d: d };
    });
  }

  function fmtAmount(amount, currency) {
    if (amount === null || amount === undefined) return null;
    var n;
    try {
      n = Number(amount).toLocaleString("nb-NO");
    } catch (e) {
      n = String(amount);
    }
    return (currency || "") + " " + n;
  }

  // ---------------------------------------------------------------- directory
  function initDirectory() {
    var grid = document.getElementById("company-grid");
    if (!grid) return;
    var items = parseCards(grid);

    var q = document.getElementById("q");
    var fLegal = document.getElementById("f-legalform");
    var fBand = document.getElementById("f-employeeband");
    var fWebsite = document.getElementById("f-website");
    var fHiring = document.getElementById("f-hiring");
    var fActivity = document.getElementById("f-activity");
    var fAllFive = document.getElementById("f-all-areas");
    var sortSel = document.getElementById("sort");
    var statLine = document.getElementById("stat-line");
    var empty = document.getElementById("empty-state");

    function apply() {
      var term = (q.value || "").trim().toLowerCase();
      var visible = 0;
      items.forEach(function (item) {
        var d = item.d;
        var ok = true;
        if (term) {
          var hay = [d.legal_name, d.brand_name, d.org, d.municipality, d.nace_label]
            .filter(Boolean)
            .join(" ")
            .toLowerCase();
          ok = hay.indexOf(term) !== -1;
        }
        if (ok && fLegal.value && d.legal_form !== fLegal.value) ok = false;
        if (ok && fBand.value && d.employee_band !== fBand.value) ok = false;
        if (ok && fWebsite.checked && d.website_state !== "exact") ok = false;
        if (ok && fHiring.checked && !d.jobs_hiring) ok = false;
        if (ok && fActivity.checked && !(d.activity_count > 0)) ok = false;
        if (ok && fAllFive.checked && (d.coverage || 0) < 5) ok = false;
        item.el.style.display = ok ? "" : "none";
        if (ok) visible++;
      });
      statLine.textContent = "Showing " + visible + " of " + items.length + " companies";
      empty.style.display = visible === 0 ? "" : "none";
    }

    function sortItems() {
      var mode = sortSel.value;
      var sorted = items.slice().sort(function (a, b) {
        if (mode === "name") return (a.d.legal_name || "").localeCompare(b.d.legal_name || "", "nb");
        if (mode === "revenue") return (b.d.revenue || 0) - (a.d.revenue || 0);
        if (mode === "employees") return (b.d.employees || 0) - (a.d.employees || 0);
        return (b.d.coverage || 0) - (a.d.coverage || 0); // "most data found" — the default
      });
      sorted.forEach(function (item) {
        grid.appendChild(item.el);
      });
    }

    [q].forEach(function (el) {
      el.addEventListener("input", apply);
    });
    [fLegal, fBand].forEach(function (el) {
      el.addEventListener("change", apply);
    });
    [fWebsite, fHiring, fActivity, fAllFive].forEach(function (el) {
      el.addEventListener("change", apply);
    });
    sortSel.addEventListener("change", function () {
      sortItems();
      apply();
    });

    sortItems();
    apply();

    // compare selection bar
    var compareBar = document.getElementById("compare-bar");
    var compareList = document.getElementById("compare-list");
    var compareBtn = document.getElementById("compare-go");

    function checkboxOf(item) {
      return item.el.querySelector('input[type="checkbox"]');
    }

    function updateCompareBar() {
      var checked = items.filter(function (item) {
        var cb = checkboxOf(item);
        return cb && cb.checked;
      });
      if (compareList) compareList.textContent = checked.map(function (c) { return c.d.legal_name; }).join(", ");
      if (compareBar) compareBar.style.display = checked.length ? "" : "none";
      if (compareBtn) compareBtn.disabled = checked.length < 2 || checked.length > 4;
    }

    items.forEach(function (item) {
      var cb = checkboxOf(item);
      if (!cb) return;
      cb.addEventListener("change", function () {
        var checkedCount = items.filter(function (i) {
          var c = checkboxOf(i);
          return c && c.checked;
        }).length;
        if (checkedCount > 4 && cb.checked) {
          cb.checked = false;
          window.alert("Compare up to 4 companies at a time.");
        }
        updateCompareBar();
      });
    });

    if (compareBtn) {
      compareBtn.addEventListener("click", function () {
        var orgs = items
          .filter(function (item) {
            var cb = checkboxOf(item);
            return cb && cb.checked;
          })
          .map(function (item) {
            return item.d.org;
          });
        window.location.href = "compare.html?orgs=" + encodeURIComponent(orgs.join(","));
      });
    }
    updateCompareBar();

    // CSV export of the rows currently visible
    var csvBtn = document.getElementById("export-visible-csv");
    if (csvBtn) {
      csvBtn.addEventListener("click", function () {
        var cols = ["org", "legal_name", "legal_form", "municipality", "nace_code", "nace_label",
          "employees", "website", "website_state", "jobs_hiring", "coverage"];
        var lines = [cols.join(",")];
        items.forEach(function (item) {
          if (item.el.style.display === "none") return;
          var d = item.d;
          lines.push(cols.map(function (c) {
            var v = d[c] == null ? "" : String(d[c]);
            if (v.indexOf(",") !== -1 || v.indexOf('"') !== -1) v = '"' + v.replace(/"/g, '""') + '"';
            return v;
          }).join(","));
        });
        var blob = new Blob([lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
        var url = URL.createObjectURL(blob);
        var a = document.createElement("a");
        a.href = url;
        a.download = "signalpost-directory.csv";
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
      });
    }
  }

  // ---------------------------------------------------------------- compare
  function initCompare() {
    var list = document.getElementById("compare-list-page");
    if (!list) return;
    var items = parseCards(list);
    var params = new URLSearchParams(window.location.search);
    var preselect = (params.get("orgs") || "").split(",").filter(Boolean);

    items.forEach(function (item) {
      var cb = item.el.querySelector('input[type="checkbox"]');
      if (cb && preselect.indexOf(item.d.org) !== -1) cb.checked = true;
    });

    var resultDiv = document.getElementById("compare-result");
    var btn = document.getElementById("compare-render");

    var rows = [
      ["Legal name", function (d) { return d.legal_name; }],
      ["Organisation number", function (d) { return d.org; }],
      ["Legal form", function (d) { return d.legal_form; }],
      ["Municipality", function (d) { return d.municipality; }],
      ["Industry (NACE)", function (d) { return d.nace_label || d.nace_code || null; }],
      ["Employees", function (d) { return d.employees; }],
      ["Revenue", function (d) { return d.revenue ? fmtAmount(d.revenue, d.revenue_currency) : null; }],
      ["Result", function (d) { return d.result != null ? fmtAmount(d.result, d.revenue_currency) : null; }],
      ["Website", function (d) {
        if (!d.website) return null;
        return d.website + (d.website_state === "exact" ? "" : " (related / ambiguous)");
      }],
      ["Profiles", function (d) { return d.profiles_count; }],
      ["Hiring", function (d) { return d.jobs_hiring ? d.jobs_count + " open role(s)" : "not hiring / none found"; }],
      ["Latest activity", function (d) {
        return d.latest_activity_text ? d.latest_activity_text + (d.latest_activity_date ? " (" + d.latest_activity_date + ")" : "") : null;
      }],
      ["Data coverage", function (d) { return d.coverage + " / 12 families available"; }],
    ];

    function render() {
      var chosen = items
        .filter(function (item) {
          var cb = item.el.querySelector('input[type="checkbox"]');
          return cb && cb.checked;
        })
        .map(function (item) { return item.d; });
      if (chosen.length < 2) {
        resultDiv.innerHTML = '<p class="muted">Pick 2 to 4 companies above, then press "Show comparison".</p>';
        return;
      }
      var html = '<div class="compare-table-wrap"><table class="data-table"><thead><tr><th scope="col">Field</th>';
      chosen.forEach(function (d) {
        html += "<th scope=\"col\"><a href=\"" + escapeHtml(d.page_url) + "\">" + escapeHtml(d.legal_name) + "</a></th>";
      });
      html += "</tr></thead><tbody>";
      rows.forEach(function (row) {
        html += "<tr><th scope=\"row\">" + escapeHtml(row[0]) + "</th>";
        chosen.forEach(function (d) {
          var v = row[1](d);
          html += "<td>" + (v === null || v === undefined || v === "" ? '<span class="muted">not available</span>' : escapeHtml(v)) + "</td>";
        });
        html += "</tr>";
      });
      html += "</tbody></table></div>";
      resultDiv.innerHTML = html;
    }

    items.forEach(function (item) {
      var cb = item.el.querySelector('input[type="checkbox"]');
      if (!cb) return;
      cb.addEventListener("change", function () {
        var checkedCount = items.filter(function (i) {
          var c = i.el.querySelector('input[type="checkbox"]');
          return c && c.checked;
        }).length;
        if (checkedCount > 4 && cb.checked) {
          cb.checked = false;
          window.alert("Compare up to 4 companies at a time.");
        }
      });
    });

    if (btn) btn.addEventListener("click", render);
    if (preselect.length >= 2) render();
  }

  // ---------------------------------------------------------------- ask this profile (evidence-bound, no LLM)
  function initQA() {
    var dataEl = document.getElementById("profile-data");
    var box = document.getElementById("qa-box");
    if (!dataEl || !box) return;
    var profile;
    try {
      profile = JSON.parse(dataEl.textContent);
    } catch (e) {
      return;
    }

    var form = document.getElementById("qa-form");
    var input = document.getElementById("qa-input");
    var log = document.getElementById("qa-log");
    var chips = document.getElementById("qa-chips");

    function citeList(evList) {
      if (!evList || !evList.length) return "";
      return (
        '<div class="small muted" style="margin-top:6px">Sources: ' +
        evList
          .map(function (e, i) {
            return '<a href="' + escapeHtml(e.source_url) + '" target="_blank" rel="noopener noreferrer">[' + (i + 1) + "]</a>";
          })
          .join(" ") +
        "</div>"
      );
    }

    function notFound(topic) {
      return '<p class="qa-not-found">Not found in checked sources' + (topic ? " (" + escapeHtml(topic) + ")" : "") + ".</p>";
    }

    function answer(questionRaw) {
      var question = (questionRaw || "").toLowerCase();
      var html = "";
      var matched = false;

      function isAny(words) {
        return words.some(function (w) { return question.indexOf(w) !== -1; });
      }

      if (isAny(["who runs", "who is the", "ceo", "leader", "leadership", "manage", "board", "daglig leder"])) {
        matched = true;
        if (profile.leadership && profile.leadership.length) {
          html = "<ul>" + profile.leadership.map(function (p) {
            return "<li>" + escapeHtml(p.name) + (p.role ? " — " + escapeHtml(p.role) : "") + citeList(p.evidence) + "</li>";
          }).join("") + "</ul>";
        } else {
          html = notFound("leadership");
        }
      } else if (isAny(["financ", "revenue", "turnover", "result", "profit", "omsetning"])) {
        matched = true;
        if (profile.financials && profile.financials.revenue) {
          var f = profile.financials;
          html = "<p>Revenue" + (f.period ? " (" + escapeHtml(f.period) + ")" : "") + ": " + escapeHtml(f.revenue.formatted) + "." +
            (f.result ? " Annual result: " + escapeHtml(f.result.formatted) + "." : "") + "</p>" + citeList(f.evidence);
        } else {
          html = notFound("financials");
        }
      } else if (isAny(["hiring", "job", "vacan", "recruit", "stilling"])) {
        matched = true;
        if (profile.jobs && profile.jobs.length) {
          html = "<p>Yes — " + profile.jobs.length + " open role(s):</p><ul>" + profile.jobs.map(function (j) {
            return "<li>" + escapeHtml(j.title) + citeList(j.evidence) + "</li>";
          }).join("") + "</ul>";
        } else {
          html = "<p>No open roles found in checked sources.</p>";
        }
      } else if (isAny(["chang", "differ", "updat", "since last"])) {
        matched = true;
        if (profile.changes && profile.changes.length) {
          html = "<ul>" + profile.changes.map(function (c) {
            return "<li>" + escapeHtml(c.summary) + "</li>";
          }).join("") + "</ul>";
        } else {
          html = "<p>No changes detected since the previous run.</p>";
        }
      } else if (isAny(["activ", "news", "recent", "latest post"])) {
        matched = true;
        if (profile.activity && profile.activity.length) {
          html = "<ul>" + profile.activity.map(function (a) {
            return "<li>" + escapeHtml(a.text) + (a.date ? " (" + escapeHtml(a.date) + ")" : "") + citeList(a.evidence) + "</li>";
          }).join("") + "</ul>";
        } else {
          html = notFound("activity");
        }
      } else if (isAny(["website", "site", "url", "homepage"])) {
        matched = true;
        if (profile.website && profile.website.url) {
          html = "<p><a href=\"" + escapeHtml(profile.website.url) + "\" target=\"_blank\" rel=\"noopener noreferrer\">" +
            escapeHtml(profile.website.url) + "</a> (" + escapeHtml(profile.website.state) + ")</p>" + citeList(profile.website.evidence);
        } else {
          html = notFound("website");
        }
      } else if (isAny(["what does", "what is", "does it do", "about", "business", "do they do"])) {
        matched = true;
        if (profile.description && profile.description.text) {
          html = "<p>" + escapeHtml(profile.description.text) + "</p>" + citeList(profile.description.evidence);
        } else if (profile.nace_label) {
          html = "<p>Registered industry: " + escapeHtml(profile.nace_label) + " (NACE " + escapeHtml(profile.nace_code) + ").</p>" + citeList(profile.nace_evidence);
        } else {
          html = notFound("description");
        }
      }

      if (!matched) {
        html = notFound("this question is outside what this profile's evidence covers");
      }
      return html;
    }

    function ask(text) {
      if (!text) return;
      var li = document.createElement("li");
      li.innerHTML = '<div class="qa-answer"><strong>Q: ' + escapeHtml(text) + "</strong><div>" + answer(text) + "</div></div>";
      log.insertBefore(li, log.firstChild);
    }

    if (chips) {
      chips.querySelectorAll("button[data-q]").forEach(function (btn) {
        btn.addEventListener("click", function () {
          ask(btn.getAttribute("data-q"));
        });
      });
    }
    if (form) {
      form.addEventListener("submit", function (ev) {
        ev.preventDefault();
        var text = input.value.trim();
        if (text) ask(text);
        input.value = "";
      });
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    initDirectory();
    initCompare();
    initQA();
  });
})();
