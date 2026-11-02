// Ulat officer dashboard. Plain script, no build step; every page works without it: links and
// forms are plain HTML, and htmx only loads the next page into #shell instead of reloading.
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

  // ---------- Officer initials ----------
  // Review forms ask for the officer's initials (no login yet). Remember the last ones on this
  // device and fill them in as the field's default, so a prefilled form does not count as edited
  // (a live refresh may still replace it) and the server still receives them on every action.
  const INITIALS_KEY = "ulat.officer";
  function fillInitials(root) {
    let saved = null;
    try { saved = localStorage.getItem(INITIALS_KEY); } catch (e) { return; }
    if (!saved) return;
    (root || document).querySelectorAll('input[name="officer"]').forEach((input) => {
      if (!input.value && !input.defaultValue) { input.defaultValue = saved; }
    });
  }
  document.addEventListener("submit", (event) => {
    const input = event.target.querySelector && event.target.querySelector('input[name="officer"]');
    if (input && input.value.trim()) { try { localStorage.setItem(INITIALS_KEY, input.value.trim()); } catch (e) { /* private mode */ } }
  });
  fillInitials();
  document.addEventListener("htmx:afterSwap", () => fillInitials());

  // ---------- Fillable forms (app/fillable.py) ----------
  // A personal field (names, contact details) has no name attribute, so the browser never posts
  // it. Its value is kept in this tab only (sessionStorage: gone when the tab closes), per form
  // (the output, so a saved new version keeps it), copied into the printout, and cleared by
  // "Clear personal details". The save bar
  // counts unsaved changes, and printing with unsaved changes says the paper shows the saved
  // version.
  const PERSONAL_PREFIX = "ulat.personal.";
  const fillForm = () => document.getElementById("fill");
  function personalKey(form) { return PERSONAL_PREFIX + (form.dataset.scope || ""); }
  function readPersonal(form) {
    try { return JSON.parse(sessionStorage.getItem(personalKey(form)) || "{}"); } catch (e) { return {}; }
  }
  function writePersonal(form, values) {
    try {
      if (Object.keys(values).length) sessionStorage.setItem(personalKey(form), JSON.stringify(values));
      else sessionStorage.removeItem(personalKey(form));
    } catch (e) { /* storage off: the value is still on screen and on paper */ }
  }
  function printPersonal(values) {
    document.querySelectorAll("[data-personal-print]").forEach((span) => {
      span.textContent = values[span.dataset.personalPrint] || "";
    });
  }
  function hydrateFill() {
    const form = fillForm();
    if (!form) return;
    const values = readPersonal(form);
    form.querySelectorAll("[data-personal]").forEach((input) => {
      const value = values[input.dataset.personal];
      // As the default too, so a restored value does not count as an unsaved change.
      if (value !== undefined && !input.value) { input.value = value; input.defaultValue = value; }
    });
    printPersonal(values);
    form.querySelectorAll("[data-personal-clear]").forEach((button) => { button.hidden = false; });
    countChanges();
  }
  function countChanges() {
    const form = fillForm();
    const status = form && form.querySelector("[data-fill-status]");
    if (!status) return 0;
    if (!status.dataset.idle) status.dataset.idle = status.textContent;
    const names = new Set();
    form.querySelectorAll("[name^='f.']").forEach((field) => { if (fieldChanged(field)) names.add(field.name); });
    status.textContent = names.size ? names.size + " unsaved change" + (names.size !== 1 ? "s" : "") + ". " + status.dataset.idle : status.dataset.idle;
    status.classList.toggle("has-changes", names.size > 0);
    return names.size;
  }
  document.addEventListener("input", (event) => {
    const input = event.target;
    const form = input.closest && input.closest("#fill");
    if (!form) return;
    if (input.dataset.personal) {
      const values = readPersonal(form);
      const value = input.value.trim();
      if (value) values[input.dataset.personal] = value; else delete values[input.dataset.personal];
      writePersonal(form, values);
      printPersonal(values);
      warnSharedDevice(form);
    }
    countChanges();
  });
  document.addEventListener("change", (event) => { if (event.target.closest && event.target.closest("#fill")) countChanges(); });
  window.addEventListener("beforeprint", () => {
    const form = fillForm();
    if (!form) return;
    printPersonal(readPersonal(form));
    if (countChanges()) snackbar("Unsaved changes are not on the printout: it shows the saved version. Save first to print them.", "error");
  });
  document.addEventListener("htmx:afterSettle", hydrateFill);

  // A shared computer: say once per tab, at the first personal detail typed, how to clear them.
  const SHARED_SEEN = "ulat.shared-warning-seen";
  function sharedSeen() { try { return sessionStorage.getItem(SHARED_SEEN) === "1"; } catch (e) { return false; } }
  function warnSharedDevice(form) {
    if (sharedSeen()) return;
    form.querySelectorAll("[data-shared-warning]").forEach((note) => { note.hidden = false; });
  }
  function clearPersonal() {
    const form = fillForm();
    if (!form) return;
    writePersonal(form, {});
    form.querySelectorAll("[data-personal]").forEach((input) => { input.value = ""; input.defaultValue = ""; });
    printPersonal({});
    countChanges();
    snackbar("Personal details cleared from this device.");
  }

  // The "?" help: a native popover (Escape and a tap outside close it). On a wide window it
  // opens beside its button, kept inside the window; on a phone the stylesheet makes it a
  // bottom sheet. Focus moves into it so a screen reader reads it; closing returns focus.
  const WIDE = window.matchMedia("(min-width: 37.5rem)");
  function placeHelp(pop) {
    const button = document.querySelector('.help-btn[popovertarget="' + CSS.escape(pop.id) + '"]');
    if (!WIDE.matches || !button) { pop.classList.remove("is-placed"); pop.style.left = pop.style.top = ""; return; }
    const at = button.getBoundingClientRect();
    const gap = 8, width = pop.offsetWidth, height = pop.offsetHeight;
    let top = at.bottom + 4;
    if (top + height > innerHeight - gap) top = Math.max(gap, at.top - height - 4);
    pop.style.left = Math.min(Math.max(gap, at.right - width), innerWidth - width - gap) + "px";
    pop.style.top = top + "px";
    pop.classList.add("is-placed");
  }
  document.addEventListener("toggle", (event) => {
    const pop = event.target;
    if (!pop.classList || !pop.classList.contains("help-pop") || event.newState !== "open") return;
    placeHelp(pop);
    pop.focus({ preventScroll: true });
  }, true);
  const replaceOpenHelp = () => document.querySelectorAll(".help-pop.is-placed:popover-open").forEach(placeHelp);
  window.addEventListener("resize", replaceOpenHelp);
  document.addEventListener("scroll", replaceOpenHelp, { capture: true, passive: true });

  // Printing: "Print final form" or "Print draft for checking". The paper shows the saved
  // version, so unsaved changes come first ("Save first"); then the fields the office must
  // answer that are still empty ("Fill them"); otherwise it prints straight away, unless the
  // checklist offers an option (the Notice of Loss's "Include the photo evidence page", off
  // by default), which it then shows on its own.
  const plural = (n, word) => n + " " + word + (n !== 1 ? "s" : "");
  function openPrintCheck() {
    const dialog = document.getElementById("print-check");
    const form = fillForm();
    const unsaved = form ? countChanges() : 0;
    const gaps = dialog && dialog.querySelector("[data-print-gaps]");
    const empty = gaps ? Number(gaps.dataset.count) : 0;
    const options = dialog ? [...dialog.querySelectorAll("[data-print-option]")] : [];
    options.forEach((option) => { option.checked = false; });
    if (!dialog || !dialog.showModal || (!unsaved && !empty && !options.length)) { printPaper(); return; }
    dialog.dataset.mode = unsaved ? "unsaved" : (empty ? "gaps" : "ready");
    dialog.querySelector("[data-print-title]").textContent = unsaved
      ? plural(unsaved, "change") + (unsaved !== 1 ? " are" : " is") + " not saved yet"
      : empty ? plural(empty, "field") + (empty !== 1 ? " are" : " is") + " still empty" : "Print the form";
    dialog.querySelector("[data-print-unsaved]").hidden = !unsaved;
    gaps.hidden = !!unsaved || !empty;
    dialog.querySelector("[data-print-back]").textContent = unsaved ? "Save first" : !empty ? "Cancel" : (empty !== 1 ? "Fill them" : "Fill it");
    dialog.querySelector("[data-print-go]").textContent = unsaved ? "Print without them" : empty ? "Print anyway" : "Print";
    const blank = form ? [...form.querySelectorAll("[data-personal]")].filter((input) => !input.value.trim()).length : 0;
    const personal = dialog.querySelector("[data-print-personal]");
    personal.hidden = !blank;
    personal.textContent = blank ? plural(blank, "personal detail") + (blank !== 1 ? " are" : " is") + " blank: they print as lines to write on." : "";
    dialog.returnValue = "";
    dialog.showModal();
  }
  // The attachment page prints only when asked for, for this one print.
  function printPaper(attachment) {
    document.documentElement.toggleAttribute("data-print-attachment", !!attachment);
    window.print();
  }
  window.addEventListener("afterprint", () => document.documentElement.removeAttribute("data-print-attachment"));
  function jumpToField(key) {
    const field = document.getElementById("f-" + key);
    if (!field) return;
    for (let at = field.closest("details"); at; at = at.parentElement && at.parentElement.closest("details")) at.open = true;
    field.scrollIntoView({ block: "center" });
    const input = field.querySelector("input:not([type=hidden]), textarea");
    if (input) input.focus({ preventScroll: true });
  }
  document.addEventListener("close", (event) => {
    const dialog = event.target;
    if (dialog.id !== "print-check") return;
    if (dialog.returnValue === "print") {
      const attach = dialog.querySelector("[data-print-option='attachment']");
      setTimeout(() => printPaper(attach && attach.checked), 0);  // after the dialog is gone
    }
    else if (dialog.returnValue === "fill" && dialog.dataset.mode === "unsaved") { const form = fillForm(); if (form) form.requestSubmit(); }
    else if (dialog.returnValue === "fill" && dialog.dataset.mode === "gaps") { const first = dialog.querySelector("[data-fill-jump]"); if (first) jumpToField(first.dataset.fillJump); }
  }, true);
  document.addEventListener("click", (event) => {
    const target = event.target.closest ? event.target : null;
    if (!target) return;
    if (target.closest("[data-print]")) { openPrintCheck(); return; }
    const choice = target.closest("[data-print-choice]");
    if (choice) { choice.closest("dialog").close(choice.dataset.printChoice); return; }
    const jump = target.closest("[data-fill-jump]");
    if (jump) {
      event.preventDefault();
      const dialog = jump.closest("dialog");
      if (dialog) { dialog.returnValue = ""; dialog.close(); }
      jumpToField(jump.dataset.fillJump);
      return;
    }
    if (target.closest("[data-personal-clear]")) { clearPersonal(); return; }
    if (target.closest("[data-shared-ok]")) {
      try { sessionStorage.setItem(SHARED_SEEN, "1"); } catch (e) { /* shown again next time */ }
      document.querySelectorAll("[data-shared-warning]").forEach((note) => { note.hidden = true; });
    }
  });

  // ---------- Live updates ----------
  // The server sends which topics changed (reports, incidents, drafts, spend) over SSE
  // (GET /events, app/live.py). Each region marked data-live="<topics>" re-fetches this page
  // and swaps in its own fresh copy (htmx `select`), so there is one render path per page.
  // A region the officer is working in (focus inside, or a field changed) is never swapped:
  // it shows a "New updates" notice instead, and its typed values survive a requested refresh.
  // A region marked data-live-quiet (a count on the navigation) never shows that notice: it holds
  // nothing to work in, and a failed refresh simply waits for the next change.

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

  const pageTokens = () => {
    const main = document.querySelector("[data-live-tokens]");
    try { return JSON.parse((main && main.dataset.liveTokens) || "null"); } catch (e) { return null; }
  };
  known = pageTokens();

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
    if (field.tagName === "SELECT") {
      // A single select with no option marked selected shows its first option by default.
      const options = Array.from(field.options);
      if (!field.multiple && !options.some((o) => o.defaultSelected)) return field.selectedIndex > 0;
      return options.some((o) => o.selected !== o.defaultSelected);
    }
    if (["hidden", "submit", "button", "reset", "image", "file"].includes(field.type)) return false;
    return field.value !== field.defaultValue;
  }

  // The officer is working here: focus is inside (other than the region itself), a popover in it
  // is open (a field's help being read), or a field differs from what the server rendered.
  function inUse(region) {
    const active = document.activeElement;
    if (active && active !== document.body && active !== region && region.contains(active)
      && active.getAttribute("tabindex") !== "-1") return true;
    try { if (region.querySelector(":popover-open")) return true; } catch (e) { /* no popover support */ }
    return Array.from(region.querySelectorAll("input, textarea, select")).some(fieldChanged);
  }

  function fieldKey(field) {
    const form = field.form;
    return (form ? form.getAttribute("action") || "" : "") + "|" + (field.name || field.id);
  }

  function showNotice(region) {
    if (region.hasAttribute("data-live-quiet") || region.querySelector(":scope > .live-notice")) return;
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
    const focus = active && region.contains(active) && active !== region ? focusKey(region, active) : null;
    return { anchor, openDetails, keys, fields, focus };
  }

  // Where focus was, in terms the fresh copy also has: an id, a field, else the element's tag and
  // position (a page heading the navigation focused), else the region itself.
  function focusKey(region, el) {
    if (el.id) return "#" + el.id;
    if (el.name) return fieldKey(el);
    const index = Array.from(region.querySelectorAll(el.tagName)).indexOf(el);
    return index >= 0 ? "@" + el.tagName + ":" + index : "region";
  }

  function findFocus(region, key) {
    if (key === "region") return region;
    if (key.startsWith("#")) return document.getElementById(key.slice(1));
    if (key.startsWith("@")) {
      const [tag, index] = key.slice(1).split(":");
      return region.querySelectorAll(tag)[Number(index)];
    }
    return Array.from(region.querySelectorAll("input, textarea, select")).find((f) => fieldKey(f) === key);
  }

  function detailsKey(details) {
    // An explicit key survives a summary whose text changes (the agent trace's totals).
    if (details.dataset.detailsKey) return details.dataset.detailsKey;
    const owner = details.parentElement && details.parentElement.closest("[id]");
    const summary = details.querySelector("summary");
    return (owner ? owner.id : "") + "|" + (summary ? summary.textContent.trim() : "");
  }

  function keyOf(el) { return el.dataset.key || el.id; }

  function restore(region, before) {
    if (!region.hasAttribute("tabindex")) region.setAttribute("tabindex", "-1");
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
      const found = findFocus(region, before.focus);
      const target = found && region.contains(found) ? found : region;
      // A heading is focusable only through the tabindex the old copy was given.
      if (target.tabIndex < 0 && !target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
      target.focus({ preventScroll: true });
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
    if (region.dataset.liveMerge === "queue") { refreshQueue(region); return; }
    if (!requested && inUse(region)) { pending.add(id); showNotice(region); return; }
    pending.delete(id);
    busy.add(id);
    const before = capture(region);
    // The region is the request's source: htmx keeps one request per source and, by default,
    // replaces a queued one with the next, so a shared source (the body) would drop all but the
    // first and last of several regions refreshing at once.
    window.htmx.ajax("GET", window.location.pathname + window.location.search, {
      source: region, target: "#" + id, select: "#" + id, swap: "outerHTML ignoreTitle:true",
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
      if (response.status === 401) { signIn(); return; }
      if (!response.ok) throw new Error(String(response.status));
      applySnapshot(await response.json());
      setState("polling", "Live, checking every 20 s");
    } catch (e) {
      setState("offline", "Offline");
    }
  }

  // The session expired: go to the sign-in page, which brings the officer back here after.
  function signIn() {
    stopPolling();
    window.location.assign("/login?next=" + encodeURIComponent(window.location.pathname + window.location.search));
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

  // Connect when a page with live sections or a map is shown; the stream then stays open while
  // the officer moves between pages (one connection per tab, no reconnect on each navigation).
  function ensureLive() {
    if (!window.htmx || source || pollTimer || !hasListeners()) return;
    failures = 0;
    connect();
  }

  // A page loaded into the shell was rendered with its own tokens. Before the stream is open they
  // are the starting point; after, a section whose topics differ from what the stream last said
  // refreshes once (a change can land between the render and the swap).
  function adoptPage() {
    Array.from(pending).forEach((id) => { if (!document.getElementById(id)) pending.delete(id); });
    const tokens = pageTokens();
    if (!tokens) return;
    if (!known || !(source || pollTimer)) { known = tokens; return; }
    const stale = new Set(Object.keys(tokens).filter((t) => tokens[t] !== known[t]));
    if (stale.size) regions().forEach((region) => {
      if (region.dataset.live.split(/\s+/).some((t) => stale.has(t))) schedule(region.id);
    });
  }

  // ---------- Maps: Leaflet loads only on a page that shows a map ----------
  const assets = new Map();
  function loadAsset(url) {
    if (!assets.has(url)) assets.set(url, new Promise((resolve, reject) => {
      const css = /\.css(\?|$)/.test(url);
      const node = document.createElement(css ? "link" : "script");
      if (css) { node.rel = "stylesheet"; node.href = url; } else { node.src = url; node.async = false; }
      node.onload = resolve;
      node.onerror = () => { assets.delete(url); node.remove(); reject(new Error(url)); };
      document.head.append(node);
    }));
    return assets.get(url);
  }

  function mountMaps() {
    document.querySelectorAll("[data-live-map]:not([data-mounted])").forEach((container) => {
      container.dataset.mounted = "";
      const urls = (container.dataset.assets || "").split(/\s+/).filter(Boolean);
      Promise.all(urls.map(loadAsset)).then(() => {
        if (window.UlatMap && container.isConnected) window.UlatMap.mount(container);
      }).catch(() => {
        const note = container.parentElement.querySelector("[data-map-note]");
        if (note) { note.textContent = "The map could not load. The list on this page has every incident."; note.hidden = false; }
      });
    });
  }

  // ---------- Snackbar: the result of an action ----------
  const SNACKBAR_MS = 6000;
  const SNACKBAR_ERROR_MS = 10000;
  const snack = document.querySelector("[data-snackbar]");
  const snackAction = snack && snack.querySelector("[data-snackbar-action]");
  let snackTimer = null;
  let snackRun = null;  // what the snackbar's action button does (Undo), while it shows

  function hideSnackbar() {
    clearTimeout(snackTimer);
    snackRun = null;
    if (snackAction) snackAction.hidden = true;
    if (!snack || !snack.classList.contains("is-open")) return;
    snack.classList.remove("is-open", "snackbar--error");
    // Emptied after it fades, so a screen reader never meets a stale message.
    snackTimer = setTimeout(() => { snack.querySelector("[data-snackbar-text]").textContent = ""; }, 200);
  }

  // `action` is {label, run}: one text button (M3 allows one), shown for as long as the message.
  function snackbar(text, tone, action) {
    if (!snack || !text) return;
    clearTimeout(snackTimer);
    snack.classList.toggle("snackbar--error", tone === "error");
    snack.classList.add("is-open");
    snack.querySelector("[data-snackbar-text]").textContent = text;
    snackRun = action ? action.run : null;
    if (snackAction) { snackAction.hidden = !action; snackAction.textContent = action ? action.label : ""; }
    snackTimer = setTimeout(hideSnackbar, tone === "error" ? SNACKBAR_ERROR_MS : SNACKBAR_MS);
  }
  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-snackbar-close]")) hideSnackbar();
    if (event.target.closest("[data-snackbar-action]") && snackRun) { const run = snackRun; hideSnackbar(); run(); }
  });

  // ---------- Page navigation without reloads (htmx boost on <body>) ----------
  // Links and forms load the next page's #shell (navigation and content); the app bar, the live
  // connection and the snackbar stay. The address bar, Back and Forward work as usual: Back
  // re-fetches the page (never a stale or stored copy) and returns to where the officer was.
  const PROGRESS_DELAY_MS = 150;
  const progress = document.querySelector("[data-page-progress]");
  const scrolls = new Map();   // scroll position per page, for Back and Forward
  const here = () => window.location.pathname + window.location.search;
  let shownPath = here();
  let nav = null;              // the boosted request in flight: { verb, elt, swapping }
  let progressTimer = null;

  function showProgress(on) {
    clearTimeout(progressTimer);
    if (!progress) return;
    if (on) progressTimer = setTimeout(() => { progress.hidden = false; }, PROGRESS_DELAY_MS);
    else progress.hidden = true;
  }

  function closePopovers() {
    document.querySelectorAll("[popover]").forEach((p) => {
      try { if (p.matches(":popover-open")) p.hidePopover(); } catch (e) { /* no popover support */ }
    });
  }

  function setBusy(form, busy) {
    if (!form || form.tagName !== "FORM") return;
    form.querySelectorAll("button:not([type]), button[type=submit]").forEach((b) => {
      b.disabled = busy;
      if (busy) b.setAttribute("aria-busy", "true"); else b.removeAttribute("aria-busy");
    });
  }

  // For screen readers and keyboards: the new page starts at its heading.
  function focusHeading() {
    const main = document.getElementById("main");
    const target = (main && main.querySelector("h1")) || main;
    if (!target) return;
    if (!target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
    target.focus({ preventScroll: true });
  }

  function pageShown() {
    shownPath = here();
    closePopovers();
    mountMaps();
    adoptPage();
    ensureLive();
    document.dispatchEvent(new CustomEvent("ulat:page"));
  }

  document.addEventListener("htmx:beforeRequest", (event) => {
    if (!event.detail.boosted) return;
    nav = { verb: event.detail.requestConfig.verb, elt: event.detail.elt, swapping: false };
    closePopovers();
    hideSnackbar();
    showProgress(true);
    setBusy(event.detail.elt, true);
  });

  document.addEventListener("htmx:beforeSwap", (event) => {
    const detail = event.detail;
    if (!detail.boosted || !nav) return;
    // Any dashboard page is shown, an error page too (a form's 4xx carries its message).
    if (/\bid="shell"/.test(detail.xhr.responseText || "")) {
      detail.shouldSwap = true;
      detail.isError = false;
      nav.swapping = true;
      return;
    }
    // Not a dashboard page (a download, an image, a plain server error): let the browser have it.
    detail.shouldSwap = false;
    if (nav.verb === "get") { window.location.assign(detail.xhr.responseURL || detail.pathInfo.finalRequestPath); return; }
    snackbar("The server could not finish this request (error " + detail.xhr.status + "). Reload the page to see what was saved.", "error");
  });

  document.addEventListener("htmx:beforeHistoryUpdate", (event) => {
    const update = event.detail.history;
    if (!update || !update.type) return;
    window.history.scrollRestoration = "manual";
    // A refused form keeps the page's own address (a reload shows the page, not the form's URL)
    // in a new entry, as the browser would: Back, or the error page's "Go back", returns to it.
    if (event.detail.xhr && event.detail.xhr.status >= 400) { update.type = "push"; update.path = here(); return; }
    // Like the browser: a link to the address already shown replaces it instead of adding one.
    if (update.type === "push" && nav && nav.verb === "get" && update.path.split("#")[0] === here()) update.type = "replace";
  });

  // Each history entry this page shows gets an id, so a Back or Forward that only moves between
  // #fragments of the same entry is told apart from one to another page at the same address.
  let entry = null;
  function markEntry() {
    entry = Date.now().toString(36) + Math.random().toString(36).slice(2);
    window.history.replaceState(Object.assign({}, window.history.state, { htmx: true, ulat: entry }), "");
  }
  document.addEventListener("htmx:pushedIntoHistory", markEntry);
  document.addEventListener("htmx:replacedInHistory", markEntry);

  document.addEventListener("htmx:afterRequest", (event) => {
    if (!event.detail.boosted) return;
    showProgress(false);
    setBusy(event.detail.elt, false);
    if (nav && !nav.swapping) nav = null;
  });

  document.addEventListener("htmx:sendError", (event) => {
    if (event.detail.boosted) snackbar("No connection. Check your signal and try again; nothing was sent.", "error");
  });

  document.addEventListener("htmx:afterSettle", (event) => {
    if (!nav || !nav.swapping || !event.detail.elt || event.detail.elt.id !== "shell") return;
    const done = nav;
    nav = null;
    pageShown();
    const failed = event.detail.xhr && event.detail.xhr.status >= 400;
    const main = document.getElementById("main");
    if (failed) {
      const problem = main.querySelector('[aria-invalid="true"], [role="alert"], .banner.alert');
      if (problem) {
        problem.scrollIntoView({ block: "center" });
        if (!problem.matches("input, textarea, select")) problem.setAttribute("tabindex", "-1");
        problem.focus({ preventScroll: true });
      } else { window.scrollTo(0, 0); focusHeading(); }
      if (done.verb !== "get") snackbar("Not saved. The page says what to fix.", "error");
      return;
    }
    const hash = window.location.hash.slice(1);
    const anchor = (hash && document.getElementById(decodeURIComponent(hash)))
      || (done.elt.dataset && done.elt.dataset.returnAnchor && document.getElementById(done.elt.dataset.returnAnchor));
    if (anchor) anchor.scrollIntoView({ block: "start" }); else window.scrollTo(0, 0);
    focusHeading();
    if (done.verb !== "get") {
      const flash = main.querySelector("[data-flash]");
      snackbar((flash && flash.textContent.trim()) || (done.elt.dataset && done.elt.dataset.done));
    }
  });

  // Back and Forward: htmx fetches the page again; show progress, report a failed load, and
  // return to the scroll position the officer left.
  document.addEventListener("htmx:historyCacheMiss", (event) => {
    showProgress(true);
    const xhr = event.detail.xhr;
    xhr.addEventListener("loadend", () => showProgress(false));
    xhr.addEventListener("error", () => snackbar("No connection: this page could not load. Try again when you have signal.", "error"));
  });
  document.addEventListener("htmx:historyCacheMissLoadError", () => {
    snackbar("This page could not load. Reload to try again.", "error");
  });
  document.addEventListener("htmx:beforeHistorySave", () => { scrolls.set(shownPath, window.scrollY); });
  document.addEventListener("htmx:historyRestore", () => {
    markEntry();
    pageShown();
    window.scrollTo(0, scrolls.get(here()) || 0);
    focusHeading();
  });
  // A Back or Forward that only changes the #fragment stays on this page (no re-fetch).
  window.addEventListener("load", () => {
    const htmxPopstate = window.onpopstate;
    if (!htmxPopstate) return;
    window.onpopstate = (event) => {
      if (event.state && event.state.ulat && event.state.ulat === entry) return;
      htmxPopstate.call(window, event);
    };
  });

  // ---------- Review queue (Simple mode: app/templates/_queue.html) ----------
  // One question at a time. An answer is a plain form; here it waits behind the snackbar's Undo
  // for as long as the snackbar shows, then posts like the form would (same fields, same
  // endpoint), and the next question is on screen at once. Leaving the page or answering the
  // next question sends a waiting answer straight away. A live refresh never replaces the
  // question being read: questions answered elsewhere drop out, new ones join the end with a
  // quiet "2 new" chip.
  const UNDO_MS = SNACKBAR_MS;
  const answered = new Set();   // keys answered on this page: hidden even while a refresh lists them
  let waiting = null;           // { form, item, body, timer }
  let queueBusy = false;
  const queueList = () => document.querySelector("[data-queue]");
  const questions = () => {
    const list = queueList();
    return list ? Array.from(list.querySelectorAll(":scope > [data-q]")).filter((li) => !answered.has(li.dataset.key)) : [];
  };
  const onScreen = () => questions().find((li) => !li.hidden);

  function syncQueue(focus) {
    const list = queueList();
    if (!list) return;
    const left = questions();
    let shown = onScreen();
    list.querySelectorAll(":scope > [data-q]").forEach((li) => { if (li !== shown) li.hidden = true; });
    if (!shown && left.length) { shown = left[0]; shown.hidden = false; }
    const region = list.closest("[data-live]");
    const empty = region.querySelector("[data-q-empty]");
    if (empty) empty.hidden = left.length > 0;
    const count = region.querySelector(".queue__count");
    if (count) count.hidden = !left.length;
    const pos = region.querySelector("[data-q-pos]"), total = region.querySelector("[data-q-total]");
    if (pos) pos.textContent = String(left.indexOf(shown) + 1);
    if (total) total.textContent = String(left.length);
    const fresh = left.filter((li) => li.hasAttribute("data-q-new"));
    const chip = region.querySelector("[data-q-new]");
    if (chip) { chip.hidden = !fresh.length; chip.textContent = fresh.length + " new"; }
    if (!shown) return;
    if (shown.hasAttribute("data-q-new")) shown.removeAttribute("data-q-new");
    const at = left.indexOf(shown);
    const back = shown.querySelector('[data-q-go="prev"]'), skip = shown.querySelector('[data-q-go="next"]');
    if (back) back.hidden = at === 0;
    if (skip) skip.hidden = left.length < 2;
    if (focus) {
      const ask = shown.querySelector(".q__ask");
      ask.setAttribute("tabindex", "-1");
      ask.focus({ preventScroll: true });
      const top = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--sticky-top")) || 0;
      const box = region.getBoundingClientRect();
      if (box.top < top) window.scrollBy(0, box.top - top);
    }
  }

  function showQuestion(li) {
    if (!li) { syncQueue(false); return; }
    questions().forEach((el) => { el.hidden = el !== li; });
    li.classList.remove("q--in"); void li.offsetWidth; li.classList.add("q--in");
    syncQueue(true);
    try { window.history.replaceState(window.history.state, "", "/review?at=" + encodeURIComponent(li.dataset.key)); } catch (e) { /* no history */ }
  }

  document.addEventListener("click", (event) => {
    const go = event.target.closest && event.target.closest("[data-q-go]");
    if (!go || !queueList()) return;
    event.preventDefault();
    const left = questions(), at = left.indexOf(onScreen());
    showQuestion(go.dataset.qGo === "prev" ? left[Math.max(0, at - 1)] : left[(at + 1) % left.length]);
  });

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!form.matches || !form.matches("[data-answer]") || !form.closest("[data-queue]")) return;
    event.preventDefault();
    sendWaiting();
    const item = form.closest("[data-q]");
    const left = questions(), at = left.indexOf(item);
    answered.add(item.dataset.key);
    waiting = { form, item, body: new URLSearchParams(new FormData(form)), timer: setTimeout(sendWaiting, UNDO_MS) };
    showQuestion(left[at + 1] || left.find((li) => li !== item) || null);
    snackbar(form.dataset.done || "Saved", "", { label: "Undo", run: undoAnswer });
  });

  function undoAnswer() {
    if (!waiting) return;
    clearTimeout(waiting.timer);
    const item = waiting.item;
    waiting = null;
    answered.delete(item.dataset.key);
    if (item.isConnected) showQuestion(item);
    snackbar("Undone. Nothing was saved.");
  }

  function refused(html) {
    const doc = new DOMParser().parseFromString(html, "text/html");
    const alert = doc.querySelector("#main [role=alert], #main .error");
    return (alert && alert.textContent.trim()) || "The server did not save it.";
  }

  function sendWaiting() {
    if (!waiting) return;
    const w = waiting;
    waiting = null;
    clearTimeout(w.timer);
    if (snackRun === undoAnswer) { snackRun = null; if (snackAction) snackAction.hidden = true; }
    const key = w.item.dataset.key;
    const failed = (message) => {
      answered.delete(key);
      if (w.item.isConnected) showQuestion(w.item);
      snackbar("Not saved: " + message, "error");
    };
    fetch(w.form.action, { method: "POST", body: w.body, credentials: "same-origin", keepalive: true, headers: { Accept: "text/html" } })
      .then((response) => response.text().then((html) => {
        if (/\/login(\?|$)/.test(new URL(response.url, window.location.href).pathname + "?")) { signIn(); return; }
        if (!response.ok) { failed(refused(html)); return; }
        const fresh = new DOMParser().parseFromString(html, "text/html").getElementById("live-review");
        const region = document.getElementById("live-review");
        if (fresh && region) mergeQueue(region, fresh);
      }))
      .catch(() => failed("no connection. Check your signal; the question is back on screen."));
  }
  window.addEventListener("pagehide", sendWaiting);
  document.addEventListener("visibilitychange", () => { if (document.hidden) sendWaiting(); });
  document.addEventListener("htmx:beforeRequest", (event) => { if (event.detail.boosted) sendWaiting(); });

  function mergeQueue(region, fresh) {
    const mine = region.querySelector("[data-queue]"), theirs = fresh.querySelector("[data-queue]");
    if (!mine || !theirs) {
      // No question on screen here (or none left): the fresh copy is the page, unless the
      // officer is typing in it (the initials).
      if (!waiting && !inUse(region) && (!mine || !questions().length)) {
        const node = document.importNode(fresh, true);
        region.replaceWith(node);
        if (window.htmx) window.htmx.process(node);
        answered.forEach((key) => { if (!document.getElementById(key)) answered.delete(key); });
        syncQueue(false);
      }
      return;
    }
    const listed = new Set(Array.from(theirs.children).map((li) => li.dataset.key));
    const shown = onScreen();
    Array.from(mine.children).forEach((li) => {
      const keep = listed.has(li.dataset.key) || li === shown || (waiting && waiting.item === li);
      if (!keep) { li.remove(); answered.delete(li.dataset.key); }
    });
    Array.from(theirs.children).forEach((li) => {
      if (document.getElementById(li.dataset.key)) return;
      const node = document.importNode(li, true);
      node.hidden = true;
      if (mine.children.length) node.setAttribute("data-q-new", "");
      mine.append(node);
      if (window.htmx) window.htmx.process(node);
    });
    syncQueue(false);
    markUpdated();
  }

  function refreshQueue(region) {
    if (queueBusy) return;
    queueBusy = true;
    fetch("/review", { cache: "no-store", credentials: "same-origin", headers: { "HX-Request": "true" } })
      .then((response) => {
        if (response.status === 401) { signIn(); return null; }
        return response.ok ? response.text() : null;
      })
      .then((html) => {
        if (!html) return;
        const fresh = new DOMParser().parseFromString(html, "text/html").getElementById(region.id);
        const now = document.getElementById(region.id);
        if (fresh && now) mergeQueue(now, fresh);
      })
      .catch(() => { /* the next change tries again */ })
      .finally(() => { queueBusy = false; });
  }
  document.addEventListener("ulat:page", () => syncQueue(false));
  syncQueue(false);

  if (window.htmx) {
    const config = window.htmx.config;
    config.historyCacheSize = 0;           // never keep report content in sessionStorage
    config.scrollIntoViewOnBoost = false;  // handled above: top, the #anchor, or Back's position
  }
  if (window.htmx) markEntry();
  mountMaps();
  ensureLive();
  hydrateFill();

  window.Ulat = { tickCountdowns, inUse, refresh, snackbar };
})();
