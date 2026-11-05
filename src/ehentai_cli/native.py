from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .auth import private_write
from .batch import prepare_items
from .client import Client
from .errors import CLIError, ConflictError, InputError, UnknownResultError
from .models import GalleryRef, category_index


def batch_size_value(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (str, int))
        or not str(value).isdigit()
        or not 1 <= int(value) <= 50
    ):
        raise InputError("Native batch size must be an integer from 1 to 50.")
    return int(value)


def make_native_plan(
    client: Client,
    values: object = None,
    *,
    category: int,
    source: int | None = None,
    batch_size: int = 50,
) -> dict:
    category = category_index(category)
    size = batch_size_value(batch_size)
    if source is not None:
        source = category_index(source)
        if source == category:
            raise InputError("Source and target categories must be different.")
        if values is not None:
            raise InputError("Merge selects a source category; do not also supply gallery input.")
        source_count = client.categories()[source]["count"]
        snapshot = client.list_favorites(category=source, all_pages=True)["items"]
        items = [GalleryRef.parse(item).to_dict() for item in snapshot]
    else:
        items = prepare_items(values)
        if any("note" in item for item in items):
            raise InputError(
                "Native move preserves notes; omit note fields or use the regular move command."
            )
        snapshot = client.list_favorites(all_pages=True)["items"]
        source_count = None
    mapping = {item["gid"]: item for item in snapshot}
    operations = []
    for item in items:
        ref = GalleryRef.parse(item)
        current = mapping.get(ref.gid)
        if current is None or current.get("category") is None:
            raise ConflictError(f"Gallery {ref.gid} is not in the visible favorites snapshot.")
        if current["token"] != ref.token:
            raise ConflictError(f"Gallery {ref.gid} token differs from the favorites snapshot.")
        if source is not None and current["category"] != source:
            raise ConflictError("Source snapshot changed while planning the merge.")
        operations.append(
            {
                **ref.to_dict(),
                "before": {"category": current["category"]},
                "after": {"category": category},
            }
        )
    return {
        "schema": 1,
        "operation": "native_move",
        "site": client.site,
        "member_id": client.member_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "batch_size": size,
        "preserve_notes": True,
        "items": operations,
        "merge_source": source,
        "source_count": source_count,
        "unlisted_source_count": max(0, source_count - len(items))
        if source_count is not None
        else 0,
    }


def _validate(plan: dict, client: Client) -> list[tuple[GalleryRef, int, int]]:
    if type(plan.get("schema")) is not int or plan["schema"] != 1:
        raise InputError("Unsupported native move plan schema.")
    if plan.get("site") != client.site or plan.get("member_id") != client.member_id:
        raise ConflictError("Plan belongs to another site/account.")
    if plan.get("preserve_notes") is not True:
        raise InputError("Native move plan must preserve notes.")
    batch_size_value(plan.get("batch_size"))
    if plan.get("merge_source") is not None:
        category_index(plan["merge_source"])
    if not isinstance(plan.get("items"), list):
        raise InputError("Native plan must contain an items array.")
    if not plan["items"] and plan.get("merge_source") is None:
        raise InputError("Native plan must contain at least one gallery.")
    operations, seen = [], set()
    for item in plan["items"]:
        ref = GalleryRef.parse(item)
        if ref.gid in seen:
            raise InputError("Plan contains duplicate gallery IDs.")
        seen.add(ref.gid)
        if not isinstance(item.get("before"), dict) or not isinstance(item.get("after"), dict):
            raise InputError("Native plan needs before/after categories.")
        if set(item["before"]) != {"category"} or set(item["after"]) != {"category"}:
            raise InputError("Native plans change categories only; note fields are not accepted.")
        operations.append(
            (
                ref,
                category_index(item["before"]["category"]),
                category_index(item["after"]["category"]),
            )
        )
    return operations


def _apply_chunk(client: Client, operations: list[tuple[GalleryRef, int, int]]) -> list[dict]:
    target = operations[0][2]
    rows = [
        {
            **ref.to_dict(),
            "before": {"category": before},
            "after": {"category": after},
            "status": "not_attempted",
        }
        for ref, before, after in operations
    ]
    try:
        current = client.membership({before for _, before, _ in operations} | {target})
    except CLIError as error:
        rows[0].update({"status": "failed", "error": {"code": error.code, "message": str(error)}})
        return rows
    pending = []
    conflict = False
    for position, (ref, before, _) in enumerate(operations):
        if current.get(ref.gid) == target:
            rows[position]["status"] = "unchanged"
        elif current.get(ref.gid) != before:
            rows[position].update(
                {
                    "status": "failed",
                    "error": {
                        "code": "state_conflict",
                        "message": "Favorite moved or disappeared since planning.",
                    },
                }
            )
            conflict = True
        else:
            pending.append(position)
    if conflict or not pending:
        return rows
    try:
        client.move_many([operations[position][0] for position in pending], target)
    except CLIError:
        # A batch POST may have been partially applied. Reconcile every submitted ID.
        pass
    try:
        actual = client.membership({target})
        verification_error = None
    except CLIError:
        actual = {}
        verification_error = UnknownResultError(
            "Batch write may have succeeded, but read-back failed."
        )
    for position in pending:
        ref = operations[position][0]
        if actual.get(ref.gid) == target:
            rows[position]["status"] = "updated"
        else:
            error = verification_error or UnknownResultError(
                "Batch did not confirm this gallery in its target category. Reconcile before retrying."
            )
            rows[position].update(
                {"status": "unknown", "error": {"code": error.code, "message": str(error)}}
            )
    return rows


def apply_native_plan(
    client: Client, plan: dict, *, journal: Path | None = None, rollback: bool = False
) -> dict:
    operations = _validate(plan, client)
    if rollback:
        operations = [(ref, after, before) for ref, before, after in reversed(operations)]
    result = {
        "site": client.site,
        "member_id": client.member_id,
        "operation": "native_move",
        "rollback": rollback,
        "preserve_notes": True,
        "items": [],
        "ok": True,
    }
    size = batch_size_value(plan["batch_size"])
    position, stopped = 0, False
    while position < len(operations):
        end = position + 1
        while (
            end < len(operations)
            and end - position < size
            and operations[end][2] == operations[position][2]
        ):
            end += 1
        chunk = operations[position:end]
        if stopped:
            rows = [
                {
                    **ref.to_dict(),
                    "before": {"category": before},
                    "after": {"category": after},
                    "status": "not_attempted",
                }
                for ref, before, after in chunk
            ]
        else:
            rows = _apply_chunk(client, chunk)
            if any(row["status"] in ("failed", "unknown", "not_attempted") for row in rows):
                result["ok"] = False
                stopped = True
        result["items"].extend(rows)
        if journal:
            private_write(journal, result)
        position = end
    if plan.get("merge_source") is not None and not rollback:
        try:
            remaining = client.categories()[plan["merge_source"]]["count"]
            result.update({"source_remaining": remaining, "complete_merge": remaining == 0})
            if remaining:
                result["ok"] = False
                result["error"] = {
                    "code": "incomplete_merge",
                    "message": "Source category still contains favorites, including possibly unlisted items or concurrent additions.",
                }
        except CLIError as error:
            result["ok"] = False
            result["error"] = {"code": error.code, "message": str(error)}
    result["summary"] = {
        status: sum(row["status"] == status for row in result["items"])
        for status in ("updated", "unchanged", "failed", "unknown", "not_attempted")
    }
    if journal:
        private_write(journal, result)
    return result
