"""Fixtures for the import runner (ticket 07). Imported by conftest.py.

    async def test_something(datasette, data_db, import_cred, import_link, events):
        cred = await import_cred()                  # alice's service account, read-only
        link = await import_link("students", key="id")
        result = await run_import(datasette, link, cred, actor=ALICE)
        rows = await data_db.execute("select * from students")
        assert [e.name for e in events] == ["create-table", "insert-rows"]

``data_db`` is a mutable in-memory database called ``data``. ``import_link``
creates a link through ``InternalDB`` whose mapping keeps every header of
the tab as a TEXT column unless told otherwise.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
import pytest_asyncio
from datasette.app import Datasette
from datasette.database import Database
from datasette_google_credentials import Credential, get_credential
from fixtures_sheets import ALICE
from mock_google.oauth import SCOPE_SHEETS_RO
from ulid import ULID

from datasette_google_sheets.internal_db import (
    ColumnMapping,
    ImportMapping,
    InternalDB,
    Link,
)

DATA_DB = "data"
STUDENTS_HEADERS = ["id", "name", "grade_level", "email"]
STUDENTS_TYPES = {"id": "INTEGER", "grade_level": "INTEGER"}

__all__ = [
    "DATA_DB",
    "STUDENTS_HEADERS",
    "STUDENTS_TYPES",
    "data_db",
    "events",
    "import_cred",
    "import_link",
    "make_mapping",
]


def make_mapping(
    headers: list[str],
    *,
    types: dict[str, str] | None = None,
    key: str | None = None,
    rename: dict[str, str] | None = None,
    skip: tuple[str, ...] = (),
    headers_row: bool = True,
) -> ImportMapping:
    """A mapping over ``headers``: TEXT unless ``types`` says otherwise,
    ``rename`` maps source -> table name, ``key`` is a table name."""
    types = types or {}
    rename = rename or {}
    return ImportMapping(
        headers_row=headers_row,
        source_headers=headers,
        columns=[
            ColumnMapping(
                source=h,
                name=rename.get(h, h),
                type=types.get(h, "TEXT"),  # type: ignore[arg-type]
                skip=h in skip,
            )
            for h in headers
        ],
        key=key,
    )


@pytest_asyncio.fixture
async def data_db(datasette: Datasette) -> Database:
    """A mutable in-memory database named ``data``, private to this test."""
    return datasette.add_memory_database(f"import_{ULID()}", name=DATA_DB)


@pytest.fixture
def import_cred(
    datasette: Datasette, sa_credential
) -> Callable[..., Awaitable[Credential]]:
    """``await import_cred()``: alice's ``sa-test@`` service account (an
    editor on the fixture spreadsheets) with the import scope."""

    async def make(key: str = "test") -> Credential:
        info = await sa_credential("alice", key=key)
        return await get_credential(
            datasette, info.id, actor=ALICE, scopes=[SCOPE_SHEETS_RO]
        )

    return make


@pytest.fixture
def import_link(
    datasette: Datasette, data_db: Database
) -> Callable[..., Awaitable[Link]]:
    """``await import_link(table, spreadsheet_id="students", gid=0,
    mode="create", headers=STUDENTS_HEADERS, types=STUDENTS_TYPES, key=None,
    mapping=None, interval_minutes=None, **link_fields)``: an import link
    owned by alice into ``data``."""

    async def make(
        table: str,
        *,
        spreadsheet_id: str = "students",
        gid: int = 0,
        mode: str = "create",
        headers: list[str] | None = None,
        types: dict[str, str] | None = None,
        key: str | None = None,
        mapping: ImportMapping | None = None,
        interval_minutes: int | None = None,
        **fields: Any,
    ) -> Link:
        if mapping is None:
            mapping = make_mapping(
                headers or STUDENTS_HEADERS,
                types=STUDENTS_TYPES if types is None else types,
                key=key,
            )
        return await InternalDB.for_datasette(datasette).create_link(
            direction="import",
            mode=mode,  # type: ignore[arg-type]
            owner_id="alice",
            credential_id="unused",
            database_name=DATA_DB,
            table_name=table,
            spreadsheet_id=spreadsheet_id,
            sheet_gid=gid,
            mapping=mapping,
            interval_minutes=interval_minutes,
            **fields,
        )

    return make


@pytest.fixture
def events(datasette: Datasette, monkeypatch) -> list:
    """Every event Datasette tracks about the ``data`` database during the
    test, in order (creating a credential tracks google-credentials's own)."""
    tracked: list = []

    async def track_event(event) -> None:
        if getattr(event, "database", None) == DATA_DB:
            tracked.append(event)

    monkeypatch.setattr(datasette, "track_event", track_event)
    return tracked
