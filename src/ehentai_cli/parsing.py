from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup, Tag

from .errors import AuthError, ParseError
from .models import FavoriteState, GalleryRef

FAVORITE_COLORS = {
    color: index
    for index, color in enumerate(
        ("000", "f00", "fa0", "dd0", "080", "9f4", "4bf", "00f", "508", "e8e")
    )
}


def document(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def categories_from(soup: BeautifulSoup) -> list[dict]:
    categories = []
    for item in soup.select(".nosel > .fp"):
        children = item.find_all("div", recursive=False)
        if len(children) < 2:
            continue
        count = children[0].get_text(strip=True).replace(",", "")
        if not count.isdigit():
            continue
        if len(categories) == 10:
            break  # The final box represents uncollected galleries.
        categories.append(
            {
                "index": len(categories),
                "name": children[-1].get_text(strip=True),
                "count": int(count),
            }
        )
    if len(categories) != 10:
        text = soup.get_text(" ", strip=True).lower()
        if "log in" in text or "logged in" in text or "login" in text:
            raise AuthError("Login expired or favorites access denied. Run 'ehentai login' again.")
        raise ParseError(
            "Could not identify the ten favorite categories; the site layout may have changed."
        )
    return categories


def _color_index(item: Tag) -> tuple[int | None, str | None]:
    for tag in item.select("[id][style]"):
        match = re.search(r"border-color\s*:\s*#([a-fA-F0-9]{3,6})\b", tag.get("style", ""))
        if not match:
            continue
        color = match[1].lower()
        if len(color) == 6 and all(color[i] == color[i + 1] for i in (0, 2, 4)):
            color = color[::2]
        if color in FAVORITE_COLORS:
            return FAVORITE_COLORS[color], tag.get("title")
    return None, None


def favorites_page(html: str) -> dict:
    soup = document(html)
    categories = categories_from(soup)
    container = soup.select_one(".itg")
    records = []
    if container is not None:
        rows = (
            container.select("tr")
            if container.name == "table"
            else container.find_all("div", recursive=False)
        )
        seen = set()
        for row in rows:
            title = row.select_one(".glink")
            if title is None:
                continue
            anchor = title.find_parent("a", href=True)
            if anchor is None:
                anchor = row.find("a", href=re.compile(r"/g/[0-9]+/[a-fA-F0-9]{10}/?"))
            if anchor is None:
                raise ParseError("A gallery title has no recognized gallery link.")
            ref = GalleryRef.parse(anchor["href"])
            if ref.gid in seen:
                continue
            seen.add(ref.gid)
            index, name = _color_index(row)
            tags = sorted({tag["title"] for tag in row.select(".gt[title]")})
            records.append(
                {
                    **ref.to_dict(),
                    "url": anchor["href"],
                    "title": title.get_text(strip=True),
                    "category": index,
                    "category_name": name,
                    "tags": tags,
                }
            )
    elif not any(
        text in soup.get_text(" ", strip=True).lower()
        for text in (
            "no hits found",
            "no favorites found",
            "no galleries found",
            "you do not have any favorites",
        )
    ):
        raise ParseError(
            "Favorites page has neither a gallery list nor a recognized empty-list message."
        )
    cursor = None
    next_link = soup.select_one("#unext[href]")
    if next_link is not None:
        values = parse_qs(urlsplit(next_link["href"]).query).get("next", [])
        if values:
            if not re.fullmatch(r"[0-9-]+", values[0]):
                raise ParseError("Unrecognized favorites pagination cursor.")
            cursor = values[0]
    return {"categories": categories, "items": records, "next": cursor}


def detail_category(html: str) -> int | None:
    soup = document(html)
    link = soup.select_one("#favoritelink")
    if link is None:
        raise ParseError("Could not read gallery favorite state (missing favorite control).")
    icon = soup.select_one("#fav > .i")
    if icon is not None:
        style = icon.get("style", "")
        match = re.search(r"background-position\s*:\s*0(?:px)?\s+-(\d+)px", style)
        if match:
            offset = int(match[1])
            index = (offset - 2) // 19
            if 0 <= index <= 9 and (offset - 2) % 19 == 0:
                return index
            raise ParseError("Unrecognized favorite category icon offset.")
        if style.strip():
            raise ParseError("Unrecognized favorite icon style; refusing to guess its category.")
    if "add to favorites" in link.get_text(" ", strip=True).lower():
        return None
    raise ParseError("Could not distinguish collected and uncollected gallery state.")


def popup_note(html: str, category: int) -> FavoriteState:
    soup = document(html)
    textarea = soup.select_one('textarea[name="favnote"]') or soup.select_one("textarea")
    if textarea is None:
        raise ParseError("Could not read the existing favorite note.")
    selected = soup.select_one('input[name="favcat"][checked]') or soup.select_one(
        'select[name="favcat"] option[selected]'
    )
    if selected is not None and str(selected.get("value")) != str(category):
        raise ParseError("Gallery detail and favorite popup disagree; reload before modifying.")
    return FavoriteState(category, textarea.get_text())


def settings_form(html: str) -> dict:
    """Serialize the settings form's successful controls, excluding profile actions."""
    soup = document(html)
    expected = {f"favorite_{index}" for index in range(10)}
    candidates = [
        form
        for form in soup.find_all("form")
        if expected.issubset({node.get("name") for node in form.find_all("input")})
    ]
    if len(candidates) != 1:
        raise ParseError("Could not identify the favorite category settings form.")
    form = candidates[0]
    if form.get("method", "get").lower() != "post":
        raise ParseError("Unexpected settings submission method.")
    action = urlsplit(form.get("action", ""))
    if action.netloc or action.scheme or action.path not in ("", "/uconfig.php"):
        raise ParseError("Unexpected settings submission destination.")
    fields = []
    for node in form.find_all(["input", "select", "textarea"]):
        name = node.get("name")
        if not name or node.has_attr("disabled") or node.find_parent("fieldset", disabled=True):
            continue
        if node.name == "input":
            kind = node.get("type", "text").lower()
            if kind in ("submit", "reset", "button", "image", "file"):
                continue
            if kind in ("radio", "checkbox") and not node.has_attr("checked"):
                continue
            fields.append((name, node.get("value", "on" if kind in ("radio", "checkbox") else "")))
        elif node.name == "textarea":
            fields.append((name, node.get_text()))
        else:
            options = [
                option for option in node.find_all("option") if not option.has_attr("disabled")
            ]
            selected = [option for option in options if option.has_attr("selected")]
            if not selected and not node.has_attr("multiple"):
                selected = options[:1]
            for option in selected:
                fields.append((name, option.get("value", option.get_text())))
    submits = form.select('input[type="submit"][name], button[type="submit"][name]')
    if not submits:
        # The live page renders its Apply control outside the settings form.
        submits = soup.select('input[type="submit"][name="apply"]')
    if len(submits) > 1:
        raise ParseError("Ambiguous settings submit controls; refusing to guess.")
    if submits:
        fields.append((submits[0]["name"], submits[0].get("value", "")))
    names = []
    for index in range(10):
        matches = [value for key, value in fields if key == f"favorite_{index}"]
        if len(matches) != 1:
            raise ParseError("Missing or duplicated favorite name field.")
        names.append(matches[0])
    profile_select = soup.select_one('#profile_form select[name="profile_set"]')
    profile = "default"
    if profile_select is not None:
        option = profile_select.select_one("option[selected]") or profile_select.find("option")
        if option is None or option.get("value") is None:
            raise ParseError("Cannot determine the settings profile.")
        profile = option["value"]
    return {"fields": fields, "profile": profile, "names": names}
