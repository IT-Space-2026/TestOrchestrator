/* Operational Logs — live-tail polling. External file because CSP is
 * script-src 'self' (no inline scripts). Distinguishes auth expiry,
 * denial, network/server failure, and malformed responses so a failed
 * poll never leaves a misleading "live" status. */
(function () {
  var tail = document.getElementById("live-tail"),
      status = document.getElementById("tail-status"),
      tbody = document.getElementById("log-rows"),
      timer = null, gen = 0;
  var tailUrl = (document.currentScript &&
                 document.currentScript.dataset.url) || "";

  function setStatus(text, cls) {
    status.textContent = text;
    status.className = "tail-status " + cls;
  }
  function esc(t) {
    return String(t).replace(/[&<>]/g, function (c) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;"}[c]; });
  }
  function rowFrom(r) {
    var tr = document.createElement("tr");
    tr.dataset.ts = r.ts;
    tr.innerHTML =
      "<td><code>" + esc((r.ts || "").split("T")[1].slice(0, 8)) +
      "</code></td><td><span class='badge sev-" + esc(r.severity) + "'>" +
      esc(r.severity) + "</span></td><td>" + esc(r.component) + "</td><td>" +
      esc(r.category) + "</td><td class='log-msg'>" + esc(r.message) +
      "</td><td><small class='muted'>" +
      esc([r.project, r.execution, r.worker, r.corr]
          .filter(Boolean).join(" ")) + "</small></td>";
    return tr;
  }
  function stopPolling() {
    if (timer) { clearInterval(timer); timer = null; }
  }
  function fetchTail() {
    var g = gen;                    // capture this lifecycle's generation
    var last = tbody.querySelector("tr[data-ts]");
    var after = last ? last.dataset.ts : "";
    fetch(tailUrl + "?after=" + encodeURIComponent(after),
          {credentials: "same-origin"})
      .then(function (r) {
        if (g !== gen) return null;       // stale lifecycle: ignore entirely
        if (r.status === 401) {           // session expired
          stopPolling();
          setStatus("sign-in required — reload to continue", "unavailable");
          return null;
        }
        if (r.status === 403) {           // denied
          stopPolling();
          setStatus("access denied", "unavailable");
          return null;
        }
        if (!r.ok) {                      // server error
          setStatus("source unavailable", "unavailable");
          return null;
        }
        var ct = (r.headers.get("content-type") || "");
        if (ct.indexOf("application/json") === -1) {  // redirect/HTML
          stopPolling();
          setStatus("sign-in required — reload to continue",
                    "unavailable");
          return null;
        }
        return r.json();
      })
      .then(function (d) {
        if (!d || g !== gen) return;  // stale: paused or re-toggled
        setStatus("live · updated " +
                  String(d.generated_at).split("T")[1].slice(0, 8), "live");
        (d.rows || []).forEach(function (r) {
          tbody.insertBefore(rowFrom(r), tbody.firstChild);
        });
      })
      .catch(function () {                // network failure
        setStatus("source unavailable", "unavailable");
      });
  }
  tail.addEventListener("change", function () {
    gen += 1;                             // new lifecycle / invalidate stale
    if (tail.checked) {
      setStatus("live", "live");
      fetchTail();                        // immediate first poll
      timer = setInterval(fetchTail, 4000);
    } else {
      stopPolling();
      setStatus("paused", "paused");
    }
  });
})();
