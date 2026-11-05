from __future__ import annotations

from collections import deque

import pytest
import requests
from test_parsing import header, row

from ehentai_cli.client import Client
from ehentai_cli.errors import AuthError, ConflictError, NetworkError, UnknownResultError
from ehentai_cli.models import FavoriteState, GalleryRef
from ehentai_cli.transport import Transport

REF = GalleryRef(12, "abcdef1234")
BEFORE = FavoriteState(1, "original")
AFTER = FavoriteState(2, "original")


class QueueTransport:
    site = "eh"

    def __init__(self, responses):
        self.responses = deque(responses)
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def detail(index):
    if index is None:
        return '<a id="favoritelink">Add to Favorites</a>'
    return f'<div id="fav"><div class="i" style="background-position:0px -{2 + 19 * index}px"></div><a id="favoritelink">Group</a></div>'


def popup(index, note="original"):
    return (
        f'<input name="favcat" value="{index}" checked><textarea name="favnote">{note}</textarea>'
    )


def test_move_preserves_note_and_verifies_post():
    transport = QueueTransport([detail(1), popup(1), "ok", detail(2), popup(2)])
    assert Client(transport, "123").set_state(REF, BEFORE, AFTER) == "updated"
    post = next(call for call in transport.calls if call[0] == "POST")
    assert post[2]["data"]["favnote"] == "original"
    assert post[2]["data"]["favcat"] == 2


def test_timed_out_post_is_reconciled_without_a_duplicate_write():
    transport = QueueTransport(
        [detail(1), popup(1), NetworkError("timed out"), detail(2), popup(2)]
    )
    assert Client(transport, "123").set_state(REF, BEFORE, AFTER) == "updated"
    assert sum(call[0] == "POST" for call in transport.calls) == 1


def test_failed_verification_is_reported_as_unknown():
    transport = QueueTransport(
        [detail(1), popup(1), NetworkError("timed out"), NetworkError("offline")]
    )
    with pytest.raises(UnknownResultError):
        Client(transport, "123").set_state(REF, BEFORE, AFTER)


def test_success_http_status_does_not_prove_a_successful_edit():
    transport = QueueTransport([detail(1), popup(1), "HTTP 200 but unchanged", detail(1), popup(1)])
    with pytest.raises(ConflictError):
        Client(transport, "123").set_state(REF, BEFORE, AFTER)


def test_repeating_an_applied_plan_does_not_toggle_or_write():
    transport = QueueTransport([detail(2), popup(2)])
    assert Client(transport, "123").set_state(REF, BEFORE, AFTER) == "unchanged"
    assert not any(call[0] == "POST" for call in transport.calls)


def test_conflicting_remote_edit_is_not_overwritten():
    transport = QueueTransport([detail(3), popup(3)])
    with pytest.raises(ConflictError):
        Client(transport, "123").set_state(REF, BEFORE, AFTER)
    assert not any(call[0] == "POST" for call in transport.calls)


def test_paginated_snapshot_deduplicates_and_preserves_cursor():
    first = (
        header()
        + f'<table class="itg"><tr><td>{row()}</td></tr></table><a id="unext" href="?next=11">next</a>'
    )
    second = (
        header()
        + f'<table class="itg"><tr><td>{row()}</td></tr><tr><td>{row(13)}</td></tr></table>'
    )
    transport = QueueTransport([first, second])
    result = Client(transport, "123").list_favorites(all_pages=True)
    assert [v["gid"] for v in result["items"]] == [12, 13]
    assert result["complete"] is True
    assert transport.calls[1][2]["params"]["next"] == "11"


def test_repeated_pagination_fails_instead_of_returning_partial_snapshot():
    page = (
        header()
        + f'<table class="itg"><tr><td>{row()}</td></tr></table><a id="unext" href="?next=11">next</a>'
    )
    with pytest.raises(ConflictError):
        Client(QueueTransport([page, page]), "123").list_favorites(all_pages=True)


def response(status=200, body=b"<html>ok</html>"):
    value = requests.Response()
    value.status_code = status
    value._content = body
    return value


def test_transport_never_retries_posts_or_follows_redirects(monkeypatch):
    transport = Transport({"ipb_member_id": "123", "ipb_pass_hash": "test"}, "eh", interval=0)
    calls = []

    def request(*args, **kwargs):
        calls.append(kwargs)
        raise requests.Timeout("secret-internal-details")

    monkeypatch.setattr(transport.session, "request", request)
    with pytest.raises(NetworkError) as caught:
        transport.request("POST", "/gallerypopups.php")
    assert len(calls) == 1
    assert calls[0]["allow_redirects"] is False
    assert "secret-internal-details" not in str(caught.value)
    monkeypatch.setattr(transport.session, "request", lambda *a, **k: response(302))
    with pytest.raises(AuthError):
        transport.request("GET", "/favorites.php")
    transport.close()


def test_get_retry_is_bounded(monkeypatch):
    transport = Transport({}, "eh", interval=0)
    calls = []
    monkeypatch.setattr("ehentai_cli.transport.time.sleep", lambda value: None)

    def request(*args, **kwargs):
        calls.append(1)
        return response(503)

    monkeypatch.setattr(transport.session, "request", request)
    with pytest.raises(NetworkError):
        transport.request("GET", "/favorites.php")
    assert len(calls) == 3
    transport.close()
