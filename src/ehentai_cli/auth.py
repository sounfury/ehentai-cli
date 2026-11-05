from __future__ import annotations

import base64
import ctypes
import json
import os
import sqlite3
import sys
import tempfile
from http.cookies import CookieError, SimpleCookie
from pathlib import Path

from .errors import AuthError, InputError


def config_dir() -> Path:
    override = os.environ.get("EHENTAI_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "ehentai-cli"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/ehentai-cli"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ehentai-cli"


def parse_cookie(raw: str) -> dict[str, str]:
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw[7:].strip()
    if "\r" in raw or "\n" in raw:
        raise InputError("Provide one Cookie header line, without embedded newlines.")
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(raw)
    except CookieError:
        raise InputError("Could not parse the Cookie header.") from None
    result = {key: morsel.value for key, morsel in jar.items()}
    if not result.get("ipb_member_id", "").isdigit() or not result.get("ipb_pass_hash"):
        raise AuthError("Cookie must contain ipb_member_id and ipb_pass_hash.")
    return result


def read_cookie_file(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        raise InputError("Cannot read the cookie file as UTF-8.") from None
    if text.startswith(("{", "[")):
        try:
            value = json.loads(text)
            if isinstance(value, dict) and isinstance(value.get("cookies"), list):
                value = value["cookies"]
            if isinstance(value, list):
                value = {item["name"]: item["value"] for item in value}
            if not isinstance(value, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in value.items()
            ):
                raise ValueError
            return parse_cookie("; ".join(f"{k}={v}" for k, v in value.items()))
        except (ValueError, KeyError, TypeError):
            raise InputError(
                "Expected a Cookie header, JSON cookie map, or browser cookie array."
            ) from None
    if text.startswith("#") or "\t" in text:
        pairs = []
        for line in text.splitlines():
            line = line.removeprefix("#HttpOnly_")
            if not line or line.startswith("#"):
                continue
            columns = line.split("\t")
            if len(columns) != 7:
                raise InputError("Invalid Netscape cookie file.")
            if columns[0].lstrip(".") in ("e-hentai.org", "exhentai.org"):
                pairs.append(f"{columns[5]}={columns[6]}")
        return parse_cookie("; ".join(pairs))
    return parse_cookie(text)


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    """Bind Windows credentials to the current Windows user (no third-party keyring)."""
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        function = crypt32.CryptUnprotectData
        second = None
    else:
        function = crypt32.CryptProtectData
        second = "ehentai-cli"
    function.restype = ctypes.c_int
    function.argtypes = [
        ctypes.POINTER(_Blob),
        ctypes.c_void_p if decrypt else ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(_Blob),
    ]
    if not function(ctypes.byref(source), second, None, None, None, 1, ctypes.byref(target)):
        raise AuthError("Windows could not decrypt/save credentials for the current user.")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel32.LocalFree(target.data)


def private_write(path: Path, data: dict) -> None:
    """Atomically replace a private UTF-8 JSON file; never leave a partial credential file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=".ehentai-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save_credentials(cookies: dict[str, str], site: str) -> Path:
    payload = {"cookies": cookies, "site": site}
    if sys.platform == "win32":
        protected = _dpapi(json.dumps(payload).encode(), decrypt=False)
        payload = {"storage": "windows-dpapi", "data": base64.b64encode(protected).decode()}
    path = config_dir() / "credentials.json"
    private_write(path, payload)
    return path


def load_credentials() -> tuple[dict[str, str], str]:
    path = config_dir() / "credentials.json"
    if not path.exists():
        raise AuthError("No saved login. Run 'ehentai login' first.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("storage") == "windows-dpapi":
            if sys.platform != "win32":
                raise AuthError(
                    "This login is encrypted for its original Windows user. Log in again."
                )
            value = json.loads(_dpapi(base64.b64decode(value["data"]), decrypt=True))
        cookies = value["cookies"]
        if not isinstance(cookies, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in cookies.items()
        ):
            raise ValueError
        cookies = parse_cookie("; ".join(f"{k}={v}" for k, v in cookies.items()))
        site = value["site"]
        if site not in ("eh", "ex"):
            raise ValueError
        return cookies, site
    except (ValueError, TypeError, KeyError, OSError):
        raise AuthError("Cannot read saved login. Run 'ehentai login' again.") from None


def jhentai_candidates() -> list[Path]:
    documents = Path.home() / "Documents"
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                documents = Path(os.path.expandvars(winreg.QueryValueEx(key, "Personal")[0]))
        except OSError:
            pass
    paths = [documents / "JHTData/temp/db/db.sqlite"]
    if sys.platform == "win32":
        paths.append(
            Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
            / "top.jtmonster/jhentai/db.sqlite"
        )
    elif sys.platform == "darwin":
        paths.extend(
            (
                Path.home() / "Library/Application Support/top.jtmonster.jhentai/db.sqlite",
                documents / "db.sqlite",
            )
        )
    else:
        paths.extend(
            (
                Path.home() / ".local/share/top.jtmonster/jhentai/db.sqlite",
                Path.home() / ".local/share/jhentai/db.sqlite",
            )
        )
    return paths


def import_jhentai(path: Path | None = None) -> tuple[dict[str, str], Path]:
    candidates = [path] if path is not None else jhentai_candidates()
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            connection = sqlite3.connect(
                candidate.resolve().as_uri() + "?mode=ro", uri=True, timeout=5
            )
            try:
                connection.execute("PRAGMA query_only = ON")
                row = connection.execute(
                    "SELECT value FROM local_config WHERE config_key = ? AND sub_config_key = ?",
                    ("eh_cookies", ""),
                ).fetchone()
            finally:
                connection.close()
            if row is None:
                continue
            entries = json.loads(row[0])
            if not isinstance(entries, list) or not all(isinstance(v, str) for v in entries):
                raise ValueError
            # Dart stores each cookie with Set-Cookie attributes; only retain the name/value pair.
            cookies = parse_cookie("; ".join(entry.split(";", 1)[0] for entry in entries))
            return cookies, candidate
        except (sqlite3.Error, ValueError, TypeError, AuthError):
            if path is not None:
                raise AuthError(
                    "Cannot read a logged-in JHenTai cookie configuration from that database."
                ) from None
    raise AuthError(
        "No readable JHenTai login found. Use --jhentai-db PATH or provide a Cookie header."
    )
