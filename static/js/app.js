// Small page behaviours shared by every template:
// 1. show Django messages as Bootstrap toasts;
// 2. disable submit buttons after the first click and show a spinner, so a double
//    click cannot send a form twice (the server is idempotent-safe anyway).
(function () {
  "use strict";

  function showToasts() {
    if (!window.bootstrap) {
      return;
    }
    document.querySelectorAll(".toast").forEach(function (element) {
      window.bootstrap.Toast.getOrCreateInstance(element, { delay: 6000 }).show();
    });
  }

  function lockSubmitButtons(form) {
    form.querySelectorAll("button[type=submit]").forEach(function (button) {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      var text = button.getAttribute("data-loading-text") || "Working...";
      button.innerHTML =
        '<span class="spinner-border spinner-border-sm me-2" aria-hidden="true"></span>';
      button.appendChild(document.createTextNode(text));
    });
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (form.matches("[data-submit-once]") && !event.defaultPrevented) {
      lockSubmitButtons(form);
    }
  });

  document.addEventListener("DOMContentLoaded", showToasts);
})();
