"""Graphiques Highcharts : composant Streamlit (st.components.v2) commun à tous les graphiques.

Highcharts Stock (qui contient le cœur), le module « more » et le module d'accessibilité sont chargés une
seule fois dans la page, sans iframe par graphique. Un graphique cliquable renvoie à Python l'identifiant
(`id`) du point cliqué, ce qui permet d'ouvrir une fiche depuis un graphique.
"""

from __future__ import annotations

import streamlit as st

from highcharts import CHART_FONT_CSS, HIGHCHARTS_CDN, INK, LINE, LIGHT_THEME

COMPONENT_NAME = "urban_vision_graphique"

_JS = """
const SCRIPTS = ["__HC__/highstock.js", "__HC__/highcharts-more.js", "__HC__/modules/accessibility.js"];

function loadOnce(url) {
  window.__uvScripts = window.__uvScripts || {};
  if (!window.__uvScripts[url]) {
    window.__uvScripts[url] = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = url;
      script.async = false;
      script.onload = () => resolve();
      script.onerror = () => { delete window.__uvScripts[url]; reject(new Error(url)); };
      document.head.appendChild(script);
    });
  }
  return window.__uvScripts[url];
}

async function loadHighcharts() {
  for (const url of SCRIPTS) await loadOnce(url);
  return window.Highcharts;
}

export default async function (component) {
  const { data, parentElement, setTriggerValue } = component;
  let box = parentElement.querySelector(".uv-chart");
  if (!box) {
    box = document.createElement("div");
    box.className = "uv-chart";
    box.setAttribute("lang", "fr");
    parentElement.append(box);
  }
  box.style.height = `${data.height}px`;
  let Highcharts;
  try {
    Highcharts = await loadHighcharts();
  } catch (error) {
    box.textContent = "Le graphique ne peut pas être chargé (bibliothèque inaccessible). Rechargez la page.";
    return undefined;
  }
  const theme = JSON.parse(JSON.stringify(data.theme));
  theme.tooltip = { ...(theme.tooltip || {}), useHTML: true, stickOnContact: true };
  if (data.footer) {
    theme.tooltip.footerFormat = data.footer.replace("__BASE__", window.location.origin + window.location.pathname);
  }
  Highcharts.setOptions(theme);
  const config = data.config;
  config.chart = { ...(config.chart || {}), height: data.height };
  if (data.clickable) {
    config.plotOptions = config.plotOptions || {};
    config.plotOptions.series = {
      ...(config.plotOptions.series || {}),
      cursor: "pointer",
      point: { events: { click: function () { setTriggerValue("clicked", this.options.id); } } },
    };
  }
  if (parentElement.__uvChart) parentElement.__uvChart.destroy();
  parentElement.__uvChart = Highcharts[data.constructor || "chart"](box, config);
  return () => {
    if (parentElement.__uvChart) parentElement.__uvChart.destroy();
    delete parentElement.__uvChart;
  };
}
""".replace("__HC__", HIGHCHARTS_CDN)

_CSS = f"""
@import url("{CHART_FONT_CSS}/400.css");
@import url("{CHART_FONT_CSS}/600.css");
@import url("{CHART_FONT_CSS}/700.css");
.uv-chart {{ width: 100%; font-family: 'Source Sans 3', sans-serif; }}
"""

CLICK_FOOTER = (f'<div style="margin-top:6px;padding-top:5px;border-top:1px solid {LINE};'
                f'font-size:13px;font-weight:700;color:{INK}">Cliquer pour analyser cette ligne ↓</div>')


def _register():
    return st.components.v2.component(COMPONENT_NAME, js=_JS, css=_CSS)


_graphique = _register()


def afficher(payload: dict, key: str | None = None, on_click=None, height: int = 300) -> None:
    """Affiche un graphique (`highcharts.chart_payload`) ; `on_click` reçoit le point cliqué s'il est cliquable."""
    global _graphique
    kwargs = {"key": key, "data": payload, "height": height}
    if on_click is not None:
        kwargs["on_clicked_change"] = on_click
    try:
        _graphique(**kwargs)
    except ValueError as error:
        if "is not registered" not in str(error):
            raise
        _graphique = _register()
        _graphique(**kwargs)


def graphique_cliquable(config: dict, key: str, on_click, height: int = 390,
                        footer: str = CLICK_FOOTER) -> None:
    """Affiche `config` ; `on_click` est appelé quand un point (de clé `id`) est cliqué."""
    payload = {"config": {k: v for k, v in config.items() if k != "lexique"}, "theme": LIGHT_THEME,
               "footer": footer, "height": height, "constructor": "chart", "clickable": True}
    afficher(payload, key=key, on_click=on_click, height=height)
