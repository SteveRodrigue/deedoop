// Theme toggle: System (default), Light or Dark, remembered in this browser.
(function () {
  var KEY = "deedup-theme";
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
