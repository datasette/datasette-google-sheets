"""The datasette-google-sheets permission model (D15).

* **Two global actions** (no resource), handed out through ``datasette.yaml``
  ``permissions:`` blocks. Both are default deny: core only default-allows
  its own view/execute actions (``DEFAULT_ALLOW_ACTIONS``), so an action
  nothing grants resolves to deny. Only root under ``--root`` gets them
  without config.

  * ``google-sheets-schedule``: create or edit a *scheduled* link (a synced
    table or a scheduled export). One-shot imports and exports need no
    action of ours, only core's permissions for the operation.
  * ``google-sheets-admin``: see every link and pause, resume, unlink or
    delete any of them.

* **Links are owner-only** (no sharing, no reassigning in v1), so the
  per-link checks are plain owner comparisons in code, never
  ``datasette.allowed()``. An admin can *view* and *manage* any link but
  never *operate* someone else's: running, editing, remapping or changing
  the credential would act with the owner's Google credential.

* **Anonymous actors can do nothing.** A ``permissions:`` block of ``true``
  matches anonymous actors too, so ``can_schedule`` and ``is_admin`` return
  False for them before asking ``allowed()``.

* **Not found, never forbidden.** For anyone without ``google-sheets-admin``,
  an unknown link id and someone else's link must look the same: a 404, not
  a 403, so ids can't be probed (google-credentials D25's reasoning). Admins can see
  every link, so for them only a real unknown id is a 404. The routes
  (ticket 13) enforce this; use ``can_view_link`` as the existence check.
"""

from typing import Any, Protocol

from datasette.permissions import Action

SCHEDULE = "google-sheets-schedule"
ADMIN = "google-sheets-admin"


class LinkOwner(Protocol):
    """Anything with the link's ``owner_id``. Stands in for the link row
    type until the internal DB (ticket 06) defines it."""

    @property
    def owner_id(self) -> str: ...


def actions() -> list[Action]:
    """The two global actions, for the ``register_actions`` hook."""
    return [
        Action(
            name=SCHEDULE,
            abbr="gshs",
            description=(
                "Can create and edit scheduled Google Sheets links "
                "(synced tables and scheduled exports)"
            ),
        ),
        Action(
            name=ADMIN,
            abbr="gsha",
            description=(
                "Can see every Google Sheets link and pause, resume, unlink "
                "or delete any of them (never run or edit them)"
            ),
        ),
    ]


def _actor_id(actor: dict[str, Any] | None) -> str | None:
    if not actor or actor.get("id") is None:
        return None
    return str(actor["id"])


async def can_schedule(datasette, actor: dict[str, Any] | None) -> bool:
    """May the actor create or edit a scheduled link?"""
    if _actor_id(actor) is None:
        return False
    return await datasette.allowed(action=SCHEDULE, actor=actor)


async def is_admin(datasette, actor: dict[str, Any] | None) -> bool:
    """Does the actor hold ``google-sheets-admin``?"""
    if _actor_id(actor) is None:
        return False
    return await datasette.allowed(action=ADMIN, actor=actor)


def _is_owner(actor: dict[str, Any] | None, link: LinkOwner) -> bool:
    actor_id = _actor_id(actor)
    return actor_id is not None and actor_id == link.owner_id


def can_view_link(
    actor: dict[str, Any] | None, link: LinkOwner, *, admin: bool
) -> bool:
    """The owner or an admin. ``admin`` is ``is_admin()``'s answer, fetched
    once per request. False means "not found" to a non-admin."""
    if _actor_id(actor) is None:
        return False
    return admin or _is_owner(actor, link)


def can_manage_link(
    actor: dict[str, Any] | None, link: LinkOwner, *, admin: bool
) -> bool:
    """Pause, resume, unlink or delete: the owner or an admin."""
    if _actor_id(actor) is None:
        return False
    return admin or _is_owner(actor, link)


def can_operate_link(actor: dict[str, Any] | None, link: LinkOwner) -> bool:
    """Run now, edit settings, update the mapping or change the credential:
    the owner only, never an admin, because the run uses the owner's
    credential (D15)."""
    return _is_owner(actor, link)
