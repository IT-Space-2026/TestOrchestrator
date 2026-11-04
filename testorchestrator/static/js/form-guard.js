/* Form guard — unsaved-change warning + submit loading/dedup. */
(function () {
  "use strict";
  const form = document.querySelector("form[method=post]");
  if (!form) return;
  let dirty = false;
  form.addEventListener("input", () => { dirty = true; });
  form.addEventListener("click", (e) => {
    if (e.target.closest("[data-add],[data-del],[data-up],[data-down],[data-act]"))
      dirty = true;
  });
  window.addEventListener("beforeunload", (e) => {
    if (dirty && !form.dataset.submitted) {
      e.preventDefault(); e.returnValue = "";
    }
  });
  form.addEventListener("submit", () => {
    form.dataset.submitted = "1";
    const btn = form.querySelector("button[type=submit]");
    if (btn) { btn.classList.add("loading"); btn.disabled = true; }
  });
})();
