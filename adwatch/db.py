"""Database engine / session helpers.

Mock and live data live in SEPARATE database files, chosen by config.MODE at
call time. This keeps deterministic mock data (and its fake MOCK:: page ids)
from ever polluting real live data, and makes the dashboard show the dataset
that matches the selected mode.
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from . import config
from .models import Base

_engines: dict[str, object] = {}
_makers: dict[str, sessionmaker] = {}


def db_url_for_mode() -> str:
    """Live uses config.DB_URL (respects ADWATCH_DB_URL); mock uses a sibling file."""
    if config.MODE == "mock":
        return f"sqlite:///{config.DATA_DIR / 'adwatch_mock.db'}"
    return config.DB_URL


def _maker() -> sessionmaker:
    url = db_url_for_mode()
    if url not in _makers:
        eng = create_engine(url, future=True)
        _engines[url] = eng
        _makers[url] = sessionmaker(bind=eng, future=True, expire_on_commit=False)
    return _makers[url]


def SessionLocal() -> Session:
    """Return a new Session bound to the current mode's database."""
    return _maker()()


def init_db() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(_maker().kw["bind"])
