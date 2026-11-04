/* Theme: light / dark / system — persisted, applied pre-render. */
(function () {
  "use strict";
  const KEY = "to-theme";
  function pref() {
    try { return localStorage.getItem(KEY) || "system"; }
    catch (e) { return "system"; }
  }
  function resolved(p) {
    if (p === "light" || p === "dark") return p;
    return matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark" : "light";
  }
  function apply() {
    const p = pref();
    document.documentElement.dataset.theme = resolved(p);
    document.documentElement.dataset.themePref = p;
    // keep native controls in step
    document.documentElement.style.colorScheme = resolved(p);
  }
  window.toSetTheme = function (p) {
    try { localStorage.setItem(KEY, p); } catch (e) {}
    apply();
  };
  matchMedia("(prefers-color-scheme: dark)")
    .addEventListener("change", apply);
  apply();
  // populate the selector once DOM is ready
  document.addEventListener("DOMContentLoaded", () => {
    const sel = document.getElementById("theme-select");
    if (sel) {
      sel.value = pref();
      sel.addEventListener("change", () => toSetTheme(sel.value));
    }
  });
})();
