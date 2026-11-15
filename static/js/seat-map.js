// Seat grid behaviour (progressive enhancement: without JavaScript the grid is a
// normal list of checkboxes and the server still enforces every rule).
//
// - limits the selection to data-max-seats and explains why when the limit is hit;
// - shows the selected seats and the total price;
// - refreshes seat statuses from GET /api/events/<id>/seats/ every 20 seconds, with a
//   small spinner, so seats taken by other customers become unavailable on screen.
(function () {
  "use strict";

  var REFRESH_MS = 20000;
  var form = document.querySelector("[data-seat-form]");
  if (!form) {
    return;
  }
  var maxSeats = parseInt(form.getAttribute("data-max-seats"), 10);
  var seatMapUrl = form.getAttribute("data-seat-map-url");
  var summary = document.querySelector("[data-selection-summary]");
  var summaryText = document.querySelector("[data-selection-text]");
  var limitNotice = document.querySelector("[data-limit-notice]");
  var submitButton = form.querySelector("button[type=submit]");
  var spinner = document.querySelector("[data-refresh-spinner]");

  var STATUS_LABELS = { available: "available", held: "held by another customer", booked: "booked" };
  var STATUS_ICONS = { available: "bi-square", held: "bi-hourglass-split", booked: "bi-lock-fill" };

  function inputs() {
    return Array.prototype.slice.call(form.querySelectorAll(".seat-input"));
  }

  function checked() {
    return inputs().filter(function (input) {
      return input.checked;
    });
  }

  function updateSummary() {
    var selected = checked();
    var total = selected.reduce(function (sum, input) {
      // Prices are rendered as exact decimals; cents keep the arithmetic exact.
      return sum + Math.round(parseFloat(input.getAttribute("data-price")) * 100);
    }, 0);
    var labels = selected.map(function (input) {
      return input.getAttribute("data-label");
    });
    if (submitButton) {
      submitButton.disabled = selected.length === 0;
    }
    summary.classList.toggle("d-none", selected.length === 0);
    summaryText.textContent =
      selected.length + " of " + maxSeats + " seats: " + labels.join(", ") +
      " | Total " + (total / 100).toFixed(2);
  }

  form.addEventListener("change", function (event) {
    var input = event.target;
    if (!input.classList.contains("seat-input")) {
      return;
    }
    if (input.checked && checked().length > maxSeats) {
      input.checked = false;
      limitNotice.textContent = "You can select up to " + maxSeats + " seats per booking.";
      limitNotice.classList.remove("d-none");
    } else {
      limitNotice.classList.add("d-none");
    }
    updateSummary();
  });

  function applyStatus(input, status) {
    var seat = input.nextElementSibling;
    var available = status === "available";
    input.disabled = !available;
    if (!available && input.checked) {
      input.checked = false;
    }
    seat.classList.remove("seat-available", "seat-held", "seat-booked");
    seat.classList.add("seat-" + status);
    var icon = seat.querySelector(".icon-status");
    icon.className = "bi icon-status " + STATUS_ICONS[status] + (available ? " icon-available" : "");
    seat.querySelector(".seat-status-text").textContent = STATUS_LABELS[status];
  }

  function refresh() {
    spinner.classList.remove("d-none");
    fetch(seatMapUrl, { headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        if (!data) {
          return;
        }
        var statusById = {};
        data.seats.forEach(function (seat) {
          statusById[seat.id] = seat.status;
        });
        inputs().forEach(function (input) {
          var status = statusById[input.value];
          if (status) {
            applyStatus(input, status);
          }
        });
        updateSummary();
      })
      .catch(function () {
        // A failed refresh is harmless: the server re-checks every seat on submit.
      })
      .finally(function () {
        spinner.classList.add("d-none");
      });
  }

  updateSummary();
  window.setInterval(refresh, REFRESH_MS);
})();
