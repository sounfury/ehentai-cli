from __future__ import annotations

import json

import pytest

from ehentai_cli.batch import apply_plan, make_plan
from ehentai_cli.cli import build_parser, main
from ehentai_cli.errors import ConflictError, InputError, UnknownResultError
from ehentai_cli.models import FavoriteState


class MemoryClient:
    site = "eh"
    member_id = "123"

    def __init__(self):
        self.states = {12: FavoriteState(1, "keep original"), 13: FavoriteState(None, "")}
        self.writes = []

    def get_state(self, ref):
        return self.states[ref.gid]

    def set_state(self, ref, expected, desired):
        current = self.states[ref.gid]
        if current == desired:
            return "unchanged"
        if current != expected:
            raise ConflictError("conflict")
        self.states[ref.gid] = desired
        self.writes.append(ref.gid)
        return "updated"


def values(*gids):
    return [{"gid": gid, "token": "abcdef1234"} for gid in gids]


def test_plan_apply_repeat_and_rollback_with_preserved_notes(tmp_path):
    client = MemoryClient()
    plan = make_plan(client, "move", values(12), category=2)
    assert not client.writes
    journal = tmp_path / "journal.json"
    result = apply_plan(client, plan, journal=journal)
    assert result["summary"]["updated"] == 1
    assert client.states[12] == FavoriteState(2, "keep original")
    assert json.loads(journal.read_text())["ok"] is True
    assert apply_plan(client, plan)["summary"]["unchanged"] == 1
    assert apply_plan(client, plan, rollback=True)["summary"]["updated"] == 1
    assert client.states[12] == FavoriteState(1, "keep original")


def test_full_add_move_note_remove_cycle():
    client = MemoryClient()
    for operation, kwargs, expected in (
        ("add", {"category": 1}, FavoriteState(1, "")),
        ("move", {"category": 2}, FavoriteState(2, "")),
        ("note", {"note": "new note"}, FavoriteState(2, "new note")),
        ("remove", {}, FavoriteState(None, "")),
    ):
        plan = make_plan(client, operation, values(13), **kwargs)
        assert apply_plan(client, plan)["ok"] is True
        assert client.states[13] == expected


def test_multiple_item_categories_notes_and_rollback():
    client = MemoryClient()
    items = [
        {"gid": 12, "token": "abcdef1234", "category": 3},
        {"gid": 13, "token": "abcdef1234", "category": 5, "note": "new favorite"},
    ]
    plan = make_plan(client, "add", {"items": items})
    result = apply_plan(client, plan)
    assert result["summary"]["updated"] == 2
    assert client.states[12] == FavoriteState(3, "keep original")
    assert client.states[13] == FavoriteState(5, "new favorite")
    assert apply_plan(client, plan)["summary"]["unchanged"] == 2
    assert apply_plan(client, plan, rollback=True)["summary"]["updated"] == 2
    assert client.states == {12: FavoriteState(1, "keep original"), 13: FavoriteState(None, "")}


def test_wrong_account_or_site_rejected_before_writes():
    client = MemoryClient()
    plan = make_plan(client, "move", values(12), category=2)
    for key, value in (("member_id", "456"), ("site", "ex")):
        with pytest.raises(ConflictError):
            apply_plan(client, {**plan, key: value})
    assert not client.writes


def test_entire_plan_is_validated_before_any_mutation():
    client = MemoryClient()
    plan = make_plan(client, "add", values(12, 13), category=2)
    plan["items"][1]["after"]["note"] = "x" * 201
    with pytest.raises(InputError):
        apply_plan(client, plan)
    assert not client.writes


def test_unknown_result_stops_batch_and_journals_remaining_items(tmp_path):
    client = MemoryClient()
    plan = make_plan(client, "add", values(12, 13), category=2)

    def fail(*args):
        raise UnknownResultError("verify before retry")

    client.set_state = fail
    result = apply_plan(client, plan, journal=tmp_path / "results.json")
    assert result["ok"] is False
    assert [item["status"] for item in result["items"]] == ["unknown", "not_attempted"]
    assert json.loads((tmp_path / "results.json").read_text())["summary"]["unknown"] == 1


def test_move_requires_existing_favorite_and_input_is_deduplicated():
    client = MemoryClient()
    with pytest.raises(ConflictError):
        make_plan(client, "move", values(13), category=2)
    with pytest.raises(InputError):
        make_plan(client, "remove", values(12, 12))
    with pytest.raises(InputError):
        make_plan(client, "note", values(12))
    assert not client.writes


@pytest.mark.parametrize(
    "argv",
    [
        ["--json", "--site", "ex", "fav", "list"],
        ["fav", "--site", "ex", "list", "--json"],
        ["fav", "list", "--site", "ex", "--json"],
    ],
)
def test_common_options_work_at_every_level(argv):
    args = build_parser().parse_args(argv)
    assert args.site == "ex"
    assert args.json is True


def test_machine_errors_do_not_echo_unknown_arguments(capsys):
    assert main(["login", "--cookie", "sensitive-do-not-echo"]) == 2
    value = json.loads(capsys.readouterr().out)
    assert value["error"]["code"] == "invalid_input"
    assert "sensitive-do-not-echo" not in json.dumps(value)


def test_no_login_machine_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EHENTAI_CONFIG_DIR", str(tmp_path))
    assert main(["whoami", "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "authentication_failed"


def test_cli_direct_dry_run_and_apply(monkeypatch, tmp_path, capsys):
    from ehentai_cli import cli

    client = MemoryClient()

    class DummyTransport:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setenv("EHENTAI_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "load_credentials", lambda: ({"ipb_member_id": "123"}, "eh"))
    monkeypatch.setattr(cli, "Transport", DummyTransport)
    monkeypatch.setattr(cli, "Client", lambda *args: client)
    assert (
        main(
            [
                "fav",
                "move",
                "https://e-hentai.org/g/12/abcdef1234/",
                "--to-category",
                "2",
                "--dry-run",
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    plan_path = output["data"]["plan_path"]
    assert client.writes == []
    assert main(["fav", "apply", "--plan", plan_path]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["summary"]["updated"] == 1
    assert client.states[12] == FavoriteState(2, "keep original")
