"""Tests de la veille de la collecte (alertes email)."""

import json
import smtplib
import time
from datetime import datetime, timedelta

import pytest

import veille_collecte as vc

NOW = datetime(2026, 10, 1, 11, 0).timestamp()
SETTINGS = {
    "host": "smtp.example.org", "port": 465, "user": "u", "password": "p",
    "from": "u@example.org", "to": "dest@example.org",
}


def _log_line(ts, level, msg):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") + f",123 [{level}] {msg}"


def _ok(ts):
    return (ts, "INFO", "OK - 3400 entités, 130000 observations mises à jour (feed ts=1)")


def _seed_passages(conn, start, n):
    conn.executemany(
        "INSERT INTO observations (trip_id, start_date, route_id, stop_sequence, stop_id, "
        "schedule_relationship, departure_delay, departure_time, last_seen_at) "
        "VALUES (?, '20260101', 'A', ?, 's', 'SCHEDULED', 0, ?, ?)",
        [(f"t{int(start)}", i, int(start) + i, int(start) + i) for i in range(n)],
    )
    conn.commit()


def _window_start(weeks_ago):
    end = datetime.fromtimestamp(NOW - vc.VOLUME_LAG_SECONDS) - timedelta(days=7 * weeks_ago)
    return (end - timedelta(seconds=vc.VOLUME_WINDOW_SECONDS)).timestamp()


def _cond(active, key="trous", title="Trous de collecte"):
    return vc.Condition(key, title, active, "détails")


def _kinds(notifications):
    return [kind for kind, _, _ in notifications]


class TestFormats:
    def test_durees(self):
        assert vc.duration(240) == "4 min"
        assert vc.duration(3600) == "1 h 00 min"
        assert vc.duration(3 * 86400 + 3600) == "3 j 1 h"


class TestLogs:
    def test_read_tail_ignore_la_ligne_coupee(self, tmp_path):
        path = tmp_path / "collect.log"
        path.write_text("premiere ligne tronquee\n" + "".join(f"ligne {i}\n" for i in range(5)))
        assert vc.read_tail(path, size=30) == ["ligne 2", "ligne 3", "ligne 4"]
        assert vc.read_tail(path)[0] == "premiere ligne tronquee"

    def test_read_tail_fichier_absent(self, tmp_path):
        assert vc.read_tail(tmp_path / "absent.log") == []

    def test_parse_log_ignore_les_tracebacks(self):
        lines = [_log_line(NOW, "INFO", "OK - 10 entités"), "Traceback (most recent call last):",
                 '  File "collect.py", line 1']
        assert vc.parse_log(lines) == [(NOW, "INFO", "OK - 10 entités")]


class TestCollecteArretee:
    def test_releve_recent_ok(self):
        assert not vc.check_heartbeat([_ok(NOW - 120)], NOW).active

    def test_flux_vide_la_nuit_reste_ok(self):
        entries = [(NOW - 30, "INFO", "OK - 0 entités, 0 observations mises à jour (feed ts=1)")]
        assert not vc.check_heartbeat(entries, NOW).active

    def test_dernier_releve_trop_ancien_alerte(self):
        entries = [_ok(NOW - 11 * 60), (NOW - 60, "WARNING", "Échec de récupération du flux : 502")]
        cond = vc.check_heartbeat(entries, NOW)
        assert cond.active
        assert "il y a 11 min" in cond.details

    def test_aucun_releve_alerte(self):
        assert vc.check_heartbeat([], NOW).active


class TestTrous:
    def test_trou_dans_la_derniere_heure_alerte(self, conn):
        conn.executemany("INSERT INTO collection_gaps VALUES (?, ?)", [
            (int(NOW) - 5 * 3600, int(NOW) - 5 * 3600 + 240),
            (int(NOW) - 600, int(NOW) - 360),
        ])
        conn.commit()
        cond = vc.check_gaps(conn, NOW)
        assert cond.active
        assert "1 trou(s) de collecte sur la dernière heure, 4 min au total" in cond.details
        assert "Sur 24 h : 2 trou(s), 8 min au total." in cond.details

    def test_trou_plus_ancien_qu_une_heure_ok(self, conn):
        conn.execute("INSERT INTO collection_gaps VALUES (?, ?)",
                     (int(NOW) - 3 * 3600, int(NOW) - 3 * 3600 + 200))
        conn.commit()
        cond = vc.check_gaps(conn, NOW)
        assert not cond.active
        assert "Sur 24 h : 1 trou(s)" in cond.details


class TestAvertissements:
    def test_avertissements_repetes_alertent(self):
        entries = {
            "collect.log": [
                (NOW - 60 * k, "WARNING", f"Rafraîchissement des agrégats lent : {70 + k}.0 s (seuil 60 s)")
                for k in range(3)
            ],
            "alerts.log": [],
        }
        cond = vc.check_logs(entries, NOW)
        assert cond.active
        assert "3 × collect.log [WARNING] Rafraîchissement des agrégats lent : N s (seuil N s)" in cond.details

    def test_trous_et_avertissements_anciens_ignores(self):
        entries = {
            "collect.log": [(NOW - 60, "WARNING", "Trou de collecte détecté : 4 minutes")] * 5
            + [(NOW - 2 * 3600, "ERROR", "Erreur inattendue pendant le traitement : x")] * 5,
            "alerts.log": [(NOW - 60, "ERROR", "Erreur inattendue : database is locked")],
        }
        cond = vc.check_logs(entries, NOW)
        assert not cond.active
        assert cond.details.startswith("1 avertissement(s)")


class TestFluxQuasiVide:
    def test_flux_quasi_vide_alerte(self, conn, monkeypatch):
        monkeypatch.setattr(vc, "VOLUME_MIN_BASELINE", 100)
        for weeks in vc.VOLUME_WEEKS:
            _seed_passages(conn, _window_start(weeks), 200)
        _seed_passages(conn, _window_start(0), 10)
        cond = vc.check_volume(conn, NOW)
        assert cond.active
        assert cond.details.startswith("10 passages enregistrés")
        assert "contre 200 habituellement" in cond.details
        assert "soit 5 %" in cond.details

    def test_volume_habituel_ok(self, conn, monkeypatch):
        monkeypatch.setattr(vc, "VOLUME_MIN_BASELINE", 100)
        for weeks in vc.VOLUME_WEEKS:
            _seed_passages(conn, _window_start(weeks), 200)
        _seed_passages(conn, _window_start(0), 180)
        assert not vc.check_volume(conn, NOW).active

    def test_creneau_habituellement_creux_ignore(self, conn):
        for weeks in vc.VOLUME_WEEKS:
            _seed_passages(conn, _window_start(weeks), 200)
        assert not vc.check_volume(conn, NOW).active


class TestNotifications:
    def test_alerte_silence_rappel_puis_fin(self):
        notifications, state = vc.plan_notifications([_cond(True)], {}, NOW)
        assert _kinds(notifications) == ["alerte"]
        state = vc.mark_sent(state, notifications, NOW)

        notifications, state = vc.plan_notifications([_cond(True)], state, NOW + 300)
        assert notifications == []

        later = NOW + vc.REMINDER_SECONDS
        notifications, state = vc.plan_notifications([_cond(True)], state, later)
        assert _kinds(notifications) == ["rappel"]
        assert notifications[0][2] == NOW
        state = vc.mark_sent(state, notifications, later)

        notifications, state = vc.plan_notifications([_cond(False)], state, later + 300)
        assert _kinds(notifications) == ["fin"]
        assert vc.mark_sent(state, notifications, later + 300) == {}

    def test_echec_d_envoi_reessaye_au_passage_suivant(self):
        _, state = vc.plan_notifications([_cond(True)], {}, NOW)
        notifications, _ = vc.plan_notifications([_cond(True)], state, NOW + 300)
        assert _kinds(notifications) == ["alerte"]
        assert notifications[0][2] == NOW

    def test_fin_sans_alerte_envoyee_silencieuse(self):
        _, state = vc.plan_notifications([_cond(True)], {}, NOW)
        notifications, state = vc.plan_notifications([_cond(False)], state, NOW + 300)
        assert notifications == []
        assert state == {}

    def test_objet_et_corps(self):
        notifications = [
            ("alerte", _cond(True), NOW),
            ("fin", _cond(False, "flux_pauvre", "Flux temps réel quasi vide"), NOW - 3600),
        ]
        subject, body = vc.compose(notifications, NOW, host="ek-hub")
        assert subject == "[Urban Vision] Alerte collecte : Trous de collecte"
        assert "NOUVELLE ALERTE — Trous de collecte (depuis le 01/10 à 11:00)" in body
        assert "RÉSOLU — Flux temps réel quasi vide (a duré 1 h 00 min)" in body

    def test_objet_retour_a_la_normale(self):
        subject, _ = vc.compose([("fin", _cond(False), NOW - 600)], NOW, host="ek-hub")
        assert subject == "[Urban Vision] Retour à la normale : Trous de collecte"


class TestConfiguration:
    def test_fichier_env_et_mot_de_passe_d_application_avec_espaces(self, tmp_path):
        env_file = tmp_path / "alertes.env"
        env_file.write_text(
            "# Gmail\nUV_SMTP_USER=moi@example.org\nUV_SMTP_PASSWORD=\"abcd efgh ijkl mnop\"\n"
        )
        assert vc.load_settings(environ={}, env_file=env_file) == {
            "host": "smtp.gmail.com", "port": 465, "user": "moi@example.org",
            "password": "abcdefghijklmnop", "from": "moi@example.org", "to": "moi@example.org",
        }

    def test_variables_d_environnement_prioritaires(self, tmp_path):
        env_file = tmp_path / "alertes.env"
        env_file.write_text("UV_SMTP_USER=fichier@example.org\nUV_SMTP_PASSWORD=x\n")
        settings = vc.load_settings(
            environ={"UV_SMTP_USER": "env@example.org", "UV_SMTP_PORT": "587",
                     "UV_ALERT_TO": "dest@example.org"},
            env_file=env_file,
        )
        assert settings["user"] == "env@example.org"
        assert settings["port"] == 587
        assert settings["to"] == "dest@example.org"

    def test_sans_mot_de_passe_non_configure(self, tmp_path):
        environ = {"UV_SMTP_USER": "moi@example.org"}
        assert vc.load_settings(environ=environ, env_file=tmp_path / "absent.env") is None


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.calls = host, port, []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append("quit")
        return False

    def starttls(self):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        self.calls.append(("send", msg["To"], msg["Subject"]))


class TestEnvoi:
    def test_port_465_en_ssl(self, monkeypatch):
        FakeSMTP.instances = []
        monkeypatch.setattr(vc.smtplib, "SMTP_SSL", FakeSMTP)
        vc.send_email(SETTINGS, "Objet", "Corps")
        smtp = FakeSMTP.instances[0]
        assert (smtp.host, smtp.port) == ("smtp.example.org", 465)
        assert smtp.calls == [("login", "u", "p"), ("send", "dest@example.org", "Objet"), "quit"]

    def test_port_587_en_starttls(self, monkeypatch):
        FakeSMTP.instances = []
        monkeypatch.setattr(vc.smtplib, "SMTP", FakeSMTP)
        vc.send_email({**SETTINGS, "port": 587}, "Objet", "Corps")
        assert FakeSMTP.instances[0].calls[:2] == ["starttls", ("login", "u", "p")]


class TestMain:
    def _prepare(self, conn, tmp_path):
        now = time.time()
        (tmp_path / "collect.log").write_text(_log_line(now - 60, *_ok(0)[1:]) + "\n")
        conn.execute("INSERT INTO collection_gaps VALUES (?, ?)", (int(now) - 600, int(now) - 360))
        conn.commit()

    def _args(self, db_path, tmp_path):
        return ["--db", str(db_path), "--log-dir", str(tmp_path), "--state", str(tmp_path / "state.json")]

    def test_dry_run_n_envoie_rien_et_n_ecrit_pas_l_etat(self, conn, db_path, tmp_path, monkeypatch, capsys):
        self._prepare(conn, tmp_path)
        monkeypatch.setattr(vc, "load_settings", lambda: SETTINGS)
        monkeypatch.setattr(vc, "send_email", lambda *a: pytest.fail("envoi en dry-run"))
        assert vc.main(["--dry-run"] + self._args(db_path, tmp_path)) == 0
        out = capsys.readouterr().out
        assert "ALERTE Trous de collecte" in out
        assert "Objet : [Urban Vision] Alerte collecte : Trous de collecte" in out
        assert not (tmp_path / "state.json").exists()

    def test_alerte_envoyee_une_seule_fois(self, conn, db_path, tmp_path, monkeypatch):
        self._prepare(conn, tmp_path)
        sent = []
        monkeypatch.setattr(vc, "load_settings", lambda: SETTINGS)
        monkeypatch.setattr(vc, "send_email", lambda settings, subject, body: sent.append(subject))
        assert vc.main(self._args(db_path, tmp_path)) == 0
        assert vc.main(self._args(db_path, tmp_path)) == 0
        assert sent == ["[Urban Vision] Alerte collecte : Trous de collecte"]
        state = json.loads((tmp_path / "state.json").read_text())["conditions"]
        assert list(state) == ["trous"]

    def test_echec_d_envoi_code_retour_1_et_alerte_conservee(self, conn, db_path, tmp_path, monkeypatch):
        self._prepare(conn, tmp_path)

        def refused(*args):
            raise smtplib.SMTPAuthenticationError(535, b"identifiants refuses")

        monkeypatch.setattr(vc, "load_settings", lambda: SETTINGS)
        monkeypatch.setattr(vc, "send_email", refused)
        assert vc.main(self._args(db_path, tmp_path)) == 1
        state = json.loads((tmp_path / "state.json").read_text())["conditions"]
        assert state["trous"]["last_sent"] is None

    def test_sans_configuration_code_retour_1(self, conn, db_path, tmp_path, monkeypatch):
        self._prepare(conn, tmp_path)
        monkeypatch.setattr(vc, "load_settings", lambda: None)
        assert vc.main(self._args(db_path, tmp_path)) == 1

    def test_email_de_test(self, monkeypatch, capsys):
        sent = []
        monkeypatch.setattr(vc, "load_settings", lambda: SETTINGS)
        monkeypatch.setattr(vc, "send_email", lambda settings, subject, body: sent.append(subject))
        assert vc.main(["--test-email"]) == 0
        assert sent == ["[Urban Vision] Email de test"]
        assert "dest@example.org" in capsys.readouterr().out

    def test_email_de_test_sans_configuration(self, monkeypatch):
        monkeypatch.setattr(vc, "load_settings", lambda: None)
        assert vc.main(["--test-email"]) == 2
