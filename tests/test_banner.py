"""The table page banner (ticket 12, D15): every viewer sees that a table is
synced and when; only the owner and admins see the sheet and the controls."""

from datetime import datetime, timedelta, timezone

import markupsafe
import pytest
import pytest_asyncio
from fixtures_import import DATA_DB
from fixtures_sheets import ALICE, BOB, make_datasette
from ulid import ULID

from datasette_google_sheets.banner import relative_time
from datasette_google_sheets.internal_db import InternalDB

ADMIN = {"id": "admin"}
SPREADSHEET_ID = "sheet-id-8c1f"
# User-controlled: must come out escaped, and only for the owner and admins.
TITLE = '<script>alert("x")</script> Grades & Co'
TAB = "Term <1>"
REASON = "The sheet's headers changed <b>badly</b>"
GENERIC = "Synced from Google Sheets"


def esc(value: str) -> str:
    """What Jinja's autoescape produces (markupsafe, ``"`` as ``&#34;``)."""
    return str(markupsafe.escape(value))


@pytest_asyncio.fixture
async def datasette(mock_google):
    return await make_datasette(
        mock_google,
        config={"permissions": {"google-sheets-admin": {"id": "admin"}}},
    )


@pytest.fixture
def idb(datasette):
    return InternalDB.for_datasette(datasette)


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) - delta).isoformat().replace("+00:00", "Z")


@pytest_asyncio.fixture
async def table(data_db):
    async def make(name: str) -> str:
        await data_db.execute_write(f"create table {name} (id integer primary key)")
        return name

    return make


@pytest_asyncio.fixture
async def synced(idb, import_link, table):
    """alice's synced ``students`` table, last synced 3 minutes ago."""
    await table("students")
    link = await import_link(
        "students", spreadsheet_id=SPREADSHEET_ID, gid=7, interval_minutes=10
    )
    return await idb.update_link(
        link.id,
        spreadsheet_title=TITLE,
        sheet_title=TAB,
        last_success_at=_iso(timedelta(minutes=3, seconds=5)),
    )


async def page(datasette, table, actor=None) -> str:
    response = await datasette.client.get(f"/{DATA_DB}/{table}", actor=actor)
    assert response.status_code == 200
    return response.text


def assert_no_details(text, link_id):
    """Nothing about the sheet or the link, raw or escaped."""
    for secret in (
        TITLE,
        esc(TITLE),
        "Grades",
        TAB,
        esc(TAB),
        SPREADSHEET_ID,
        "docs.google.com",
        link_id,
        "/-/google-sheets/links/",
        "Sync now",
        "Unlink",
        "headers changed",
    ):
        assert secret not in text, secret


# ---------------------------------------------------------------- synced


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", [None, BOB], ids=["anonymous", "other"])
async def test_other_viewers_see_the_generic_banner_only(datasette, synced, actor):
    text = await page(datasette, "students", actor)
    assert "Synced from Google Sheets · every 10 min" in text
    assert "3 min ago" in text
    assert f'title="{synced.last_success_at}"' in text
    assert "read-only in Datasette" in text
    assert 'class="message-info google-sheets-banner"' in text
    assert "Sync paused" not in text
    assert_no_details(text, synced.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", [ALICE, ADMIN], ids=["owner", "admin"])
async def test_owner_and_admin_see_the_sheet_and_controls(datasette, synced, actor):
    text = await page(datasette, "students", actor)
    assert GENERIC in text
    # Escaped, never raw.
    assert TITLE not in text
    assert TAB not in text
    assert esc(TITLE) in text
    assert esc(TAB) in text
    assert f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit#gid=7" in text
    detail = f'href="/-/google-sheets/links/{synced.id}"'
    assert f"{detail}>Sync now</a>" in text
    assert f"{detail}>Unlink</a>" in text
    # Links, not forms: no POST from this banner.
    banner = text.split("google-sheets-banner")[1].split("</div>")[0]
    assert "<form" not in banner


@pytest.mark.asyncio
async def test_paused_state_for_everyone_reason_for_owner_and_admin(
    datasette, synced, idb
):
    await idb.update_link(
        synced.id,
        status="paused",
        enabled=False,
        status_code="headers_changed",
        status_detail=REASON,
    )
    for actor in (None, BOB):
        text = await page(datasette, "students", actor)
        assert "⚠ Sync paused" in text
        assert 'class="message-warning google-sheets-banner"' in text
        assert_no_details(text, synced.id)
    for actor in (ALICE, ADMIN):
        text = await page(datasette, "students", actor)
        assert "⚠ Sync paused" in text
        assert f"Paused: {esc(REASON)}" in text
        assert REASON not in text


@pytest.mark.asyncio
async def test_error_state_reason_for_owner_only(datasette, synced, idb):
    await idb.update_link(synced.id, status="error", status_detail=REASON)
    text = await page(datasette, "students", ALICE)
    assert "Last sync failed, retrying on schedule" in text
    assert esc(REASON) in text
    text = await page(datasette, "students", BOB)
    assert "Sync paused" not in text
    assert_no_details(text, synced.id)


@pytest.mark.asyncio
async def test_not_synced_yet(datasette, import_link, table):
    await table("fresh")
    await import_link("fresh", interval_minutes=10)
    text = await page(datasette, "fresh")
    assert "every 10 min · not synced yet" in text


@pytest.mark.asyncio
async def test_interval_shows_the_floor(datasette, import_link, table):
    """A link stored below ``min_interval_minutes`` runs at the floor (D36)."""
    await table("fast")
    await import_link("fast", interval_minutes=2)
    assert "every 5 min" in await page(datasette, "fast")


# --------------------------------------------------------------- one-shot


@pytest_asyncio.fixture
async def imported(idb, import_link, table):
    await table("once")
    link = await import_link("once", spreadsheet_id=SPREADSHEET_ID)
    return await idb.update_link(
        link.id,
        spreadsheet_title=TITLE,
        sheet_title=TAB,
        created_table=True,
        last_success_at="2026-09-28T10:00:00.000Z",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", [ALICE, ADMIN], ids=["owner", "admin"])
async def test_one_shot_provenance_for_owner_and_admin(datasette, imported, actor):
    text = await page(datasette, "once", actor)
    assert "Imported from <a href=" in text
    assert esc(TITLE) in text
    assert TITLE not in text
    assert ">2026-09-28</time>" in text
    assert 'title="2026-09-28T10:00:00.000Z"' in text
    assert f'href="/-/google-sheets/links/{imported.id}"' in text
    assert GENERIC not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", [None, BOB], ids=["anonymous", "other"])
async def test_one_shot_provenance_hidden_from_others(datasette, imported, actor):
    text = await page(datasette, "once", actor)
    assert "google-sheets-banner" not in text
    assert "Imported from" not in text
    assert_no_details(text, imported.id)


@pytest.mark.asyncio
async def test_failed_one_shot_import_shows_nothing(datasette, import_link, table):
    await table("never")
    await import_link("never")
    assert "google-sheets-banner" not in await page(datasette, "never", ALICE)


@pytest.mark.asyncio
async def test_unlinked_sync_becomes_provenance(datasette, synced, idb):
    """Unlink clears the interval: the table is editable again and its owner
    sees where it came from."""
    await idb.update_link(synced.id, interval_minutes=None)
    assert "Imported from" in await page(datasette, "students", ALICE)
    assert "google-sheets-banner" not in await page(datasette, "students", BOB)


# ------------------------------------------------------------ other tables


@pytest.mark.asyncio
async def test_no_banner_on_unrelated_tables(datasette, synced, table, data_db):
    await table("plain")
    await data_db.execute_write("create view v as select * from students")
    for actor in (None, ALICE, ADMIN):
        assert "google-sheets-banner" not in await page(datasette, "plain", actor)
        assert "google-sheets-banner" not in await page(datasette, "v", actor)
    # The same table name in another database isn't synced.
    other = datasette.add_memory_database(f"banner_{ULID()}", name="other")
    await other.execute_write("create table students (id integer primary key)")
    response = await datasette.client.get("/other/students", actor=ALICE)
    assert "google-sheets-banner" not in response.text


# ----------------------------------------------------------- relative time


@pytest.mark.parametrize(
    "delta,expected",
    [
        (timedelta(seconds=10), "just now"),
        (timedelta(seconds=-30), "just now"),
        (timedelta(minutes=1), "1 min ago"),
        (timedelta(minutes=59, seconds=59), "59 min ago"),
        (timedelta(hours=1), "1 hour ago"),
        (timedelta(hours=23), "23 hours ago"),
        (timedelta(days=1), "1 day ago"),
        (timedelta(days=40), "40 days ago"),
    ],
)
def test_relative_time(delta, expected):
    now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    iso = (now - delta).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    assert relative_time(iso, now) == expected
