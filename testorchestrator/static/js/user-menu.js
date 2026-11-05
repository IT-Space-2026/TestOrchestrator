/* Sidebar-footer user/help menu: opens upward, Esc closes and returns
 * focus to the button, arrow keys move between menu items. */
(function () {
  "use strict";

  var button = document.getElementById("user-menu-button");
  var list = document.getElementById("user-menu-list");
  if (!button || !list) return;

  function items() {
    return list.querySelectorAll('[role="menuitem"]');
  }

  function openMenu() {
    list.hidden = false;
    button.setAttribute("aria-expanded", "true");
    var first = items()[0];
    if (first) first.focus();
  }

  function closeMenu(restoreFocus) {
    list.hidden = true;
    button.setAttribute("aria-expanded", "false");
    if (restoreFocus) button.focus();
  }

  button.addEventListener("click", function () {
    if (list.hidden) openMenu(); else closeMenu(false);
  });

  button.addEventListener("keydown", function (event) {
    if (event.key === "ArrowUp" || event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openMenu();
    }
  });

  list.addEventListener("keydown", function (event) {
    var current = Array.prototype.indexOf.call(items(), document.activeElement);
    if (event.key === "Escape") {
      event.preventDefault();
      closeMenu(true);
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      var next = items()[current + 1] || items()[0];
      if (next) next.focus();
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      var all = items();
      var prev = all[current - 1] || all[all.length - 1];
      if (prev) prev.focus();
    }
  });

  document.addEventListener("click", function (event) {
    if (!list.hidden && !event.target.closest(".user-menu")) {
      closeMenu(false);
    }
  });
})();


/* details-cancel buttons — CSP-safe (no inline handlers). */
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-cancel-details]");
  if (!btn) return;
  const d = btn.closest("details");
  if (d) d.removeAttribute("open");
});


/* bulk-cancel toolbar — select-all + count + disable, CSP-safe. */
document.addEventListener("change", (e) => {
  const all = e.target.closest("[data-select-all]");
  const items = document.querySelectorAll(".sel-ex");
  if (all) items.forEach((c) => { c.checked = all.checked; });
  if (e.target.classList.contains("sel-ex") || all) {
    const n = [...items].filter((c) => c.checked && !c.disabled).length;
    const cnt = document.getElementById("sel-count");
    const btn = document.getElementById("bulk-cancel");
    if (cnt) cnt.textContent = n;
    if (btn) btn.disabled = n === 0;
  }
});

/* Release scope type filter — narrows the add-select by kind. */
(function () {
  "use strict";
  const f = document.getElementById("scope-type-filter");
  if (!f) return;
  f.addEventListener("change", () => {
    const k = f.value;
    const sel = document.getElementById("scope-item-select");
    if (!sel) return;
    let firstVisible = null;
    sel.querySelectorAll("option").forEach((o) => {
      o.hidden = k && !o.dataset.kind.startsWith(k);
      if (!o.hidden && firstVisible === null) firstVisible = o;
    });
    // reselect the first visible option — a hidden selection stays
    // selected otherwise.
    if (firstVisible) sel.value = firstVisible.value;
  });
})();
