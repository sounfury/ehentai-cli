from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

from .errors import InputError

SITES = {"eh": "https://e-hentai.org", "ex": "https://exhentai.org"}
GALLERY_PATH = re.compile(r"^/g/([1-9][0-9]*)/([a-fA-F0-9]{10})/?$")


def category_index(value: object) -> int:
    if isinstance(value, bool):
        raise InputError("Category must be an integer from 0 to 9.")
    if not isinstance(value, (str, int)) or not re.fullmatch(r"[0-9]", str(value)):
        raise InputError("Category must be an integer from 0 to 9.")
    return int(value)


def validate_note(note: object) -> str:
    if not isinstance(note, str) or len(note) > 200:
        raise InputError("A favorite note must be a string of at most 200 characters.")
    return note


@dataclass(frozen=True)
class GalleryRef:
    gid: int
    token: str

    @classmethod
    def parse(cls, value: object) -> GalleryRef:
        if isinstance(value, dict):
            if "url" in value:
                return cls.parse(value["url"])
            gid, token = value.get("gid"), value.get("token")
            if isinstance(gid, bool) or not str(gid).isdigit() or int(gid) <= 0:
                raise InputError("A gallery needs a positive integer gid.")
            if not isinstance(token, str) or not re.fullmatch(r"[a-fA-F0-9]{10}", token):
                raise InputError("A gallery needs its 10-character hexadecimal token.")
            return cls(int(gid), token.lower())
        if not isinstance(value, str):
            raise InputError("Use a gallery URL or an object containing gid and token.")
        try:
            parts = urlsplit(value)
        except ValueError:
            raise InputError("Malformed gallery URL.") from None
        if parts.scheme != "https" or parts.netloc not in ("e-hentai.org", "exhentai.org"):
            raise InputError("Use an HTTPS gallery URL on e-hentai.org or exhentai.org.")
        match = GALLERY_PATH.fullmatch(parts.path)
        if not match or parts.query or parts.fragment:
            raise InputError("Expected a gallery URL: https://e-hentai.org/g/GID/TOKEN/.")
        return cls(int(match[1]), match[2].lower())

    def url(self, site: str) -> str:
        return f"{SITES[site]}/g/{self.gid}/{self.token}/"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FavoriteState:
    category: int | None
    note: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def parse(cls, value: object) -> FavoriteState:
        if not isinstance(value, dict) or "category" not in value or "note" not in value:
            raise InputError("A state must include category and note.")
        index = None if value["category"] is None else category_index(value["category"])
        note = validate_note(value["note"])
        if index is None and note:
            raise InputError("An uncollected gallery cannot have a favorite note.")
        return cls(index, note)
