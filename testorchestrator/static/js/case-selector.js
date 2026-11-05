/* Case selector — two-panel search/select/order widget.
   Progressive enhancement: the form still posts case_refs[] hidden
   inputs, so it works without JS as a plain multi-select fallback. */
(function () {
  "use strict";

  function build(root) {
    const data = JSON.parse(root.dataset.cases || "[]");
    const selected = new Map();   // ref -> true, insertion-ordered
    (JSON.parse(root.dataset.selected || "[]"))
      .forEach((r) => selected.set(r, true));
    // per-member enabled state (isRun) — disabled rows listed in
    // data-disabled
    const disabledSet = new Set(
      JSON.parse(root.dataset.disabled || "[]"));
    const selectedCfg = new Map();
    data.forEach(() => {});
    selected.forEach((_, r) =>
      selectedCfg.set(r, !disabledSet.has(r)));

    root.innerHTML = `
      <div class="case-selector">
        <div class="case-panel">
          <div class="case-panel-head">
            Available
            <input type="search" placeholder="Search cases…"
                   aria-label="Search available cases">
          </div>
          <ul class="case-list" role="listbox"
              aria-label="Available cases"></ul>
        </div>
        <div class="case-move" aria-hidden="false">
          <button type="button" class="button secondary sm" data-act="add-visible"
                  title="Add all visible">→</button>
          <button type="button" class="button secondary sm" data-act="remove-visible"
                  title="Remove all visible">←</button>
        </div>
        <div class="case-panel">
          <div class="case-panel-head">
            Selected <span class="count badge accent">0</span>
          </div>
          <ul class="case-list" role="listbox"
              aria-label="Selected cases"></ul>
        </div>
      </div>`;

    const search = root.querySelector("input[type=search]");
    const avail = root.querySelectorAll(".case-list")[0];
    const sel = root.querySelectorAll(".case-list")[1];
    const count = root.querySelector(".count");
    const hiddenHost = root.querySelector("[data-hidden]");

    function visibleAvailable() {
      const q = (search.value || "").toLowerCase();
      return data.filter((c) => !selected.has(c) &&
                         c.toLowerCase().includes(q));
    }

    function render() {
      const vis = visibleAvailable();
      avail.innerHTML = vis.length
        ? vis.map((c) => `
          <li><span class="case-path" title="${c}">${c}</span>
            <span class="case-actions">
              <button type="button" class="button ghost icon"
                      data-add="${c}" aria-label="Add ${c}">+</button>
            </span></li>`).join("")
        : `<li class="empty-row">No matching cases</li>`;

      const selArr = [...selected.keys()];
      sel.innerHTML = selArr.length
        ? selArr.map((c, i) => `
          <li><label class="member-enabled" title="Enabled (isRun)">
            <input type="checkbox" name="member_enabled" value="${c}"
                   ${selectedCfg.get(c) !== false ? "checked" : ""}>
          </label><span class="case-path" title="${c}">${c}</span>
            <span class="case-actions">
              <button type="button" class="button ghost icon" data-up="${i}"
                      aria-label="Move up" ${i === 0 ? "disabled" : ""}>↑</button>
              <button type="button" class="button ghost icon" data-down="${i}"
                      aria-label="Move down"
                      ${i === selArr.length - 1 ? "disabled" : ""}>↓</button>
              <button type="button" class="button ghost icon" data-del="${c}"
                      aria-label="Remove ${c}">✕</button>
            </span></li>`).join("")
        : `<li class="empty-row">No cases selected</li>`;
      count.textContent = String(selArr.length);

      // hidden inputs post the ordered selection as case_refs[]
      if (hiddenHost) {
        hiddenHost.innerHTML = selArr.map((c) =>
          `<input type="hidden" name="case_refs" value="${c}">`).join("");
      }
    }

    function move(i, dir) {
      const arr = [...selected.keys()];
      const j = i + dir;
      if (j < 0 || j >= arr.length) return;
      [arr[i], arr[j]] = [arr[j], arr[i]];
      selected.clear();
      arr.forEach((c) => selected.set(c, true));
    }

    root.addEventListener("click", (e) => {
      const t = e.target.closest("[data-add],[data-del],[data-up],[data-down],[data-act]");
      if (!t) return;
      if (t.dataset.add) selected.set(t.dataset.add, true);
      if (t.dataset.del) selected.delete(t.dataset.del);
      if (t.dataset.up) move(+t.dataset.up, -1);
      if (t.dataset.down) move(+t.dataset.down, +1);
      if (t.dataset.act === "add-visible")
        visibleAvailable().forEach((c) => selected.set(c, true));
      if (t.dataset.act === "remove-visible")
        visibleAvailable().forEach((c) => selected.delete(c));
      render();
    });
    search.addEventListener("input", render);
    render();

    // binding-change guard — selected cases may not exist in the
    // other repo; warn (server-side validation is authoritative).
    const bindSel = root.closest("form")
      ?.querySelector("[data-guard-bind]");
    const warnEl = document.getElementById("binding-warn");
    if (bindSel && warnEl) {
      bindSel.addEventListener("change", () => {
        warnEl.hidden = selected.size === 0;
      });
    }
  }

  document.querySelectorAll("[data-case-selector]").forEach(build);
})();
