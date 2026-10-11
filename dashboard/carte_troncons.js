const DECK_URL = "https://cdn.jsdelivr.net/npm/deck.gl@9.1.14/dist.min.js";
const MAPLIBRE_URL = "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.js";
const MAPLIBRE_CSS = "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.css";
const STYLE_URL = "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json";
const ORDER = { "-": 0, d: 1, t: 1, p: 1, v: 2, o: 3, c: 4 };
const FERRY = 4;
const WHOLE_DAY = 24;
const FIT_PADDING = { top: 120, left: 40, right: 40, bottom: 150 };

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

function seconds(s) {
  const a = Math.abs(s);
  return a < 60 ? `${a} s` : `${Math.floor(a / 60)} min ${String(a % 60).padStart(2, "0")} s`;
}

function when(slot) {
  return slot === WHOLE_DAY ? "sur la journée" : `entre ${slot} h et ${slot === 23 ? "minuit" : `${slot + 1} h`}`;
}

function plural(n, one, many) {
  return n > 1 ? many : one;
}

function verdict(data, slot) {
  const scope = data.segments.filter((s) => s.c);
  const count = (code) => scope.filter((s) => s.t[slot] === code).length;
  const red = count("c");
  const ochre = count("o");
  const measured = red + ochre + count("v");
  const time = slot === WHOLE_DAY ? "Sur toute la journée" : `À ${slot} h`;
  const place = data.place;
  let text;
  if (measured === 0) {
    text = `${time}, pas assez de données pour se prononcer ${place}.`;
  } else if (red > 0) {
    text = `${time}, ${red} ${plural(red, "tronçon ralentit", "tronçons ralentissent")} nettement les bus ${place}`
      + (ochre > 0 ? `, et ${ochre} un peu.` : ".");
  } else if (ochre > 0) {
    text = `${time}, aucun tronçon ne ralentit nettement les bus ${place} ; ${ochre} les ${plural(ochre, "ralentit", "ralentissent")} un peu.`;
  } else {
    text = `${time}, les bus ne perdent pas de temps ${place}.`;
  }
  const sub = `${measured} ${plural(measured, "tronçon mesuré", "tronçons mesurés")} sur ${scope.length}, les autres en gris.`;
  return { text, sub };
}

const GREY_REASONS = {
  p: "Service très peu fréquent à ce moment : trop peu de passages pour se prononcer.",
  d: "Temps réel non fiable sur la ligne qui passe ici : ce tronçon n'est pas mesuré.",
  t: "Début ou fin de ligne : le temps n'y est pas mesuré.",
  "-": "Aucun passage à ce moment.",
};

function lineText(seg) {
  if (!seg.l) return "";
  return `${seg.l.includes(",") ? "Lignes" : "Ligne"} ${esc(seg.l)}`;
}

function measuredAt(seg, slot) {
  return "voc".includes(seg.t[slot]);
}

function shortStatus(seg, slot) {
  if (measuredAt(seg, slot)) {
    const s = seg.v[slot][0];
    return s > 0 ? `Les bus y perdent ${seconds(s)} ${when(slot)}.` : `Les bus n'y perdent pas de temps ${when(slot)}.`;
  }
  return GREY_REASONS[seg.t[slot]] || GREY_REASONS["-"];
}

function worstHour(seg) {
  let best = null;
  for (let h = 0; h < WHOLE_DAY; h += 1) {
    if (!measuredAt(seg, h)) continue;
    const s = seg.v[h][0];
    if (s > 0 && (best === null || s > best.s)) best = { h, s };
  }
  return best;
}

function hourStrip(seg, slot, colors) {
  const cells = [];
  for (let h = 0; h < WHOLE_DAY; h += 1) {
    const code = seg.t[h];
    const ok = measuredAt(seg, h);
    const color = ok ? colors[code] : "#E4E4DE";
    let label = code === "-" ? "aucun passage" : "non mesuré";
    if (ok) label = seg.v[h][0] > 0 ? `${seconds(seg.v[h][0])} perdues` : "pas de temps perdu";
    cells.push(`<span class="${h === slot ? "now" : ""}" style="background:${color}" title="${h} h : ${label}"></span>`);
  }
  return `<div class="uvt-strip" role="img" aria-label="Temps perdu heure par heure">${cells.join("")}</div>`
    + '<div class="uvt-strip-axis"><span>0 h</span><span>6 h</span><span>12 h</span><span>18 h</span><span>24 h</span></div>';
}

function panelHtml(seg, slot, colors) {
  const code = seg.t[slot];
  let body;
  if (measuredAt(seg, slot)) {
    const [s, pct, n, part] = seg.v[slot];
    body = s > 0
      ? `<div class="uvt-figure">${seconds(s)}</div><p>C'est le temps que les bus perdent habituellement ici ${when(slot)}, soit ${pct} % du temps prévu pour ce trajet.</p>`
      : `<div class="uvt-figure ok">Pas de temps perdu</div><p>Les bus n'y perdent pas de temps ${when(slot)}.</p>`;
    if (part > 0) body += `<p>${part} % des passages y perdent plus de 2 minutes.</p>`;
    if ("oc".includes(code)) body += "<p>C'est l'un des endroits où le retard des bus se crée : circulation, feux ou montée des voyageurs.</p>";
    body += `<p class="uvt-foot">Valeur habituelle sur ${n} passages de la période scolaire.</p>`;
  } else {
    body = `<p class="uvt-grey-reason">${GREY_REASONS[code] || GREY_REASONS["-"]}</p>`;
    if (code === "p" && seg.v[slot]) body += `<p class="uvt-foot">${seg.v[slot][2]} passages mesurés à ce moment.</p>`;
  }
  const worst = worstHour(seg);
  const worstText = worst ? `<p>C'est vers <b>${worst.h} h</b> que les bus y perdent le plus de temps (${seconds(worst.s)}).</p>` : "";
  return `<button class="uvt-close" type="button" aria-label="Fermer">×</button><h3>${esc(seg.n)}</h3>`
    + `<div class="uvt-lines">${lineText(seg)}</div>${body}${worstText}${hourStrip(seg, slot, colors)}`;
}

function legendHtml(colors) {
  return `<div class="uvt-bar"><span style="background:${colors.v}">ne perdent pas de temps</span>`
    + `<span class="o" style="background:${colors.o}">perdent un peu de temps</span>`
    + `<span style="background:${colors.c}">perdent beaucoup de temps</span></div>`
    + '<span class="uvt-key"><span class="uvt-swatch"></span>gris : non mesuré</span>'
    + '<span class="uvt-key"><span class="uvt-swatch bus"></span>bus</span>'
    + '<span class="uvt-key"><span class="uvt-swatch ferry"></span>ferry</span>'
    + '<span class="uvt-key"><span class="uvt-swatch tram"></span>tram : mesure par tronçon indisponible</span>';
}

function buildLayers(deck, state) {
  const { data, slot } = state;
  const colors = data.colors;
  const rank = (s) => s.c * 10 + ORDER[s.t[slot]];
  const sorted = data.segments.slice().sort((a, b) => rank(a) - rank(b));
  const dashed = new deck.PathStyleExtension({ dash: true });
  const colorOf = (seg) => {
    const code = seg.t[slot];
    if (!seg.c) return measuredAt(seg, slot) ? rgb(colors[code], 55) : rgb(colors.grey, 50);
    if (code === "v") return rgb(colors.v, 120);
    if ("oc".includes(code)) return rgb(colors[code], 255);
    return rgb(colors.grey, 210);
  };
  const widthOf = (seg) => ({ c: 5.5, o: 4.5, v: 1.6 }[seg.t[slot]] || 2.2);
  const common = { getPath: (d) => d.p, widthUnits: "pixels", capRounded: true, jointRounded: true, parameters: { depthTest: false } };
  const layers = [
    new deck.PathLayer({
      id: "tram", data: data.tram, getPath: (d) => d, widthUnits: "pixels", getWidth: 6, getColor: [169, 169, 160, 190],
      getDashArray: [1, 1.4], dashJustified: true, extensions: [dashed], parameters: { depthTest: false }, pickable: true,
    }),
    new deck.PathLayer({
      id: "lisere", ...common, data: sorted.filter((d) => d.c && "oc".includes(d.t[slot])),
      getWidth: (d) => widthOf(d) + 3, getColor: [255, 255, 255, 235],
    }),
    new deck.PathLayer({
      id: "troncons", ...common, data: sorted, pickable: true, autoHighlight: true, highlightColor: [40, 54, 24, 140],
      getWidth: widthOf, getColor: colorOf,
      getDashArray: (d) => (d.m === FERRY ? [3, 2] : [0, 0]), extensions: [dashed],
      updateTriggers: { getColor: slot, getWidth: slot },
    }),
  ];
  if (state.selected) {
    const sel = state.selected;
    layers.push(new deck.PathLayer({ id: "selection", ...common, data: [sel], getWidth: widthOf(sel) + 6, getColor: [40, 54, 24, 230] }));
    layers.push(new deck.PathLayer({ id: "selection-coeur", ...common, data: [sel], getWidth: widthOf(sel) + 1, getColor: colorOf(sel) }));
  }
  return layers;
}

function render(state) {
  const { data, slot } = state;
  const v = verdict(data, slot);
  state.head.innerHTML = `${data.label ? `<div class="uvt-label">${esc(data.label)}</div>` : ""}`
    + `<div class="uvt-verdict">${esc(v.text)}</div><div class="uvt-sub">${esc(v.sub)}</div>`;
  state.clock.textContent = slot === WHOLE_DAY ? "Toute la journée" : `${slot} h`;
  state.range.value = String(slot === WHOLE_DAY ? state.lastHour : slot);
  state.range.classList.toggle("off", slot === WHOLE_DAY);
  state.allDay.setAttribute("aria-pressed", slot === WHOLE_DAY ? "true" : "false");
  if (state.selected) {
    state.panel.innerHTML = panelHtml(state.selected, slot, data.colors);
    state.panel.style.display = "block";
    state.panel.querySelector(".uvt-close").onclick = () => { state.selected = null; render(state); };
  } else {
    state.panel.style.display = "none";
  }
  if (state.overlay && state.deck) state.overlay.setProps({ layers: buildLayers(state.deck, state) });
}

function buildControls(state) {
  const bottom = document.createElement("div");
  bottom.className = "uvt-bottom";
  bottom.innerHTML = '<div class="uvt-controls"><div class="uvt-clock" aria-live="polite"></div>'
    + '<div class="uvt-range-box"><input class="uvt-range" type="range" min="0" max="23" step="1" aria-label="Heure de la journée">'
    + '<div class="uvt-ticks"><span>0 h</span><span>6 h</span><span>12 h</span><span>18 h</span><span>23 h</span></div></div>'
    + '<button class="uvt-allday" type="button" aria-pressed="false">Toute la journée</button></div>'
    + '<div class="uvt-legend"></div>';
  state.root.append(bottom);
  state.clock = bottom.querySelector(".uvt-clock");
  state.range = bottom.querySelector(".uvt-range");
  state.allDay = bottom.querySelector(".uvt-allday");
  state.legend = bottom.querySelector(".uvt-legend");
  state.range.addEventListener("input", () => {
    state.slot = Number(state.range.value);
    state.lastHour = state.slot;
    render(state);
  });
  state.allDay.addEventListener("click", () => {
    state.slot = state.slot === WHOLE_DAY ? state.lastHour : WHOLE_DAY;
    render(state);
  });
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
    const head = document.createElement("div");
    head.className = "uvt-head";
    const tip = document.createElement("div");
    tip.className = "uvt-tip";
    const panel = document.createElement("div");
    panel.className = "uvt-panel";
    panel.setAttribute("role", "dialog");
    root.append(canvasBox, head, tip, panel);
    parentElement.append(css, root);
    state = parentElement.__uvTroncons = { root, canvasBox, head, tip, panel };
    buildControls(state);
  }
  state.root.style.height = `${data.height}px`;
  const key = `${data.view_key}|${data.segments.length}|${data.slot}`;
  state.data = data;
  if (state.dataKey !== key) {
    state.dataKey = key;
    state.slot = data.slot;
    state.lastHour = data.slot === WHOLE_DAY ? 8 : data.slot;
    state.selected = null;
  }
  state.legend.innerHTML = legendHtml(data.colors);

  let deck;
  let maplibregl;
  try {
    [maplibregl, deck] = await Promise.all([loadScript(MAPLIBRE_URL, "maplibregl"), loadScript(DECK_URL, "deck")]);
  } catch (error) {
    state.root.innerHTML = '<div class="uvt-error">La carte ne peut pas être chargée (bibliothèques cartographiques inaccessibles).</div>';
    return undefined;
  }
  state.deck = deck;
  const fit = (duration) => {
    if (data.bounds) state.map.fitBounds(data.bounds, { padding: FIT_PADDING, duration, maxZoom: 15 });
  };
  if (!state.map) {
    state.viewKey = data.view_key;
    state.map = new maplibregl.Map({
      container: state.canvasBox, style: STYLE_URL, center: [data.view.longitude, data.view.latitude],
      zoom: data.view.zoom, minZoom: 9, maxZoom: 18, dragRotate: false, pitchWithRotate: false, touchPitch: false,
      attributionControl: { compact: true },
    });
    state.map.touchZoomRotate.disableRotation();
    state.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    fit(0);
    state.overlay = new deck.MapboxOverlay({
      interleaved: false,
      layers: [],
      onHover: (info) => {
        const hit = info.object && info.layer && !info.layer.id.startsWith("selection");
        state.map.getCanvas().style.cursor = hit ? "pointer" : "";
        if (!hit) { state.tip.style.display = "none"; return; }
        state.tip.innerHTML = info.layer.id === "tram"
          ? "<b>Tram</b><div>Mesure par tronçon indisponible pour le tram : son temps réel est publié par paliers.</div>"
          : `<b>${esc(info.object.n)}</b><div class="l">${lineText(info.object)}</div><div>${shortStatus(info.object, state.slot)}</div><div class="m">Cliquer pour le détail</div>`;
        state.tip.style.display = "block";
        const left = Math.min(info.x + 16, state.root.clientWidth - state.tip.offsetWidth - 8);
        const top = Math.min(info.y + 16, state.root.clientHeight - state.tip.offsetHeight - 150);
        state.tip.style.left = `${Math.max(8, left)}px`;
        state.tip.style.top = `${Math.max(8, top)}px`;
      },
      onClick: (info) => {
        state.selected = info.object && info.layer && info.layer.id === "troncons" ? info.object : null;
        state.tip.style.display = "none";
        render(state);
      },
    });
    state.map.addControl(state.overlay);
    state.root.addEventListener("mouseleave", () => { state.tip.style.display = "none"; });
  } else if (data.view_key !== state.viewKey) {
    state.viewKey = data.view_key;
    fit(600);
  }
  render(state);
  return () => {
    if (state.map) state.map.remove();
    delete parentElement.__uvTroncons;
  };
}
