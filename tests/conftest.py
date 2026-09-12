import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src" / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "dashboard"))
sys.path.insert(0, str(PROJECT_ROOT / "reports"))


@pytest.fixture
def db_path(tmp_path):
    """Chemin d'une base SQLite temporaire, isolée de la base réelle."""
    return tmp_path / "uv_test.db"


@pytest.fixture
def conn(db_path):
    """Connexion sur base temporaire avec le schéma réel (init_db)."""
    import db as dbio

    c = sqlite3.connect(str(db_path))
    c.execute("PRAGMA journal_mode=WAL;")
    dbio.init_db(c)
    yield c
    c.close()