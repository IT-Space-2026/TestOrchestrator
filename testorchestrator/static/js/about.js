/* About dialog behaviour (spec v1.7).
 *
 * - Opens instantly from server-rendered markup: no network access.
 * - Escape closes the dialog and focus returns to the invoking control.
 * - Focus is trapped while the dialog is open.
 */
(function () {
  "use strict";

  var dialog = document.getElementById("about-dialog");
  var openItem = document.getElementById("about-menu-item");
  var closeButton = document.getElementById("about-close");
  if (!dialog || !openItem || !closeButton) return;

  var invoker = null;

  function focusable() {
    return dialog.querySelectorAll(
      'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])'
    );
  }

  function openDialog() {
    invoker = document.activeElement;
    dialog.hidden = false;
    closeButton.focus();
  }

  function closeDialog() {
    dialog.hidden = true;
    if (invoker && typeof invoker.focus === "function") {
      invoker.focus();
    }
  }

  openItem.addEventListener("click", function (event) {
    event.preventDefault();
    openDialog();
  });

  closeButton.addEventListener("click", closeDialog);

  dialog.addEventListener("click", function (event) {
    if (event.target === dialog) closeDialog(); // backdrop click
  });

  dialog.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      event.preventDefault();
      closeDialog();
      return;
    }
    if (event.key !== "Tab") return;

    var items = Array.prototype.slice.call(focusable());
    if (!items.length) return;
    var first = items[0];
    var last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
})();
