"""Export fixtures (ticket 08): a Datasette with a ``data`` database to
export from, and a helper that stores export links. Imported by conftest.py.

    async def test_something(export_datasette, export_link):
        link = await export_link(source_kind="table", table_name="items")

``data`` holds (``max_returned_rows`` is 10, so tables and views page):

| name      | kind                      | contents                                    |
|-----------|---------------------------|---------------------------------------------|
| items     | rowid table               | 25 rows ``n, s, b``: NULLs, a blob, a formula, JSON text |
| people    | table, ``id`` primary key | 3 rows                                      |
| items_v   | view                      | ``n, s`` of items with ``n <= 23``          |
| secret    | table                     | readable by alice only                      |
| wide      | table                     | 10 columns × 100 rows, for the cell cap     |
| by_n      | stored query              | ``n, s`` of items where ``n > :min``        |
| alice_only| stored query              | private, owned by alice                     |

The cell cap (``max_export_cells``) is 1,000: ``wide`` with its header is
1,010 cells, 1,000 without.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
import pytest_asyncio
from datasette.app import Datasette
from fixtures_google import MockGoogle
from fixtures_sheets import make_datasette
from ulid import ULID

from datasette_google_sheets.internal_db import InternalDB, Link

__all__ = [
    "EXPORT_MAX_CELLS",
    "FORMULA",
    "export_datasette",
    "export_link",
]

EXPORT_MAX_CELLS = 1_000
MAX_RETURNED_ROWS = 10
FORMULA = '=IMPORTXML("https://example.com", "//a")'
JSON_TEXT = json.dumps({"a": [1, 2]})

EXPORT_CONFIG: dict[str, Any] = {
    "settings": {"max_returned_rows": MAX_RETURNED_ROWS},
    "databases": {"data": {"tables": {"secret": {"allow": {"id": "alice"}}}}},
}


def _item(n: int) -> tuple[Any, ...]:
    s: Any = f"row {n}"
    b: Any = None
    if n == 2:
        s = None
    elif n == 3:
        s = FORMULA
    elif n == 4:
        s = JSON_TEXT
    if n == 1:
        b = b"\x00\xff"
    return (n, s, b)


async def seed_export_data(datasette: Datasette) -> None:
    # A unique memory name: memory databases with the same name share their
    # data across Datasette instances in one process.
    db = datasette.add_memory_database(f"export_{ULID()}", name="data")

    def seed(conn):
        conn.execute("create table items (n integer, s text, b blob)")
        conn.executemany(
            "insert into items values (?, ?, ?)", [_item(n) for n in range(1, 26)]
        )
        conn.execute("create table people (id text primary key, name text)")
        conn.executemany(
            "insert into people values (?, ?)",
            [("a", "Ann"), ("b", "Ben"), ("c", "Cat")],
        )
        conn.execute("create view items_v as select n, s from items where n <= 23")
        conn.execute("create table secret (x text)")
        conn.execute("insert into secret values ('hidden')")
        columns = [f"c{i}" for i in range(10)]
        conn.execute(
            f"create table wide ({columns[0]} integer primary key, "
            f"{', '.join(columns[1:])})"
        )
        conn.executemany(
            f"insert into wide values ({', '.join('?' * 10)})",
            [[r * 10 + c for c in range(10)] for r in range(100)],
        )

    await db.execute_write_fn(seed)
    await datasette.add_query(
        "data", "by_n", "select n, s from items where n > :min order by n"
    )
    await datasette.add_query(
        "data",
        "alice_only",
        "select n from items",
        is_private=True,
        owner_id="alice",
    )


@pytest_asyncio.fixture
async def export_datasette(mock_google: MockGoogle) -> Datasette:
    """``make_datasette`` with the ``data`` database, ``max_returned_rows``
    10 and a 1,000-cell export cap. Override ``datasette`` with it to get
    credentials on it (``sa_credential`` / ``oauth_credential``)."""
    datasette = await make_datasette(
        mock_google,
        config=EXPORT_CONFIG,
        sheets_config={"max_export_cells": EXPORT_MAX_CELLS},
    )
    await seed_export_data(datasette)
    return datasette


@pytest.fixture
def export_link(export_datasette: Datasette) -> Callable[..., Awaitable[Link]]:
    """``await export_link(credential_id=..., **fields)``: store an export
    link owned by alice in ``data``. Defaults to a ``replace`` export of the
    ``items`` table to the ``export`` spreadsheet's first tab (gid 0)."""
    idb = InternalDB.for_datasette(export_datasette)

    async def create(**fields: Any) -> Link:
        defaults: dict[str, Any] = {
            "direction": "export",
            "mode": "replace",
            "owner_id": "alice",
            "credential_id": "unused",
            "database_name": "data",
            "source_kind": "table",
            "table_name": "items",
            "spreadsheet_id": "export",
            "sheet_gid": 0,
        }
        return await idb.create_link(**{**defaults, **fields})

    return create
