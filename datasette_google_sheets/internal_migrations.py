"""Internal-database schema for datasette-google-sheets (via sqlite-migrate).

Append-only: never edit an applied migration, add a new ``m00N_...`` one.

Two tables:

* ``datasette_google_sheets_links``: every import, export and synced table
  is a link (D2). Enumerated columns (``direction``, ``mode``, ``status``,
  ...) have no CHECK constraints, so a new value needs no table rebuild;
  they are validated in Python (``internal_db``).
* ``datasette_google_sheets_runs``: run history per link (D11/D17), pruned
  to ``runs_retain_per_link`` rows per link.

The runs FK declares ``ON DELETE CASCADE``, but SQLite only honours it with
``PRAGMA foreign_keys=ON``, which Datasette's internal-DB connection doesn't
set, so ``InternalDB.delete_link`` deletes the runs explicitly.
"""

from sqlite_migrate import Migrations
from sqlite_utils import Database

internal_migrations = Migrations("datasette-google-sheets.internal")


@internal_migrations()
def m001_links_and_runs(db: Database):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS datasette_google_sheets_links (
            id                   TEXT PRIMARY KEY,  -- ULID
            direction            TEXT NOT NULL,     -- import | export
            mode                 TEXT NOT NULL,     -- D8, D14
            owner_id             TEXT NOT NULL,
            credential_id        TEXT NOT NULL,     -- google-auth credential id
            database_name        TEXT NOT NULL,
            table_name           TEXT,              -- import target, or export table/view
            source_kind          TEXT,              -- exports: table|view|query|sql
            query_name           TEXT,
            sql                  TEXT,
            params               TEXT,              -- JSON object
            spreadsheet_id       TEXT NOT NULL,
            sheet_gid            INTEGER NOT NULL,  -- tabs are tracked by gid (D10)
            spreadsheet_title    TEXT,              -- last seen; display only
            sheet_title          TEXT,              -- last seen; display only
            mapping              TEXT,              -- JSON, imports (D10/D11)
            options              TEXT,              -- JSON, exports
            interval_minutes     INTEGER,           -- NULL = manual only
            created_table        INTEGER NOT NULL DEFAULT 0,
            created_schema       TEXT,              -- sqlite_master.sql at creation (D8)
            enabled              INTEGER NOT NULL DEFAULT 1,
            status               TEXT NOT NULL DEFAULT 'ok',  -- ok|error|paused
            status_code          TEXT,
            status_detail        TEXT,
            status_data          TEXT,              -- JSON, e.g. a header diff
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            last_run_at          TEXT,
            last_success_at      TEXT,
            last_hash            TEXT,              -- D12
            created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        CREATE INDEX IF NOT EXISTS datasette_google_sheets_links_owner
            ON datasette_google_sheets_links(owner_id);
        CREATE INDEX IF NOT EXISTS datasette_google_sheets_links_table
            ON datasette_google_sheets_links(database_name, table_name);
        -- At most one export link per tab (D14). A new-spreadsheet export
        -- waiting for its first run has spreadsheet_id '' (D33), so any number
        -- of those may coexist.
        CREATE UNIQUE INDEX IF NOT EXISTS datasette_google_sheets_links_export_tab
            ON datasette_google_sheets_links(spreadsheet_id, sheet_gid)
            WHERE direction = 'export' AND spreadsheet_id != '';
        -- A table has at most one sync (D8).
        CREATE UNIQUE INDEX IF NOT EXISTS datasette_google_sheets_links_sync_table
            ON datasette_google_sheets_links(database_name, table_name)
            WHERE direction = 'import' AND interval_minutes IS NOT NULL;

        CREATE TABLE IF NOT EXISTS datasette_google_sheets_runs (
            id            INTEGER PRIMARY KEY,
            link_id       TEXT NOT NULL
                              REFERENCES datasette_google_sheets_links(id)
                              ON DELETE CASCADE,
            started_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            finished_at   TEXT,
            "trigger"     TEXT NOT NULL,     -- manual | scheduled
            actor_id      TEXT,
            status        TEXT NOT NULL DEFAULT 'running',
                                             -- running|success|no_change|error
            rows_read     INTEGER,
            rows_written  INTEGER,
            added         INTEGER,
            changed       INTEGER,
            removed       INTEGER,
            cells         INTEGER,
            warnings      TEXT NOT NULL DEFAULT '[]',  -- JSON list of strings
            error_code    TEXT,
            error_message TEXT,
            hash          TEXT
        );
        CREATE INDEX IF NOT EXISTS datasette_google_sheets_runs_link
            ON datasette_google_sheets_runs(link_id, id DESC);
    """)
