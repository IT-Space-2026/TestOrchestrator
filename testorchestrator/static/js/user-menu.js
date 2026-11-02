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
