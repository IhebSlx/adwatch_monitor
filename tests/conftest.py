"""Gemeinsame Fixtures und Testdaten fuer alle Testdateien.

Lag bis eben in test_core.py, zusammen mit 197 Tests in einer Datei von
6.000 Zeilen. Beim Aufteilen muss die Fixture an einen Ort, den pytest von
sich aus in jede Datei traegt -- das ist conftest.py.
"""
import datetime as dt   # noqa: F401 -- von Testdateien genutzt
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """Point the app at a throwaway SQLite file for this test.

    ADWATCH_DATA_DIR is redirected too, not just the DB URL: init_db() takes a
    startup backup, and without this every test wrote a 4 KB snapshot of its empty
    temp database into the REAL data/backups/ and then rotated — so running the
    test suite silently destroyed the production backups. 13 of 14 retained
    backups were test junk before this was fixed.
    """
    db = tmp_path / "t.db"
    monkeypatch.setenv("ADWATCH_DB_URL", f"sqlite:///{db}")
    monkeypatch.setenv("ADWATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADWATCH_BACKUP_DIR", str(tmp_path / "backups"))
    # rebuild the engine bound to the temp URL
    import importlib
    from adwatch import config as cfg
    importlib.reload(cfg)
    from adwatch import db as dbmod
    importlib.reload(dbmod)
    dbmod.init_db()
    yield dbmod
