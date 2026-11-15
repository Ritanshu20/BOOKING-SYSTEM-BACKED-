// Hold countdown on the booking page.
// Reads the ISO expiry time from data-expires-at and shows mm:ss. When it reaches zero
// it marks the hold as expired and disables the Pay button. The server is always the
// authority: an expired hold is rejected there regardless of what this timer shows.
(function () {
  "use strict";

  var element = document.querySelector("[data-countdown]");
  if (!element) {
    return;
  }
  var expiresAt = new Date(element.getAttribute("data-expires-at")).getTime();
  var display = element.querySelector("[data-countdown-value]");
  var expiredNotice = document.querySelector("[data-expired-notice]");

  function pad(value) {
    return value < 10 ? "0" + value : String(value);
  }

  function tick() {
    var remaining = Math.max(0, Math.floor((expiresAt - Date.now()) / 1000));
    display.textContent = pad(Math.floor(remaining / 60)) + ":" + pad(remaining % 60);
    if (remaining === 0) {
      window.clearInterval(timer);
      element.classList.add("text-danger");
      expiredNotice.classList.remove("d-none");
      document.querySelectorAll("[data-requires-active-hold]").forEach(function (button) {
        button.disabled = true;
      });
    }
  }

  var timer = window.setInterval(tick, 1000);
  tick();
})();
