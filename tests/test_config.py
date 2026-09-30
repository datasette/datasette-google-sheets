import pytest
from datasette.app import Datasette
from datasette.utils import StartupError

from datasette_google_sheets.config import Config, get_config

OVERRIDES = {
    "min_interval_minutes": 1,
    "default_interval_minutes": 30,
    "max_import_cells": 2_000,
    "max_export_cells": 20_000_000,
    "runs_retain_per_link": 7,
    "max_consecutive_failures": 3,
    "preview_rows": 500,
}


def make_datasette(plugin_config):
    return Datasette(
        memory=True,
        config={"plugins": {"datasette-google-sheets": plugin_config}},
    )


async def startup_error(plugin_config) -> str:
    datasette = make_datasette(plugin_config)
    with pytest.raises(StartupError) as info:
        await datasette.invoke_startup()
    return str(info.value)


@pytest.mark.asyncio
async def test_defaults():
    datasette = Datasette(memory=True)
    await datasette.invoke_startup()
    config = get_config(datasette)
    assert config.model_dump() == {
        "min_interval_minutes": 5,
        "default_interval_minutes": 10,
        "max_import_cells": 1_000_000,
        "max_export_cells": 5_000_000,
        "runs_retain_per_link": 100,
        "max_consecutive_failures": 5,
        "preview_rows": 50,
    }


@pytest.mark.asyncio
async def test_overrides():
    datasette = make_datasette(OVERRIDES)
    await datasette.invoke_startup()
    assert get_config(datasette).model_dump() == OVERRIDES


@pytest.mark.asyncio
async def test_env_value_is_resolved(monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEETS_TEST_PREVIEW_ROWS", "25")
    datasette = make_datasette(
        {"preview_rows": {"$env": "GOOGLE_SHEETS_TEST_PREVIEW_ROWS"}}
    )
    await datasette.invoke_startup()
    assert get_config(datasette).preview_rows == 25


@pytest.mark.asyncio
async def test_get_config_is_cached():
    datasette = make_datasette({"preview_rows": 10})
    await datasette.invoke_startup()
    assert get_config(datasette) is get_config(datasette)


@pytest.mark.asyncio
async def test_get_config_validates_without_startup():
    assert get_config(make_datasette({"preview_rows": 10})).preview_rows == 10
    with pytest.raises(StartupError, match="preview_rows"):
        get_config(make_datasette({"preview_rows": 0}))


@pytest.mark.asyncio
async def test_unknown_key_fails_startup():
    message = await startup_error({"max_import_cell": 10})
    assert message.startswith("Invalid datasette-google-sheets plugin configuration:")
    assert "max_import_cell: Extra inputs are not permitted" in message


@pytest.mark.asyncio
async def test_non_mapping_config_fails_startup():
    message = await startup_error(["preview_rows"])
    assert "(root):" in message


@pytest.mark.parametrize(
    "key,value",
    [
        ("min_interval_minutes", 0),
        ("default_interval_minutes", 0),
        ("max_import_cells", 0),
        ("max_export_cells", 0),
        ("max_export_cells", 20_000_001),
        ("runs_retain_per_link", 0),
        ("max_consecutive_failures", 0),
        ("preview_rows", 0),
        ("preview_rows", 501),
        ("preview_rows", "many"),
    ],
)
@pytest.mark.asyncio
async def test_bound_violation_fails_startup(key, value):
    message = await startup_error({key: value})
    lines = message.splitlines()
    assert lines[0] == "Invalid datasette-google-sheets plugin configuration:"
    # Exactly one error, naming the field.
    assert len(lines) == 2
    assert lines[1].startswith(f"  {key}: ")


@pytest.mark.asyncio
async def test_default_interval_below_min_interval_fails_startup():
    message = await startup_error(
        {"min_interval_minutes": 15, "default_interval_minutes": 10}
    )
    assert message.splitlines()[1] == (
        "  default_interval_minutes: Value error, must be greater than or equal "
        "to min_interval_minutes (15)"
    )


def test_default_interval_equal_to_min_interval_is_allowed():
    config = Config.model_validate(
        {"min_interval_minutes": 15, "default_interval_minutes": 15}
    )
    assert config.default_interval_minutes == 15


def test_default_interval_checked_against_default_min_interval():
    with pytest.raises(ValueError, match="min_interval_minutes \\(5\\)"):
        Config.model_validate({"default_interval_minutes": 4})


@pytest.mark.asyncio
async def test_error_does_not_echo_input_values():
    message = await startup_error({"preview_rows": "secret-looking-value-XYZ"})
    assert "secret-looking-value-XYZ" not in message
