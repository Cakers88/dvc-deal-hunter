"""Polite HTTP session shared by all adapters.

Rules: identify ourselves, keep request rates low, retry gently, never hammer.
"""
from __future__ import annotations

import time
import random
import logging

import requests

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0 Safari/537.36 dvc-deal-hunter/1.0 (personal, low-frequency)"
)


class PoliteSession:
    def __init__(self, min_delay: float = 1.5, timeout: float = 30):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        self.min_delay = min_delay
        self.timeout = timeout
        self._last: dict[str, float] = {}
        self.requests_made = 0

    def _wait(self, host: str) -> None:
        last = self._last.get(host, 0)
        gap = time.time() - last
        if gap < self.min_delay:
            time.sleep(self.min_delay - gap + random.uniform(0, 0.5))
        self._last[host] = time.time()

    def get(self, url: str, **kw) -> requests.Response:
        host = requests.utils.urlparse(url).netloc
        for attempt in range(3):
            self._wait(host)
            try:
                r = self.s.get(url, timeout=self.timeout, **kw)
                self.requests_made += 1
                if r.status_code in (429, 503):
                    wait = int(r.headers.get("Retry-After", "20") or 20)
                    log.warning("%s %s -> %s, backing off %ss", host, url, r.status_code, wait)
                    time.sleep(min(wait, 60))
                    continue
                return r
            except requests.RequestException as e:
                log.warning("GET %s failed (%s), attempt %d", url, e, attempt + 1)
                time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"GET failed after retries: {url}")

    def text(self, url: str, **kw) -> str:
        r = self.get(url, **kw)
        r.raise_for_status()
        return r.text
