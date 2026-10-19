"""Network access, behind a small interface so tests can swap in a fake.

``Transport`` is the only thing the pipeline knows about the network. To read
from a local mirror, an S3 client (boto3) or a cache, implement these three
methods; nothing else changes.
"""

from __future__ import annotations

from typing import Protocol

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class NotFoundError(Exception):
    """The requested object does not exist (yet)."""


class Transport(Protocol):
    """Read-only access to objects addressed by URL."""

    def exists(self, url: str) -> bool:
        """True if the object exists. Must not raise for a missing object."""
        ...

    def get_text(self, url: str) -> str:
        """Whole object as text. Raises ``NotFoundError`` if missing."""
        ...

    def get_range(self, url: str, start: int, end: int | None) -> bytes:
        """Bytes ``start..end`` inclusive; ``end=None`` means to end of file.

        Raises ``NotFoundError`` if missing.
        """
        ...


class HttpTransport:
    """Anonymous HTTPS access to the public bucket.

    Retries connection errors, throttling (429) and server errors (5xx) with
    exponential backoff (1s, 2s, 4s, ...).

    Trade-offs:
      * 403 is treated the same as 404 ("not there"), because S3 returns 403
        for missing keys in some configurations. A proxy or firewall that
        blocks the bucket therefore shows up as "missing", not as an error.
      * A short read raises ``IOError`` and is not retried here; the file is
        marked failed and picked up on the next run.
    """

    def __init__(self, timeout: float = 60.0, retries: int = 5):
        self.timeout = timeout
        retry = Retry(
            total=retries,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "HEAD"),
        )
        self.session = requests.Session()
        self.session.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=16))

    def exists(self, url: str) -> bool:
        resp = self.session.head(url, timeout=self.timeout)
        if resp.status_code in (403, 404):  # S3 returns 403 for missing keys sometimes
            return False
        resp.raise_for_status()
        return True

    def get_text(self, url: str) -> str:
        resp = self._get(url)
        return resp.text

    def get_range(self, url: str, start: int, end: int | None) -> bytes:
        rng = f"bytes={start}-{'' if end is None else end}"
        resp = self._get(url, headers={"Range": rng})
        data = resp.content
        if end is not None and len(data) != end - start + 1:
            raise IOError(
                f"Short read for {url} {rng}: got {len(data)} bytes"
            )
        return data

    def _get(self, url: str, headers: dict | None = None) -> requests.Response:
        resp = self.session.get(url, headers=headers, timeout=self.timeout)
        if resp.status_code in (403, 404):
            raise NotFoundError(url)
        resp.raise_for_status()
        return resp
