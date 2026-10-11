const DECK_URL = "https://cdn.jsdelivr.net/npm/deck.gl@9.1.14/dist.min.js";
const MAPLIBRE_URL = "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.js";
const MAPLIBRE_CSS = "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.css";
const STYLE_URL = "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json";
const ORDER = { "-": 0, d: 1, t: 1, p: 1, v: 2, o: 3, c: 4 };
const FERRY = 4;
const WHOLE_DAY = 24;
const PLAY_MS = 1000;
let STEP = 1;
let DATE = null;

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

function slotEnd(slot) {
  return slot + STEP >= 24 ? "minuit" : `${slot + STEP} h`;
}

function when(slot) {
  return slot === WHOLE_DAY ? "sur la journée" : `entre ${slot} h et ${slotEnd(slot)}`;
}

function clockText(slot) {
  if (slot === WHOLE_DAY) return "Toute la journée";
  return STEP === 1 ? `${slot} h` : `${slot}-${slot + STEP} h`;
}

function plural(n, one, many) {
  return n > 1 ? many : one;
}

const TRAM_NOTE = "Le détail par portion entre deux arrêts n'est pas mesurable pour le tram : son temps réel est publié par paliers.";

function listText(items) {
  return items.length > 1 ? `${items.slice(0, -1).join(", ")} et ${items[items.length - 1]}` : items[0];
}

function tramLost(s) {
  return s > 0 ? seconds(s) : "rien";
}

function tramVerdict(line, slot) {
  const time = slot === WHOLE_DAY ? "Sur toute la journée" : `À ${slot} h`;
  const rows = line.s.filter((d) => d.v[slot]);
  const text = rows.length
    ? `${time}, le tram ${line.n} perd habituellement ${listText(rows.map((d) => `${tramLost(d.v[slot][0])} vers ${d.vers}`))} sur l'ensemble du trajet.`
    : `${time}, pas assez de trajets de tram mesurés pour se prononcer.`;
  return { text, sub: TRAM_NOTE };
}

function coverageText(measured, total) {
  const reason = DATE && DATE.reason ? ` ${DATE.reason}` : "";
  return `Ce jour-là, ${measured} ${plural(measured, "portion", "portions")} sur ${total} `
    + `${plural(measured, "a", "ont")} assez de passages pour être mesurées. Les autres sont grises.${reason}`;
}

function verdict(data, slot) {
  if (data.tram_line && !data.tram_lines[data.tram_line]) {
    return {
      text: "Le temps perdu de bout en bout du tram n'est calculé que pour les jours types (semaine, samedi, dimanche).",
      sub: TRAM_NOTE,
    };
  }
  if (data.unmeasured) {
    return {
      text: `La ${data.label.charAt(0).toLowerCase()}${data.label.slice(1)} n'est pas mesurable : son temps réel est absent ou non fiable.`,
      sub: "Son tracé est en gris.",
    };
  }
  if (data.tram_line && data.tram_lines[data.tram_line]) return tramVerdict(data.tram_lines[data.tram_line], slot);
  const scope = data.segments.filter((s) => s.c);
  const count = (code) => scope.filter((s) => s.t[slot] === code).length;
  const red = count("c");
  const ochre = count("o");
  const measured = red + ochre + count("v");
  const time = slot === WHOLE_DAY ? "Sur toute la journée" : (STEP === 1 ? `À ${slot} h` : `Entre ${slot} h et ${slotEnd(slot)}`);
  const place = data.place;
  let text;
  if (measured === 0) {
    text = `${time}, pas assez de données pour se prononcer ${place}.`;
  } else if (red > 0) {
    text = `${time}, ${red} ${plural(red, "portion entre deux arrêts ralentit", "portions entre deux arrêts ralentissent")} nettement les bus ${place}`
      + (ochre > 0 ? `, et ${ochre} un peu.` : ".");
  } else if (ochre > 0) {
    text = `${time}, aucune portion entre deux arrêts ne ralentit nettement les bus ${place} ; ${ochre} les ${plural(ochre, "ralentit", "ralentissent")} un peu.`;
  } else {
    text = `${time}, les bus ne perdent pas de temps ${place}.`;
  }
  const sub = DATE
    ? coverageText(measured, scope.length)
    : `${measured} ${plural(measured, "portion mesurée", "portions mesurées")} sur ${scope.length}, les autres en gris.`;
  return { text, sub };
}

const GREY_REASONS = {
  p: "Service très peu fréquent à ce moment : trop peu de passages pour se prononcer.",
  d: "Temps réel non fiable sur la ligne qui passe ici : cette portion n'est pas mesurée.",
  t: "Début ou fin de ligne : le temps n'y est pas mesuré.",
  "-": "Aucun passage à ce moment.",
};

function greyReason(code) {
  if (DATE && code === "p") return "Moins de 8 passages sur ce créneau ce jour-là : pas assez pour se prononcer.";
  return GREY_REASONS[code] || GREY_REASONS["-"];
}

function lineText(seg, long = false) {
  const others = long ? "d'autres lignes y passent mais ne sont pas mesurables" : "et des lignes non mesurables";
  if (!seg.l) return seg.u ? "Lignes non mesurables ici" : "";
  return `${seg.l.includes(",") ? "Lignes" : "Ligne"} ${esc(seg.l)}${seg.u ? ` ; ${others}` : ""}`;
}

function extraTime(pct) {
  if (pct >= 200) return "Le trajet prend alors plus du triple du temps prévu.";
  if (pct >= 100) return "Le trajet prend alors plus du double du temps prévu.";
  return `Soit ${pct} % de temps en plus que prévu.`;
}

function measuredAt(seg, slot) {
  return "voc".includes(seg.t[slot]);
}

function shortStatus(seg, slot) {
  if (measuredAt(seg, slot)) {
    const s = seg.v[slot][0];
    if (DATE) return s > 0 ? `Les bus y ont perdu ${seconds(s)} ${when(slot)}.` : `Les bus n'y ont pas perdu de temps ${when(slot)}.`;
    return s > 0 ? `Les bus y perdent ${seconds(s)} ${when(slot)}.` : `Les bus n'y perdent pas de temps ${when(slot)}.`;
  }
  return greyReason(seg.t[slot]);
}

function worstHour(seg) {
  let best = null;
  for (let h = 0; h < WHOLE_DAY; h += STEP) {
    if (!measuredAt(seg, h)) continue;
    const s = seg.v[h][0];
    if (s > 0 && (best === null || s > best.s)) best = { h, s };
  }
  return best;
}

function hourStrip(seg, slot, colors) {
  const cells = [];
  for (let h = 0; h < WHOLE_DAY; h += STEP) {
    const code = seg.t[h];
    const ok = measuredAt(seg, h);
    const color = ok ? colors[code] : "#E4E4DE";
    let label = code === "-" ? "aucun passage" : "non mesuré";
    if (ok) label = seg.v[h][0] > 0 ? `${seconds(seg.v[h][0])} perdues` : "pas de temps perdu";
    cells.push(`<span class="${h === slot ? "now" : ""}" style="background:${color}" title="${clockText(h)} : ${label}"></span>`);
  }
  return `<div class="uvt-strip" style="grid-template-columns:repeat(${24 / STEP},1fr)" role="img" aria-label="Temps perdu heure par heure">${cells.join("")}</div>`
    + '<div class="uvt-strip-axis"><span>0 h</span><span>6 h</span><span>12 h</span><span>18 h</span><span>24 h</span></div>';
}

function panelHtml(seg, slot, colors) {
  const code = seg.t[slot];
  let body;
  if (measuredAt(seg, slot)) {
    const [s, pct, n, part] = seg.v[slot];
    const lost = DATE ? `C'est le temps médian que les bus ont perdu ici ce jour-là, ${when(slot)}.`
      : `C'est le temps que les bus perdent habituellement ici ${when(slot)}.`;
    body = s > 0
      ? `<div class="uvt-figure t-${code}">${seconds(s)}</div><p>${lost} ${extraTime(pct)}</p>`
      : `<div class="uvt-figure ok">Pas de temps perdu</div><p>${shortStatus(seg, slot)}</p>`;
    if (part > 0) body += `<p>${part} % des passages y perdent plus de 2 minutes.</p>`;
    if ("oc".includes(code)) body += "<p>C'est l'un des endroits où le retard des bus se crée : circulation, feux ou montée des voyageurs.</p>";
    body += DATE ? `<p class="uvt-foot">Médiane de ${n} passages ce jour-là.</p>`
      : `<p class="uvt-foot">Valeur habituelle sur ${n} passages de la période scolaire.</p>`;
  } else {
    body = `<p class="uvt-grey-reason">${greyReason(code)}</p>`;
    if (code === "p" && seg.v[slot]) body += `<p class="uvt-foot">${seg.v[slot][2]} passages mesurés à ce moment.</p>`;
  }
  const worst = worstHour(seg);
  let worstText = "";
  if (worst && DATE) worstText = `<p>C'est entre <b>${worst.h} h et ${slotEnd(worst.h)}</b> que les bus y ont perdu le plus de temps ce jour-là (${seconds(worst.s)}).</p>`;
  else if (worst) worstText = `<p>C'est vers <b>${worst.h} h</b> que les bus y perdent le plus de temps (${seconds(worst.s)}).</p>`;
  return `<button class="uvt-close" type="button" aria-label="Fermer">×</button><h3>${esc(seg.n)}</h3>`
    + `<div class="uvt-lines">${lineText(seg, true)}</div>${body}${worstText}${hourStrip(seg, slot, colors)}`;
}

function minutes(s) {
  const m = Math.round(s / 60);
  return m >= 60 ? `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")}` : `${m} min`;
}

function tramPanelHtml(line, slot) {
  const rows = line.s.map((d) => {
    const v = d.v[slot];
    if (!v) return `<div class="uvt-tram-row"><span>Vers ${esc(d.vers)}</span><b class="na">pas assez de trajets</b></div>`;
    const [s, planned] = v;
    const pct = s > 0 ? Math.round((100 * s) / planned) : 0;
    return `<div class="uvt-tram-row"><span>Vers ${esc(d.vers)}</span><b>${s > 0 ? seconds(s) : "pas de temps perdu"}</b></div>`
      + `<p class="uvt-foot">Sur ${minutes(planned)} de trajet prévu${pct > 0 ? `, soit ${pct} % de temps en plus` : ""}.</p>`;
  }).join("");
  let worst = null;
  for (const d of line.s) {
    for (let h = 0; h < WHOLE_DAY; h += 1) {
      const v = d.v[h];
      if (v && v[0] > 0 && (worst === null || v[0] > worst.s)) worst = { h, s: v[0], vers: d.vers };
    }
  }
  const trips = line.s.reduce((n, d) => n + (d.v[slot] ? d.v[slot][2] : 0), 0);
  return `<button class="uvt-close" type="button" aria-label="Fermer">×</button><h3>Tram ${esc(line.n)}</h3>`
    + `<div class="uvt-lines">Temps perdu de bout en bout ${when(slot)}</div>${rows}`
    + (worst ? `<p>C'est vers <b>${worst.h} h</b> que le tram perd le plus de temps (${seconds(worst.s)} vers ${esc(worst.vers)}).</p>` : "")
    + `<p class="uvt-grey-reason">${TRAM_NOTE}</p>`
    + (trips ? `<p class="uvt-foot">Valeur habituelle sur ${trips} trajets complets de la période scolaire.</p>` : "");
}

function legendHtml(colors) {
  const item = (cls, color, long, short) => `<span class="${cls}" style="background:${color}"><b class="lg">${long}</b><b class="sh">${short}</b></span>`;
  return `<div class="uvt-bar">${item("", colors.v, "ne perdent pas de temps", "pas de perte")}`
    + `${item("o", colors.o, "perdent un peu de temps", "un peu")}${item("", colors.c, "perdent beaucoup de temps", "beaucoup")}</div>`
    + '<span class="uvt-key keep"><span class="uvt-swatch"></span>gris : non mesuré</span>'
    + '<span class="uvt-key"><span class="uvt-swatch bus"></span>bus</span>'
    + '<span class="uvt-key"><span class="uvt-swatch ferry"></span>ferry</span>'
    + '<span class="uvt-key keep"><span class="uvt-swatch tram"></span>tram : mesure par tronçon indisponible</span>';
}

function buildLayers(deck, state) {
  const { data, slot } = state;
  const colors = data.colors;
  const rank = (s) => s.c * 10 + ORDER[s.t[slot]];
  const sorted = data.segments.slice().sort((a, b) => rank(a) - rank(b));
  const dashed = new deck.PathStyleExtension({ dash: true });
  const focused = data.focused;
  const colorOf = (seg) => {
    const code = seg.t[slot];
    if (!seg.c) return measuredAt(seg, slot) ? rgb(colors[code], focused ? 40 : 55) : rgb(colors.grey, focused ? 35 : 50);
    if (data.unmeasured) return rgb(colors.grey, 245);
    if (code === "v") return rgb(colors.v, focused ? 235 : 120);
    if ("oc".includes(code)) return rgb(colors[code], 255);
    return rgb(colors.grey, focused ? 240 : 210);
  };
  const widthOf = (seg) => {
    if (focused && !seg.c) return 1.2;
    if (data.unmeasured) return 5;
    if (focused) return { c: 6, o: 5.5, v: 4 }[seg.t[slot]] || 3.5;
    return { c: 5.5, o: 4.5, v: 1.6 }[seg.t[slot]] || 2.2;
  };
  const common = { getPath: (d) => d.p, widthUnits: "pixels", capRounded: true, jointRounded: true, parameters: { depthTest: false } };
  const layers = [
    new deck.PathLayer({
      id: "tram", data: data.tram, getPath: (d) => d.p, widthUnits: "pixels",
      getWidth: (d) => (focused ? (d.c ? 8 : 4) : 6),
      getColor: (d) => (focused ? (d.c ? [120, 120, 112, 240] : [169, 169, 160, 90]) : [169, 169, 160, 190]),
      getDashArray: [1, 1.4], dashJustified: true, extensions: [dashed], parameters: { depthTest: false }, pickable: true,
    }),
    new deck.PathLayer({
      id: "lisere", ...common, data: sorted.filter((d) => d.c && (focused || "oc".includes(d.t[slot]))),
      getWidth: (d) => widthOf(d) + 3, getColor: [255, 255, 255, 235],
    }),
    new deck.PathLayer({
      id: "troncons", ...common, data: sorted, pickable: true, autoHighlight: true, highlightColor: [40, 54, 24, 140],
      getWidth: widthOf, getColor: colorOf,
      getDashArray: (d) => (d.m === FERRY ? [3, 2] : [0, 0]), extensions: [dashed],
      updateTriggers: { getColor: slot, getWidth: slot },
    }),
  ];
  if (state.selected && state.selected.tram) {
    const paths = data.tram.filter((d) => d.r === state.selected.tram);
    layers.splice(1, 0, new deck.PathLayer({
      id: "selection-tram", data: paths, getPath: (d) => d.p, widthUnits: "pixels", getWidth: 11,
      getColor: [40, 54, 24, 200], capRounded: true, jointRounded: true, parameters: { depthTest: false },
    }));
  } else if (state.selected) {
    const sel = state.selected;
    layers.push(new deck.PathLayer({ id: "selection", ...common, data: [sel], getWidth: widthOf(sel) + 6, getColor: [40, 54, 24, 230] }));
    layers.push(new deck.PathLayer({ id: "selection-coeur", ...common, data: [sel], getWidth: widthOf(sel) + 1, getColor: colorOf(sel) }));
  }
  return layers;
}

function fitPadding(state) {
  const box = state.root.getBoundingClientRect();
  const head = state.head.getBoundingClientRect();
  const bottom = state.root.querySelector(".uvt-bottom").getBoundingClientRect();
  const top = state.data && state.data.focused ? Math.max(head.bottom - box.top + 16, 40) : 24;
  const below = Math.max(box.bottom - bottom.top + 16, 40);
  const room = box.height - top - below;
  return room < 120
    ? { top: 40, bottom: 40, left: 24, right: 24 }
    : { top, bottom: below, left: 32, right: 56 };
}

function fitHeight(state) {
  const mobile = window.innerWidth < 640;
  const top = state.root.getBoundingClientRect().top;
  const h = mobile
    ? Math.min(Math.max(window.innerHeight - 24, 460), 720)
    : Math.min(Math.max(window.innerHeight - Math.max(top, 0) - 14, 400), 760);
  state.root.style.height = `${Math.round(h)}px`;
  if (state.map) state.map.resize();
}

function render(state) {
  const { data, slot } = state;
  STEP = data.step || 1;
  DATE = data.date || null;
  const v = verdict(data, slot);
  state.head.innerHTML = `${data.label ? `<div class="uvt-label">${esc(data.label)}</div>` : ""}`
    + `<div class="uvt-verdict">${esc(v.text)}</div><div class="uvt-sub">${esc(v.sub)}</div>`;
  state.clock.textContent = clockText(slot);
  state.range.max = String(24 - STEP);
  state.range.step = String(STEP);
  state.range.value = String(slot === WHOLE_DAY ? state.lastHour : slot);
  state.play.setAttribute("aria-pressed", state.timer ? "true" : "false");
  state.play.textContent = state.timer ? "Pause" : "▶ Lecture";
  state.range.classList.toggle("off", slot === WHOLE_DAY);
  state.allDay.setAttribute("aria-pressed", slot === WHOLE_DAY ? "true" : "false");
  if (state.selected) {
    const tram = state.selected.tram && data.tram_lines[state.selected.tram];
    state.panel.innerHTML = tram ? tramPanelHtml(tram, slot) : panelHtml(state.selected, slot, data.colors);
    state.panel.style.display = "block";
    state.panel.querySelector(".uvt-close").onclick = () => { state.selected = null; render(state); };
  } else {
    state.panel.style.display = "none";
  }
  if (state.overlay && state.deck) state.overlay.setProps({ layers: buildLayers(state.deck, state) });
}

function stopPlay(state) {
  if (state.timer) window.clearInterval(state.timer);
  state.timer = null;
}

function firstServedSlot(data) {
  const scope = data.segments.filter((s) => s.c);
  for (let h = 0; h < WHOLE_DAY; h += STEP) {
    if (h >= 5 && scope.some((s) => measuredAt(s, h))) return h;
  }
  return 6;
}

function buildControls(state) {
  const bottom = document.createElement("div");
  bottom.className = "uvt-bottom";
  bottom.innerHTML = '<div class="uvt-controls"><div class="uvt-clock" aria-live="polite"></div>'
    + '<div class="uvt-range-box"><input class="uvt-range" type="range" min="0" max="23" step="1" aria-label="Heure de la journée">'
    + '<div class="uvt-ticks"><span>0 h</span><span>6 h</span><span>12 h</span><span>18 h</span><span>23 h</span></div></div>'
    + '<button class="uvt-play" type="button" aria-pressed="false" aria-label="Lecture animée heure par heure">▶ Lecture</button>'
    + '<button class="uvt-allday" type="button" aria-pressed="false">Toute la journée</button></div>'
    + '<div class="uvt-legend"></div>';
  state.root.append(bottom);
  state.clock = bottom.querySelector(".uvt-clock");
  state.range = bottom.querySelector(".uvt-range");
  state.allDay = bottom.querySelector(".uvt-allday");
  state.play = bottom.querySelector(".uvt-play");
  state.legend = bottom.querySelector(".uvt-legend");
  state.range.addEventListener("input", () => {
    stopPlay(state);
    state.slot = Number(state.range.value);
    state.lastHour = state.slot;
    render(state);
  });
  state.allDay.addEventListener("click", () => {
    stopPlay(state);
    state.slot = state.slot === WHOLE_DAY ? state.lastHour : WHOLE_DAY;
    render(state);
  });
  state.play.addEventListener("click", () => {
    if (state.timer) {
      stopPlay(state);
      render(state);
      return;
    }
    const last = 24 - STEP;
    if (state.slot === WHOLE_DAY || state.slot >= last) state.slot = firstServedSlot(state.data);
    state.lastHour = state.slot;
    state.timer = window.setInterval(() => {
      if (state.slot + STEP > last) {
        stopPlay(state);
      } else {
        state.slot += STEP;
        state.lastHour = state.slot;
      }
      render(state);
    }, PLAY_MS);
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
  if (!state.sized) {
    state.sized = true;
    fitHeight(state);
    window.addEventListener("resize", () => fitHeight(state));
  }
  const key = `${data.view_key}|${data.mode}|${data.segments.length}|${data.slot}`;
  state.data = data;
  STEP = data.step || 1;
  if (state.dataKey !== key) {
    stopPlay(state);
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
  render(state);
  const fit = (duration) => {
    if (data.bounds) state.map.fitBounds(data.bounds, { padding: fitPadding(state), duration, maxZoom: 15 });
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
        const tramLine = info.layer.id === "tram" && state.data.tram_lines[info.object.r];
        state.tip.innerHTML = info.layer.id === "tram"
          ? `<b>Tram${tramLine ? ` ${esc(tramLine.n)}` : ""}</b><div>Mesure par portion entre deux arrêts indisponible pour le tram.</div>`
            + (tramLine ? '<div class="m">Touchez ou cliquez pour le temps perdu de bout en bout</div>' : "")
          : `<b>${esc(info.object.n)}</b><div class="l">${lineText(info.object)}</div><div>${shortStatus(info.object, state.slot)}</div><div class="m">Touchez ou cliquez pour le détail</div>`;
        state.tip.style.display = "block";
        const left = Math.min(info.x + 16, state.root.clientWidth - state.tip.offsetWidth - 8);
        const top = Math.min(info.y + 16, state.root.clientHeight - state.tip.offsetHeight - 150);
        state.tip.style.left = `${Math.max(8, left)}px`;
        state.tip.style.top = `${Math.max(8, top)}px`;
      },
      onClick: (info) => {
        const layer = info.object && info.layer ? info.layer.id : null;
        if (layer === "tram") state.selected = state.data.tram_lines[info.object.r] ? { tram: info.object.r } : null;
        else state.selected = layer === "troncons" ? info.object : null;
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
    stopPlay(state);
    if (state.map) state.map.remove();
    delete parentElement.__uvTroncons;
  };
}
