"""Tests du script de veille des visites humaines (logs nginx)."""

import datetime as dt
import sys
from pathlib import Path

import pytest

import veille_visiteurs as vv

UA_CHROME = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/128.0.0.0 Safari/537.36")
UA_GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
UA_GENERIQUE = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def make_line(ip="1.2.3.4", ts="16/Sep/2026:16:02:00 +0200", path="/", ua=UA_CHROME,
              status=200):
    return '%s - - [%s] "GET %s HTTP/1.1" %s 2687 "-" "%s"\n' % (ip, ts, path, status, ua)


class TestParse:
    def test_parse_ligne_valide(self):
        ev = vv.parse_line(make_line())
        assert ev["ip"] == "1.2.3.4"
        assert ev["status"] == 200
        assert ev["when"].tzinfo is not None

    def test_statut_400_exclu_de_read_logs(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "access.log").write_text(
            make_line(ua=UA_CHROME, status=400))
        assert vv.read_logs(logs, dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)) == []


class TestClassification:
    def test_googlebot_exclu(self):
        assert not vv.is_human_candidate(UA_GOOGLEBOT)

    def test_chrome_inclus(self):
        assert vv.is_human_candidate(UA_CHROME)

    def test_ua_sans_version_exclu(self):
        assert not vv.is_human_candidate(UA_GENERIQUE)


class TestCheminParDefaut:
    def test_racine_pointe_sur_le_projet(self):
        expected = Path(__file__).resolve().parents[1]
        assert vv.ROOT == expected

    def test_artifacts_par_defaut_reports_analytics(self):
        root = Path(__file__).resolve().parents[1]
        assert (vv.ROOT / "reports" / "analytics") == root / "reports" / "analytics"


class TestEndToEnd:
    def _run(self, logs_dir, state, html):
        sys.argv = ["veille", "--logs-dir", str(logs_dir), "--state", str(state),
                    "--html", str(html), "--no-lookup"]
        vv.main()

    def test_incrementiel_seulement_nouvelles_lignes(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "access.log").write_text(
            make_line(ip="1.1.1.1", ts="16/Sep/2026:14:00:00 +0200") +
            make_line(ip="1.1.1.1", ts="16/Sep/2026:14:05:00 +0200"))
        st = tmp_path / "state.json"
        self._run(logs, st, tmp_path / "v.html")

        import json
        vis = json.loads(st.read_text())["visitors"]
        assert set(vis) == {"1.1.1.1"}
        assert vis["1.1.1.1"]["hits"] == 2

        (logs / "access.log").write_text(
            make_line(ip="2.2.2.2", ts="16/Sep/2026:15:00:00 +0200"))
        self._run(logs, st, tmp_path / "v.html")
        vis = json.loads(st.read_text())["visitors"]
        assert set(vis) == {"1.1.1.1", "2.2.2.2"}
        assert vis["1.1.1.1"]["hits"] == 2

    def test_etat_roundtrip_days(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "access.log").write_text(
            make_line(ts="15/Sep/2026:10:00:00 +0200") +
            make_line(ts="16/Sep/2026:11:00:00 +0200"))
        st = tmp_path / "state.json"
        self._run(logs, st, tmp_path / "v.html")
        import json
        vis = json.loads(st.read_text())["visitors"]
        assert len(vis["1.2.3.4"]["days"]) == 2

    def test_html_contient_ip(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "access.log").write_text(make_line(ip="9.9.9.9"))
        html = tmp_path / "v.html"
        self._run(logs, tmp_path / "state.json", html)
        assert "9.9.9.9" in html.read_text()
        assert "heure" in html.read_text()


class TestFlag:
    def test_flag_france(self):
        rec = {"geo": {"countryCode": "FR", "country": "France"}}
        assert "France" in vv.flag(rec)

    def test_flag_cloud(self):
        rec = {"geo": {"countryCode": "US", "as": "AS15169 Google LLC", "isp": "Google"}}
        assert "Cloud" in vv.flag(rec)


class TestSelf:
    def test_self_reconnu(self):
        assert vv.is_self("90.120.193.41")
        assert not vv.is_self("37.58.152.1")


class TestBdc:
    def test_parse_bdc(self):
        row = {"localityName": "Bordeaux centre", "postcode": "33000",
               "location": {"latitude": 44.837, "longitude": -0.579},
               "confidence": "medium"}
        out = vv.parse_bdc(row)
        assert out["locality"] == "Bordeaux centre"
        assert out["postcode"] == "33000"
        assert out["lat"] == 44.837
        assert out["lon"] == -0.579

    def test_lookup_bdc_sans_cle(self):
        from datetime import datetime, timezone
        v = {"1.2.3.4": {"geo": {"countryCode": "FR"}, "bdc_t": 0}}
        vv.lookup_bdc(v, datetime.now(timezone.utc), None)
        assert v["1.2.3.4"]["geo"].get("bdc") is None

    def test_bdc_key_depuis_fichier(self, tmp_path, monkeypatch):
        kf = tmp_path / "bdc.key"
        kf.write_text("  abc123\n")
        monkeypatch.delenv("BDC_API_KEY", raising=False)
        monkeypatch.setenv("BDC_API_KEY_FILE", str(kf))
        assert vv.bdc_key() == "abc123"


class TestHtml:
    def _base(self, now=None):
        now = now or dt.datetime.now(dt.timezone.utc)
        return {
            "90.120.193.41": {"first": now, "last": now, "hits": 3,
                              "days": {"2026-09-16"}, "paths": ["/"],
                              "geo": {"countryCode": "FR", "regionName": "New Aquitaine",
                                      "city": "Bordeaux", "lat": 44.837, "lon": -0.579,
                                      "zip": "33000", "isp": "Orange"},
                              "geo_t": 0},
            "20.245.121.3": {"first": now, "last": now, "hits": 50,
                             "days": {"2026-09-16"}, "paths": ["/.env"],
                             "geo": {"countryCode": "US", "city": "Redmond"},
                             "geo_t": 0},
        }

    def test_html_filtre_na(self, tmp_path):
        target = tmp_path / "v.html"
        vv.render_html(self._base(), target, dt.datetime.now(dt.timezone.utc))
        html = target.read_text()
        head, sep, tail = html.partition('<table>')
        table = tail[:tail.find('</table>') + 8]
        assert "90.120.193.41" in table
        assert "20.245.121.3" not in table
        assert "20.245.121.3" in tail

    def test_html_carte_na(self, tmp_path):
        target = tmp_path / "v.html"
        vv.render_html(self._base(), target, dt.datetime.now(dt.timezone.utc))
        html = target.read_text()
        assert "leaflet" in html
        assert "Carte des connexions" in html
        assert "cartocdn.com" in html
        assert '"ip": "90.120.193.41"' in html
        assert "44.837" in html

    def test_html_table_localite_bdc(self, tmp_path):
        target = tmp_path / "v.html"
        base = self._base()
        base["90.120.193.41"]["geo"]["bdc"] = {"locality": "Mérignac",
                                               "postcode": "33700",
                                               "lat": 44.83, "lon": -0.62}
        vv.render_html(base, target, dt.datetime.now(dt.timezone.utc))
        html = target.read_text()
        assert "Mérignac" in html
        assert "33700" in html
        assert "city-hint" in html

    def test_html_responsive_compte_today(self, tmp_path):
        target = tmp_path / "v.html"
        base = self._base()
        older = dict(base)
        older["90.120.193.41"]["last"] = dt.datetime(2026, 9, 10, 12, 0,
                                                     tzinfo=dt.timezone(dt.timedelta(hours=2)))
        vv.render_html(older, target, dt.datetime(2026, 9, 16, 18, 0, tzinfo=dt.timezone.utc))
        assert "actif(s) aujourd" in target.read_text()

    def test_html_self_violet(self, tmp_path):
        target = tmp_path / "v.html"
        vv.render_html(self._base(), target, dt.datetime.now(dt.timezone.utc))
        html = target.read_text()
        assert "class='p-self'" in html
        assert "Votre IP" in html
        assert "90.120.193.41" in html