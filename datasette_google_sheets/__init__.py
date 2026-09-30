"""datasette-google-sheets: import and export Google Sheets.

This module holds the plugin hooks only; logic lives in its own modules.
Credentials come from datasette-google-auth's public API and schedules from
datasette-cron.
"""

from datasette import hookimpl
from datasette_vite import vite_entry
from sqlite_utils import Database as SqliteUtilsDatabase

from .config import load_config
from .internal_db import InternalDB
from .internal_migrations import internal_migrations
from .permissions import actions
from .router import router

# Import route modules to trigger registration on the shared router
from .routes import api, pages

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

    return inner


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
