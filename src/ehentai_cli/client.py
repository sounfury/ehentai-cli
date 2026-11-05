from __future__ import annotations

from .errors import CLIError, ConflictError, InputError, UnknownResultError
from .models import FavoriteState, GalleryRef, category_index
from .parsing import (
    categories_from,
    detail_category,
    document,
    favorites_page,
    popup_note,
    settings_form,
)
from .transport import Transport


class Client:
    def __init__(self, transport: Transport, member_id: str):
        self.transport = transport
        self.site = transport.site
        self.member_id = member_id

    def categories(self) -> list[dict]:
        return categories_from(document(self.transport.request("GET", "/favorites.php")))

    def list_favorites(
        self,
        *,
        category: int | None = None,
        query: str | None = None,
        all_pages: bool = False,
        cursor: str | None = None,
        max_pages: int = 1000,
    ) -> dict:
        if max_pages < 1:
            raise InputError("max-pages must be positive.")
        params: dict = {}
        if category is not None:
            params["favcat"] = category
        if query:
            params["f_search"] = query
        items, seen_gids, seen_cursors = [], set(), set()
        if cursor:
            seen_cursors.add(cursor)
        for page_number in range(1, max_pages + 1):
            if cursor:
                params["next"] = cursor
            page = favorites_page(self.transport.request("GET", "/favorites.php", params=params))
            for item in page["items"]:
                if item["category"] is None and category is not None:
                    item["category"] = category
                    item["category_name"] = page["categories"][category]["name"]
                if item["gid"] not in seen_gids:
                    seen_gids.add(item["gid"])
                    items.append(item)
            cursor = page["next"]
            if not all_pages or cursor is None:
                return {
                    "items": items,
                    "count": len(items),
                    "pages": page_number,
                    "next": cursor,
                    "complete": cursor is None,
                }
            if cursor in seen_cursors:
                raise ConflictError(
                    "Pagination repeated a cursor; refusing to return an incomplete snapshot."
                )
            seen_cursors.add(cursor)
        raise ConflictError(
            "Reached max-pages before the end. Increase --max-pages or narrow the query."
        )

    def settings(self) -> dict:
        return settings_form(self.transport.request("GET", "/uconfig.php"))

    def submit_settings(self, fields: list[tuple[str, str]]) -> None:
        self.transport.request("POST", "/uconfig.php", data=fields)

    def move_many(self, refs: list[GalleryRef], category: int) -> None:
        category = category_index(category)
        if not refs or len(refs) > 50 or len({ref.gid for ref in refs}) != len(refs):
            raise InputError("A native batch must contain 1 to 50 distinct galleries.")
        data = [("ddact", f"fav{category}")]
        data.extend(("modifygids[]", str(ref.gid)) for ref in refs)
        self.transport.request("POST", "/favorites.php", data=data)

    def membership(self, categories: set[int]) -> dict[int, int]:
        result = {}
        for category in sorted(categories):
            page = self.list_favorites(category=category, all_pages=True)
            for item in page["items"]:
                if item["category"] != category:
                    raise ConflictError("Favorite list returned an unexpected category.")
                if item["gid"] in result and result[item["gid"]] != category:
                    raise ConflictError("Favorite moved while reading the category snapshot.")
                result[item["gid"]] = category
        return result

    def get_state(self, ref: GalleryRef) -> FavoriteState:
        html = self.transport.request("GET", f"/g/{ref.gid}/{ref.token}/")
        category = detail_category(html)
        if category is None:
            return FavoriteState(None, "")
        popup = self.transport.request(
            "GET", "/gallerypopups.php", params={"gid": ref.gid, "t": ref.token, "act": "addfav"}
        )
        return popup_note(popup, category)

    def set_state(self, ref: GalleryRef, expected: FavoriteState, desired: FavoriteState) -> str:
        current = self.get_state(ref)
        if current == desired:
            return "unchanged"
        if current != expected:
            raise ConflictError(
                "Favorite changed since planning; refusing to overwrite another edit."
            )
        data = {
            "favcat": "favdel" if desired.category is None else desired.category,
            "favnote": desired.note,
            "apply": "Apply Changes" if desired.category is None else "Add to Favorites",
            "update": 1,
        }
        submission_error = None
        try:
            self.transport.request(
                "POST",
                "/gallerypopups.php",
                params={"gid": ref.gid, "t": ref.token, "act": "addfav"},
                data=data,
            )
        except CLIError as error:
            # An interrupted response does not establish whether a POST was applied.
            submission_error = error
        try:
            actual = self.get_state(ref)
        except CLIError:
            raise UnknownResultError(
                "Write may have reached the server, but verification failed. Re-run the saved plan to reconcile."
            ) from None
        if actual == desired:
            return "updated"
        if actual != expected:
            raise UnknownResultError(
                "Read-back differs from both original and requested state. Inspect before retrying."
            )
        if submission_error:
            raise submission_error
        raise ConflictError("Site did not apply the requested state; original state is unchanged.")
