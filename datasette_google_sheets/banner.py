"""The table page banner (``top_table``): why a table can't be edited and
when it last synced (D15, D17).

* **Synced table, any viewer** (anonymous included): "Synced from Google
  Sheets · every N min · last synced ‹ago›", read-only, and the paused
  state, without its reason.
* **The owner or a ``google-sheets-admin``** also get the spreadsheet title,
  tab and URL, the pause (or error) reason and links to the link detail page
  for Sync now / Unlink. Links, not POST forms: no JS or CSRF handling here.
* **One-shot import (D2 provenance), owner or admin only:** "Imported from
  ‹sheet› on ‹date›" for the newest successful one-shot import into the
  table that the viewer may see.

Titles and URLs are only put in the template context for viewers who may
see them, so they can't leak into anyone else's HTML. The template is
rendered with Datasette's autoescaping Jinja environment: titles and pause
reasons are user-controlled text.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .internal_db import InternalDB, Link
from .permissions import can_view_link, is_admin
from .schedule import effective_interval
from .sheets import spreadsheet_url

if TYPE_CHECKING:
    from datasette.app import Datasette
    from datasette.utils.asgi import Request

TEMPLATE = "google_sheets_banner.html"


def _parse(iso: str) -> datetime:
    """Our timestamps are ISO 8601 UTC with a ``Z`` suffix."""
    value = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _plural(count: int, unit: str) -> str:
    return f"{count} {unit}{'' if count == 1 else 's'}"


def relative_time(iso: str, now: datetime | None = None) -> str:
    """ "just now", "3 min ago", "2 hours ago", "5 days ago"."""
    now = now or datetime.now(timezone.utc)
    seconds = max(0, int((now - _parse(iso)).total_seconds()))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{_plural(hours, 'hour')} ago"
    return f"{_plural(hours // 24, 'day')} ago"


def _details(datasette: Datasette, link: Link) -> dict[str, Any]:
    """What only the owner and admins may see."""
    return {
        "spreadsheet_title": link.spreadsheet_title,
        "sheet_title": link.sheet_title,
        "spreadsheet_url": (
            spreadsheet_url(link.spreadsheet_id, link.sheet_gid)
            if link.spreadsheet_id
            else None
        ),
        "detail_url": datasette.urls.path(f"/-/google-sheets/links/{link.id}"),
    }


def _synced_context(
    datasette: Datasette, link: Link, *, details: bool
) -> dict[str, Any]:
    assert link.interval_minutes is not None
    context: dict[str, Any] = {
        "kind": "synced",
        "interval_minutes": effective_interval(datasette, link.interval_minutes),
        "paused": link.status == "paused",
        "last_synced": None,
        "details": None,
    }
    if link.last_success_at:
        context["last_synced"] = {
            "iso": link.last_success_at,
            "ago": relative_time(link.last_success_at),
        }
    if details:
        context["details"] = {
            **_details(datasette, link),
            # Retrying on schedule (error) or needing a human (paused), D17.
            "failing": link.status == "error",
            "reason": link.status_detail if link.status != "ok" else None,
        }
    return context


def _imported_context(datasette: Datasette, link: Link) -> dict[str, Any]:
    assert link.last_success_at is not None
    return {
        "kind": "imported",
        "imported_at": {
            "iso": link.last_success_at,
            "date": _parse(link.last_success_at).date().isoformat(),
        },
        "details": _details(datasette, link),
    }


async def table_banner(
    datasette: Datasette, request: Request | None, database: str, table: str
) -> str | None:
    """The banner HTML for a table page, or None for a table with no link
    (or only links the viewer may not see)."""
    links = await InternalDB.for_datasette(datasette).import_links_for_table(
        database, table
    )
    if not links:
        return None
    actor = request.actor if request is not None else None
    admin = await is_admin(datasette, actor)
    synced = next((link for link in links if link.synced), None)
    if synced is not None:
        context = _synced_context(
            datasette, synced, details=can_view_link(actor, synced, admin=admin)
        )
    else:
        imported = next(
            (
                link
                for link in links
                if link.last_success_at and can_view_link(actor, link, admin=admin)
            ),
            None,
        )
        if imported is None:
            return None
        context = _imported_context(datasette, imported)
    return await datasette.render_template(
        TEMPLATE, {"banner": context}, request=request
    )
