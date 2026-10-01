import argparse
import json
import os
import re
import smtplib
import socket
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "urban_vision.db"
STATE_PATH = DATA_DIR / "veille_collecte.json"
ENV_FILE = Path("/etc/urban-vision/alertes.env")
LOG_FILES = ("collect.log", "alerts.log")

HEARTBEAT_MAX_AGE_SECONDS = 600
GAP_WINDOW_SECONDS = 3600
LOG_WINDOW_SECONDS = 3600
LOG_MIN_LINES = 3
VOLUME_WINDOW_SECONDS = 2 * 3600
VOLUME_LAG_SECONDS = 600
VOLUME_WEEKS = (1, 2, 3)
VOLUME_MIN_BASELINE = 2000
VOLUME_MIN_RATIO = 0.2
REMINDER_SECONDS = 12 * 3600
LOG_TAIL_BYTES = 512 * 1024
SMTP_TIMEOUT_SECONDS = 30

LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \[(\w+)\] (.*)$")
NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


@dataclass
class Condition:
    key: str
    title: str
    active: bool
    details: str


def hm(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def day_hm(ts):
    return datetime.fromtimestamp(ts).strftime("%d/%m à %H:%M")


def duration(seconds):
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes:02d} min"
    days, hours = divmod(hours, 24)
    return f"{days} j {hours} h"


def read_tail(path, size=LOG_TAIL_BYTES):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            start = max(0, f.tell() - size)
            f.seek(start)
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[1:] if start else lines


def parse_log(lines):
    entries = []
    for line in lines:
        m = LOG_LINE.match(line)
        if m:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
            entries.append((ts, m.group(2), m.group(3)))
    return entries


def check_heartbeat(entries, now):
    last_ok = max(
        (ts for ts, level, msg in entries if level == "INFO" and msg.startswith("OK - ")),
        default=None,
    )
    if last_ok is None:
        return Condition("collecte_arretee", "Collecte arrêtée", True,
                         "Aucun relevé réussi dans la fin de data/collect.log.")
    age = now - last_ok
    return Condition(
        "collecte_arretee", "Collecte arrêtée", age > HEARTBEAT_MAX_AGE_SECONDS,
        f"Dernier relevé réussi le {day_hm(last_ok)} (il y a {duration(age)}).",
    )


def check_gaps(conn, now):
    rows = conn.execute(
        "SELECT gap_start, gap_end FROM collection_gaps WHERE gap_end >= ? ORDER BY gap_start",
        (int(now - 86400),),
    ).fetchall()
    recent = [(s, e) for s, e in rows if e >= now - GAP_WINDOW_SECONDS]
    day = f"Sur 24 h : {len(rows)} trou(s), {duration(sum(e - s for s, e in rows))} au total."
    if not recent:
        return Condition("trous", "Trous de collecte", False,
                         f"Aucun trou de collecte sur la dernière heure. {day}")
    start, end = max(recent, key=lambda gap: gap[1] - gap[0])
    return Condition(
        "trous", "Trous de collecte", True,
        f"{len(recent)} trou(s) de collecte sur la dernière heure, "
        f"{duration(sum(e - s for s, e in recent))} au total ; le plus long : "
        f"{duration(end - start)} (de {hm(start)} à {hm(end)}). {day}",
    )


def check_logs(entries_by_file, now):
    counts = {}
    for name, entries in entries_by_file.items():
        for ts, level, msg in entries:
            if (ts >= now - LOG_WINDOW_SECONDS and level in ("WARNING", "ERROR", "CRITICAL")
                    and not msg.startswith("Trou de collecte")):
                key = (name, level, NUMBER.sub("N", msg)[:120])
                counts[key] = counts.get(key, 0) + 1
    total = sum(counts.values())
    if not counts:
        return Condition("journaux", "Avertissements répétés dans les logs", False,
                         "Aucun avertissement sur la dernière heure.")
    lines = [f"{n} × {name} [{level}] {msg}"
             for (name, level, msg), n in sorted(counts.items(), key=lambda kv: -kv[1])]
    return Condition(
        "journaux", "Avertissements répétés dans les logs", total >= LOG_MIN_LINES,
        f"{total} avertissement(s) sur la dernière heure :\n" + "\n".join(lines),
    )


def count_passages(conn, start, end):
    return conn.execute(
        "SELECT COUNT(*) FROM observations WHERE departure_time >= ? AND departure_time < ?",
        (int(start.timestamp()), int(end.timestamp())),
    ).fetchone()[0]


def check_volume(conn, now):
    end = datetime.fromtimestamp(now - VOLUME_LAG_SECONDS)
    start = end - timedelta(seconds=VOLUME_WINDOW_SECONDS)
    current = count_passages(conn, start, end)
    refs = [count_passages(conn, start - timedelta(days=7 * k), end - timedelta(days=7 * k))
            for k in VOLUME_WEEKS]
    baseline = statistics.median(refs)
    active = baseline >= VOLUME_MIN_BASELINE and current < VOLUME_MIN_RATIO * baseline
    ratio = f", soit {100 * current / baseline:.0f} %" if baseline else ""
    details = (
        f"{current} passages enregistrés entre {start:%H:%M} et {end:%H:%M}, contre "
        f"{baseline:.0f} habituellement (médiane du même créneau les "
        f"{len(VOLUME_WEEKS)} semaines précédentes){ratio}."
    )
    if active:
        details += (" Le collecteur tourne mais le flux GTFS-RT TBM ne contient presque "
                    "plus de courses (incident côté TBM, grève ou jour férié).")
    return Condition("flux_pauvre", "Flux temps réel quasi vide", active, details)


def evaluate(db_path, log_dir, now):
    entries = {name: parse_log(read_tail(Path(log_dir) / name)) for name in LOG_FILES}
    conditions = [check_heartbeat(entries["collect.log"], now)]
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        try:
            conditions += [check_gaps(conn, now), check_volume(conn, now)]
        finally:
            conn.close()
    except sqlite3.Error as e:
        conditions.append(Condition("base", "Base de données illisible", True, str(e)))
    conditions.append(check_logs(entries, now))
    return conditions


def plan_notifications(conditions, state, now):
    notifications = []
    new_state = {}
    for cond in conditions:
        prev = state.get(cond.key, {})
        if cond.active:
            since = prev.get("since", now) if prev.get("active") else now
            last_sent = prev.get("last_sent") if prev.get("active") else None
            if last_sent is None:
                notifications.append(("alerte", cond, since))
            elif now - last_sent >= REMINDER_SECONDS:
                notifications.append(("rappel", cond, since))
            new_state[cond.key] = {"active": True, "since": since, "last_sent": last_sent}
        elif prev.get("active") and prev.get("last_sent") is not None:
            notifications.append(("fin", cond, prev.get("since", now)))
            new_state[cond.key] = prev
    return notifications, new_state


def mark_sent(state, notifications, now):
    for kind, cond, _ in notifications:
        if kind == "fin":
            state.pop(cond.key, None)
        else:
            state[cond.key]["last_sent"] = now
    return state


def compose(notifications, now, host=None):
    host = host or socket.gethostname()
    alerting = [cond.title for kind, cond, _ in notifications if kind != "fin"]
    if alerting:
        subject = "[Urban Vision] Alerte collecte : " + ", ".join(alerting)
    else:
        subject = "[Urban Vision] Retour à la normale : " + ", ".join(
            cond.title for _, cond, _ in notifications)
    labels = {"alerte": "NOUVELLE ALERTE", "rappel": "TOUJOURS EN COURS", "fin": "RÉSOLU"}
    parts = [f"Veille de la collecte Urban Vision ({host}), le {day_hm(now)}.", ""]
    for kind, cond, since in notifications:
        if kind == "fin":
            header = f"{labels[kind]} — {cond.title} (a duré {duration(now - since)})"
        else:
            header = f"{labels[kind]} — {cond.title} (depuis le {day_hm(since)})"
        parts += [header, "  " + cond.details.replace("\n", "\n  "), ""]
    parts += [
        "Pour diagnostiquer :",
        "  ssh ek-hub",
        "  tail -n 100 ~/Urban-Vision/data/collect.log",
        "  sudo systemctl status urban-vision-collect.service",
        "",
        "Envoyé par src/scripts/veille_collecte.py (timer urban-vision-veille-collecte.timer). "
        f"Rappel toutes les {REMINDER_SECONDS // 3600} h tant qu'un problème dure.",
    ]
    return subject, "\n".join(parts)


def read_env_file(path):
    values = {}
    try:
        text = Path(path).read_text()
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


def load_settings(environ=None, env_file=None):
    environ = os.environ if environ is None else environ
    values = read_env_file(env_file or environ.get("UV_ALERT_ENV_FILE", ENV_FILE))
    values.update({k: v for k, v in environ.items() if k.startswith("UV_")})
    user = values.get("UV_SMTP_USER", "").strip()
    password = "".join(values.get("UV_SMTP_PASSWORD", "").split())
    if not user or not password:
        return None
    return {
        "host": values.get("UV_SMTP_HOST", "smtp.gmail.com").strip(),
        "port": int(values.get("UV_SMTP_PORT", "465")),
        "user": user,
        "password": password,
        "from": values.get("UV_ALERT_FROM", user).strip(),
        "to": values.get("UV_ALERT_TO", user).strip(),
    }


def send_email(settings, subject, body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings["from"]
    msg["To"] = settings["to"]
    msg.set_content(body)
    if settings["port"] == 465:
        smtp = smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=SMTP_TIMEOUT_SECONDS)
    else:
        smtp = smtplib.SMTP(settings["host"], settings["port"], timeout=SMTP_TIMEOUT_SECONDS)
    with smtp:
        if settings["port"] != 465:
            smtp.starttls()
        smtp.login(settings["user"], settings["password"])
        smtp.send_message(msg)


def load_state(path):
    try:
        return json.loads(Path(path).read_text()).get("conditions", {})
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"conditions": state}, indent=2))
    tmp.replace(path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Veille de la collecte Urban Vision (alertes email).")
    ap.add_argument("--test-email", action="store_true", dest="test_email",
                    help="envoie un email de test puis s'arrête")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="affiche le diagnostic sans envoyer d'email ni enregistrer l'état")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--log-dir", default=str(DATA_DIR))
    ap.add_argument("--state", default=str(STATE_PATH))
    args = ap.parse_args(argv)
    settings = load_settings()

    if args.test_email:
        if settings is None:
            print("Envoi non configuré : UV_SMTP_USER et UV_SMTP_PASSWORD sont requis "
                  f"(variables d'environnement ou {ENV_FILE}).", file=sys.stderr)
            return 2
        send_email(settings, "[Urban Vision] Email de test",
                   "La veille de la collecte Urban Vision peut vous envoyer des alertes.")
        print(f"Email de test envoyé à {settings['to']}.")
        return 0

    now = time.time()
    conditions = evaluate(args.db, args.log_dir, now)
    for cond in conditions:
        print(f"{'ALERTE' if cond.active else 'ok':6s} {cond.title} — {cond.details}")
    notifications, state = plan_notifications(conditions, load_state(args.state), now)
    if args.dry_run:
        if notifications:
            subject, body = compose(notifications, now)
            print(f"\nEmail qui serait envoyé :\nObjet : {subject}\n\n{body}")
        return 0

    status = 0
    if notifications:
        subject, body = compose(notifications, now)
        if settings is None:
            print("Alerte non envoyée : UV_SMTP_USER et UV_SMTP_PASSWORD ne sont pas configurés.",
                  file=sys.stderr)
            status = 1
        else:
            try:
                send_email(settings, subject, body)
                state = mark_sent(state, notifications, now)
                print(f"Email envoyé à {settings['to']} : {subject}")
            except (OSError, smtplib.SMTPException) as e:
                print(f"Échec de l'envoi de l'email : {e}", file=sys.stderr)
                status = 1
    save_state(args.state, state)
    return status


if __name__ == "__main__":
    sys.exit(main())
