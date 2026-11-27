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
    if (w === "submit" && t.form) { deedoopRememberScroll(t.form); t.form.submit(); }
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

// Saving keeps your place: a POST form reloads the page, so remember where it was
// scrolled and go back there when the same page comes back after the redirect.
var SCROLL_KEY = "deedoop-scroll";
function deedoopRememberScroll(form) {
  if (!form || (form.getAttribute("method") || "").toLowerCase() !== "post") return;
  try {
    sessionStorage.setItem(SCROLL_KEY, JSON.stringify({ p: location.pathname, y: window.scrollY, t: Date.now() }));
  } catch (e) {}
}
document.addEventListener("submit", function (ev) { deedoopRememberScroll(ev.target); }, true);
(function () {
  var s = null;
  try {
    s = JSON.parse(sessionStorage.getItem(SCROLL_KEY) || "null");
    sessionStorage.removeItem(SCROLL_KEY);
  } catch (e) { s = null; }
  var url = new URL(location.href);
  var restore = s && s.p === location.pathname && Date.now() - s.t < 60000 && typeof s.y === "number";
  // The confirmation is shown once: drop it from the address, so reload or Back does not repeat it.
  var changed = false;
  ["saved", "error", "msg", "done", "checked"].forEach(function (k) {
    if (url.searchParams.has(k)) { url.searchParams.delete(k); changed = true; }
  });
  if (restore && url.hash) { url.hash = ""; changed = true; }
  if (changed) {
    try { history.replaceState(history.state, "", url.pathname + url.search + url.hash); } catch (e) {}
  }
  if (!restore) return;
  var go = function () { window.scrollTo(0, s.y); };
  go();
  requestAnimationFrame(go);
  // Again after images load, and after the browser's own jump to a #section in the
  // redirect (Chrome repeats that jump while the page settles).
  window.addEventListener("load", function () { go(); setTimeout(go, 0); setTimeout(go, 250); }, { once: true });
})();

// Floating confirmation ("toast") after a save. The server renders them inside the
// live region; moving them out and back in makes screen readers announce them.
// Confirmations go after a few seconds (paused while hovered or focused); errors
// stay until closed, so there is time to read them (WCAG 2.2.1).
(function () {
  var region = document.getElementById("toasts");
  if (!region) return;
  var OK_MS = 5000;
  function close(t) {
    if (!t.parentNode) return;
    t.classList.add("leaving");
    setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 200);
  }
  function arm(t) {
    if (t.getAttribute("data-toast") !== "ok") return;
    var left = OK_MS, started = 0, timer = null;
    function start() { started = Date.now(); timer = setTimeout(function () { close(t); }, left); }
    function pause() { if (timer) { clearTimeout(timer); timer = null; left -= Date.now() - started; } }
    t.addEventListener("mouseenter", pause);
    t.addEventListener("mouseleave", function () { if (!timer) start(); });
    t.addEventListener("focusin", pause);
    t.addEventListener("focusout", function () { if (!timer) start(); });
    start();
  }
  var items = Array.prototype.slice.call(region.querySelectorAll("[data-toast]"));
  items.forEach(function (t) { region.removeChild(t); });
  region.classList.add("ready");
  setTimeout(function () {
    items.forEach(function (t) {
      if (t.getAttribute("data-toast") === "err") t.setAttribute("role", "alert");
      region.appendChild(t);
      arm(t);
    });
  }, 150);
  region.addEventListener("click", function (ev) {
    var b = ev.target.closest("[data-toast-close]");
    if (b) close(b.closest("[data-toast]"));
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key !== "Escape") return;
    region.querySelectorAll("[data-toast]").forEach(close);
  });
})();
