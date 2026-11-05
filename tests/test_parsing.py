from __future__ import annotations

import pytest

from ehentai_cli.errors import AuthError, ParseError
from ehentai_cli.parsing import detail_category, favorites_page, popup_note


def header() -> str:
    return (
        '<div class="nosel">'
        + "".join(
            f'<div class="fp"><div>{index:,}</div><div>Group &amp; {index}</div></div>'
            for index in range(11)
        )
        + "</div>"
    )


def row(gid=12, token="abcdef1234", color="f00") -> str:
    return f"""<a href="https://e-hentai.org/g/{gid}/{token}/"><div class="glink">Test &amp; title</div></a>
        <div id="posted_{gid}" style="border-color:#{color};" title="Group 1">2026-01-01</div>
        <div class="gt" title="language:english"></div>"""


@pytest.mark.parametrize("layout", ["gltm", "gltc", "glte", "gld"])
def test_all_gallery_layouts(layout):
    content = (
        f'<div class="itg {layout}"><div>{row()}</div></div>'
        if layout == "gld"
        else f'<table class="itg {layout}"><tbody><tr><th>Header</th></tr><tr><td>{row()}</td></tr></tbody></table>'
    )
    result = favorites_page(
        header() + content + '<a id="unext" href="?favcat=1&amp;next=11-1234">Next</a>'
    )
    assert len(result["categories"]) == 10
    assert result["categories"][1] == {"index": 1, "name": "Group & 1", "count": 1}
    assert result["items"][0]["category"] == 1
    assert result["items"][0]["title"] == "Test & title"
    assert result["items"][0]["tags"] == ["language:english"]
    assert result["next"] == "11-1234"


def test_empty_and_unrecognized_pages_are_distinguished():
    assert favorites_page(header() + "<p>No hits found</p>")["items"] == []
    with pytest.raises(ParseError):
        favorites_page(header() + "<p>Something changed</p>")
    with pytest.raises(AuthError):
        favorites_page("Please log in")


def test_detail_states_and_note_preservation():
    assert (
        detail_category(
            '<div id="fav"><div class="i" style="background-position:0px -40px"></div><a id="favoritelink">Group 2</a></div>'
        )
        == 2
    )
    assert (
        detail_category(
            '<div id="fav"><div class="i"></div><a id="favoritelink">Add to Favorites</a></div>'
        )
        is None
    )
    state = popup_note(
        '<input name="favcat" value="2" checked><textarea name="favnote">  original &amp; note\n</textarea>',
        2,
    )
    assert state.note == "  original & note\n"
    with pytest.raises(ParseError):
        popup_note('<input name="favcat" value="3" checked><textarea>keep</textarea>', 2)
    with pytest.raises(ParseError):
        detail_category('<a id="favoritelink">Unknown state</a>')
    with pytest.raises(ParseError):
        popup_note("<p>Unexpected response</p>", 2)
