// Follow the operating system's light/dark preference for Bootstrap components.
// Loaded in <head> (not deferred) so the page never flashes the wrong theme.
(function () {
  "use strict";
  var query = window.matchMedia("(prefers-color-scheme: dark)");
  function apply() {
    document.documentElement.setAttribute("data-bs-theme", query.matches ? "dark" : "light");
  }
  apply();
  query.addEventListener("change", apply);
})();
