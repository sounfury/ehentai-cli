from __future__ import annotations

import math
import time
from urllib.parse import urlsplit

import requests

from . import __version__
from .errors import AuthError, InputError, NetworkError, RateLimitError
from .models import SITES


class Transport:
    """Credential-scoped requests with bounded GET retries and no POST retries/redirects."""

    def __init__(
        self,
        cookies: dict[str, str],
        site: str,
        *,
        proxy: str | None = None,
        interval: float = 1.0,
        timeout: float = 30.0,
    ):
        if site not in SITES:
            raise InputError("Site must be eh or ex.")
        if (
            not math.isfinite(interval)
            or not math.isfinite(timeout)
            or interval < 0
            or timeout <= 0
        ):
            raise InputError(
                "Interval must be finite and nonnegative; timeout must be finite and positive."
            )
        try:
            proxy_scheme = urlsplit(proxy).scheme if proxy else None
        except ValueError:
            raise InputError("Malformed proxy URL.") from None
        if proxy and proxy_scheme not in ("http", "https", "socks5", "socks5h"):
            raise InputError("Proxy must use http, https, socks5, or socks5h.")
        self.site = site
        self.base_url = SITES[site]
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": f"ehentai-cli/{__version__}", "Accept": "text/html"}
        )
        self.session.cookies.set("nw", "1", domain=urlsplit(self.base_url).hostname, path="/")
        self.session.cookies.set("datatags", "1", domain=urlsplit(self.base_url).hostname, path="/")
        for name, value in cookies.items():
            self.session.cookies.set(name, value, domain=urlsplit(self.base_url).hostname, path="/")
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        self.interval = interval
        self.timeout = timeout
        self._last_request = 0.0

    def close(self) -> None:
        self.session.close()

    def request(
        self, method: str, path: str, *, params: dict | None = None, data: dict | None = None
    ) -> str:
        # No arbitrary URL may receive the user's cookies, including through redirects.
        if not path.startswith("/") or path.startswith("//") or ":" in path or "\\" in path:
            raise InputError("Requests must use a relative path on the selected site.")
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            pause = self.interval - (time.monotonic() - self._last_request)
            if pause > 0:
                time.sleep(pause)
            self._last_request = time.monotonic()
            try:
                response = self.session.request(
                    method,
                    self.base_url + path,
                    params=params,
                    data=data,
                    timeout=(min(self.timeout, 15.0), self.timeout),
                    allow_redirects=False,
                )
            except requests.RequestException:
                if attempt + 1 < attempts:
                    time.sleep(min(2**attempt, 8))
                    continue
                raise NetworkError(
                    "Request failed or timed out. Check connectivity, proxy, and site access."
                ) from None
            if response.status_code in (429, 502, 503, 504) and attempt + 1 < attempts:
                delay = response.headers.get("Retry-After", "")
                time.sleep(min(float(delay), 15) if delay.isdigit() else min(2 ** (attempt + 1), 8))
                continue
            if response.status_code == 429:
                raise RateLimitError("Site rate limit reached. Wait before retrying.")
            if response.status_code in (401, 403):
                raise AuthError(
                    "Site denied access. Check Cookie validity, EX permissions, or browser verification."
                )
            if 300 <= response.status_code < 400:
                raise AuthError(
                    "Site redirected the request. Check login/site access; redirects are not followed."
                )
            if response.status_code != 200:
                raise NetworkError(f"Site returned HTTP {response.status_code}.")
            if not response.content:
                raise AuthError(
                    "Site returned an empty page. Check EX permissions and the igneous cookie."
                )
            response.encoding = "utf-8"
            html = response.text
            lower = html[:12000].lower()
            if "your ip address" in lower or "this ip address has been" in lower:
                raise RateLimitError(
                    "Site temporarily blocked this IP. Stop requests and try later."
                )
            if "cf-chl-" in lower or "just a moment..." in lower:
                raise AuthError("Browser verification required. Refresh your Cookie in a browser.")
            return html
        raise NetworkError("Request failed.")  # Defensive: loop always returns or raises.
