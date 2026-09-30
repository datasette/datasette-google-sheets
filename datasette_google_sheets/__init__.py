"""datasette-google-sheets: import and export Google Sheets.

This module holds the plugin hooks only; logic lives in its own modules.
Credentials come from datasette-google-auth's public API and schedules from
datasette-cron.
"""

from datasette import hookimpl
from datasette_vite import vite_entry

from .router import router

# Import route modules to trigger registration on the shared router
from .routes import api, pages

_ = (pages, api)


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
