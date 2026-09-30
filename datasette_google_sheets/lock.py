"""Synced tables are read-only: a table-level permission deny (D7).

For the six write actions on a table, ``permission_resources_sql`` returns a
deny row per synced table, for every actor (anonymous and root included).
Datasette resolves the most specific rule first and a deny beats an allow at
the same level (``utils/actions_sql.py``, ``check_permissions_for_actions``),
so a table-level deny wins over root's global allow and any ``datasette.yaml``
allow. That also blocks the JSON write API and ``execute-write-sql``, which
checks insert/update/delete-row per written table (``write_sql.py``,
``row_mutation_requirements``).

Core runs every plugin's permission SQL against the **internal** database
(``check_permissions_for_actions`` and ``build_allowed_resources_sql`` both
execute on ``get_internal_database()``), so the rule selects from our links
table directly. No cache: creating a sync, pausing it (still locked, D32) and
unlinking it (``interval_minutes`` cleared) take effect on the next check.

Our own sync writes use ``execute_write_fn`` and never call ``allowed()``, and
the runner checks a synced link's owner at the database level, which a
table-level rule can't touch (D35). No triggers in v1: direct Python writes
by other plugins and the sqlite3 CLI aren't covered.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from datasette.permissions import PermissionSQL

from .internal_db import LINKS

if TYPE_CHECKING:
    from datasette.app import Datasette

LOCKED_ACTIONS = frozenset(
    {
        "insert-row",
        "update-row",
        "delete-row",
        "alter-table",
        "drop-table",
        "set-column-type",
    }
)
REASON = "synced from Google Sheets"

# One SELECT with no CTE: core inlines plugin SQL into its own queries, where
# a leading WITH is a syntax error. Paused syncs stay locked (D32): the lock
# lasts until unlink clears interval_minutes.
_SYNCED_TABLES_SQL = f"""
SELECT database_name AS parent, table_name AS child, 0 AS allow,
  :google_sheets_lock_reason AS reason
FROM {LINKS}
WHERE direction = 'import' AND interval_minutes IS NOT NULL
"""

_READY = "_google_sheets_lock_ready"


async def _links_table_exists(datasette: Datasette) -> bool:
    """Our table appears when our ``startup`` applies the migrations. A
    permission check before that (another plugin's startup hook, a test that
    never starts Datasette) must not fail on a missing table; there are no
    links to lock then anyway. Once it exists it stays, so remember that."""
    if getattr(datasette, _READY, False):
        return True
    exists = await datasette.get_internal_database().table_exists(LINKS)
    if exists:
        setattr(datasette, _READY, True)
    return exists


def synced_table_deny(
    datasette: Datasette, action: str
) -> Callable[[], Awaitable[PermissionSQL | None]] | None:
    """The ``permission_resources_sql`` result: None for any other action
    (the hot path, no query), else an async callable core awaits for the
    deny rows (None before startup)."""
    if action not in LOCKED_ACTIONS:
        return None

    async def inner() -> PermissionSQL | None:
        if not await _links_table_exists(datasette):
            return None
        return PermissionSQL(
            sql=_SYNCED_TABLES_SQL,
            params={"google_sheets_lock_reason": REASON},
        )

    return inner
