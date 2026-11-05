from __future__ import annotations

import sqlite3

import pytest

from ehentai_cli import auth
from ehentai_cli.errors import AuthError, InputError
from ehentai_cli.models import FavoriteState, GalleryRef, category_index

COOKIE = "ipb_member_id=123; ipb_pass_hash=test-only-not-a-real-pass-hash; igneous=test-only"


def test_cookie_header_and_required_fields():
    assert auth.parse_cookie("Cookie: " + COOKIE)["ipb_member_id"] == "123"
    for value in (
        "",
        "ipb_member_id=123",
        "ipb_pass_hash=test",
        "ipb_member_id=abc; ipb_pass_hash=test",
    ):
        with pytest.raises(AuthError):
            auth.parse_cookie(value)
    with pytest.raises(InputError):
        auth.parse_cookie(COOKIE + "\r\nInjected: header")


@pytest.mark.parametrize("kind", ["header", "json", "browser", "netscape"])
def test_cookie_file_formats(tmp_path, kind):
    import json

    path = tmp_path / "input.txt"
    cookies = auth.parse_cookie(COOKIE)
    contents = {
        "header": COOKIE,
        "json": json.dumps(cookies),
        "browser": json.dumps([{"name": k, "value": v} for k, v in cookies.items()]),
        "netscape": "# Netscape HTTP Cookie File\n"
        + "\n".join(
            f"#HttpOnly_.e-hentai.org\tTRUE\t/\tTRUE\t0\t{k}\t{v}" for k, v in cookies.items()
        )
        + "\n.example.com\tTRUE\t/\tTRUE\t0\tipb_member_id\t999",
    }
    path.write_text(contents[kind], encoding="utf-8-sig")
    assert auth.read_cookie_file(path) == cookies


def test_credentials_roundtrip_private_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("EHENTAI_CONFIG_DIR", str(tmp_path / "config"))
    cookies = auth.parse_cookie(COOKIE)
    path = auth.save_credentials(cookies, "ex")
    assert auth.load_credentials() == (cookies, "ex")
    if auth.sys.platform == "win32":
        assert cookies["ipb_pass_hash"] not in path.read_text()
        assert "windows-dpapi" in path.read_text()
    else:
        assert path.stat().st_mode & 0o777 == 0o600


def test_missing_corrupt_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("EHENTAI_CONFIG_DIR", str(tmp_path))
    with pytest.raises(AuthError):
        auth.load_credentials()
    (tmp_path / "credentials.json").write_text("broken", encoding="utf-8")
    with pytest.raises(AuthError):
        auth.load_credentials()


def test_jhentai_import_uses_readonly_database(tmp_path):
    import json

    path = tmp_path / "db.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE local_config (config_key TEXT, sub_config_key TEXT, value TEXT)"
        )
        connection.execute(
            "INSERT INTO local_config VALUES (?, ?, ?)",
            (
                "eh_cookies",
                "",
                json.dumps([pair.strip() + "; Path=/; HttpOnly" for pair in COOKIE.split(";")]),
            ),
        )
    original = path.read_bytes()
    cookies, source = auth.import_jhentai(path)
    assert cookies == auth.parse_cookie(COOKIE)
    assert source == path
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/g/12/abcdef1234/",
        "http://e-hentai.org/g/12/abcdef1234/",
        "https://e-hentai.org@evil.example/g/12/abcdef1234/",
        "https://e-hentai.org/g/12/abcdef1234/?next=https://evil.example",
        "https://e-hentai.org/g/0/abcdef1234/",
        "https://e-hentai.org/g/12/not-token/",
    ],
)
def test_reject_untrusted_gallery_urls(url):
    with pytest.raises(InputError):
        GalleryRef.parse(url)


def test_model_validation():
    assert GalleryRef.parse("https://exhentai.org/g/12/ABCDEF1234/") == GalleryRef(12, "abcdef1234")
    for value in (True, -1, 10, 1.2, None):
        with pytest.raises(InputError):
            category_index(value)
    with pytest.raises(InputError):
        FavoriteState.parse({"category": None, "note": "orphan"})
