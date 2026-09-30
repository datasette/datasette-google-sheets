"""The import runner (ticket 07) against the vendored mock Google."""

import ast
import logging
from pathlib import Path

import pytest
from datasette.utils import tilde_encode
from fixtures_import import STUDENTS_HEADERS, STUDENTS_TYPES, make_mapping
from fixtures_sheets import ALICE

from datasette_google_sheets import importer
from datasette_google_sheets.config import Config
from datasette_google_sheets.importer import (
    ImporterError,
    coerce,
    column_names,
    preview,
    run_import,
    suggest_key,
    to_records,
)
from datasette_google_sheets.internal_db import InternalDB

STUDENTS = [
    ["id", "name", "grade_level", "email"],
    [1, "Alice Chen", 10, "alice@school.edu"],
    [2, "Bob Jones", 11, "bob@school.edu"],
    [3, "Clara Smith", 10, "clara@school.edu"],
    [4, "David Kim", 12, "david@school.edu"],
    [5, "Eva Lopez", 11, "eva@school.edu"],
]


def values_calls(mock_google, spreadsheet_id="students"):
    return mock_google.calls(f"/v4/spreadsheets/{spreadsheet_id}/values", method="GET")


async def rows(db, table, order="rowid"):
    result = await db.execute(f"select rowid, * from [{table}] order by {order}")
    return [tuple(row) for row in result.rows]


async def persist(datasette, link, result):
    """What ticket 09 does after a successful run."""
    fields = {"last_hash": result.hash}
    if result.created_table:
        fields |= {"created_table": True, "created_schema": result.created_schema}
    return await InternalDB.for_datasette(datasette).update_link(link.id, **fields)


def seed(mock_google, spreadsheet_id, rows, **kwargs):
    """(Re)place a one-tab spreadsheet (gid 0) in the mock."""
    mock_google.sheets.add(spreadsheet_id, {"Sheet1": rows}, **kwargs)


# ------------------------------------------------------------ normalisation


def test_column_names():
    assert column_names([" a ", "", "A", "a", None], 6) == [
        "a",
        "column_2",
        "A_2",
        "a_3",
        "column_5",
        "column_6",
    ]


def test_to_records_pads_drops_blank_rows_and_numbers_rows():
    sheet = to_records([["a", "b"], [1], ["", ""], [], ["x", "", 3]], headers_row=True)
    assert sheet.headers == ["a", "b", "column_3"]
    assert sheet.records == [
        {"a": 1, "b": None, "column_3": None},
        {"a": "x", "b": None, "column_3": 3},
    ]
    assert sheet.row_numbers == [2, 5]
    no_header = to_records([["a", "b"], [1, 2]], headers_row=False)
    assert no_header.headers == ["column_1", "column_2"]
    assert no_header.row_numbers == [1, 2]


@pytest.mark.parametrize(
    "value,type,expected",
    [
        (None, "INTEGER", (None, True)),
        (3, "INTEGER", (3, True)),
        (3.0, "INTEGER", (3, True)),
        ("12", "INTEGER", (12, True)),
        (True, "INTEGER", (1, True)),
        (3.5, "INTEGER", (3.5, False)),
        ("n/a", "INTEGER", ("n/a", False)),
        (2**70, "INTEGER", (2**70, False)),
        (3, "REAL", (3.0, True)),
        ("1.5", "REAL", (1.5, True)),
        ("nan", "REAL", ("nan", False)),
        ("abc", "REAL", ("abc", False)),
        (10, "TEXT", ("10", True)),
        (True, "TEXT", ("TRUE", True)),
        ("x", "TEXT", ("x", True)),
    ],
)
def test_coerce(value, type, expected):
    assert coerce(value, type) == expected


def test_suggest_key():
    records = [
        {"code": "a", "ID": 1, "n": 1},
        {"code": "b", "ID": 1, "n": 2},
    ]
    # ID is repeated, so the first unique, filled-in column wins.
    assert suggest_key(["code", "ID", "n"], records) == "code"
    assert suggest_key(["n", "ID"], [{"n": 1, "ID": 7}, {"n": 2, "ID": 8}]) == "ID"
    assert suggest_key(["a"], [{"a": None}, {"a": 1}]) is None
    assert suggest_key(["a"], []) is None


# ------------------------------------------------------------------ preview


@pytest.mark.asyncio
async def test_preview_students(datasette, import_cred):
    cred = await import_cred()
    result = await preview(cred, "students", 0, rows=2)
    assert result.spreadsheet_title == "Students"
    assert result.tab.title == "students"
    assert result.headers == STUDENTS_HEADERS
    assert result.records == [
        {"id": 1, "name": "Alice Chen", "grade_level": 10, "email": "alice@school.edu"},
        {"id": 2, "name": "Bob Jones", "grade_level": 11, "email": "bob@school.edu"},
    ]
    assert result.total_rows == 5
    assert result.types == {
        "id": "INTEGER",
        "name": "TEXT",
        "grade_level": "INTEGER",
        "email": "TEXT",
    }
    assert result.key == "id"
    assert result.cells == 1000 * 26


@pytest.mark.asyncio
async def test_preview_types_use_every_row_not_just_the_preview(
    datasette, mock_google, import_cred
):
    seed(mock_google, "mixed", [["n", "code"], [1, 1], [2, "B"], [2.5, "C"]])
    result = await preview(await import_cred(), "mixed", 0, rows=1)
    assert result.records == [{"n": 1, "code": 1}]
    assert result.types == {"n": "REAL", "code": "TEXT"}
    # n repeats nothing but has 1 and 2 then 2.5: unique, so it's the key.
    assert result.key == "n"


@pytest.mark.asyncio
async def test_preview_blank_and_duplicate_headers(datasette, import_cred):
    result = await preview(await import_cred(), "ragged", 0)
    assert result.headers == ["name", "column_2", "name_2", "score", "column_5"]
    assert result.records == [
        {
            "name": "alice",
            "column_2": "x",
            "name_2": "a2",
            "score": 10,
            "column_5": None,
        },
        {
            "name": "bob",
            "column_2": None,
            "name_2": None,
            "score": None,
            "column_5": None,
        },
        {
            "name": "carol",
            "column_2": None,
            "name_2": None,
            "score": 30,
            "column_5": "extra",
        },
    ]
    assert result.key == "name"


@pytest.mark.asyncio
async def test_preview_without_header_row(datasette, import_cred):
    result = await preview(await import_cred(), "students", 1001, headers_row=False)
    assert result.headers == ["column_1", "column_2", "column_3"]
    assert result.total_rows == 3
    assert result.types["column_1"] == "TEXT"


@pytest.mark.asyncio
async def test_preview_errors(datasette, mock_google, import_cred):
    cred = await import_cred()
    with pytest.raises(ImporterError) as missing:
        await preview(cred, "students", 999)
    assert missing.value.code == "tab_missing"
    with pytest.raises(ImporterError) as large:
        await preview(cred, "students", 0, max_cells=100)
    assert large.value.code == "too_large"
    assert values_calls(mock_google) == []


# ----------------------------------------------------------------- create


@pytest.mark.asyncio
async def test_create(datasette, data_db, import_cred, import_link, events):
    link = await import_link("students", key="id")
    result = await run_import(datasette, link, await import_cred(), actor=ALICE)
    assert result.status == "success"
    assert (result.rows_read, result.rows_written, result.added) == (5, 5, 5)
    assert (result.changed, result.removed) == (0, 0)
    assert result.cells == 5 * 4
    assert result.table == "students"
    assert result.spreadsheet_title == "Students"
    assert result.sheet_title == "students"
    assert result.warnings == []
    assert result.created_table is True
    schema = (
        await data_db.execute("select sql from sqlite_master where name = 'students'")
    ).single_value()
    assert result.created_schema == schema
    assert "INTEGER PRIMARY KEY" in schema
    assert await rows(data_db, "students") == [(r[0], *r) for r in STUDENTS[1:]]
    assert [e.name for e in events] == ["create-table", "insert-rows"]
    assert all(e.actor == ALICE for e in events)
    assert events[0].table == "students" and events[0].schema == schema
    assert events[1].num_rows == 5


@pytest.mark.asyncio
async def test_create_refuses_an_existing_table(
    datasette, data_db, import_cred, import_link, events
):
    await data_db.execute_write("create table Students (x)")
    link = await import_link("students")
    with pytest.raises(ImporterError) as error:
        await run_import(datasette, link, await import_cred())
    assert error.value.code == "table_exists"
    assert events == []


@pytest.mark.asyncio
async def test_rename_and_skip(datasette, data_db, import_cred, import_link):
    mapping = make_mapping(
        STUDENTS_HEADERS,
        types=STUDENTS_TYPES,
        rename={"name": "full_name"},
        skip=("email",),
    )
    link = await import_link("students", mapping=mapping)
    await run_import(datasette, link, await import_cred())
    result = await data_db.execute("select * from students order by rowid limit 1")
    assert result.columns == ["id", "full_name", "grade_level"]
    assert tuple(result.first()) == (1, "Alice Chen", 10)


# -------------------------------------------------------------- append


@pytest.mark.asyncio
async def test_append(datasette, data_db, import_cred, import_link, events):
    await data_db.execute_write(
        "create table people (ID integer, Name text, grade_level integer,"
        " email text, notes text)"
    )
    await data_db.execute_write("insert into people (ID, Name) values (0, 'Zed')")
    link = await import_link("People", mode="append")
    result = await run_import(datasette, link, await import_cred(), actor=ALICE)
    assert (result.added, result.rows_written, result.removed) == (5, 5, 0)
    assert result.table == "people"
    assert len(await rows(data_db, "people")) == 6
    assert [(e.name, e.table, e.num_rows) for e in events] == [
        ("insert-rows", "people", 5)
    ]


@pytest.mark.asyncio
async def test_append_refuses_new_columns(datasette, data_db, import_cred, import_link):
    await data_db.execute_write("create table people (id integer, name text)")
    link = await import_link("people", mode="append")
    with pytest.raises(ImporterError) as error:
        await run_import(datasette, link, await import_cred())
    assert error.value.code == "table_changed"
    assert error.value.data == {
        "table": "people",
        "missing_columns": ["grade_level", "email"],
    }
    assert await rows(data_db, "people") == []


# ------------------------------------------------------------- replace


@pytest.mark.asyncio
async def test_replace(datasette, data_db, import_cred, import_link, events):
    await data_db.execute_write(
        "create table people (id integer, name text, grade_level integer, email text)"
    )
    await data_db.execute_write_many(
        "insert into people (id, name) values (?, ?)", [(90, "old"), (91, "older")]
    )
    link = await import_link("people", mode="replace")
    result = await run_import(datasette, link, await import_cred())
    assert (result.added, result.removed, result.rows_written) == (5, 2, 5)
    assert [r[1] for r in await rows(data_db, "people")] == [1, 2, 3, 4, 5]
    assert [e.name for e in events] == ["insert-rows"]


@pytest.mark.asyncio
async def test_rerun_without_key_replaces(
    datasette, mock_google, data_db, import_cred, import_link
):
    cred = await import_cred()
    link = await import_link("students")
    link = await persist(datasette, link, await run_import(datasette, link, cred))
    seed(mock_google, "students", [STUDENTS[0], STUDENTS[3], STUDENTS[1]])
    result = await run_import(datasette, link, cred)
    assert (result.added, result.removed) == (2, 5)
    assert [r[1] for r in await rows(data_db, "students")] == [3, 1]


# -------------------------------------------------------------- upsert


@pytest.mark.asyncio
async def test_keyed_rerun_upserts_and_keeps_rowids(
    datasette, mock_google, data_db, import_cred, import_link, events
):
    """A keyed re-run of a created table (a sync): added, changed and
    removed counts; untouched and updated rows keep their rowids, so their
    row URLs keep working."""
    cred = await import_cred()
    link = await import_link("students", key="email")
    link = await persist(datasette, link, await run_import(datasette, link, cred))
    before = {r[4]: r[0] for r in await rows(data_db, "students")}
    events.clear()

    seed(
        mock_google,
        "students",
        [
            STUDENTS[0],
            STUDENTS[1],  # unchanged
            [2, "Bob Jones", 12, "bob@school.edu"],  # changed
            STUDENTS[4],  # unchanged
            STUDENTS[5],  # unchanged
            [6, "Fay Wu", 9, "fay@school.edu"],  # added
            # clara removed
        ],
    )
    result = await run_import(datasette, link, cred, actor=ALICE)
    assert (result.added, result.changed, result.removed) == (1, 1, 1)
    assert result.rows_written == 2
    after = {r[4]: r[0] for r in await rows(data_db, "students")}
    for email in ("alice@school.edu", "bob@school.edu", "david@school.edu"):
        assert after[email] == before[email]
    assert "clara@school.edu" not in after
    assert (
        await data_db.execute(
            "select grade_level from students where email = 'bob@school.edu'"
        )
    ).single_value() == 12
    assert [e.name for e in events] == ["delete-row", "upsert-rows"]
    assert events[0].pks == ["clara@school.edu"]
    assert events[1].num_rows == 2 and events[1].actor == ALICE

    row_url = datasette.urls.row("data", "students", tilde_encode("bob@school.edu"))
    response = await datasette.client.get(row_url + ".json?_shape=objects")
    assert response.status_code == 200
    assert response.json()["rows"][0]["grade_level"] == 12


@pytest.mark.asyncio
async def test_upsert_existing_table(
    datasette, mock_google, data_db, import_cred, import_link
):
    """One-shot upsert into a table this plugin didn't create: no UNIQUE
    constraint needed, keys compared as the mapping types them."""
    await data_db.execute_write(
        "create table people (id text, name text, grade_level integer, email text)"
    )
    await data_db.execute_write_many(
        "insert into people (id, name, grade_level) values (?, ?, ?)",
        [("1", "Alice Chen", 10), ("2", "Old Bob", 11), ("9", "Gone", 1)],
    )
    link = await import_link(
        "people", mode="upsert", types={"grade_level": "INTEGER"}, key="id"
    )
    result = await run_import(datasette, link, await import_cred())
    assert (result.added, result.changed, result.removed) == (3, 2, 1)
    people = await rows(data_db, "people")
    assert [r[:3] for r in people[:2]] == [
        (1, "1", "Alice Chen"),
        (2, "2", "Bob Jones"),
    ]
    assert [r[1] for r in people] == ["1", "2", "3", "4", "5"]


@pytest.mark.asyncio
async def test_unchanged_values_are_not_counted_as_changed(
    datasette, mock_google, data_db, import_cred, import_link
):
    """Values SQLite stores differently from how Sheets sends them (a
    number in a TEXT column, a mismatch in an INTEGER one) aren't changes."""
    seed(mock_google, "loose", [["k", "t", "n"], ["a", 10, "n/a"], ["b", 1.5, 2]])
    link = await import_link(
        "loose",
        spreadsheet_id="loose",
        headers=["k", "t", "n"],
        types={"n": "INTEGER"},
        key="k",
    )
    cred = await import_cred()
    link = await persist(datasette, link, await run_import(datasette, link, cred))
    result = await run_import(datasette, link, cred, force=True)
    assert (result.added, result.changed, result.removed) == (0, 0, 0)


# ------------------------------------------------------------ strict rules


@pytest.mark.asyncio
async def test_reordered_columns_are_fine(
    datasette, mock_google, data_db, import_cred, import_link
):
    cred = await import_cred()
    link = await import_link("students", key="id")
    link = await persist(datasette, link, await run_import(datasette, link, cred))
    seed(mock_google, "students", [[r[3], r[0], r[2], r[1]] for r in STUDENTS])
    result = await run_import(datasette, link, cred)
    assert result.status == "no_change"  # same records, same mapping


@pytest.mark.parametrize(
    "header,added,removed",
    [
        (["id", "name", "grade_level", "email", "phone"], ["phone"], []),
        (["id", "name", "grade_level"], [], ["email"]),
        (["id", "Name!", "grade_level", "email"], ["Name!"], ["name"]),
    ],
)
@pytest.mark.asyncio
async def test_headers_changed(
    datasette, mock_google, data_db, import_cred, import_link, header, added, removed
):
    seed(mock_google, "students", [header, [1, "a", 2, "b", "c"][: len(header)]])
    link = await import_link("students")
    with pytest.raises(ImporterError) as error:
        await run_import(datasette, link, await import_cred())
    assert error.value.code == "headers_changed"
    assert error.value.data == {"added": added, "removed": removed}


@pytest.mark.parametrize(
    "values", [[], [STUDENTS[0]], [STUDENTS[0], [], ["", "", "", ""]]]
)
@pytest.mark.asyncio
async def test_empty_sheet(
    datasette, mock_google, data_db, import_cred, import_link, values
):
    seed(mock_google, "students", values)
    link = await import_link("students")
    with pytest.raises(ImporterError) as error:
        await run_import(datasette, link, await import_cred())
    assert error.value.code == "empty_sheet"


@pytest.mark.asyncio
async def test_bad_key_names_rows(
    datasette, mock_google, data_db, import_cred, import_link, events
):
    seed(
        mock_google,
        "students",
        [
            STUDENTS[0],
            [1, "Alice Chen", 10, "a@x"],
            ["", "No Id", 10, "b@x"],  # row 3: blank
            [1, "Alice Again", 10, "c@x"],  # row 4: repeats row 2
            ["seven", "Word Id", 10, "d@x"],  # row 5: not INTEGER
            [5.0, "Float Id", 10, "e@x"],  # fine: 5
        ],
    )
    link = await import_link("students", key="id")
    with pytest.raises(ImporterError) as error:
        await run_import(datasette, link, await import_cred())
    assert error.value.code == "bad_key"
    assert error.value.data == {
        "key": "id",
        "blank_rows": [3],
        "duplicate_rows": [2, 4],
        "invalid_rows": [5],
    }
    message = error.value.message
    assert "rows 3" in message and "rows 2, 4" in message and "rows 5" in message
    for value in ("Alice", "seven", "a@x"):
        assert value not in message
    assert await data_db.table_names() == []


@pytest.mark.asyncio
async def test_bad_key_names_at_most_ten_rows(
    datasette, mock_google, data_db, import_cred, import_link
):
    seed(mock_google, "students", [STUDENTS[0], *[["", "x", 1, "y"]] * 12])
    link = await import_link("students", key="id")
    with pytest.raises(ImporterError) as error:
        await run_import(datasette, link, await import_cred())
    assert error.value.data["blank_rows"] == list(range(2, 12))
    assert "and more" in error.value.message


@pytest.mark.asyncio
async def test_too_large_fetches_no_values(
    datasette, mock_google, data_db, import_cred, import_link, monkeypatch
):
    monkeypatch.setattr(
        datasette, "_google_sheets_config", Config(max_import_cells=26_000 - 1)
    )
    link = await import_link("students")
    with pytest.raises(ImporterError) as error:
        await run_import(datasette, link, await import_cred())
    assert error.value.code == "too_large"
    assert error.value.data == {"cells": 26_000, "max_cells": 25_999}
    assert values_calls(mock_google) == []


@pytest.mark.asyncio
async def test_tab_missing(datasette, data_db, import_cred, import_link):
    link = await import_link("students", gid=42)
    with pytest.raises(ImporterError) as error:
        await run_import(datasette, link, await import_cred())
    assert error.value.code == "tab_missing"


@pytest.mark.asyncio
async def test_database_missing(datasette, data_db, import_cred, import_link):
    link = await import_link("students")
    datasette.remove_database("data")
    with pytest.raises(ImporterError) as error:
        await run_import(datasette, link, await import_cred())
    assert error.value.code == "database_missing"


@pytest.mark.asyncio
async def test_type_mismatch_warns_and_stores_as_is(
    datasette, mock_google, data_db, import_cred, import_link
):
    seed(
        mock_google,
        "students",
        [
            STUDENTS[0],
            [1, "A", "ten", "a@x"],
            [2, "B", "n/a", "b@x"],
            [3, "C", 9.5, "c"],
        ],
    )
    link = await import_link("students", key="id")
    result = await run_import(datasette, link, await import_cred())
    assert result.status == "success"
    assert result.warnings == [
        "Column 'grade_level': 3 values not INTEGER, stored as they are"
    ]
    assert [r[3] for r in await rows(data_db, "students")] == ["ten", "n/a", 9.5]


# ------------------------------------------------------- no_change / force


@pytest.mark.asyncio
async def test_no_change_skips_the_write_and_force_writes(
    datasette, data_db, import_cred, import_link, events
):
    cred = await import_cred()
    link = await import_link("students", key="id")
    first = await run_import(datasette, link, cred)
    link = await persist(datasette, link, first)
    events.clear()

    again = await run_import(datasette, link, cred)
    assert again.status == "no_change"
    assert again.hash == first.hash
    assert (again.rows_read, again.rows_written, again.added) == (5, 0, 0)
    assert events == []

    forced = await run_import(datasette, link, cred, force=True)
    assert forced.status == "success"
    assert (forced.added, forced.changed, forced.removed) == (0, 0, 0)


@pytest.mark.asyncio
async def test_mapping_change_changes_the_hash(
    datasette, data_db, import_cred, import_link
):
    cred = await import_cred()
    link = await import_link("students", key="id")
    link = await persist(datasette, link, await run_import(datasette, link, cred))
    mapping = make_mapping(
        STUDENTS_HEADERS,
        types={**STUDENTS_TYPES, "email": "TEXT"},
        key="id",
        skip=("email",),
    )
    link = await InternalDB.for_datasette(datasette).update_link(
        link.id, mapping=mapping
    )
    result = await run_import(datasette, link, cred)
    assert result.status == "success"


@pytest.mark.asyncio
async def test_no_change_still_notices_a_dropped_table(
    datasette, data_db, import_cred, import_link
):
    cred = await import_cred()
    link = await import_link("students", key="id")
    link = await persist(datasette, link, await run_import(datasette, link, cred))
    await data_db.execute_write("drop table students")
    with pytest.raises(ImporterError) as error:
        await run_import(datasette, link, cred)
    assert error.value.code == "table_missing"


@pytest.mark.asyncio
async def test_table_changed_on_sync(datasette, data_db, import_cred, import_link):
    cred = await import_cred()
    link = await import_link("students", key="id", interval_minutes=10)
    link = await persist(datasette, link, await run_import(datasette, link, cred))
    await data_db.execute_write("alter table students rename column email to mail")
    with pytest.raises(ImporterError) as error:
        await run_import(datasette, link, cred, force=True)
    assert error.value.code == "table_changed"
    assert error.value.data["missing_columns"] == ["email"]


@pytest.mark.asyncio
async def test_table_missing_for_existing_table_modes(
    datasette, data_db, import_cred, import_link
):
    for mode in ("append", "replace", "upsert"):
        link = await import_link(f"nope_{mode}", mode=mode, key="id")
        with pytest.raises(ImporterError) as error:
            await run_import(datasette, link, await import_cred())
        assert error.value.code == "table_missing"


# ------------------------------------------------------------- atomicity


@pytest.mark.parametrize("key", [None, "id"])
@pytest.mark.asyncio
async def test_a_failed_write_leaves_the_table_as_it_was(
    datasette, mock_google, data_db, import_cred, import_link, events, key
):
    """A failure after the delete, part-way through the inserts, rolls the
    whole run back: replace (no key) and upsert (key) alike."""
    cred = await import_cred()
    link = await import_link("students", key=key)
    link = await persist(datasette, link, await run_import(datasette, link, cred))
    before = await rows(data_db, "students")
    await data_db.execute_write(
        "create trigger boom before insert on students when new.name = 'Boom'"
        " begin select raise(abort, 'injected failure'); end"
    )
    events.clear()
    seed(
        mock_google,
        "students",
        [
            STUDENTS[0],
            STUDENTS[1],
            [2, "Bobby", 11, "b@x"],
            [7, "New", 9, "n@x"],
            [8, "Boom", 9, "x@x"],
        ],
    )
    with pytest.raises(ImporterError) as error:
        await run_import(datasette, link, cred)
    assert error.value.code == "write_failed"
    assert error.value.data == {"transient": False}
    assert "injected failure" in error.value.message
    assert await rows(data_db, "students") == before
    assert events == []


# --------------------------------------------------------------- privacy


def test_never_logs():
    tree = ast.parse(Path(importer.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
            assert "logging" not in names
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "print"


@pytest.mark.asyncio
async def test_no_cell_values_in_logs(
    datasette, mock_google, data_db, import_cred, import_link, caplog
):
    caplog.set_level(logging.DEBUG)
    cred = await import_cred()
    link = await import_link("students", key="id")
    link = await persist(datasette, link, await run_import(datasette, link, cred))
    seed(mock_google, "students", [STUDENTS[0], STUDENTS[1], STUDENTS[1]])
    with pytest.raises(ImporterError):
        await run_import(datasette, link, cred)
    for value in ("Alice Chen", "alice@school.edu", "Bob Jones"):
        assert value not in caplog.text
