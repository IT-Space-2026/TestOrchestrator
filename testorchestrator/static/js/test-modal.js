/* Test-tab creation modals — Test Suite / Test Collection.
   CSP-safe (no inline handlers): open/close, focus trap, dirty-close
   guard, duplicate-submit guard, fetch POST with server-side
   validation-error preservation in-dialog. */
(function () {
  "use strict";
  var openModal = null, lastFocus = null, dirty = false;

  function focusables(m) {
    return Array.prototype.slice.call(
      m.querySelectorAll("input,select,textarea,button,[tabindex]"))
      .filter(function (e) { return !e.disabled && e.offsetParent !== null; });
  }
  function open(m) {
    lastFocus = document.activeElement;
    openModal = m; dirty = false;
    m.classList.add("on");
    document.body.classList.add("modal-open");
    var f = focusables(m); if (f.length) f[0].focus();
  }
  function close(m) {
    m.classList.remove("on");
    document.body.classList.remove("modal-open");
    openModal = null;
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }
  function requestClose(m) {
    if (dirty && !window.confirm(
        "Discard unsaved changes?")) return;
    close(m);
  }
  function trap(e) {
    if (!openModal || e.key !== "Tab") return;
    var f = focusables(openModal); if (!f.length) return;
    var i = f.indexOf(document.activeElement);
    if (e.shiftKey && i <= 0) { f[f.length - 1].focus(); e.preventDefault(); }
    else if (!e.shiftKey && i === f.length - 1) {
      f[0].focus(); e.preventDefault(); }
  }
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && openModal) requestClose(openModal);
    else trap(e);
  });
  document.addEventListener("click", function (e) {
    var opener = e.target.closest("[data-modal-open]");
    if (opener) {
      var m = document.getElementById(opener.dataset.modalOpen);
      if (m) { open(m); e.preventDefault(); }
      return;
    }
    if (e.target.closest("[data-modal-close]") && openModal) {
      requestClose(openModal); e.preventDefault();
    }
  });
  document.addEventListener("input", function (e) {
    if (openModal && openModal.contains(e.target)) dirty = true;
  });
  document.addEventListener("submit", function (e) {
    var form = e.target.closest("form[data-modal-form]");
    if (!form) return;
    e.preventDefault();
    var modal = form.closest(".modal");
    var btn = form.querySelector("[data-modal-submit]");
    if (btn && btn.disabled) return;          // duplicate-submit guard
    var orig = btn ? btn.textContent : "";
    if (btn) { btn.disabled = true; btn.textContent = "Creating…"; }
    var err = modal.querySelector(".modal-error");
    if (err) err.textContent = "";
    var fd = new FormData(form);
    fetch(form.action, {method: "POST", body: fd,
                        headers: {"X-Requested-With": "fetch"},
                        redirect: "follow"})
      .then(function (r) { return r.text().then(function (t) {
        return {url: r.url, body: t, ok: r.ok}; }); })
      .then(function (res) {
        // success: server redirected to the draft editor -> navigate
        if (res.url && res.url.indexOf("author") >= 0 && res.ok) {
          window.location.href = res.url; return;
        }
        // failure: pull the flashed error out of the returned page
        var m = res.body.match(/class="[^"]*flash[^"]*error[^"]*"[^>]*>([\s\S]*?)</);
        if (err && m) err.textContent = m[1].trim();
        else if (err && !res.ok) err.textContent =
          "Creation failed (" + res.url + ").";
        if (!err || !err.textContent) window.location.href = res.url;
      })
      .catch(function () {
        if (err) err.textContent = "Request failed — check connectivity.";
      })
      .finally(function () {
        if (btn) { btn.disabled = false; btn.textContent = orig; }
      });
  });
})();
