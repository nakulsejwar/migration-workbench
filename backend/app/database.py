"""SQLite engine/session setup.

Every transaction starts with BEGIN IMMEDIATE (SQLAlchemy's documented pysqlite recipe). SQLite allows a single
writer anyway; taking the write lock up front means read-check-write sequences (claiming a run, rollback) are
serialised across requests/threads and can never interleave. busy_timeout makes waiting writers queue instead of failing.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> Engine:
    sqlite = url.startswith("sqlite")
    kwargs: dict = {}
    if sqlite:
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 15}
        if ":memory:" not in url:
            Path(url.split("///", 1)[-1]).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, **kwargs)
    if sqlite:
        @event.listens_for(engine, "connect")
        def _on_connect(dbapi_conn, _rec):
            dbapi_conn.isolation_level = None  # we control BEGIN ourselves
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=15000")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

        @event.listens_for(engine, "begin")
        def _on_begin(conn):
            conn.exec_driver_sql("BEGIN IMMEDIATE")
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=True)


def init_db(engine: Engine) -> None:
    from . import models  # noqa: F401  (register tables)
    Base.metadata.create_all(engine)
