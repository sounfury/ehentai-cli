from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .auth import private_write
from .client import Client
from .errors import CLIError, ConflictError, InputError, UnknownResultError
from .models import category_index


def validate_category_name(value: object) -> str:
    if not isinstance(value, str):
        raise InputError("Category name must be a string.")
    value = value.strip()
    try:
        length = len(value.encode("utf-16-le")) // 2
    except UnicodeError:
        raise InputError("Category name contains invalid Unicode.") from None
    if not value or length > 20 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise InputError(
            "Category name must contain 1 to 20 characters, without control characters."
        )
    return value


def make_rename_plan(client: Client, category: int, name: str) -> dict:
    category = category_index(category)
    name = validate_category_name(name)
    settings = client.settings()
    return {
        "schema": 1,
        "operation": "rename",
        "site": client.site,
        "member_id": client.member_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "profile": settings["profile"],
        "category": category,
        "before": settings["names"][category],
        "after": name,
    }


def apply_rename_plan(
    client: Client, plan: dict, *, journal: Path | None = None, rollback: bool = False
) -> dict:
    if type(plan.get("schema")) is not int or plan["schema"] != 1:
        raise InputError("Unsupported rename plan schema.")
    if plan.get("site") != client.site or plan.get("member_id") != client.member_id:
        raise ConflictError("Plan belongs to another site/account.")
    category = category_index(plan.get("category"))
    if not isinstance(plan.get("profile"), str) or not isinstance(plan.get("before"), str):
        raise InputError("Rename plan must include its profile and original name.")
    after = validate_category_name(plan.get("after"))
    if after != plan["after"]:
        raise InputError("Rename plan contains a noncanonical category name.")
    before = plan["before"]
    if len(before) > 20 or any(ord(char) < 32 for char in before):
        raise InputError("Invalid original category name in plan.")
    expected, desired = (after, before) if rollback else (before, after)
    row = {"category": category, "before": expected, "after": desired}
    result = {
        "site": client.site,
        "member_id": client.member_id,
        "rollback": rollback,
        "operation": "rename",
        "items": [row],
        "ok": True,
    }
    try:
        current = client.settings()
        if current["profile"] != plan["profile"]:
            raise ConflictError("The active settings profile changed since planning.")
        if current["names"][category] == desired:
            row["status"] = "unchanged"
        else:
            if current["names"][category] != expected:
                raise ConflictError(
                    "Category was renamed since planning; refusing to overwrite it."
                )
            field = f"favorite_{category}"
            submitted = [
                (key, desired if key == field else value) for key, value in current["fields"]
            ]
            submission_error = None
            try:
                client.submit_settings(submitted)
            except CLIError as error:
                submission_error = error
            try:
                actual = client.settings()
            except CLIError:
                raise UnknownResultError(
                    "Rename may have succeeded, but settings read-back failed. Reconcile using this plan."
                ) from None
            if actual["profile"] != current["profile"]:
                raise UnknownResultError("Settings profile changed while verifying the rename.")
            unchanged = Counter(
                (key, value) for key, value in actual["fields"] if key != field
            ) == Counter((key, value) for key, value in current["fields"] if key != field)
            if not unchanged:
                raise UnknownResultError(
                    "Other settings changed during rename verification; inspect the settings page."
                )
            if actual["names"][category] == desired:
                row["status"] = "updated"
            elif actual["names"][category] == expected and submission_error:
                raise submission_error
            else:
                raise UnknownResultError("Requested category name was not confirmed by read-back.")
    except CLIError as error:
        row.update(
            {
                "status": "unknown" if error.code == "result_unknown" else "failed",
                "error": {"code": error.code, "message": str(error)},
            }
        )
        result["ok"] = False
    result["summary"] = {
        status: int(row["status"] == status)
        for status in ("updated", "unchanged", "failed", "unknown", "not_attempted")
    }
    if journal:
        private_write(journal, result)
    return result
