const DECK_URL = "https://cdn.jsdelivr.net/npm/deck.gl@9.1.14/dist.min.js";
const MAPLIBRE_URL = "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.js";
const MAPLIBRE_CSS = "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.css";
const STYLE_URL = "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json";
const EARTH = 156543.03392;

function loadScript(url, globalName) {
  if (window[globalName]) return Promise.resolve(window[globalName]);
  window.__uvScripts = window.__uvScripts || {};
  if (!window.__uvScripts[url]) {
    window.__uvScripts[url] = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = url;
      script.async = true;
      script.onload = () => resolve(window[globalName]);
      script.onerror = () => {
        delete window.__uvScripts[url];
        reject(new Error(`${url} indisponible`));
      };
      document.head.appendChild(script);
    });
  }
  return window.__uvScripts[url];
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function chipColor(key) {
  return key.split("|")[1] || "#283618";
}

function spreadOffsets(stops, groups, zoom, minSep) {
  const offsets = new Array(stops.length).fill(null);
  for (const g of groups) {
    const n = g.m.length;
    if (n < 2) continue;
    const mpp = EARTH * Math.cos((g.y * Math.PI) / 180) / Math.pow(2, zoom);
    const kx = 111320 * Math.cos((g.y * Math.PI) / 180);
    const radius = Math.max(minSep / 2, (minSep * n) / (2 * Math.PI));
    g.m.forEach((idx, j) => {
      const s = stops[idx];
      const ex = ((s.x - g.x) * kx) / mpp;
      const ey = (-(s.y - g.y) * 111320) / mpp;
      let dist = Math.hypot(ex, ey);
      let ux;
      let uy;
      if (dist < 0.5) {
        const angle = Math.PI + (2 * Math.PI * j) / n;
        ux = Math.cos(angle);
        uy = Math.sin(angle);
        dist = 0;
      } else {
        ux = ex / dist;
        uy = ey / dist;
      }
      const push = Math.max(0, radius - dist);
      offsets[idx] = [ux * push, uy * push];
    });
  }
  return offsets;
}

function stopRow(s, strong) {
  const label = s.d || "sens unique";
  const weight = strong ? "700" : "500";
  return `<div class="uv-row"><span class="uv-chip" style="background:${chipColor(s.k)}"></span>`
    + `<span style="font-weight:${weight}">${esc(label)}</span>`
    + `<span class="uv-num">${Math.round(s.s)}/100 · ${Math.round(s.p)} % &gt; 5 min</span></div>`;
}

function tooltipHtml(data, object) {
  let members;
  if (object.m) {
    members = object.m.map((i) => data.stops[i]).sort((a, b) => a.s - b.s);
  } else {
    const g = data.groups[object.g];
    members = [object].concat(g.m.map((i) => data.stops[i]).filter((s) => s.i !== object.i).sort((a, b) => a.s - b.s));
  }
  const first = members[0];
  const lines = [...new Set(members.flatMap((s) => (s.l || "").split(", ").filter(Boolean)))];
  const passages = members.reduce((acc, s) => acc + s.o, 0);
  const rows = members.slice(0, 6).map((s, i) => stopRow(s, i === 0)).join("");
  const more = members.length > 6 ? `<div class="uv-more">+ ${members.length - 6} autre(s) quai(s)</div>` : "";
  let scope = "";
  if (object.m && members.length > 1) {
    scope = `<div class="uv-sub">${members.length} quais regroupés · le moins fiable en tête</div>`;
  } else if (members.length > 1) {
    scope = '<div class="uv-sub">Ce quai en premier, puis les autres quais de l\'arrêt</div>';
  }
  return `<div class="uv-title">${esc(first.n)}</div>${scope}${rows}${more}`
    + `<div class="uv-foot">Lignes ${esc(lines.join(", "))} · ${passages.toLocaleString("fr-FR")} passages</div>`
    + '<div class="uv-cta">Cliquer pour ouvrir la fiche →</div>';
}

function pathTooltip(object) {
  return `<div class="uv-title">${esc(object.g)} Ligne ${esc(object.l)}</div>`
    + `<div class="uv-row"><span class="uv-chip" style="background:rgb(${object.c.join(",")})"></span>`
    + `<span style="font-weight:700">${esc(object.e.charAt(0).toUpperCase() + object.e.slice(1))}</span>`
    + `<span class="uv-num">${object.s}/100</span></div>`
    + '<div class="uv-foot">Ligne qui dessert l\'arrêt sélectionné</div>';
}

function pathLabels(paths) {
  const seen = new Set();
  const out = [];
  for (const d of paths) {
    if (seen.has(d.r) || d.p.length < 2) continue;
    seen.add(d.r);
    out.push({ ...d, at: d.p[Math.floor(d.p.length * 0.3)] });
  }
  return out;
}

function pathLayers(deck, paths) {
  const common = {
    data: paths,
    getPath: (d) => d.p,
    widthUnits: "pixels",
    capRounded: true,
    jointRounded: true,
    parameters: { depthTest: false },
  };
  return [
    new deck.PathLayer({ id: "lignes-lisere", ...common, getColor: [255, 255, 255, 235], getWidth: 10 }),
    new deck.PathLayer({
      id: "lignes-trace", ...common, getColor: (d) => [...d.c, 240], getWidth: 5,
      pickable: true, autoHighlight: true, highlightColor: [40, 54, 24, 120],
    }),
  ];
}

function labelLayer(deck, paths) {
  return new deck.TextLayer({
      id: "lignes-noms",
      data: pathLabels(paths),
      getPosition: (d) => d.at,
      getText: (d) => d.l,
      getColor: (d) => [...d.t, 255],
      getSize: 13,
      fontFamily: "Lato, system-ui, sans-serif",
      fontWeight: 900,
      characterSet: "auto",
      background: true,
      getBackgroundColor: (d) => [...d.c, 255],
      backgroundPadding: [7, 3, 7, 3],
      getBorderColor: [255, 255, 255, 255],
      getBorderWidth: 2,
      pickable: true,
    });
}

function buildLayers(deck, state) {
  const data = state.data;
  const zoom = state.map.getZoom();
  const grouped = zoom < data.split_zoom;
  const bucket = Math.round(zoom * 4) / 4;
  if (!grouped && state.offsetBucket !== bucket) {
    state.offsets = spreadOffsets(data.stops, data.groups, zoom, data.min_sep_px);
    state.offsetBucket = bucket;
  }
  const offsets = state.offsets || [];
  const iconProps = {
    iconAtlas: data.atlas.url,
    iconMapping: data.atlas.mapping,
    getIcon: (d) => d.k,
    getSize: (d) => d.z,
    sizeUnits: "meters",
    sizeMinPixels: data.size_px[0],
    sizeMaxPixels: data.size_px[1],
    pickable: true,
    autoHighlight: true,
    highlightColor: [254, 250, 224, 170],
  };
  const layers = [];
  const paths = data.paths || [];
  if (paths.length) {
    layers.push(...pathLayers(deck, paths));
    iconProps.opacity = 0.45;
  }
  if (grouped) {
    layers.push(new deck.IconLayer({ id: "groupes", data: data.groups, getPosition: (d) => [d.x, d.y], ...iconProps }));
  } else {
    layers.push(new deck.IconLayer({
      id: "quais",
      data: data.stops,
      getPosition: (d) => [d.x, d.y],
      getPixelOffset: (d) => offsets[d.idx] || [0, 0],
      updateTriggers: { getPixelOffset: bucket },
      ...iconProps,
    }));
  }
  const sel = state.selectedStop;
  if (sel) {
    const g = data.groups[sel.g];
    const selection = [{
      ...sel,
      position: grouped ? [g.x, g.y] : [sel.x, sel.y],
      offset: grouped ? [0, 0] : (offsets[sel.idx] || [0, 0]),
    }];
    const common = {
      data: selection,
      iconAtlas: data.atlas.url,
      iconMapping: data.atlas.mapping,
      getPosition: (d) => d.position,
      getPixelOffset: (d) => d.offset,
      sizeUnits: "pixels",
      pickable: false,
      updateTriggers: { getPosition: [grouped, bucket], getPixelOffset: [grouped, bucket] },
    };
    layers.push(new deck.IconLayer({ id: "selection-halo", ...common, getIcon: () => "halo", getSize: 46 }));
    layers.push(new deck.IconLayer({ id: "selection", ...common, getIcon: (d) => d.k, getSize: 26 }));
  }
  if (paths.length) layers.push(labelLayer(deck, paths));
  return layers;
}

function renderBadge(state, flyTo) {
  let badge = state.root.querySelector(".uv-badge");
  const sel = state.selectedStop;
  if (!sel) {
    if (badge) badge.remove();
    return;
  }
  if (!badge) {
    badge = document.createElement("div");
    badge.className = "uv-badge";
    state.root.appendChild(badge);
  }
  const paths = state.data.paths || [];
  badge.innerHTML = `<span class="uv-dot"></span><span><b>Arrêt sélectionné</b> · ${esc(sel.n)}`
    + `${sel.d ? " — " + esc(sel.d) : ""}</span><button type="button" data-a="centrer">Centrer</button>`
    + (paths.length ? '<button type="button" data-a="lignes">Voir les lignes entières</button>' : "");
  badge.querySelector('[data-a="centrer"]').onclick = () => flyTo(sel, Math.max(state.map.getZoom(), state.data.focus_zoom));
  const whole = badge.querySelector('[data-a="lignes"]');
  if (whole) {
    whole.onclick = () => {
      const pts = paths.flatMap((d) => d.p);
      const lons = pts.map((q) => q[0]);
      const lats = pts.map((q) => q[1]);
      state.map.fitBounds([[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]],
        { padding: 48, duration: 900, essential: true });
    };
  }
}

export default async function (component) {
  const { data, parentElement, setTriggerValue } = component;
  let state = parentElement.__uvCarte;
  if (!state) {
    const css = document.createElement("link");
    css.rel = "stylesheet";
    css.href = MAPLIBRE_CSS;
    const root = document.createElement("div");
    root.className = "uv-map";
    const canvasBox = document.createElement("div");
    canvasBox.className = "uv-canvas";
    const tip = document.createElement("div");
    tip.className = "uv-tip";
    const hint = document.createElement("div");
    hint.className = "uv-hint";
    root.append(canvasBox, tip, hint);
    parentElement.append(css, root);
    state = parentElement.__uvCarte = { root, canvasBox, tip, hint };
  }
  state.root.style.height = `${data.height}px`;
  data.stops.forEach((s, i) => { s.idx = i; });
  const previousSelection = state.selectedId;
  state.data = data;
  state.selectedId = data.selected;
  state.selectedStop = data.stops.find((s) => s.i === data.selected) || null;
  state.offsetBucket = null;

  let deck;
  let maplibregl;
  try {
    [maplibregl, deck] = await Promise.all([loadScript(MAPLIBRE_URL, "maplibregl"), loadScript(DECK_URL, "deck")]);
  } catch (error) {
    state.root.innerHTML = '<div class="uv-error">La carte ne peut pas être chargée (bibliothèques '
      + "cartographiques inaccessibles). La liste « Chercher un arrêt » et le tableau des arrêts restent "
      + "utilisables.</div>";
    return undefined;
  }

  const flyTo = (stop, zoom) => state.map.flyTo({ center: [stop.x, stop.y], zoom, duration: 900, essential: true });
  const refresh = () => {
    if (!state.overlay) return;
    state.overlay.setProps({ layers: buildLayers(deck, state) });
    state.hint.textContent = state.map.getZoom() < state.data.split_zoom
      ? "Les quais d'un même arrêt sont regroupés : zoomez pour les voir séparément."
      : "Chaque quai est affiché séparément.";
  };

  if (!state.map) {
    let initial = data.view;
    if (state.selectedStop && data.focus_on_load) {
      initial = { longitude: state.selectedStop.x, latitude: state.selectedStop.y, zoom: data.focus_zoom };
    }
    state.viewKey = data.view_key;
    state.map = new maplibregl.Map({
      container: state.canvasBox,
      style: STYLE_URL,
      center: [initial.longitude, initial.latitude],
      zoom: initial.zoom,
      minZoom: 8,
      maxZoom: 19,
      dragRotate: false,
      pitchWithRotate: false,
      touchPitch: false,
      attributionControl: { compact: true },
    });
    state.map.touchZoomRotate.disableRotation();
    state.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    state.overlay = new deck.MapboxOverlay({
      interleaved: false,
      layers: [],
      onHover: (info) => {
        const hit = info.object && info.layer && !info.layer.id.startsWith("selection");
        const onPath = hit && info.layer.id.startsWith("lignes");
        state.map.getCanvas().style.cursor = hit && !onPath ? "pointer" : "";
        if (!hit) {
          state.tip.style.display = "none";
          return;
        }
        state.tip.innerHTML = onPath ? pathTooltip(info.object) : tooltipHtml(state.data, info.object);
        state.tip.style.display = "block";
        const width = state.root.clientWidth;
        const height = state.root.clientHeight;
        const left = Math.min(info.x + 16, width - state.tip.offsetWidth - 8);
        const top = Math.min(info.y + 16, height - state.tip.offsetHeight - 8);
        state.tip.style.left = `${Math.max(8, left)}px`;
        state.tip.style.top = `${Math.max(8, top)}px`;
      },
      onClick: (info) => {
        if (!info.object || !info.layer || info.layer.id.startsWith("selection") || info.layer.id.startsWith("lignes")) return;
        const id = info.object.m ? info.object.w : info.object.i;
        state.lastClicked = id;
        setTriggerValue("clicked", id);
      },
    });
    state.map.addControl(state.overlay);
    state.lastZoom = state.map.getZoom();
    state.map.on("zoom", () => {
      const zoom = state.map.getZoom();
      const split = state.data.split_zoom;
      const crossed = (state.lastZoom < split) !== (zoom < split);
      const bucketChanged = Math.round(state.lastZoom * 4) !== Math.round(zoom * 4);
      state.lastZoom = zoom;
      if (crossed || (bucketChanged && zoom >= split)) refresh();
    });
    state.root.addEventListener("mouseleave", () => { state.tip.style.display = "none"; });
  } else if (data.view_key !== state.viewKey) {
    state.viewKey = data.view_key;
    state.map.jumpTo({ center: [data.view.longitude, data.view.latitude], zoom: data.view.zoom });
  } else if (state.selectedStop && data.selected !== previousSelection && data.selected !== state.lastClicked) {
    flyTo(state.selectedStop, Math.max(state.map.getZoom(), data.focus_zoom));
  }
  refresh();
  renderBadge(state, flyTo);
  return () => {
    if (state.map) state.map.remove();
    delete parentElement.__uvCarte;
  };
}
