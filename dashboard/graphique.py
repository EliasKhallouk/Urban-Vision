"""Graphique Highcharts cliquable : composant Streamlit (st.components.v2).

Contrairement aux graphiques de `highcharts.render` (iframe sans retour), ce
composant renvoie à Python l'identifiant (`id`) du point cliqué, ce qui permet
d'ouvrir une fiche depuis un graphique.
"""

from __future__ import annotations

import streamlit as st

from highcharts import CHART_FONT_CSS, HIGHCHARTS_CDN, LIGHT_THEME

COMPONENT_NAME = "urban_vision_graphique_cliquable"

_JS = """
const HC_URL = "__HC__/highcharts.js";

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

export default async function (component) {
  const { data, parentElement, setTriggerValue } = component;
  let box = parentElement.querySelector(".uv-chart");
  if (!box) {
    box = document.createElement("div");
    box.className = "uv-chart";
    parentElement.append(box);
  }
  box.style.height = `${data.height}px`;
  let Highcharts;
  try {
    Highcharts = await loadScript(HC_URL, "Highcharts");
  } catch (error) {
    box.textContent = "Le graphique ne peut pas être chargé (bibliothèque inaccessible).";
    return undefined;
  }
  Highcharts.setOptions(data.theme);
  const config = data.config;
  config.chart = { ...(config.chart || {}), height: data.height };
  config.tooltip = { ...(config.tooltip || {}), useHTML: true, footerFormat: data.footer };
  config.plotOptions = config.plotOptions || {};
  config.plotOptions.series = {
    ...(config.plotOptions.series || {}),
    cursor: "pointer",
    point: { events: { click: function () { setTriggerValue("clicked", this.options.id); } } },
  };
  if (parentElement.__uvChart) parentElement.__uvChart.destroy();
  parentElement.__uvChart = Highcharts.chart(box, config);
  return () => {
    if (parentElement.__uvChart) parentElement.__uvChart.destroy();
    delete parentElement.__uvChart;
  };
}
""".replace("__HC__", HIGHCHARTS_CDN)

_CSS = f"""
@import url("{CHART_FONT_CSS}/400.css");
.uv-chart {{ width: 100%; }}
"""

CLICK_FOOTER = ('<div style="margin-top:6px;padding-top:5px;border-top:1px solid rgba(221, 161, 94, 0.40);'
                'font-size:13px;font-weight:700;color:#283618">Cliquer pour analyser cette ligne ↓</div>')


def _register():
    return st.components.v2.component(COMPONENT_NAME, js=_JS, css=_CSS)


_graphique = _register()


def graphique_cliquable(config: dict, key: str, on_click, height: int = 390,
                        footer: str = CLICK_FOOTER) -> None:
    """Affiche `config` ; `on_click` est appelé quand un point (de clé `id`) est cliqué."""
    global _graphique
    payload = {"config": {k: v for k, v in config.items() if k != "lexique"}, "theme": LIGHT_THEME,
               "footer": footer, "height": height}
    try:
        _graphique(key=key, data=payload, on_clicked_change=on_click, height=height)
    except ValueError as error:
        if "is not registered" not in str(error):
            raise
        _graphique = _register()
        _graphique(key=key, data=payload, on_clicked_change=on_click, height=height)
