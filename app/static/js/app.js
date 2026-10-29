// Ulat officer dashboard. Plain script, no build step; every page works without it.
(function () {
  "use strict";

  // Filing countdowns: the server renders the same text from the deadlines table
  // (app/deadlines.py countdown); this only keeps "due in" current while a page stays open.
  const DAY = 86400000;
  const hm = (ms) => { const m = Math.floor(ms / 60000); return Math.floor(m / 60) + "h " + (m % 60) + "m"; };
  function tickCountdowns(root) {
    (root || document).querySelectorAll("[data-due]").forEach((el) => {
      const left = Date.parse(el.dataset.due) - Date.now();
      const days = Math.floor(-left / DAY);
      el.textContent = left >= 0 ? "due in " + hm(left)
        : -left < DAY ? "overdue by " + hm(-left)
        : "overdue by " + days + " day" + (days !== 1 ? "s" : "");
      const chip = el.closest(".chip");
      if (chip) { chip.classList.toggle("alert", left < 0); chip.classList.toggle("warn", left >= 0); }
    });
  }
  setInterval(tickCountdowns, 30000);

  window.Ulat = { tickCountdowns };
})();
