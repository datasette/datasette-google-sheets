"""Request and response models for the JSON API (``routes/api.py``).

They are the API's contract: the router turns them into the OpenAPI
document (``just openapi``) that ``frontend/api.d.ts`` is generated from.
The pages (tickets 15-18) reuse them as page data.

Never a token or key. Link rows hold spreadsheet ids, titles and SQL: only
the link's owner and admins ever get a ``LinkInfo``.
"""

# No `from __future__ import annotations`: the router builds JSON Schema
# from these classes at import time, and the route modules annotate with them.
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .exporter import ExportResult
from .importer import ImportResult
from .internal_db import (
    ColumnType,
    Direction,
    ExportMode,
    ExportOptions,
    ImportMapping,
    ImportMode,
    Link,
    LinkStatus,
    Run,
    SourceKind,
)
from .runner import OutcomeStatus, RunOutcome
from .sheets import Tab, spreadsheet_url

# --- Responses -----------------------------------------------------------------


class StatusResponse(BaseModel):
    """Setup flags for the pages' notices."""

    internal_db_persistent: bool
    """False without ``--internal``: scheduled links are refused (D19)."""
    oauth_configured: bool | None
    """Whether google-auth can "Connect Google". None = unknown (google-auth
    doesn't export ``oauth_configured`` yet, D21): show Connect Google."""
    can_schedule: bool
    """Holds ``google-sheets-schedule`` (D15)."""
    is_admin: bool
    """Holds ``google-sheets-admin`` (D15)."""
    min_interval_minutes: int
    default_interval_minutes: int


class CredentialOption(BaseModel):
    """A credential the actor may use for this direction's scope."""

    id: str
    type: str
    """``google_oauth`` or ``service_account``."""
    label: str
    google_email: str | None
    """The service account's email: share spreadsheets with it (D18)."""
    status: str
    status_detail: str | None


class CredentialsResponse(BaseModel):
    credentials: list[CredentialOption]
    connect_url: str
    """Starts google-auth's "Connect Google", coming back to ``return_to``."""


class TabInfo(BaseModel):
    gid: int
    title: str
    index: int
    rows: int
    columns: int
    cells: int
    """``rows * columns``: what the import cap checks (D20)."""

    @classmethod
    def from_tab(cls, tab: Tab) -> "TabInfo":
        return cls(
            gid=tab.gid,
            title=tab.title,
            index=tab.index,
            rows=tab.rows,
            columns=tab.columns,
            cells=tab.rows * tab.columns,
        )


class InspectResponse(BaseModel):
    spreadsheet_id: str
    spreadsheet_title: str
    spreadsheet_url: str
    tabs: list[TabInfo]
    """Every tab in display order. Non-grid tabs have 0 rows and columns
    (D31): the wizard hides them."""
    gid: int | None
    """The tab named in the pasted URL (``#gid=``), if any."""


class PreviewResponse(BaseModel):
    """The import wizard's mapping step (D11). With ``too_large`` nothing
    was fetched: ``message`` says why and the other fields are empty."""

    too_large: bool
    message: str | None = None
    cells: int
    """The tab's grid size (rows x columns)."""
    max_cells: int
    """``max_import_cells`` (D20)."""
    spreadsheet_title: str | None = None
    tab: TabInfo | None = None
    headers: list[str] = Field(default_factory=list)
    """Normalised source headers, in sheet order."""
    rows: list[list[Any]] = Field(default_factory=list)
    """The first ``preview_rows`` data rows, in ``headers`` order."""
    total_rows: int = 0
    types: dict[str, ColumnType] = Field(default_factory=dict)
    """Suggested type per header."""
    key: str | None = None
    """Suggested key header, or None."""


class LinkInfo(BaseModel):
    """A link as its owner or an admin sees it."""

    id: str
    direction: Direction
    mode: ImportMode | ExportMode
    owner_id: str
    owner_name: str | None
    """The owner's display name (admins only, via ``actors_from_ids``).
    None: show ``owner_id``."""
    credential_id: str
    database_name: str
    table_name: str | None
    source_kind: SourceKind | None
    query_name: str | None
    sql: str | None
    params: dict[str, Any] | None
    spreadsheet_id: str
    """``''`` for a new-spreadsheet export before its first run (D33)."""
    sheet_gid: int
    spreadsheet_title: str | None
    sheet_title: str | None
    spreadsheet_url: str | None
    mapping: ImportMapping | None
    options: ExportOptions | None
    interval_minutes: int | None
    scheduled: bool
    synced: bool
    """A scheduled import: the table is locked (D7, D8)."""
    created_table: bool
    enabled: bool
    status: LinkStatus
    status_code: str | None
    status_detail: str | None
    status_data: dict[str, Any] | None
    consecutive_failures: int
    last_run_at: str | None
    last_success_at: str | None
    created_at: str
    updated_at: str
    can_operate: bool
    """Run, change settings or the mapping, convert: the owner only (D15)."""
    can_manage: bool
    """Pause, resume, unlink or delete: the owner or an admin (D15)."""

    @classmethod
    def from_link(
        cls,
        link: Link,
        *,
        can_operate: bool,
        can_manage: bool,
        owner_name: str | None = None,
    ) -> "LinkInfo":
        return cls(
            **link.model_dump(
                exclude={"last_hash", "created_schema"},
            ),
            owner_name=owner_name,
            spreadsheet_url=(
                spreadsheet_url(link.spreadsheet_id, link.sheet_gid)
                if link.spreadsheet_id
                else None
            ),
            scheduled=link.scheduled,
            synced=link.synced,
            can_operate=can_operate,
            can_manage=can_manage,
        )


class LinkListResponse(BaseModel):
    links: list[LinkInfo]


class RunListResponse(BaseModel):
    runs: list[Run]
    """Newest first."""


class RunOutcomeInfo(BaseModel):
    """What one "Run now" (or the first run after create) did."""

    run_id: int | None
    status: OutcomeStatus
    code: str | None = None
    message: str | None = None
    """Owner-facing failure message."""
    reconnect_url: str | None = None
    share_with: str | None = None
    """The service account's email to share the sheet with."""
    link_status: LinkStatus | None = None
    rows_read: int | None = None
    rows_written: int | None = None
    added: int | None = None
    changed: int | None = None
    removed: int | None = None
    cells: int | None = None
    warnings: list[str] = Field(default_factory=list)
    spreadsheet_url: str | None = None
    """Exports: the tab written to."""

    @classmethod
    def from_outcome(cls, outcome: RunOutcome) -> "RunOutcomeInfo":
        info = cls(
            run_id=outcome.run_id,
            status=outcome.status,
            code=outcome.code,
            message=outcome.message,
            reconnect_url=outcome.reconnect_url,
            share_with=outcome.share_with,
            link_status=outcome.link_status,
        )
        result = outcome.result
        if isinstance(result, ImportResult):
            info = info.model_copy(
                update={
                    "rows_read": result.rows_read,
                    "rows_written": result.rows_written,
                    "added": result.added,
                    "changed": result.changed,
                    "removed": result.removed,
                    "cells": result.cells,
                    "warnings": list(result.warnings),
                }
            )
        elif isinstance(result, ExportResult):
            info = info.model_copy(
                update={
                    "rows_read": result.rows_read,
                    "rows_written": result.rows_written,
                    "cells": result.cells,
                    "spreadsheet_url": result.url,
                }
            )
        return info


class LinkRunResponse(BaseModel):
    """A link after a run, and what the run did."""

    link: LinkInfo
    run: RunOutcomeInfo


class DeleteResponse(BaseModel):
    id: str
    deleted: bool


# --- Requests ------------------------------------------------------------------


class InspectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential_id: str
    url: str
    """A Google Sheets URL or a bare spreadsheet id."""
    direction: Direction = "import"
    """Which scope the credential must have: read-only for imports,
    read-write for exports."""


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential_id: str
    spreadsheet_id: str
    gid: int
    headers_row: bool = True


class CreateLinkRequest(BaseModel):
    """An import (``table_name`` + ``mapping``) or an export (``source_kind``
    + its field). ``interval_minutes`` schedules it (a synced table for an
    import); leave it out for a one-shot link."""

    model_config = ConfigDict(extra="forbid")

    direction: Direction
    mode: ImportMode | ExportMode
    credential_id: str
    database: str
    table_name: str | None = None
    """Imports: the target table. Exports: the table or view."""
    source_kind: SourceKind | None = None
    query_name: str | None = None
    sql: str | None = None
    params: dict[str, Any] | None = None
    spreadsheet_id: str | None = None
    """Omit for a ``new`` export: the first run creates the spreadsheet."""
    gid: int | None = None
    spreadsheet_title: str | None = None
    """A ``new`` export: the new spreadsheet's title."""
    mapping: ImportMapping | None = None
    options: ExportOptions | None = None
    interval_minutes: int | None = None


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    force: bool = False
    """Write even if the sheet is unchanged (D12)."""


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recreate: bool = False
    """``table_missing`` only: create the table again on the next run
    (D19). Only for links that created their table."""


class SettingsRequest(BaseModel):
    """Only the fields present change. ``interval_minutes: null`` removes
    the schedule (for a synced table, that's an unlink)."""

    model_config = ConfigDict(extra="forbid")

    interval_minutes: int | None = None
    credential_id: str | None = None
    options: ExportOptions | None = None


class MappingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mapping: ImportMapping


class ConvertToSyncedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interval_minutes: int


__all__ = [
    "ConvertToSyncedRequest",
    "CreateLinkRequest",
    "CredentialOption",
    "CredentialsResponse",
    "DeleteResponse",
    "InspectRequest",
    "InspectResponse",
    "LinkInfo",
    "LinkListResponse",
    "LinkRunResponse",
    "MappingRequest",
    "PreviewRequest",
    "PreviewResponse",
    "ResumeRequest",
    "RunListResponse",
    "RunOutcomeInfo",
    "RunRequest",
    "SettingsRequest",
    "StatusResponse",
    "TabInfo",
]
