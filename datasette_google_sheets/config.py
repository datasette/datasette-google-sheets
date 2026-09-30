"""The plugin's own configuration: the ``plugins: datasette-google-sheets:`` block.

Validated once at startup. A typo'd key or a bad value fails startup with a
``StartupError`` naming the field, rather than being silently ignored.
Validation errors never echo input values.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from datasette.utils import StartupError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
)

if TYPE_CHECKING:
    from datasette.app import Datasette

PLUGIN_NAME = "datasette-google-sheets"

# Google's limit on cells in one spreadsheet.
GOOGLE_MAX_CELLS = 20_000_000


class Config(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        hide_input_in_errors=True,
        title="datasette-google-sheets plugin config",
        use_attribute_docstrings=True,
    )

    min_interval_minutes: int = Field(5, ge=1)
    """Shortest schedule allowed for a link, in minutes. Protects the Sheets
    API quota (D16)."""

    default_interval_minutes: int = Field(10, ge=1)
    """Interval offered by default for new scheduled links, in minutes. Must be
    at least ``min_interval_minutes``."""

    max_import_cells: int = Field(1_000_000, ge=1)
    """Largest tab (rows x columns from its grid properties) an import will
    fetch (D20)."""

    max_export_cells: int = Field(5_000_000, ge=1, le=GOOGLE_MAX_CELLS)
    """Largest result (rows x columns) an export will write (D20). At most
    Google's 20,000,000-cell spreadsheet limit."""

    runs_retain_per_link: int = Field(100, ge=1)
    """Run history rows kept per link; older runs are pruned."""

    max_consecutive_failures: int = Field(5, ge=1)
    """Failed runs in a row after which a link auto-pauses (D17)."""

    preview_rows: int = Field(50, ge=1, le=500)
    """Rows shown in the import wizard's mapping preview (D11)."""

    @field_validator("default_interval_minutes")
    @classmethod
    def _at_least_min_interval(cls, value: int, info: ValidationInfo) -> int:
        # Fields validate in declaration order, so min_interval_minutes is in
        # info.data unless it failed its own validation.
        minimum = info.data.get("min_interval_minutes")
        if minimum is not None and value < minimum:
            raise ValueError(
                f"must be greater than or equal to min_interval_minutes ({minimum})"
            )
        return value


def _format_error(error: ValidationError) -> str:
    # Only field paths and messages, never the input values.
    lines = [f"Invalid {PLUGIN_NAME} plugin configuration:"]
    for detail in error.errors(include_input=False, include_url=False):
        loc = ".".join(str(part) for part in detail["loc"]) or "(root)"
        lines.append(f"  {loc}: {detail['msg']}")
    return "\n".join(lines)


def load_config(datasette: Datasette) -> Config:
    """Validate the plugin config (``$env``/``$file`` already resolved).

    Raises ``StartupError`` with a message naming each bad key.
    """
    raw = datasette.plugin_config(PLUGIN_NAME) or {}
    try:
        return Config.model_validate(raw)
    except ValidationError as error:
        raise StartupError(_format_error(error)) from None


def get_config(datasette: Datasette) -> Config:
    """The config validated at startup (validates now if startup hasn't run)."""
    config = getattr(datasette, "_google_sheets_config", None)
    if config is None:
        config = load_config(datasette)
        setattr(datasette, "_google_sheets_config", config)  # noqa: B010
    return config
