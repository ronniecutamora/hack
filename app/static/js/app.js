// Ulat officer dashboard. Plain script, no build step; every page works without it.
(function () {
  "use strict";

  // ---------- Filing countdowns ----------
  // The server renders the same text from the deadlines table (app/deadlines.py countdown);
  // this only keeps "due in" current while a page stays open, and after every live swap.
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
  document.addEventListener("htmx:afterSwap", () => tickCountdowns());

  // ---------- Live updates ----------
  // The server sends which topics changed (reports, incidents, drafts, spend) over SSE
  // (GET /events, app/live.py). Each region marked data-live="<topics>" re-fetches this page
  // and swaps in its own fresh copy (htmx `select`), so there is one render path per page.
  // A region the officer is working in (focus inside, or a field changed) is never swapped:
  // it shows a "New updates" notice instead, and its typed values survive a requested refresh.

  const POLL_MS = 20000;          // fallback polling of GET /events/token
  const SSE_FAILURES = 3;         // consecutive stream errors before falling back to polling
  const SSE_RETRY_EVERY = 6;      // while polling, try the stream again every 6th poll (2 min)
  const DEBOUNCE_MS = 250;
  const HIGHLIGHT_MS = 6000;
  const KEYED = "tr[id], li[id], article[id], section[id], [data-key]";

  const status = document.querySelector("[data-live-status]");
  const regions = () => Array.from(document.querySelectorAll("[data-live][id]"));
  const hasListeners = () => regions().length > 0 || document.querySelector("[data-live-map]");
  const timers = new Map();
  const busy = new Set();
  const again = new Set();
  const pending = new Set();
  let known = null;

  try { known = JSON.parse(document.body.dataset.liveTokens || "null"); } catch (e) { known = null; }

  const clock = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Manila", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });

  function setState(state, label) {
    if (!status) return;
    status.hidden = false;
    status.dataset.state = state;
    const text = status.querySelector("[data-live-label]");
    if (text.textContent !== label) text.textContent = label;
  }

  function markUpdated() {
    const time = status && status.querySelector("[data-live-updated]");
    if (time) { time.textContent = clock.format(new Date()); time.dateTime = new Date().toISOString(); }
  }

  // A snapshot is {tokens: {topic: token}}; topics whose token moved since the last one changed.
  function applySnapshot(snapshot) {
    const tokens = snapshot.tokens || {};
    if (!known) { known = tokens; return; }
    const changed = new Set(Object.keys(tokens).filter((t) => tokens[t] !== known[t]));
    known = tokens;
    if (!changed.size) return;
    regions().forEach((region) => {
      if (region.dataset.live.split(/\s+/).some((t) => changed.has(t))) schedule(region.id);
    });
    document.dispatchEvent(new CustomEvent("ulat:change", { detail: { topics: Array.from(changed) } }));
  }

  function schedule(id) {
    clearTimeout(timers.get(id));
    timers.set(id, setTimeout(() => { timers.delete(id); refresh(id, false); }, DEBOUNCE_MS));
  }

  function fieldChanged(field) {
    if (field.type === "checkbox" || field.type === "radio") return field.checked !== field.defaultChecked;
    if (field.tagName === "SELECT") return Array.from(field.options).some((o) => o.selected !== o.defaultSelected);
    if (["hidden", "submit", "button", "reset", "image", "file"].includes(field.type)) return false;
    return field.value !== field.defaultValue;
  }

  // The officer is working here: focus is inside (other than the region itself), or a field differs
  // from what the server rendered.
  function inUse(region) {
    const active = document.activeElement;
    if (active && active !== document.body && active !== region && region.contains(active)) return true;
    return Array.from(region.querySelectorAll("input, textarea, select")).some(fieldChanged);
  }

  function fieldKey(field) {
    const form = field.form;
    return (form ? form.getAttribute("action") || "" : "") + "|" + (field.name || field.id);
  }

  function showNotice(region) {
    if (region.querySelector(":scope > .live-notice")) return;
    const notice = document.createElement("div");
    notice.className = "live-notice no-print";
    notice.setAttribute("role", "status");
    notice.innerHTML = '<span>New updates for this section.</span> <button type="button" data-live-refresh>Refresh section</button>';
    region.prepend(notice);
  }

  // What must survive a swap: the scroll position (as an element's offset from the viewport
  // top), open <details>, keyed elements (to spot new rows), changed fields and focus.
  function capture(region) {
    const top = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--sticky-top")) || 0;
    // Only when the officer has scrolled into the region: at its top, new rows simply appear.
    let anchor = null;
    if (region.getBoundingClientRect().top < top) for (const el of region.querySelectorAll("[id]")) {
      const box = el.getBoundingClientRect();
      if (box.bottom > top && box.height > 0) { anchor = { id: el.id, top: box.top }; break; }
    }
    const openDetails = new Set(Array.from(region.querySelectorAll("details[open]")).map(detailsKey));
    const keys = new Set(Array.from(region.querySelectorAll(KEYED)).map(keyOf));
    const fields = new Map();
    region.querySelectorAll("input, textarea, select").forEach((f) => {
      if (fieldChanged(f)) fields.set(fieldKey(f), f.type === "checkbox" || f.type === "radio" ? f.checked : f.value);
    });
    const active = document.activeElement;
    const focus = active && region.contains(active) && active !== region
      ? (active.id ? "#" + active.id : active.name ? fieldKey(active) : "region") : null;
    return { anchor, openDetails, keys, fields, focus };
  }

  function detailsKey(details) {
    const owner = details.parentElement && details.parentElement.closest("[id]");
    const summary = details.querySelector("summary");
    return (owner ? owner.id : "") + "|" + (summary ? summary.textContent.trim() : "");
  }

  function keyOf(el) { return el.dataset.key || el.id; }

  function restore(region, before) {
    region.querySelectorAll("details").forEach((d) => { if (before.openDetails.has(detailsKey(d))) d.open = true; });
    region.querySelectorAll("input, textarea, select").forEach((f) => {
      const key = fieldKey(f);
      if (!before.fields.has(key)) return;
      const value = before.fields.get(key);
      if (f.type === "checkbox" || f.type === "radio") f.checked = value; else f.value = value;
      const details = f.closest("details");
      if (details) details.open = true;
    });
    if (before.focus) {
      const target = before.focus === "region" ? region : before.focus.startsWith("#")
        ? document.getElementById(before.focus.slice(1))
        : Array.from(region.querySelectorAll("input, textarea, select")).find((f) => fieldKey(f) === before.focus);
      (target && region.contains(target) ? target : region).focus({ preventScroll: true });
    }
    if (before.anchor) {
      const el = document.getElementById(before.anchor.id);
      if (el) window.scrollBy(0, el.getBoundingClientRect().top - before.anchor.top);
    }
    const fresh = Array.from(region.querySelectorAll(KEYED)).filter((el) => !before.keys.has(keyOf(el)));
    if (before.keys.size && fresh.length < 50) {
      fresh.forEach((el) => el.classList.add("is-new"));
      setTimeout(() => fresh.forEach((el) => el.classList.remove("is-new")), HIGHLIGHT_MS);
    }
    tickCountdowns(region);
  }

  function refresh(id, requested) {
    const region = document.getElementById(id);
    if (!region) return;
    if (busy.has(id)) { again.add(id); return; }
    if (document.hidden && !requested) { pending.add(id); return; }
    if (!requested && inUse(region)) { pending.add(id); showNotice(region); return; }
    pending.delete(id);
    busy.add(id);
    const before = capture(region);
    if (!region.hasAttribute("tabindex")) region.setAttribute("tabindex", "-1");
    window.htmx.ajax("GET", window.location.pathname + window.location.search, {
      target: "#" + id, select: "#" + id, swap: "outerHTML",
    }).then(() => {
      const fresh = document.getElementById(id);
      if (fresh && fresh !== region) { restore(fresh, before); markUpdated(); }
    }).catch(() => {
      showNotice(region);
    }).finally(() => {
      busy.delete(id);
      if (again.delete(id)) schedule(id);
    });
  }

  // A requested refresh, or the officer leaving a section that has updates waiting.
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-live-refresh]");
    if (!button) return;
    const region = button.closest("[data-live][id]");
    if (region) refresh(region.id, true);
  });
  document.addEventListener("focusout", () => {
    setTimeout(() => pending.forEach((id) => {
      const region = document.getElementById(id);
      if (region && !inUse(region) && !document.hidden) refresh(id, false);
    }), 0);
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) pending.forEach((id) => refresh(id, false));
  });

  // ---------- Connection: SSE first, polling when it keeps failing ----------
  let source = null;
  let failures = 0;
  let pollTimer = null;
  let polls = 0;

  function connect() {
    if (!window.EventSource) { startPolling(); return; }
    if (source) source.close();
    setState("connecting", "Connecting");
    source = new EventSource("/events");
    source.addEventListener("open", () => { failures = 0; stopPolling(); setState("live", "Live"); });
    const onSnapshot = (event) => { try { applySnapshot(JSON.parse(event.data)); } catch (e) { /* ignore a bad frame */ } };
    source.addEventListener("hello", onSnapshot);
    source.addEventListener("change", onSnapshot);
    source.addEventListener("error", () => {
      failures += 1;
      if (source.readyState === EventSource.CLOSED || failures >= SSE_FAILURES) {
        source.close();
        source = null;
        startPolling();
      } else {
        setState("reconnecting", "Reconnecting");
      }
    });
  }

  async function poll() {
    polls += 1;
    if (polls % SSE_RETRY_EVERY === 0) { stopPolling(); failures = 0; connect(); return; }
    try {
      const response = await fetch("/events/token", { cache: "no-store", headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(String(response.status));
      applySnapshot(await response.json());
      setState("polling", "Live, checking every 20 s");
    } catch (e) {
      setState("offline", "Offline");
    }
  }

  function startPolling() {
    if (pollTimer) return;
    setState(navigator.onLine === false ? "offline" : "reconnecting", navigator.onLine === false ? "Offline" : "Reconnecting");
    pollTimer = setInterval(poll, POLL_MS);
    poll();
  }

  function stopPolling() { clearInterval(pollTimer); pollTimer = null; }

  window.addEventListener("offline", () => setState("offline", "Offline"));
  window.addEventListener("online", () => { if (!source || source.readyState !== EventSource.OPEN) { stopPolling(); failures = 0; connect(); } });
  window.addEventListener("pagehide", () => { if (source) { source.close(); source = null; } });
  window.addEventListener("pageshow", (event) => { if (event.persisted && hasListeners()) { failures = 0; connect(); } });

  if (window.htmx) {
    window.htmx.config.ignoreTitle = true;
    window.htmx.config.historyEnabled = false;
  }
  if (hasListeners() && window.htmx) connect();

  window.Ulat = { tickCountdowns, inUse, refresh };
})();
