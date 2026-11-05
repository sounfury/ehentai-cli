from __future__ import annotations

import argparse
import getpass
import json
import math
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .auth import (
    config_dir,
    import_jhentai,
    load_credentials,
    parse_cookie,
    private_write,
    read_cookie_file,
    save_credentials,
)
from .batch import apply_plan, make_plan
from .client import Client
from .errors import CLIError, InputError
from .models import GalleryRef, category_index
from .transport import Transport


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse's default message can echo malformed arguments, including secrets.
        raise InputError("Invalid command arguments. Run the command with --help.")


def _number(value: str) -> float:
    try:
        result = float(value)
        if math.isfinite(result) and result >= 0:
            return result
    except ValueError:
        pass
    raise InputError("Expected a finite nonnegative number.")


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Compact JSON output (all output is JSON by default)",
    )
    parser.add_argument(
        "--site",
        choices=("eh", "ex"),
        default=argparse.SUPPRESS,
        help="Site; defaults to the saved login's site",
    )
    parser.add_argument(
        "--proxy",
        default=argparse.SUPPRESS,
        help="HTTP(S) or SOCKS5 proxy URL; environment/system proxies also work",
    )
    parser.add_argument(
        "--interval",
        type=_number,
        default=argparse.SUPPRESS,
        help="Minimum seconds between requests (default: 1)",
    )
    parser.add_argument(
        "--timeout",
        type=_number,
        default=argparse.SUPPRESS,
        help="Read timeout in seconds (default: 30)",
    )


def _selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("urls", nargs="*", help="HTTPS gallery URLs")
    parser.add_argument("--input", type=Path, help="JSON array or list export; use - for stdin")
    parser.add_argument(
        "--category",
        "--to-category",
        dest="category",
        type=category_index,
        help="Favorite category index, 0..9",
    )
    parser.add_argument(
        "--text",
        "--note",
        dest="note",
        help="New note (max 200 characters); otherwise preserve existing notes",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(
        prog="ehentai",
        description="Cookie-authenticated EH/EX favorites management for users and AI agents.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    _common(parser)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    login = commands.add_parser("login", help="Validate and save a Cookie login")
    _common(login)
    sources = login.add_mutually_exclusive_group()
    sources.add_argument(
        "--cookie-file", type=Path, help="Cookie header, JSON cookies, or Netscape cookies.txt"
    )
    sources.add_argument(
        "--cookie-stdin", action="store_true", help="Read a Cookie header from stdin"
    )
    sources.add_argument(
        "--from-jhentai",
        action="store_true",
        help="Read cookies from a local JHenTai database (read-only)",
    )
    login.add_argument(
        "--jhentai-db", type=Path, help="Explicit JHenTai SQLite path; requires --from-jhentai"
    )
    for name, help_text in (("whoami", "Verify saved login"), ("logout", "Delete the saved login")):
        _common(commands.add_parser(name, help=help_text))
    fav = commands.add_parser("fav", help="Query and manage online favorites")
    _common(fav)
    actions = fav.add_subparsers(dest="action", required=True, parser_class=Parser)
    _common(actions.add_parser("categories", help="List the ten category names and counts"))
    listing = actions.add_parser("list", help="List/search favorites with cursor pagination")
    _common(listing)
    listing.add_argument("--category", type=category_index)
    listing.add_argument("--query", help="EH search expression")
    listing.add_argument(
        "--all", action="store_true", help="Fetch all pages before returning a snapshot"
    )
    listing.add_argument("--cursor", help="Resume using next from a previous result")
    listing.add_argument("--max-pages", type=int, default=1000)
    listing.add_argument("--out", type=Path, help="Save a JSON export")
    show = actions.add_parser("show", help="Read a gallery's category and note")
    _common(show)
    show.add_argument("url")
    for name in ("add", "move", "note", "remove"):
        action = actions.add_parser(
            name, help=f"Plan, execute and verify {name} for one or more galleries"
        )
        _common(action)
        _selection(action)
        action.add_argument(
            "--dry-run",
            action="store_true",
            help="Save an operation plan without changing the server",
        )
        action.add_argument(
            "--out",
            type=Path,
            help="Save a result journal here instead of in the user config directory",
        )
    plan = actions.add_parser(
        "plan", help="Save a snapshot-based batch plan without changing favorites"
    )
    _common(plan)
    plan.add_argument("operation", choices=("add", "move", "note", "remove"))
    _selection(plan)
    plan.add_argument("--out", type=Path, help="Destination for the plan")
    for name in ("apply", "rollback"):
        action = actions.add_parser(
            name,
            help="Apply a saved plan"
            if name == "apply"
            else "Restore the original states from a plan",
        )
        _common(action)
        action.add_argument("--plan", required=True, type=Path)
        action.add_argument("--out", type=Path, help="Destination for the result journal")
    return parser


def read_json(path: Path) -> object:
    try:
        value = json.loads(
            sys.stdin.read() if str(path) == "-" else path.read_text(encoding="utf-8-sig")
        )
        if isinstance(value, dict) and value.get("ok") is True and "data" in value:
            return value["data"]
        return value
    except (OSError, UnicodeError, ValueError):
        raise InputError("Could not read valid UTF-8 JSON from the input file/stdin.") from None


def _values(args: argparse.Namespace) -> object:
    if args.input and args.urls:
        raise InputError("Use either gallery URLs or --input, not both.")
    if args.input:
        return read_json(args.input)
    if not args.urls:
        raise InputError("Provide at least one gallery URL or --input FILE.")
    return args.urls


def _run_directory() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return config_dir() / "runs" / f"{stamp}-{uuid.uuid4().hex[:8]}"


def execute(args: argparse.Namespace) -> tuple[dict, int]:
    if args.command == "logout":
        (config_dir() / "credentials.json").unlink(missing_ok=True)
        return {"logged_out": True}, 0
    if args.command == "login":
        if args.jhentai_db and not args.from_jhentai:
            raise InputError("--jhentai-db requires --from-jhentai.")
        if args.from_jhentai:
            cookies, _ = import_jhentai(args.jhentai_db)
        elif args.cookie_file:
            cookies = read_cookie_file(args.cookie_file)
        elif args.cookie_stdin:
            cookies = parse_cookie(sys.stdin.read())
        else:
            if not sys.stdin.isatty():
                raise InputError("Use --cookie-stdin for noninteractive login.")
            cookies = parse_cookie(getpass.getpass("Cookie (hidden): "))
        site = getattr(args, "site", "eh")
    else:
        cookies, saved_site = load_credentials()
        site = getattr(args, "site", saved_site)
    transport = Transport(
        cookies,
        site,
        proxy=getattr(args, "proxy", None),
        interval=getattr(args, "interval", 1.0),
        timeout=getattr(args, "timeout", 30.0),
    )
    client = Client(transport, cookies["ipb_member_id"])
    try:
        if args.command in ("login", "whoami"):
            categories = client.categories()
            result = {
                "authenticated": True,
                "site": site,
                "member_id": client.member_id,
                "favorite_count": sum(c["count"] for c in categories),
            }
            if args.command == "login":
                result["credential_file"] = str(save_credentials(cookies, site))
            return result, 0
        if args.action == "categories":
            return {"items": client.categories()}, 0
        if args.action == "list":
            if args.cursor and not re.fullmatch(r"[0-9-]+", args.cursor):
                raise InputError("Invalid pagination cursor.")
            result = client.list_favorites(
                category=args.category,
                query=args.query,
                all_pages=args.all,
                cursor=args.cursor,
                max_pages=args.max_pages,
            )
            if args.out:
                private_write(args.out, result)
            return result, 0
        if args.action == "show":
            ref = GalleryRef.parse(args.url)
            return {**ref.to_dict(), **client.get_state(ref).to_dict()}, 0
        run_dir = _run_directory()
        if args.action in ("apply", "rollback"):
            plan = read_json(args.plan)
            plan_path = args.plan
        else:
            values = _values(args)
            operation = args.operation if args.action == "plan" else args.action
            plan = make_plan(client, operation, values, category=args.category, note=args.note)
            plan_path = args.out if args.action == "plan" and args.out else run_dir / "plan.json"
            private_write(plan_path, plan)
            if args.action == "plan" or args.dry_run:
                return {**plan, "dry_run": True, "plan_path": str(plan_path)}, 0
        journal = args.out or run_dir / "result.json"
        if journal.resolve() == plan_path.resolve():
            raise InputError("Result journal and plan must use different file paths.")
        result = apply_plan(client, plan, journal=journal, rollback=args.action == "rollback")
        result.update({"plan_path": str(plan_path), "result_path": str(journal)})
        return result, 0 if result["ok"] else 5
    finally:
        transport.close()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = None
    try:
        args = build_parser().parse_args(argv)
        result, code = execute(args)
        output = {"ok": code == 0, "data": result}
    except CLIError as error:
        code = error.exit_code
        output = {"ok": False, "error": {"code": error.code, "message": str(error)}}
    except (OSError, UnicodeError):
        code = 1
        output = {
            "ok": False,
            "error": {
                "code": "local_io_error",
                "message": "Local file operation failed. Check the path and permissions.",
            },
        }
    except (KeyboardInterrupt, EOFError):
        code = 130
        output = {
            "ok": False,
            "error": {
                "code": "interrupted",
                "message": "Interrupted. Use the saved plan to inspect/reconcile any writes.",
            },
        }
    print(
        json.dumps(output, ensure_ascii=False, indent=None if getattr(args, "json", False) else 2)
    )
    return code
