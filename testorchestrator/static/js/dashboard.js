/* Dashboard live updates — near-real-time ~5 s poll of /dashboard/data.
 * - single in-flight request (no overlapping polls)
 * - backoff on errors (5s → 10s → 20s → 60s cap), recovery on success
 * - pauses when the tab is hidden
 * - DOM patched in place: filters, scroll, and row state are preserved
 * - stale-data indicator after a missed interval */
(function () {
  var script = document.currentScript;
  var url = script && script.dataset.url;
  var sel = document.getElementById("p-days");
  if (sel && sel.form) sel.addEventListener("change", function () { sel.form.submit(); });
  if (!url) return;

  var BASE_MS = 5000, MAX_MS = 60000, delay = BASE_MS;
  var inFlight = false, timer = null;

  var staleEl = document.getElementById("dash-stale");
  var updatedEl = document.getElementById("dash-updated");

  function setStale(on) { if (staleEl) staleEl.hidden = !on; }
  function setUpdated(ts) {
    if (updatedEl) updatedEl.textContent = "Updated " + ts.slice(0, 19) + "Z";
  }

  function setText(key, value) {
    document.querySelectorAll("[data-m='" + key + "']")
      .forEach(function (el) { el.textContent = value; });
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;",
              '"': "&quot;", "'": "&#39;"}[c];
    });
  }

  function renderProjects(rows) {
    var tbody = document.getElementById("proj-rows");
    var empty = document.getElementById("proj-no-data");
    if (!tbody) return;
    if (empty) empty.hidden = rows.length > 0;
    var q = (document.getElementById("proj-q").value || "").toLowerCase();
    tbody.innerHTML = rows.map(function (p) {
      var hide = q && p.name.toLowerCase().indexOf(q) === -1;
      return "<tr data-name='" + esc(p.name.toLowerCase()) + "'" +
        (hide ? " hidden" : "") + ">" +
        "<td><a href='/projects/" + esc(p.id) + "'><strong>" + esc(p.name) +
        "</strong></a><div class='muted'>" + esc((p.description || "").slice(0, 80)) + "</div></td>" +
        "<td>" + esc((p.latest_finished_at || "—").slice(0, 16)) + "</td>" +
        "<td>" + (p.latest_outcome ? "<span class='badge outcome-" + esc(p.latest_outcome) + "'>" + esc(p.latest_outcome) + "</span>" : "—") + "</td>" +
        "<td>" + p.active + (p.queued ? " (" + p.queued + " queued)" : "") + "</td>" +
        "<td>" + (p.pass_rate === null ? "—" : p.pass_rate + "% <span class='muted'>of " + p.pass_total + "</span>") + "</td>" +
        "<td>" + esc((p.next_run_at || "—").slice(0, 16)) + "</td>" +
        "<td><a href='/projects/" + esc(p.id) + "'>Open</a></td></tr>";
    }).join("");
  }

  function renderRecent(rows) {
    var tbody = document.getElementById("recent-rows");
    var empty = document.getElementById("recent-no-data");
    if (!tbody) return;
    if (empty) empty.hidden = rows.length > 0;
    tbody.innerHTML = rows.map(function (e) {
      return "<tr><td><a href='/executions/" + esc(e.id) + "'><code>" +
        esc(e.id.slice(0, 8)) + "</code></a></td><td>" + esc(e.suite) +
        "</td><td>" + esc(e.state) + "</td><td>" +
        (e.outcome !== "none" ? "<span class='badge outcome-" + esc(e.outcome) + "'>" + esc(e.outcome) + "</span>" : "—") +
        "</td><td>" + esc(e.created_by) + "</td></tr>";
    }).join("");
  }

  function renderChart(series) {
    var el = document.getElementById("chart-outcomes");
    if (!el || typeof Plotly === "undefined" || !window.TOCharts) return;
    var fig = window.TOCharts.outcomeTrend(series);
    if (!fig.data.length) { el.innerHTML = ""; return; }
    Plotly.react(el, fig.data, fig.layout,
      {responsive: true, displaylogo: false});
  }

  function apply(data) {
    var m = data.metrics;
    setText("projects_total", m.projects_total);
    setText("executions_active", m.executions_active);
    setText("executions_queued", m.executions_queued);
    setText("passed_period", m.passed_period);
    setText("failed_period", m.failed_period);
    setText("runners_available", m.runners_available + "/" + m.runners_total);
    setText("runners_healthy", m.runners_healthy);
    setText("schedules_upcoming", m.schedules_upcoming);
    setText("next_schedule_at", (m.next_schedule_at || "—").slice(0, 19));
    renderProjects(data.projects);
    renderRecent(data.recent);
    renderChart(data.chart_outcomes);
    setUpdated(data.generated_at);
  }

  function schedule(ms) {
    clearTimeout(timer);
    timer = setTimeout(tick, ms);
  }

  function tick() {
    if (document.hidden || inFlight) { schedule(delay); return; }
    inFlight = true;
    fetch(url, {headers: {"Accept": "application/json"}, cache: "no-store"})
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        delay = BASE_MS; setStale(false); apply(data);
      })
      .catch(function () {
        setStale(true);
        delay = Math.min(delay * 2, MAX_MS);
      })
      .finally(function () {
        inFlight = false;
        schedule(delay);
      });
  }

  // client-side project filter (instant, before next poll)
  var qEl = document.getElementById("proj-q");
  if (qEl) {
    qEl.addEventListener("input", function () {
      var q = qEl.value.toLowerCase(), shown = 0;
      document.querySelectorAll("#proj-rows tr").forEach(function (tr) {
        var show = !q || tr.dataset.name.indexOf(q) !== -1;
        tr.hidden = !show;
        if (show) shown++;
      });
      var empty = document.getElementById("proj-empty");
      if (empty) empty.hidden = shown > 0 || !document.querySelector("#proj-rows tr");
    });
  }

  schedule(0);
})();
