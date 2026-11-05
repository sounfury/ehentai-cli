from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .auth import private_write
from .client import Client
from .errors import CLIError, ConflictError, InputError
from .models import FavoriteState, GalleryRef, category_index, validate_note


def prepare_items(values: object) -> list[dict]:
    if isinstance(values, dict):
        values = values.get("items")
    if not isinstance(values, list) or not values:
        raise InputError(
            "Input must be a nonempty JSON array or an exported object containing items."
        )
    result, seen = [], set()
    for value in values:
        ref = GalleryRef.parse(value)
        if ref.gid in seen:
            raise InputError("Input contains a duplicate gid. Deduplicate before making a plan.")
        seen.add(ref.gid)
        item = {**ref.to_dict()}
        if isinstance(value, dict):
            if "note" in value:
                item["note"] = validate_note(value["note"])
            if "category" in value and value["category"] is not None:
                item["category"] = category_index(value["category"])
        result.append(item)
    return result


def make_plan(
    client: Client,
    operation: str,
    values: object,
    *,
    category: int | None = None,
    note: str | None = None,
) -> dict:
    if operation not in ("add", "move", "note", "remove"):
        raise InputError("Unsupported operation.")
    items = prepare_items(values)
    if category is not None:
        category = category_index(category)
    if note is not None:
        note = validate_note(note)
    # Validate the complete input before even reading remote states.
    for item in items:
        if operation in ("add", "move") and category is None and "category" not in item:
            raise InputError(
                "Add/move requires --category/--to-category or category in every item."
            )
        if operation == "note" and note is None and "note" not in item:
            raise InputError("Note updates require --text or note in every item.")
    operations = []
    for item in items:
        ref = GalleryRef.parse(item)
        before = client.get_state(ref)
        if operation in ("move", "note") and before.category is None:
            raise ConflictError(f"Gallery {ref.gid} is not collected; use add first.")
        if operation == "remove":
            after = FavoriteState(None, "")
        else:
            target = (
                (category if category is not None else item["category"])
                if operation in ("add", "move")
                else before.category
            )
            after = FavoriteState(
                target, note if note is not None else item.get("note", before.note)
            )
        operations.append({**ref.to_dict(), "before": before.to_dict(), "after": after.to_dict()})
    return {
        "schema": 1,
        "site": client.site,
        "member_id": client.member_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "operation": operation,
        "items": operations,
    }


def validate_plan(
    plan: object, client: Client
) -> list[tuple[GalleryRef, FavoriteState, FavoriteState]]:
    if not isinstance(plan, dict) or type(plan.get("schema")) is not int or plan["schema"] != 1:
        raise InputError("Unsupported plan schema.")
    if plan.get("site") != client.site or plan.get("member_id") != client.member_id:
        raise ConflictError("Plan belongs to another site/account. Log in to the matching account.")
    if not isinstance(plan.get("items"), list) or not plan["items"]:
        raise InputError("Plan must contain at least one operation.")
    operations, seen = [], set()
    for item in plan["items"]:
        ref = GalleryRef.parse(item)
        if ref.gid in seen:
            raise InputError("Plan contains duplicate gallery IDs.")
        seen.add(ref.gid)
        operations.append(
            (ref, FavoriteState.parse(item.get("before")), FavoriteState.parse(item.get("after")))
        )
    return operations


def apply_plan(
    client: Client, plan: dict, *, journal: Path | None = None, rollback: bool = False
) -> dict:
    operations = validate_plan(plan, client)
    if rollback:
        operations = [(ref, after, before) for ref, before, after in reversed(operations)]
    result = {
        "site": client.site,
        "member_id": client.member_id,
        "rollback": rollback,
        "items": [],
        "ok": True,
    }
    stopped = False
    for ref, before, after in operations:
        row = {**ref.to_dict(), "before": before.to_dict(), "after": after.to_dict()}
        if stopped:
            row["status"] = "not_attempted"
        else:
            try:
                row["status"] = client.set_state(ref, before, after)
            except CLIError as error:
                row.update(
                    {
                        "status": "unknown" if error.code == "result_unknown" else "failed",
                        "error": {"code": error.code, "message": str(error)},
                    }
                )
                result["ok"] = False
                stopped = True
        result["items"].append(row)
        if journal:
            private_write(journal, result)
    result["summary"] = {
        status: sum(row["status"] == status for row in result["items"])
        for status in ("updated", "unchanged", "failed", "unknown", "not_attempted")
    }
    if journal:
        private_write(journal, result)
    return result
