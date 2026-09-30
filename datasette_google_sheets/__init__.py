"""datasette-google-sheets: import and export Google Sheets.

This module holds the plugin hooks only; logic lives in its own modules.
Credentials come from datasette-google-auth's public API and schedules from
datasette-cron.
"""

from datasette import hookimpl
from datasette.utils import StartupError
from datasette_vite import vite_entry
from sqlite_utils import Database as SqliteUtilsDatabase

from .config import load_config
from .internal_db import InternalDB
from .internal_migrations import internal_migrations
from .lock import synced_table_deny
from .permissions import actions
from .router import router

# Import route modules to trigger registration on the shared router
from .routes import api, pages
from .schedule import cron_handlers
from .schedule import start as start_schedules

_ = (pages, api)


@hookimpl
def startup(datasette):
    async def inner():
        # Validate plugin config first, so a bad config fails startup (with a
        # StartupError naming the bad key) before anything touches the
        # internal database.
        datasette._google_sheets_config = load_config(datasette)

        def apply_google_sheets_migrations(connection):
            internal_migrations.apply(SqliteUtilsDatabase(connection))

        await datasette.get_internal_database().execute_write_fn(
            apply_google_sheets_migrations
        )
        # Runs left "running" by a crashed process become errors. Safe here:
        # nothing can be running before every startup hook has finished.
        await InternalDB.for_datasette(datasette).mark_abandoned_runs()
        # datasette-cron's startup is tryfirst and has finished by now, so
        # its scheduler exists; its loop starts only after every startup
        # hook, so the reconciled tasks are in place before its first tick.
        if getattr(datasette, "_cron_scheduler", None) is None:
            raise StartupError(
                "datasette-google-sheets needs the datasette-cron plugin loaded"
            )
        await start_schedules(datasette)

    return inner


@hookimpl
def cron_register_handlers(datasette):
    # Registered by cron as google_sheets:run-link.
    return cron_handlers()


@hookimpl
def register_actions(datasette):
    return actions()


@hookimpl
def register_routes():
    return router.routes()


@hookimpl
def extra_template_vars(datasette):
    # Safe without a built frontend: vite_entry only reads manifest.json if it
    # exists, and raises only when a template calls it with an entrypoint.
    entry = vite_entry(
        datasette=datasette,
        plugin_package="datasette_google_sheets",
    )
    return {"datasette_google_sheets_vite_entry": entry}


@hookimpl
def permission_resources_sql(datasette, actor, action):
    # Synced tables are read-only for every actor, root included (D7).
    return synced_table_deny(datasette, action)
