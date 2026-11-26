// Theme toggle: System (default), Light or Dark, remembered in this browser.
(function () {
  var KEY = "deedoop-theme";
  function get() {
    try { return localStorage.getItem(KEY) || "system"; } catch (e) { return "system"; }
  }
  function apply(choice) {
    var root = document.documentElement;
    if (choice === "light" || choice === "dark") root.setAttribute("data-theme", choice);
    else root.removeAttribute("data-theme");
    document.querySelectorAll("[data-theme-choice]").forEach(function (b) {
      b.setAttribute("aria-pressed", b.getAttribute("data-theme-choice") === choice ? "true" : "false");
    });
  }
  document.addEventListener("click", function (ev) {
    var b = ev.target.closest("[data-theme-choice]");
    if (!b) return;
    var choice = b.getAttribute("data-theme-choice");
    try {
      if (choice === "system") localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, choice);
    } catch (e) {}
    apply(choice);
  });
  apply(get());
})();

// Review queue: K keep left, J keep right, B keep both, N not the same photo, S decide later.
document.addEventListener("keydown", function (ev) {
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
  var t = ev.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
  var b = document.querySelector('[data-review] button[data-key="' + ev.key.toLowerCase() + '"]');
  if (b && !b.disabled) { ev.preventDefault(); b.click(); }
});

// Small form behaviours. They live here, not in onchange="" attributes, because
// the Content-Security-Policy allows no inline scripts (SC6).
document.addEventListener("change", function (ev) {
  var t = ev.target;
  var what = t.getAttribute && t.getAttribute("data-change");
  if (!what) return;
  what.split(" ").forEach(function (w) {
    if (w === "reset-dir" && t.form && t.form.dir) t.form.dir.value = "";
    if (w === "submit" && t.form) t.form.submit();
    if (w === "toggle-path-text" && t.form && t.form.text) t.form.text.hidden = t.value !== "path";
    if (w === "confirm-delete") {
      var p = document.getElementById("confirm-delete");
      if (p) p.hidden = !(t.value === "delete" && t.checked);
    }
  });
});
document.addEventListener("input", function (ev) {
  var t = ev.target;
  if (t.getAttribute && t.getAttribute("data-input") === "show-value" && t.nextElementSibling) {
    t.nextElementSibling.textContent = t.value;
  }
});

// Signed out: forms that would change something are shown, but disabled.
function deedoopReadonly() {
  if (!document.body.hasAttribute("data-readonly")) return;
  // b.form also finds buttons outside their form (form="..." attribute).
  document.querySelectorAll("button, input[type=submit]").forEach(function (b) {
    var f = b.form;
    if (!f || (f.getAttribute("method") || "").toLowerCase() !== "post" || f.hasAttribute("data-public")) return;
    b.disabled = true;
    b.title = "Sign in to change this";
  });
}
deedoopReadonly();
document.addEventListener("htmx:afterSwap", deedoopReadonly);
