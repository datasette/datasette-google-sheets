from dataclasses import dataclass

import pytest
from datasette.app import Datasette

from datasette_google_sheets.permissions import (
    ADMIN,
    SCHEDULE,
    can_manage_link,
    can_operate_link,
    can_schedule,
    can_view_link,
    is_admin,
)

ALICE = {"id": "alice"}
BOB = {"id": "bob"}
ROOT = {"id": "root"}


@dataclass(frozen=True)
class Link:
    owner_id: str


ALICES_LINK = Link(owner_id="alice")


async def make_datasette(config=None, **kwargs) -> Datasette:
    datasette = Datasette(memory=True, config=config, **kwargs)
    await datasette.invoke_startup()
    return datasette


@pytest.mark.asyncio
async def test_actions_registered_global():
    datasette = await make_datasette()
    for name in (SCHEDULE, ADMIN):
        action = datasette.get_action(name)
        assert action is not None
        assert action.resource_class is None


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", [ALICE, ROOT, None])
async def test_default_deny(actor):
    datasette = await make_datasette()
    assert not await can_schedule(datasette, actor)
    assert not await is_admin(datasette, actor)
    for name in (SCHEDULE, ADMIN):
        assert not await datasette.allowed(action=name, actor=actor)


@pytest.mark.asyncio
async def test_config_grants_schedule():
    datasette = await make_datasette({"permissions": {SCHEDULE: True}})
    assert await can_schedule(datasette, ALICE)
    # Granting one action doesn't grant the other.
    assert not await is_admin(datasette, ALICE)


@pytest.mark.asyncio
async def test_config_grants_admin_by_actor_id():
    datasette = await make_datasette({"permissions": {ADMIN: {"id": "bob"}}})
    assert await is_admin(datasette, BOB)
    assert not await is_admin(datasette, ALICE)
    assert not await can_schedule(datasette, BOB)


@pytest.mark.asyncio
async def test_root_holds_both_under_root_flag():
    datasette = Datasette(memory=True)
    datasette.root_enabled = True
    await datasette.invoke_startup()
    assert await can_schedule(datasette, ROOT)
    assert await is_admin(datasette, ROOT)


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", [None, {}, {"id": None}])
async def test_anonymous_denied_even_when_config_allows_everyone(actor):
    # A `true` permissions block matches anonymous actors too.
    datasette = await make_datasette({"permissions": {SCHEDULE: True, ADMIN: True}})
    assert await datasette.allowed(action=SCHEDULE, actor=None)
    assert not await can_schedule(datasette, actor)
    assert not await is_admin(datasette, actor)


# (actor, admin) -> (view, manage, operate) on Alice's link.
MATRIX = [
    pytest.param(ALICE, False, (True, True, True), id="owner"),
    pytest.param(ALICE, True, (True, True, True), id="owner-admin"),
    pytest.param(BOB, False, (False, False, False), id="other"),
    pytest.param(BOB, True, (True, True, False), id="admin-not-owner"),
    pytest.param(None, False, (False, False, False), id="anonymous"),
    pytest.param(None, True, (False, False, False), id="anonymous-admin-flag"),
    pytest.param({"id": None}, True, (False, False, False), id="id-none"),
]


@pytest.mark.parametrize("actor,admin,expected", MATRIX)
def test_link_helper_matrix(actor, admin, expected):
    assert (
        can_view_link(actor, ALICES_LINK, admin=admin),
        can_manage_link(actor, ALICES_LINK, admin=admin),
        can_operate_link(actor, ALICES_LINK),
    ) == expected


def test_admin_can_manage_but_not_operate():
    assert can_manage_link(BOB, ALICES_LINK, admin=True)
    assert not can_operate_link(BOB, ALICES_LINK)


def test_integer_actor_id_matches_text_owner_id():
    # Actor ids may be ints; owner_id is stored as text.
    link = Link(owner_id="42")
    assert can_operate_link({"id": 42}, link)
    assert not can_operate_link({"id": 43}, link)


@pytest.mark.asyncio
async def test_helpers_with_real_admin_check():
    datasette = await make_datasette({"permissions": {ADMIN: {"id": "bob"}}})
    admin = await is_admin(datasette, BOB)
    assert can_view_link(BOB, ALICES_LINK, admin=admin)
    assert can_manage_link(BOB, ALICES_LINK, admin=admin)
    assert not can_operate_link(BOB, ALICES_LINK)
