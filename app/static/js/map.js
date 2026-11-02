// Officer map (Leaflet). Barangay boundaries and incident markers come from this app, so they
// still draw when the OpenStreetMap tiles cannot load (an offline demo). Updates live with the
// rest of the page: app.js dispatches "ulat:change" with the changed topics. app.js loads Leaflet
// and this file only when a page shows a map, and calls UlatMap.mount for each map on it.
(function () {
  "use strict";
  if (!window.L) return;

  const TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
  const ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    + ' · Boundaries: <a href="https://data.humdata.org/dataset/cod-ab-phl">OCHA COD-AB</a> (CC BY-IGO)';
  const BOUNDARY = { color: "#0b4f8a", weight: 1.5, opacity: 0.8, fillColor: "#0b4f8a", fillOpacity: 0.04 };
  const ACTIVE = { fillColor: "#b7861a", fillOpacity: 0.22 };

  // Text goes in with textContent only: titles and places come from reports.
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  const plural = (n, word) => n + " " + word + (n === 1 ? "" : "s");

  function pinSize(p) {
    return p.kind === "outside" ? 26 : Math.min(44, 26 + 4 * Math.max(0, (p.report_count || 1) - 1));
  }

  // Every pin is a 48 px touch target (Material and WCAG target size), however small it is drawn;
  // pins closer than that on screen are shown as one group that fans out when tapped.
  const TARGET = 48;

  function wrap(pin, size) {
    pin.style.width = pin.style.height = size + "px";
    return L.divIcon({ html: pin, className: "pin-wrap", iconSize: [TARGET, TARGET], iconAnchor: [TARGET / 2, TARGET / 2], popupAnchor: [0, -size / 2] });
  }

  function icon(p) {
    const pin = el("span", "pin " + (p.kind === "outside" ? "pin--outside"
      : "pin--" + p.status + (p.approximate ? " pin--approx" : "")), p.kind === "outside" ? "!" : String(p.report_count));
    return wrap(pin, pinSize(p));
  }

  function groupIcon(members) {
    const waiting = members.some((f) => f.properties.status === "needs_officer");
    return wrap(el("span", "pin pin--group" + (waiting ? " pin--group-waiting" : ""), "+" + members.length), 40);
  }

  // Greedy grouping in screen pixels at the current zoom, most urgent first, so a pin waiting for
  // review is never hidden under another one. O(n × groups), fine for a city's incidents.
  function groupByScreen(map, features) {
    const urgent = (f) => (f.properties.kind === "outside" ? 0 : f.properties.status === "needs_officer" ? 1 : 2);
    const groups = [];
    features.slice().sort((a, b) => urgent(a) - urgent(b)).forEach((f) => {
      const [lon, lat] = f.geometry.coordinates;
      const point = map.latLngToContainerPoint([lat, lon]);
      const near = groups.find((g) => g.point.distanceTo(point) < TARGET);
      if (near) near.members.push(f);
      else groups.push({ point, latlng: L.latLng(lat, lon), members: [f] });
    });
    return groups;
  }

  function describe(p) {
    if (p.kind === "outside") return "Report #" + p.report_id + ": flagged, pinned outside Biñan";
    return p.title + ": " + p.status_label + ", " + plural(p.report_count, "report") + (p.approximate ? ", approximate position" : "");
  }

  function popup(p) {
    const box = el("div", "map-popup");
    if (p.kind === "outside") {
      const link = el("a", null, "Report #" + p.report_id);
      link.href = p.url;
      box.append(el("h3", null), el("p", null, "Pinned outside all Biñan barangays and flagged by the trust checks. Verify before using it."));
      box.firstChild.append(link);
      return box;
    }
    const heading = el("h3");
    const link = el("a", null, p.title);
    link.href = p.url;
    heading.append(link);
    const chips = el("ul", "chips");
    chips.append(el("li", "chip " + (p.status === "needs_officer" ? "warn" : "info"), p.status_label));
    if (p.approximate) chips.append(el("li", "chip warn", "approximate: barangay centre"));
    if (p.reporter_count === 1) chips.append(el("li", "chip warn", "single source"));
    if (p.flagged_report_count > 0) chips.append(el("li", "chip warn", plural(p.flagged_report_count, "flagged report")));
    const facts = el("p", null, plural(p.report_count, "report")
      + (p.reporter_count != null ? " from " + plural(p.reporter_count, "reporter") : "")
      + " · flood depth latest " + p.latest_depth + ", max " + p.max_depth);
    box.append(heading, chips, facts);
    return box;
  }

  async function getJSON(url) {
    const response = await fetch(url, { headers: { Accept: "application/geo+json, application/json" } });
    if (!response.ok) throw new Error(url + ": " + response.status);
    return response.json();
  }

  async function init(container) {
    const compact = container.dataset.compact === "true";
    const note = container.parentElement.querySelector("[data-map-note]");
    const say = (text) => { if (note) { note.textContent = text; note.hidden = !text; } };
    container.hidden = false;
    const map = L.map(container, { scrollWheelZoom: !compact, zoomSnap: 0.5 });
    let tilesFailed = false;
    L.tileLayer(TILES, { maxZoom: 19, attribution: ATTRIBUTION })
      .on("tileerror", () => {
        if (tilesFailed) return;
        tilesFailed = true;
        say("Basemap tiles did not load (offline?). Barangay boundaries and incidents are still shown.");
      })
      .addTo(map);

    let boundaries;
    try {
      boundaries = L.geoJSON(await getJSON(container.dataset.boundaries), { style: BOUNDARY }).addTo(map);
    } catch (e) {
      say("The barangay boundaries could not be loaded.");
      map.setView([14.33, 121.08], 13);
      return;
    }
    // Frame Biñan itself until the first load: a pin far outside never pulls the view away.
    map.fitBounds(boundaries.getBounds(), { padding: [8, 8] });
    let framed = false;

    const markers = L.layerGroup().addTo(map);
    const spider = L.layerGroup().addTo(map);
    let features = [];

    function marker(f, latlng) {
      const p = f.properties;
      return L.marker(latlng, { icon: icon(p), title: describe(p), alt: describe(p), riseOnHover: true,
        zIndexOffset: p.status === "needs_officer" ? 500 : 0 })
        .bindPopup(() => popup(p), { maxWidth: 300 });
    }

    // Fan a group out around its place, each pin on a short leg, a full target apart.
    function fanOut(group) {
      spider.clearLayers();
      const n = group.members.length;
      const radius = Math.max(TARGET, (n * (TARGET + 6)) / (2 * Math.PI));
      group.members.forEach((f, i) => {
        const angle = (2 * Math.PI * i) / n - Math.PI / 2;
        const at = map.containerPointToLatLng(group.point.add([radius * Math.cos(angle), radius * Math.sin(angle)]));
        L.polyline([group.latlng, at], { color: "#43474e", weight: 1.5, opacity: 0.7, interactive: false }).addTo(spider);
        marker(f, at).addTo(spider);
      });
    }

    function draw() {
      markers.clearLayers();
      spider.clearLayers();
      groupByScreen(map, features).forEach((g) => {
        if (g.members.length === 1) {
          marker(g.members[0], g.latlng).addTo(markers);
          return;
        }
        const label = g.members.length + " incidents close together: tap to spread them out";
        L.marker(g.latlng, { icon: groupIcon(g.members), title: label, alt: label, riseOnHover: true, zIndexOffset: 600 })
          .on("click", () => fanOut(g))
          .addTo(markers);
      });
    }
    map.on("zoomend", draw);
    map.on("click", () => spider.clearLayers());

    async function load() {
      let data;
      try {
        data = await getJSON(container.dataset.incidents);
      } catch (e) {
        say("Could not refresh the incidents; showing the last positions loaded.");
        return;
      }
      if (!tilesFailed) say("");
      const active = new Map();
      features = data.features;
      features.forEach((f) => {
        const p = f.properties;
        if (p.kind === "incident" && p.barangay) active.set(p.barangay, (active.get(p.barangay) || 0) + 1);
      });
      // Once, on the first load: zoom to the incidents (all inside Biñan). Later updates never
      // move the map under the officer's cursor.
      const inside = data.features.filter((f) => f.properties.kind === "incident");
      if (!framed && inside.length) {
        const bounds = L.latLngBounds(inside.map((f) => [f.geometry.coordinates[1], f.geometry.coordinates[0]]));
        map.fitBounds(bounds.pad(0.25), { maxZoom: 16 });
      }
      framed = true;
      draw();
      boundaries.eachLayer((layer) => {
        const name = layer.feature.properties.name;
        const count = active.get(name) || 0;
        layer.setStyle(count ? Object.assign({}, BOUNDARY, ACTIVE) : BOUNDARY);
        layer.unbindTooltip().bindTooltip(name + (count ? " · " + plural(count, "active incident") : ""), { sticky: true });
      });
    }
    await load();
    const onChange = (event) => {
      const topics = event.detail.topics || [];
      if (topics.includes("incidents") || topics.includes("reports")) load();
    };
    document.addEventListener("ulat:change", onChange);
    mounted.push({ container, map, onChange });
  }

  // A map whose page was replaced by another one (in-app navigation) is taken down with it.
  const mounted = [];
  document.addEventListener("ulat:page", () => {
    for (let i = mounted.length - 1; i >= 0; i--) {
      const m = mounted[i];
      if (m.container.isConnected) continue;
      document.removeEventListener("ulat:change", m.onChange);
      m.map.remove();
      mounted.splice(i, 1);
    }
  });

  window.UlatMap = { mount: init };
})();
