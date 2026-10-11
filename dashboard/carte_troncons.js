const DECK_URL = "https://cdn.jsdelivr.net/npm/deck.gl@9.1.14/dist.min.js";
const MAPLIBRE_URL = "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.js";
const MAPLIBRE_CSS = "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.css";
const STYLE_URL = "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json";
const ORDER = { "-": 0, d: 1, t: 1, p: 1, v: 2, o: 3, c: 4 };
const FERRY = 4;

function loadScript(url, globalName) {
  if (window[globalName]) return Promise.resolve(window[globalName]);
  window.__uvScripts = window.__uvScripts || {};
  if (!window.__uvScripts[url]) {
    window.__uvScripts[url] = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = url;
      script.async = true;
      script.onload = () => resolve(window[globalName]);
      script.onerror = () => { delete window.__uvScripts[url]; reject(new Error(url)); };
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

function rgb(hex, alpha = 255) {
  const h = hex.replace("#", "");
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16), alpha];
}

function slotLabel(slot) {
  return slot === 24 ? "sur toute la journée" : `entre ${slot} h et ${slot === 23 ? "minuit" : `${slot + 1} h`}`;
}

function seconds(s) {
  const a = Math.abs(s);
  return a < 60 ? `${a} s` : `${Math.floor(a / 60)} min ${String(a % 60).padStart(2, "0")} s`;
}

function tooltip(seg, slot) {
  const code = seg.t[slot];
  const plural = seg.l && seg.l.includes(",");
  const head = `<b>${esc(seg.n)}</b><div class="l">${seg.l ? `${plural ? "Lignes" : "Ligne"} ${esc(seg.l)}` : ""}</div>`;
  const when = slotLabel(slot);
  if ("voc".includes(code)) {
    const [s, pct, n] = seg.v[slot];
    const verdict = s <= 0
      ? `Les bus n'y perdent pas de temps ${when}.`
      : `Les bus y perdent <strong>${seconds(s)}</strong> ${when}, soit ${pct} % du temps prévu.`;
    return `${head}<div class="v">${verdict}</div><div class="m">Valeur habituelle sur ${n} passages : circulation, feux ou montée des voyageurs.</div>`;
  }
  const reasons = {
    p: "Pas assez de données à ce moment pour se prononcer.",
    t: "Début ou fin de ligne : le temps n'y est pas mesuré.",
    d: "Le temps réel de cette ligne est trop incomplet pour mesurer ce tronçon.",
    "-": "Aucun passage mesuré à ce moment.",
  };
  return `${head}<div class="v">${reasons[code] || reasons["-"]}</div>`;
}

function legendHtml(colors) {
  return `<div class="uvt-bar"><span style="background:${colors.v}">ne perdent pas de temps</span>`
    + `<span class="o" style="background:${colors.o}">perdent un peu de temps</span>`
    + `<span style="background:${colors.c}">perdent beaucoup de temps</span></div>`
    + `<span class="uvt-grey"><span class="uvt-swatch"></span>pas assez de données</span>`
    + '<span class="uvt-grey"><span class="uvt-swatch bus"></span>bus</span>'
    + '<span class="uvt-grey"><span class="uvt-swatch ferry"></span>ferry</span>'
    + '<span class="uvt-grey"><span class="uvt-swatch tram"></span>tram : mesure par tronçon indisponible</span>';
}

function buildLayers(deck, state) {
  const { data } = state;
  const slot = data.slot;
  const colors = data.colors;
  const sorted = data.segments.slice().sort((a, b) => ORDER[a.t[slot]] - ORDER[b.t[slot]]);
  const dashed = new deck.PathStyleExtension({ dash: true });
  const colorOf = (seg) => {
    const code = seg.t[slot];
    const alpha = seg.c ? 255 : 90;
    if (code === "v") return rgb(colors.v, seg.c ? 170 : 60);
    if ("oc".includes(code)) return rgb(colors[code], alpha);
    return rgb(colors.grey, seg.c ? 230 : 80);
  };
  const common = { getPath: (d) => d.p, widthUnits: "pixels", capRounded: true, jointRounded: true, parameters: { depthTest: false } };
  return [
    new deck.PathLayer({
      id: "tram", data: data.tram, getPath: (d) => d, widthUnits: "pixels", getWidth: 6, getColor: [169, 169, 160, 200],
      getDashArray: [1, 1.4], dashJustified: true, extensions: [dashed], parameters: { depthTest: false },
      pickable: true,
    }),
    new deck.PathLayer({
      id: "liseré", ...common, data: sorted, getWidth: (d) => ({ c: 8, o: 7 }[d.t[slot]] || 4.5),
      getColor: [255, 255, 255, 230], updateTriggers: { getWidth: slot },
    }),
    new deck.PathLayer({
      id: "troncons", ...common, data: sorted, pickable: true, autoHighlight: true, highlightColor: [40, 54, 24, 120],
      getWidth: (d) => ({ c: 5.5, o: 4.5, v: 2.5 }[d.t[slot]] || 2.5), getColor: colorOf,
      getDashArray: (d) => (d.m === FERRY ? [3, 2] : [0, 0]), extensions: [dashed],
      updateTriggers: { getColor: slot, getWidth: slot },
    }),
  ];
}

export default async function (component) {
  const { data, parentElement } = component;
  let state = parentElement.__uvTroncons;
  if (!state) {
    const css = document.createElement("link");
    css.rel = "stylesheet";
    css.href = MAPLIBRE_CSS;
    const root = document.createElement("div");
    root.className = "uvt";
    const canvasBox = document.createElement("div");
    canvasBox.className = "uvt-canvas";
    const tip = document.createElement("div");
    tip.className = "uvt-tip";
    const legend = document.createElement("div");
    legend.className = "uvt-legend";
    root.append(canvasBox, tip, legend);
    parentElement.append(css, root);
    state = parentElement.__uvTroncons = { root, canvasBox, tip, legend };
  }
  state.root.style.height = `${data.height}px`;
  state.data = data;
  state.legend.innerHTML = legendHtml(data.colors);

  let deck;
  let maplibregl;
  try {
    [maplibregl, deck] = await Promise.all([loadScript(MAPLIBRE_URL, "maplibregl"), loadScript(DECK_URL, "deck")]);
  } catch (error) {
    state.root.innerHTML = '<div class="uvt-error">La carte ne peut pas être chargée (bibliothèques cartographiques inaccessibles).</div>';
    return undefined;
  }
  if (!state.map) {
    state.viewKey = data.view_key;
    state.map = new maplibregl.Map({
      container: state.canvasBox, style: STYLE_URL, center: [data.view.longitude, data.view.latitude],
      zoom: data.view.zoom, minZoom: 9, maxZoom: 18, dragRotate: false, pitchWithRotate: false, touchPitch: false,
      attributionControl: { compact: true },
    });
    state.map.touchZoomRotate.disableRotation();
    if (data.bounds) state.map.fitBounds(data.bounds, { padding: { top: 24, left: 24, right: 24, bottom: 80 }, duration: 0 });
    state.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    state.overlay = new deck.MapboxOverlay({
      interleaved: false,
      layers: [],
      onHover: (info) => {
        if (!info.object) { state.tip.style.display = "none"; return; }
        state.tip.innerHTML = info.layer.id === "tram"
          ? "<b>Tram</b><div class=\"v\">Mesure par tronçon indisponible pour le tram : son temps réel est publié par paliers.</div>"
          : tooltip(info.object, state.data.slot);
        state.tip.style.display = "block";
        const left = Math.min(info.x + 16, state.root.clientWidth - state.tip.offsetWidth - 8);
        const top = Math.min(info.y + 16, state.root.clientHeight - state.tip.offsetHeight - 70);
        state.tip.style.left = `${Math.max(8, left)}px`;
        state.tip.style.top = `${Math.max(8, top)}px`;
      },
    });
    state.map.addControl(state.overlay);
    state.root.addEventListener("mouseleave", () => { state.tip.style.display = "none"; });
  } else if (data.view_key !== state.viewKey) {
    state.viewKey = data.view_key;
    if (data.bounds) state.map.fitBounds(data.bounds, { padding: { top: 24, left: 24, right: 24, bottom: 80 }, duration: 600 });
    else state.map.jumpTo({ center: [data.view.longitude, data.view.latitude], zoom: data.view.zoom });
  }
  state.overlay.setProps({ layers: buildLayers(deck, state) });
  return () => {
    if (state.map) state.map.remove();
    delete parentElement.__uvTroncons;
  };
}
