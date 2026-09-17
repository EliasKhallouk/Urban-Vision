import argparse
import datetime as dt
import gzip
import glob
import html as htmlmod
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BOTS = (
    "googlebot", "bingbot", "duckduckbot", "baiduspider", "yandex",
    "semrush", "ahrefsbot", "petalbot", "libredtail", "censys",
    "zmap", "odin", "l9explore", "l9scan", "leakix", "infrawatch",
    "internetmeasurement", "cyberconvoy", "forestengine",
    "visionheight", "zgrab", "masscan", "nuclei", "sqlmap", "nikto",
    "fscan", "nessus", "photon", "slurp", "python-requests", "curl",
    "wget/", "go-http-client", "headless", "phantomjs", "puppeteer",
    "playwright", "selenium", "okhttp", "http-client", "checker",
    "monitor", "uptime", "pingdom", "statuscake", "wpscan", "crawler",
    "spider", "compatible;", "research-scan",
)
BROWSER = re.compile(
    r"(Chrome/|CriOS/|Firefox/|FxiOS/|Edg/|OPR/|Safari/\d+|Mobile/.*Safari/)"
)
ROOT = Path(__file__).resolve().parents[2]
CLOUDS = (
    "AMAZON", "AWS", "GOOGLE", "MICROSOFT", "OVH", "SCALEWAY",
    "DIGITALOCEAN", "DIGITAL OCEAN", "HETZNER", "ORACLE", "VULTR",
    "ALIBABA", "LINODE", "AKAMAI", "CLOUDFLARE", "HOSTING",
)
LINE = re.compile(
    r'^(\S+) \S+ \S+ \[([^\]]+)\] "(\S+) ([^"]*)" (\d{3})'
)
TZ = re.compile(r"(\+|-)\d{4}$")


def parse_line(line):
    m = LINE.match(line)
    if not m:
        return None
    ip, ts, method, target, status = m.groups()
    try:
        when = dt.datetime.strptime(ts, "%d/%b/%Y:%H:%M:%S %z")
    except ValueError:
        return None
    return {"ip": ip, "when": when, "method": method, "status": int(status),
            "target": target}


def is_human_candidate(ua):
    lua = ua.lower()
    for token in BOTS:
        if token in lua:
            return False
    return bool(BROWSER.search(ua))


def payload(event):
    path = event["target"].split()[0].split("?")[0]
    ua = event.get("ua")
    return event["ip"], event["when"], event["method"], event["status"], path, ua


def read_logs(logs_dir, after):
    events = []
    pattern = str(Path(logs_dir) / "access.log*")
    for fname in sorted(glob.glob(pattern)):
        raw = gzip.open(fname, "rt", errors="replace") if fname.endswith(".gz") \
            else open(fname, errors="replace")
        with raw as stream:
            for line in stream:
                event = parse_line(line)
                if not event:
                    continue
                if event["when"] <= after:
                    continue
                if event["status"] not in (200, 101, 206, 304):
                    continue
                if event["method"] not in ("GET", "POST", "HEAD"):
                    continue
                event["ua"] = line.split('"')[-2] if '"' in line else ""
                if not is_human_candidate(event["ua"]):
                    continue
                events.append(event)
    return events


def bump(visitors, event):
    ip, when, method, status, path, ua = payload(event)
    rec = visitors.get(ip)
    if rec is None:
        rec = {"first": when, "last": when, "hits": 1, "days": {when.date().isoformat()},
               "paths": [path], "geo": None, "geo_t": 0}
        visitors[ip] = rec
    else:
        rec["hits"] += 1
        rec["first"] = min(rec["first"], when)
        rec["last"] = max(rec["last"], when)
        rec["days"].add(when.date().isoformat())
        if path not in rec["paths"]:
            rec["paths"].append(path)
            if len(rec["paths"]) > 12:
                del rec["paths"][0]


def lookup_geo(visitors, now):
    pending = [ip for ip, rec in visitors.items()
               if not (rec["geo"] and rec["geo"].get("isp") and rec["geo"].get("lat") is not None)
               and ":" not in ip and rec["geo_t"] <= now.timestamp() - 3600]
    for chunk in (pending[i:i + 100] for i in range(0, min(len(pending), 500), 100)):
        try:
            data = json.dumps(chunk).encode()
            req = urllib.request.Request(
                "http://ip-api.com/batch/?fields=status,message,country,countryCode,"
                "regionName,city,lat,lon,zip,isp,org,as", data=data,
                headers={"Content-Type": "application/json",
                         "User-Agent": "urban-vision-veille"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                rows = json.load(resp)
        except Exception:
            return
        for ip, row in zip(chunk, rows + [{}] * (len(chunk) - len(rows))):
            rec = visitors[ip]
            rec["geo_t"] = now.timestamp()
            if row.get("status") == "success":
                rec["geo"] = {k: row.get(k) for k in
                              ("country", "countryCode", "regionName", "city",
                               "lat", "lon", "zip", "isp", "org", "as")}


def parse_bdc(row):
    loc = row.get("location") or {}
    return {
        "locality": row.get("localityName") or (loc.get("localityName") or None),
        "postcode": row.get("postcode") or (loc.get("postcode") or None),
        "lat": (loc.get("latitude") or None) or row.get("latitude"),
        "lon": (loc.get("longitude") or None) or row.get("longitude"),
        "confidence": row.get("confidence") or (loc.get("confidence") or None),
    }


def bdc_key():
    key = os.environ.get("BDC_API_KEY")
    if key:
        return key
    path = os.environ.get("BDC_API_KEY_FILE") or "/etc/urban-vision/bdc.key"
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None


def lookup_bdc(visitors, now, key):
    if not key:
        return
    for ip, rec in visitors.items():
        geo = rec.get("geo") or {}
        if geo.get("countryCode") != "FR" or ":" in ip:
            continue
        if profile_class(rec) == "p-bot":
            continue
        bdc = geo.get("bdc")
        delay = 7 * 86400 if bdc else 2 * 3600
        if rec.get("bdc_t", 0) > now.timestamp() - delay:
            continue
        rec["bdc_t"] = now.timestamp()
        url = ("https://api-bdc.net/data/ip-geolocation?ip=%s"
               "&localityLanguage=fr&key=%s" % (ip, urllib.parse.quote(key)))
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "urban-vision-veille"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.load(resp)
        except Exception:
            continue
        if isinstance(data, dict) and ("localityName" in data or "location" in data):
            geo["bdc"] = parse_bdc(data)
            rec["bdc_t"] = now.timestamp()
        time.sleep(0.5)


def flag(rec):
    geo = rec.get("geo") or {}
    hay = " ".join(str(geo.get(k, "")) for k in ("isp", "org", "as")).upper()
    cloud = any(t in hay for t in CLOUDS)
    fr = geo.get("countryCode") == "FR"
    if cloud:
        return "Cloud (probable bot)" if not fr else "Cloud FR (probable bot)"
    if fr:
        return "France — visiteur probable"
    if geo.get("country"):
        return geo.get("country")
    return "inconnu"


def is_na(rec):
    geo = rec.get("geo") or {}
    return geo.get("countryCode") == "FR" and "AQUITAINE" in geo.get("regionName", "").upper()


SELF_IPS = {"90.120.193.41"}


def is_self(ip):
    return ip in SELF_IPS


def profile_class(rec):
    geo = rec.get("geo") or {}
    hay = " ".join(str(geo.get(k, "")) for k in ("isp", "org", "as")).upper()
    if any(t in hay for t in CLOUDS):
        return "p-bot"
    if geo.get("countryCode") == "FR":
        return "p-hum"
    return "p-int"


def _row(ip, rec, today_iso):
    first = rec["first"].strftime("%d/%m %H:%M")
    last = rec["last"].strftime("%d/%m %H:%M")
    today = (' <span class="chip chip-today">aujourd&rsquo;hui</span>'
             if rec["last"].date().isoformat() == today_iso else "")
    geo = rec.get("geo") or {}
    bdc = geo.get("bdc") or {}
    base_city = (geo.get("city") or "").strip()
    loc = (bdc.get("locality") or "").strip()
    postcode = (bdc.get("postcode") or geo.get("zip") or "").strip()
    if loc:
        cell = htmlmod.escape(loc)
        if base_city and loc.lower() != base_city.lower():
            cell += ' <span class="city-hint">&asymp; ' + htmlmod.escape(base_city) + "</span>"
    elif base_city:
        cell = htmlmod.escape(base_city)
    else:
        cell = "&ndash;"
    if postcode:
        cell += " " + htmlmod.escape(postcode)
    isp = htmlmod.escape(geo.get("isp") or geo.get("org") or "&ndash;")
    tag = htmlmod.escape(flag(rec))
    prof = profile_class(rec)
    if is_self(ip):
        prof = "p-self"
        tag = "Votre IP &mdash; utilisateur"
    pages = " &middot; ".join(htmlmod.escape(p) for p in rec["paths"][:5])
    return ("<tr class='%s'><td>%s%s</td><td>%s</td><td>%s</td><td>%d</td>"
            "<td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (prof, last, today, htmlmod.escape(ip), first,
               rec["hits"], cell, isp, tag, ", ".join(sorted(rec["days"])), pages))


def _table(rows, today_iso):
    lines = [("<tr><th>Dernier passage</th><th>IP</th><th>Premi&egrave;re connexion</th>"
              "<th>Requ&ecirc;tes</th><th>Ville</th><th>ISP / organisme</th><th>Profil</th>"
              "<th>Jours pr&eacute;sents</th><th>Pages visit&eacute;es</th></tr>")]
    lines.extend(_row(ip, rec, today_iso) for ip, rec in rows)
    return ("<table>%s</table>" % "".join(lines)) if rows else \
        "<p class='none'>Aucune visite &agrave; afficher.</p>"


def _point(ip, rec):
    geo = rec.get("geo") or {}
    bdc = geo.get("bdc") or {}
    lat = bdc.get("lat") if bdc.get("lat") is not None else geo.get("lat")
    lon = bdc.get("lon") if bdc.get("lon") is not None else geo.get("lon")
    if lat is None or lon is None:
        return None
    prof = profile_class(rec)
    color = {"p-self": "#c77dff", "p-bot": "#f5b84b",
             "p-int": "#8b93a3"}.get(prof, "#34d399")
    return {"ip": ip, "city": geo.get("city") or "",
            "locality": bdc.get("locality") or "",
            "zip": bdc.get("postcode") or geo.get("zip") or "",
            "isp": geo.get("isp") or geo.get("org") or "",
            "lat": lat, "lon": lon,
            "first": rec["first"].strftime("%d/%m %H:%M"),
            "last": rec["last"].strftime("%d/%m %H:%M"),
            "hits": rec["hits"], "color": color}


def _map_html(pts):
    if not pts:
        return ""
    data = json.dumps(pts, ensure_ascii=True)
    return """<details open class="map-box"><summary>Carte des connexions Nouvelle-Aquitaine (%d)</summary>
<div id="map"></div>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
var PTS = %s;
var map = L.map('map');
L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
 {subdomains:'abcd', maxZoom:19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>'}).addTo(map);
var pts = PTS.map(function(p){return [p.lat, p.lon];});
if (pts.length === 1) { map.setView(pts[0], 13); }
else { map.fitBounds(L.latLngBounds(pts).pad(0.15)); }
PTS.forEach(function(p){
 var m = L.circleMarker([p.lat, p.lon], {radius:7, color:'#0f1115', weight:1, fillColor:p.color, fillOpacity:.85});
 var zip = p.zip ? ' <small>' + p.zip + '</small>' : '';
 m.bindTooltip('<b>' + p.city + '</b>' + zip + '<br/><small>' + (p.locality || '') + '</small><br/>' +
   p.ip + ' · ' + p.isp + '<br/>du ' + p.first + ' au ' + p.last + ' &middot; ' + p.hits + ' req');
 m.addTo(map);
});
</script></details>""" % (len(pts), data)


def _dernier_fr(ip, rec, today_iso):
    if not ip:
        return "<div class='dernier'><span class='lbl'>Dernier visiteur en France</span> &ndash;</div>"
    geo = rec.get("geo") or {}
    region = geo.get("regionName") or ""
    if region.upper() == "NEW AQUITAINE":
        region = "Nouvelle-Aquitaine"
    loc = (geo.get("bdc") or {}).get("locality") or geo.get("city") or ""
    loc = htmlmod.escape(loc)
    region = htmlmod.escape(region)
    where = " &ndash; ".join(x for x in (loc, region) if x)
    today = (' <span class="chip chip-today">aujourd&rsquo;hui</span>'
             if rec["last"].date().isoformat() == today_iso else "")
    last = rec["last"].strftime("%d/%m &agrave; %H:%M")
    isp = geo.get("isp") or ""
    tag = " &middot; Votre IP" if is_self(ip) else ""
    return ("<div class='dernier'><span class='lbl'>Dernier visiteur en France</span>"
            "<b>%s</b>%s &mdash; %s%s &middot; %s &mdash; %s</div>"
            % (htmlmod.escape(ip), today, where, tag,
               htmlmod.escape(isp), last))


def render_html(visitors, html_path, now):
    now_paris = now.astimezone(dt.timezone(dt.timedelta(hours=2)))
    today_iso = now_paris.date().isoformat()
    ordered = sorted(visitors.items(), key=lambda kv: kv[1]["last"], reverse=True)
    na = [(ip, rec) for ip, rec in ordered if is_na(rec)]
    autres_fr = [(ip, rec) for ip, rec in ordered
                 if not is_na(rec) and (rec.get("geo") or {}).get("countryCode") == "FR"]
    others = [(ip, rec) for ip, rec in ordered
              if not is_na(rec) and (rec.get("geo") or {}).get("countryCode") != "FR"]
    recent, older = na[:40], na[40:]
    pts = [p for p in (_point(ip, rec) for ip, rec in recent) if p]
    today_count = sum(1 for ip, rec in na
                      if rec["last"].date().isoformat() == today_iso)
    latest = ordered[0][1]["last"].strftime("%d/%m/%Y &agrave; %H:%M") if ordered else "&ndash;"
    top_fr = next(((ip, rec) for ip, rec in ordered
                   if (rec.get("geo") or {}).get("countryCode") == "FR"), (None, None))
    section = _map_html(pts)
    section += _table(recent, today_iso)
    if older:
        section += ("<details><summary>Anciennes visites Nouvelle-Aquitaine "
                    "(%d)</summary>%s</details>" % (len(older), _table(older, today_iso)))
    if autres_fr:
        section += ("<details><summary>Autres r&eacute;gions France "
                    "(%d)</summary>%s</details>" % (len(autres_fr), _table(autres_fr, today_iso)))
    if others:
        section += ("<details><summary>Hors France / non g&eacute;olocalis&eacute;es "
                    "(%d)</summary>%s</details>" % (len(others), _table(others, today_iso)))
    html = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="300">
<title>Urban Vision — visiteurs Nouvelle-Aquitaine</title>
<style>
:root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--txt:#e7eaf0;--mut:#98a2b3;
--green:#34d399;--amber:#f5b84b;--grey:#8b93a3;--blue:#7db4ff;--purple:#c77dff}
*{box-sizing:border-box}
body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;background:var(--bg);color:var(--txt)}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 48px}
header{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap}
h1{font-size:21px;margin:0 0 4px}
.sub{color:var(--mut);font-size:13px}
.dernier{display:flex;gap:10px;flex-wrap:wrap;align-items:baseline;background:rgba(125,180,255,.07);border:1px solid var(--line);border-left:4px solid var(--blue);border-radius:10px;padding:11px 14px;font-size:13px;margin-top:18px}
.dernier .lbl{color:var(--blue);font-weight:600;text-transform:uppercase;font-size:11px;letter-spacing:.05em;margin-right:4px}
.stats{display:flex;gap:12px;flex-wrap:wrap;margin:22px 0 6px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:10px 16px;min-width:130px}
.stat b{display:block;font-size:22px;line-height:1.1}
.stat span{color:var(--mut);font-size:12px}
table{border-collapse:collapse;font-size:13px;margin-top:14px;background:var(--panel);border-radius:10px;overflow:hidden;width:100%%}
th{background:#1d222c;text-align:left;padding:8px 10px;color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.city-hint{color:var(--mut);font-weight:400;font-size:11px}
td{border-top:1px solid var(--line);padding:8px 10px;vertical-align:top}
tr.p-self td{background:rgba(199,125,255,.09)}
tr.p-self td:first-child{box-shadow:inset 4px 0 0 rgba(199,125,255,.85)}
tr.p-hum td{background:rgba(52,211,153,.07)}
tr.p-hum td:first-child{box-shadow:inset 4px 0 0 rgba(52,211,153,.75)}
tr.p-bot td{background:rgba(245,184,75,.07)}
tr.p-bot td:first-child{box-shadow:inset 4px 0 0 rgba(245,184,75,.75)}
tr.p-int td{background:rgba(139,147,163,.06)}
tr.p-int td:first-child{box-shadow:inset 4px 0 0 rgba(139,147,163,.6)}
tr:hover td{background:rgba(125,180,255,.08)}
.chip{display:inline-block;margin-left:6px;padding:1px 8px;border-radius:99px;font-size:10px;font-weight:700;vertical-align:middle}
.chip-today{background:rgba(125,180,255,.16);color:var(--blue);border:1px solid rgba(125,180,255,.4)}
details{border:1px solid var(--line);border-radius:10px;background:var(--panel);margin-top:14px}
details.map-box{padding:0}
#map{height:340px;border-radius:0 0 10px 10px;background:#1d222c;z-index:0}
summary{cursor:pointer;padding:12px 16px;color:var(--blue);font-size:13px;font-weight:600}
details table{margin:0;border-radius:0}
details[open] summary{border-bottom:1px solid var(--line)}
.none{color:var(--mut);font-size:13px;padding:10px 4px}
.legend{color:var(--mut);font-size:12px;margin-top:16px;line-height:1.7}
.dot{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:6px;vertical-align:middle}
.dot-h{background:rgba(52,211,153,.6)}.dot-b{background:rgba(245,184,75,.55)}.dot-i{background:rgba(139,147,163,.6)}.dot-s{background:rgba(199,125,255,.7)}
footer{color:var(--mut);font-size:12px;margin-top:22px}
</style>
</head>
<body><div class="wrap">
<header>
<div>
<h1>Urban Vision &mdash; qui s&rsquo;est connect&eacute;, et &agrave; quelle heure&nbsp;?</h1>
<div class="sub">Connexions &laquo;&nbsp;humaines&nbsp;&raquo; (navigateurs), r&eacute;gion Nouvelle-Aquitaine uniquement &mdash; derni&egrave;re analyse : %s</div>
</div>
<div class="sub">Dernier hit enregistr&eacute; : %s<br>Mise &agrave; jour automatique toutes les 5 min</div>
</header>
%s
<div class="stats">
<div class="stat"><b>%d</b><span>visiteurs Nouvelle-Aquitaine suivis</span></div>
<div class="stat"><b>%d</b><span>actif(s) aujourd&rsquo;hui</span></div>
<div class="stat"><b>%d</b><span>connexions hors r&eacute;gion masqu&eacute;es</span></div>
</div>
%s
<div class="legend">
<span class="dot dot-s"></span>Votre IP (utilisateur du site)
<span class="dot dot-h"></span>R&eacute;sidentiel / entreprise en Nouvelle-Aquitaine &mdash; visiteur pr&eacute;sum&eacute;
<span class="dot dot-b"></span>H&eacute;bergeur / cloud (probable bot)
<span class="dot dot-i"></span>Hors France ou non g&eacute;olocalis&eacute;
</div>
<footer>G&eacute;n&eacute;r&eacute; par src/scripts/veille_visiteurs.py &mdash; les listes pays/h&eacute;bergeurs se consultent dans les sections d&eacute;pliables ci-dessus.</footer>
</div></body>
</html>""" % (now_paris.strftime("%d/%m/%Y &agrave; %H:%M"), latest,
             _dernier_fr(*top_fr, today_iso),
             len(na), today_count, len(autres_fr) + len(others), section)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html, encoding="utf-8")


def iso(value):
    return dt.datetime.fromisoformat(value) if isinstance(value, str) else value


def main():
    ap = argparse.ArgumentParser(description="Veille des visites humaines (logs nginx)")
    arts = ROOT / "reports" / "analytics"
    ap.add_argument("--logs-dir", default="/var/log/nginx")
    ap.add_argument("--state", default=str(arts / "veille_state.json"))
    ap.add_argument("--html", default=str(arts / "visiteurs_reels.html"))
    ap.add_argument("--since", default=None, help="rejouer depuis une date ISO")
    ap.add_argument("--no-lookup", action="store_true", dest="no_lookup")
    args = ap.parse_args()

    state_path = Path(args.state)
    state = json.loads(state_path.read_text(encoding="utf-8")) \
        if state_path.exists() else {"last": None, "visitors": {}}
    visitors = state.setdefault("visitors", {})
    for rec in visitors.values():
        rec["first"] = iso(rec["first"])
        rec["last"] = iso(rec["last"])
        days = rec.get("days")
        rec["days"] = set(days) if isinstance(days, list) else set()

    after = dt.datetime.fromisoformat(args.since) if args.since \
        else (iso(state.get("last")) or dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc))
    today = dt.datetime.now(dt.timezone.utc)
    events = read_logs(args.logs_dir, after)
    for event in events:
        bump(visitors, event)
    if events:
        state["last"] = max(e["when"] for e in events).isoformat()
    if not args.no_lookup:
        lookup_geo(visitors, today)
        lookup_bdc(visitors, today, bdc_key())

    state["generated"] = today.isoformat()
    for ip in visitors:
        visitors[ip]["days"] = sorted(visitors[ip]["days"])
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1, default=str),
                          encoding="utf-8")
    render_html(visitors, Path(args.html), today)
    return 0


if __name__ == "__main__":
    sys.exit(main())