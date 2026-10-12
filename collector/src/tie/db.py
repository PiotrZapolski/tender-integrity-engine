from __future__ import annotations

from importlib import resources

import psycopg
from psycopg.rows import dict_row


def connect(url: str) -> psycopg.Connection:
    return psycopg.connect(url, autocommit=False, row_factory=dict_row)


def migrate(conn: psycopg.Connection) -> list[str]:
    conn.execute(
        "create table if not exists schema_migrations ("
        " name text primary key, applied_at timestamptz not null default now())"
    )
    done = {r["name"] for r in conn.execute("select name from schema_migrations").fetchall()}
    files = sorted(
        (p for p in resources.files("tie.migrations").iterdir() if p.name.endswith(".sql")),
        key=lambda p: p.name,
    )
    applied = []
    for f in files:
        if f.name in done:
            continue
        conn.execute(f.read_text(encoding="utf-8"))
        conn.execute("insert into schema_migrations(name) values (%s)", (f.name,))
        applied.append(f.name)
    conn.commit()
    return applied
