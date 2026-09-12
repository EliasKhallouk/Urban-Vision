import sqlite3

import pytest
from google.transit import gtfs_realtime_pb2 as gtfs

from gtfs_factory import alert_feed


def _feed_alertes():
    return alert_feed(
        [
            {
                "id": "a1",
                "header": "Ligne A interrompue",
                "description": "Travaux",
                "cause": gtfs.Alert.Cause.MAINTENANCE,
                "routes": ["A", "B"],
                "periods": [(100, 200)],
            },
            {
                "id": "a2",
                "header": "Perturbation generalisee",
                "routes": [""],
                "periods": [],
            },
        ]
    )


class TestProcessFeed:
    def test_enregistre_alertes_par_route_et_periode(self, conn):
        import collect_alerts as ca

        n = ca.process_feed(conn, _feed_alertes())
        assert n == 3  # a1 sur A + B, a2 sur route vide

        rows = conn.execute(
            "SELECT alert_id, route_id, active_period_start, active_period_end, "
            "header_text, description_text, cause, last_seen_at "
            "FROM service_alerts ORDER BY alert_id, route_id"
        ).fetchall()

        assert rows[0][:4] == ("a1", "A", 100, 200)
        assert rows[0][4] == "Ligne A interrompue"
        assert rows[0][5] == "Travaux"
        assert rows[0][6] == int(gtfs.Alert.Cause.MAINTENANCE)
        assert rows[1][:4] == ("a1", "B", 100, 200)

        # sans période -> période illimitée (start=0, end=NULL)
        assert rows[2][:4] == ("a2", "", 0, None)
        assert rows[2][4] == "Perturbation generalisee"

    def test_upsert_met_a_jour_sans_dupliquer(self, conn):
        import collect_alerts as ca

        ca.process_feed(conn, _feed_alertes())
        nouveau = alert_feed(
            [{"id": "a1", "header": "NOUVEAU TEXTE", "routes": ["A", "B"],
              "periods": [(100, 300)]}]
        )
        ca.process_feed(conn, nouveau)

        count = conn.execute("SELECT COUNT(*) FROM service_alerts").fetchone()[0]
        assert count == 3

        row = conn.execute(
            "SELECT header_text, active_period_end FROM service_alerts "
            "WHERE alert_id = 'a1' AND route_id = 'A'"
        ).fetchone()
        assert row == ("NOUVEAU TEXTE", 300)


class TestRecuperationApresVerrou:
    def test_rollback_et_reprise_apres_database_is_locked(self, db_path):
        """Reproduit le bug de prod : un écrivain tient le verrou pendant que le
        service d'alertes écrit. Avec rollback (comme dans main()), le service
        se récupère au cycle suivant sans état corrompu."""
        import db as dbio
        import collect_alerts as ca

        # base temporaire sans schéma : on l'initialise d'abord
        c = sqlite3.connect(str(db_path))
        dbio.init_db(c)
        c.close()

        # Ecrivain qui tient le verrou d'écriture
        writer = sqlite3.connect(str(db_path))

        # Connexion "alertes" configurée comme en prod ; on pose les pragmas
        # AVANT que l'écrivain prenne le verrou (sinon la bascule WAL échoue).
        alerts = sqlite3.connect(str(db_path))
        alerts.execute("PRAGMA journal_mode=WAL;")
        alerts.execute("PRAGMA busy_timeout = 50")  # court : échec rapide si verrou tenu

        writer.execute("BEGIN")
        writer.execute(
            "INSERT INTO service_alerts VALUES ('held','0',0,0,'h','d',0,0)"
        )

        feed = _feed_alertes()
        with pytest.raises(sqlite3.OperationalError):
            ca.process_feed(alerts, feed)
        alerts.rollback()  # ce que fait main() dans son except

        writer.commit()  # le verrou est libéré

        n = ca.process_feed(alerts, feed)
        assert n == 3
        assert alerts.execute(
            "SELECT COUNT(*) FROM service_alerts"
        ).fetchone()[0] >= 3
        alerts.close()
        writer.close()


class TestConfig:
    def test_service_configure_busy_timeout(self, db_path):
        import collect_alerts as ca

        assert ca.DB_BUSY_TIMEOUT_MS == 180_000
        c = sqlite3.connect(str(db_path))
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute(f"PRAGMA busy_timeout = {ca.DB_BUSY_TIMEOUT_MS};")
        assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 180_000
        c.close()