from __future__ import annotations

import copy
from unittest.mock import Mock

import pytest

from ehentai_cli.batch import apply_plan
from ehentai_cli.categories import make_rename_plan, validate_category_name
from ehentai_cli.cli import build_parser
from ehentai_cli.client import Client
from ehentai_cli.errors import ConflictError, InputError, NetworkError, ParseError
from ehentai_cli.models import FavoriteState, GalleryRef
from ehentai_cli.native import make_native_plan
from ehentai_cli.parsing import settings_form


class SiteClient:
    site = "eh"
    member_id = "123"

    def __init__(self, count=3):
        self.states = {gid: FavoriteState(1, f"note {gid}") for gid in range(1, count + 1)}
        self.batch_calls = []
        self.other_settings = [("dm", "extended"), ("xl_1", "on"), ("untouched", "value")]
        self.names = [f"Group {index}" for index in range(10)]
        self.profile = "1"
        self.submissions = []
        self.partial_batch = False
        self.unknown_readback = False
        self.extra_source_count = 0
        self.mutate_other_setting = False

    def categories(self):
        return [
            {
                "index": index,
                "name": self.names[index],
                "count": sum(state.category == index for state in self.states.values())
                + (self.extra_source_count if index == 1 else 0),
            }
            for index in range(10)
        ]

    def list_favorites(self, *, category=None, all_pages=False):
        return {
            "items": [
                {"gid": gid, "token": "abcdef1234", "category": state.category}
                for gid, state in self.states.items()
                if state.category is not None and (category is None or category == state.category)
            ]
        }

    def membership(self, categories):
        if self.unknown_readback and self.batch_calls:
            raise NetworkError("read-back unavailable")
        return {
            gid: state.category
            for gid, state in self.states.items()
            if state.category in categories
        }

    def move_many(self, refs, category):
        self.batch_calls.append([ref.gid for ref in refs])
        for ref in refs[:1] if self.partial_batch else refs:
            self.states[ref.gid] = FavoriteState(category, self.states[ref.gid].note)
        if self.partial_batch:
            raise NetworkError("response lost")

    def settings(self):
        return {
            "profile": self.profile,
            "names": self.names.copy(),
            "fields": self.other_settings
            + [(f"favorite_{index}", name) for index, name in enumerate(self.names)],
        }

    def submit_settings(self, fields):
        self.submissions.append(fields)
        self.names = [dict(fields)[f"favorite_{index}"] for index in range(10)]
        if self.mutate_other_setting:
            self.other_settings = [("dm", "unexpected")]


def test_native_protocol_uses_repeated_id_fields_in_one_post():
    transport = Mock()
    transport.site = "eh"
    refs = [GalleryRef(1, "abcdef1234"), GalleryRef(2, "abcdef1234")]
    Client(transport, "123").move_many(refs, 3)
    transport.request.assert_called_once_with(
        "POST",
        "/favorites.php",
        data=[("ddact", "fav3"), ("modifygids[]", "1"), ("modifygids[]", "2")],
    )


def test_cli_native_merge_and_rename_options():
    parser = build_parser()
    args = parser.parse_args(
        ["fav", "merge", "--from-category", "1", "--into-category", "2", "--dry-run"]
    )
    assert args.from_category == 1 and args.into_category == 2 and args.dry_run
    args = parser.parse_args(["fav", "rename", "--category", "2", "--name", "已阅读"])
    assert args.category == 2 and args.name == "已阅读"
    args = parser.parse_args(
        [
            "fav",
            "move",
            "--input",
            "test.json",
            "--to-category",
            "2",
            "--native",
            "--batch-size",
            "20",
        ]
    )
    assert args.native and args.batch_size == 20


def test_native_merge_batches_and_preserves_notes_then_rolls_back():
    client = SiteClient(count=7)
    original = client.states.copy()
    plan = make_native_plan(client, source=1, category=2, batch_size=3)
    assert client.batch_calls == []
    result = apply_plan(client, plan)
    assert result["summary"]["updated"] == 7
    assert result["complete_merge"] is True
    assert [len(batch) for batch in client.batch_calls] == [3, 3, 1]
    assert all(state.category == 2 for state in client.states.values())
    assert {gid: state.note for gid, state in client.states.items()} == {
        gid: state.note for gid, state in original.items()
    }
    assert apply_plan(client, plan)["summary"]["unchanged"] == 7
    assert len(client.batch_calls) == 3
    assert apply_plan(client, plan, rollback=True)["summary"]["updated"] == 7
    assert client.states == original


def test_native_move_subsets_and_rollback_to_multiple_original_categories():
    client = SiteClient()
    client.states[2] = FavoriteState(3, "other original note")
    original = client.states.copy()
    plan = make_native_plan(
        client,
        [GalleryRef(1, "abcdef1234").to_dict(), GalleryRef(2, "abcdef1234").to_dict()],
        category=5,
    )
    assert apply_plan(client, plan)["summary"]["updated"] == 2
    assert client.batch_calls == [[1, 2]]
    assert apply_plan(client, plan, rollback=True)["summary"]["updated"] == 2
    assert client.states == original


def test_native_partial_post_is_reconciled_per_item_and_stops():
    client = SiteClient(count=5)
    plan = make_native_plan(client, source=1, category=2, batch_size=3)
    client.partial_batch = True
    result = apply_plan(client, plan)
    assert [row["status"] for row in result["items"]] == [
        "updated",
        "unknown",
        "unknown",
        "not_attempted",
        "not_attempted",
    ]
    assert len(client.batch_calls) == 1
    assert result["ok"] is False
    client.partial_batch = False
    assert apply_plan(client, plan)["summary"] == {
        "updated": 4,
        "unchanged": 1,
        "failed": 0,
        "unknown": 0,
        "not_attempted": 0,
    }


def test_native_detects_conflict_before_submitting_chunk():
    client = SiteClient()
    plan = make_native_plan(client, source=1, category=2)
    client.states[2] = FavoriteState(4, "concurrent note")
    result = apply_plan(client, plan)
    assert result["ok"] is False
    assert client.batch_calls == []
    assert result["items"][1]["error"]["code"] == "state_conflict"


def test_native_unavailable_verification_is_unknown():
    client = SiteClient()
    plan = make_native_plan(client, source=1, category=2)
    client.unknown_readback = True
    result = apply_plan(client, plan)
    assert result["summary"]["unknown"] == 3
    assert result["ok"] is False


def test_merge_reports_unlisted_remaining_items_instead_of_claiming_complete():
    client = SiteClient()
    client.extra_source_count = 2
    plan = make_native_plan(client, source=1, category=2)
    assert plan["unlisted_source_count"] == 2
    result = apply_plan(client, plan)
    assert result["summary"]["updated"] == 3
    assert result["complete_merge"] is False
    assert result["source_remaining"] == 2
    assert result["error"]["code"] == "incomplete_merge"
    assert result["ok"] is False


def test_empty_merge_is_a_successful_noop():
    client = SiteClient(count=0)
    plan = make_native_plan(client, source=1, category=2)
    assert apply_plan(client, plan)["complete_merge"] is True
    assert client.batch_calls == []


def test_native_plan_rejects_notes_wrong_account_and_bad_batch_before_writes():
    client = SiteClient()
    with pytest.raises(InputError):
        make_native_plan(client, [{"gid": 1, "token": "abcdef1234", "note": "change"}], category=2)
    with pytest.raises(InputError):
        make_native_plan(client, source=1, category=1)
    plan = make_native_plan(client, source=1, category=2)
    for changed in ({**plan, "member_id": "456"}, {**plan, "site": "ex"}):
        with pytest.raises(ConflictError):
            apply_plan(client, changed)
    with pytest.raises(InputError):
        apply_plan(client, {**plan, "batch_size": 51})
    malformed = copy.deepcopy(plan)
    malformed["items"][-1]["after"]["note"] = "not supported"
    with pytest.raises(InputError):
        apply_plan(client, malformed)
    assert client.batch_calls == []


def test_rename_preserves_fresh_other_settings_and_supports_rollback():
    client = SiteClient()
    plan = make_rename_plan(client, 2, "  已阅读  ")
    assert plan["after"] == "已阅读"
    client.other_settings.append(("changed_after_planning", "keep this fresh setting"))
    before = copy.deepcopy(client.settings())
    result = apply_plan(client, plan)
    assert result["summary"]["updated"] == 1
    assert client.names[2] == "已阅读"
    assert dict(client.submissions[0])["changed_after_planning"] == "keep this fresh setting"
    assert apply_plan(client, plan)["summary"]["unchanged"] == 1
    assert len(client.submissions) == 1
    assert apply_plan(client, plan, rollback=True)["summary"]["updated"] == 1
    assert client.settings() == before


def test_rename_conflict_and_profile_change_prevent_submissions():
    client = SiteClient()
    plan = make_rename_plan(client, 2, "New")
    client.names[2] = "Concurrent edit"
    assert apply_plan(client, plan)["items"][0]["error"]["code"] == "state_conflict"
    client.names[2] = "Group 2"
    client.profile = "2"
    assert apply_plan(client, plan)["ok"] is False
    assert client.submissions == []


def test_rename_detects_unrelated_settings_change():
    client = SiteClient()
    plan = make_rename_plan(client, 2, "New")
    client.mutate_other_setting = True
    result = apply_plan(client, plan)
    assert result["items"][0]["status"] == "unknown"
    assert result["ok"] is False


@pytest.mark.parametrize("name", ["", "   ", "x" * 21, "line\nbreak", "\ud800", "😀" * 11])
def test_rename_name_limits(name):
    with pytest.raises(InputError):
        validate_category_name(name)


def test_settings_form_serializes_successful_controls_and_excludes_profile_actions():
    names = "".join(f'<input name="favorite_{index}" value="Group {index}">' for index in range(10))
    html = f"""<form id="profile_form" method="post"><input name="profile_action" value="delete"><select name="profile_set"><option value="0">Default</option><option selected value="1">Current</option></select></form>
    <form method="post" action="/uconfig.php">{names}
      <input name="token" type="hidden" value="preserve">
      <input name="dm" type="radio" value="a"><input name="dm" type="radio" checked value="b">
      <input name="checked" type="checkbox" checked><input name="unchecked" type="checkbox">
      <input name="disabled" disabled value="omit"><textarea name="text">keep &amp; preserve</textarea>
      <select name="choice"><option value="first">First</option><option selected value="second">Second</option></select>
      <input type="reset" name="reset" value="Reset"><input type="submit" name="apply" value="Apply">
    </form>"""
    parsed = settings_form(html)
    fields = dict(parsed["fields"])
    assert parsed["profile"] == "1"
    assert fields["dm"] == "b"
    assert fields["checked"] == "on"
    assert fields["text"] == "keep & preserve"
    assert fields["choice"] == "second"
    assert fields["apply"] == "Apply"
    assert fields["token"] == "preserve"
    external_apply = html.replace(
        '<input type="submit" name="apply" value="Apply">',
        '</form><input type="submit" name="apply" value="Apply">',
    )
    assert dict(settings_form(external_apply)["fields"])["apply"] == "Apply"
    assert not {"profile_action", "profile_set", "unchecked", "disabled", "reset"} & fields.keys()
    with pytest.raises(ParseError):
        settings_form(
            html.replace('action="/uconfig.php"', 'action="https://example.com/settings"')
        )
    with pytest.raises(ParseError):
        settings_form("<form>Unknown page</form>")
