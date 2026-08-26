"""Polite, cached HTTP layer.

This pipeline reads a few thousand pages across several hundred small campaign
websites, most of them running on shared hosting. That imposes some
obligations, which are enforced here rather than left to each source module:

*   an honest, identifiable User-Agent with contact info;
*   robots.txt is fetched once per host and respected;
*   a per-host minimum interval between requests;
*   everything is cached on disk, so re-running the analysis (which happens a
    lot while tuning the classifier) costs zero additional requests.

The module also distinguishes *network egress being blocked by policy* from
*a site being down*, because those need very different responses from an
operator and are easy to confuse.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

USER_AGENT = (
    "dcp-research-bot/0.1 (US House candidate platform analysis; "
    "+https://github.com/readchaoticera/demcandidateplatform)"
)

DEFAULT_CACHE = Path("data/cache")
DEFAULT_TTL = timedelta(days=7)
DEFAULT_NEGATIVE_TTL = timedelta(days=1)
"""Failures are remembered too, but for less time than successes: a site
down today may be up tomorrow, and re-running the pipeline must not spend
minutes re-attempting the same dead domains."""
DEFAULT_MIN_INTERVAL = 1.5  # seconds between requests to the same host
DEFAULT_TIMEOUT = 20


class FetchError(RuntimeError):
    """Base class for fetch failures."""


class EgressBlocked(FetchError):
    """The network policy refused the connection.

    Distinct from a site being unreachable: this means the *environment*
    cannot make the request at all, so retrying or trying another candidate's
    site will fail identically. Callers should abort the run, not soldier on
    recording hundreds of spurious UNKNOWNs.
    """


class RobotsDisallowed(FetchError):
    """robots.txt forbids this path for our user agent."""


@dataclass
class Response:
    url: str
    status: int
    text: str
    from_cache: bool
    fetched_at: datetime

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def _is_egress_block(exc: Exception) -> bool:
    """Heuristic for "the proxy refused us" vs "the site is down".

    The agent proxy answers 403 to CONNECT, which surfaces through requests as
    a ProxyError/TunnelError mentioning the status.
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    markers = (
        "tunnel connection failed",
        "proxyerror",
        "403 forbidden",
        "egress",
        "cannot connect to proxy",
    )
    return any(m in text for m in markers)


class Fetcher:
    """Cached, rate-limited, robots-aware HTTP client."""

    def __init__(
        self,
        cache_dir: Path | str = DEFAULT_CACHE,
        ttl: timedelta = DEFAULT_TTL,
        negative_ttl: timedelta = DEFAULT_NEGATIVE_TTL,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        respect_robots: bool = True,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl
        self.negative_ttl = negative_ttl
        self.min_interval = min_interval
        self.respect_robots = respect_robots
        self.timeout = timeout

        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}

        # Per-host locks let several hosts be fetched at once while keeping
        # requests to any single host strictly serialised and rate-limited.
        # Politeness is a per-host obligation, so concurrency across the few
        # hundred distinct campaign domains costs no site anything.
        self._registry_lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}

        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        # respect_retry_after_header is deliberately OFF. urllib3 honours a
        # Retry-After header with no upper bound, so a single site answering
        # 429 with a large value parks a worker for hours - observed stalling a
        # whole run while every other thread sat idle. Backoff is capped
        # instead, and retries kept low: across several hundred small sites,
        # one that needs three retries is a site that is down.
        retry = Retry(
            total=2,
            backoff_factor=0.5,
            backoff_max=8,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
            respect_retry_after_header=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=16)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # -- cache ---------------------------------------------------------------

    def _cache_path(self, url: str) -> Path:
        h = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / h[:2] / f"{h}.json"

    def _read_cache(self, url: str) -> Optional[Response]:
        p = self._cache_path(url)
        if not p.exists():
            return None
        try:
            blob = json.loads(p.read_text(encoding="utf-8"))
            fetched = datetime.fromisoformat(blob["fetched_at"])
        except (ValueError, KeyError, OSError):
            return None
        status = blob.get("status", 0)
        age = datetime.utcnow() - fetched
        if age > (self.ttl if 200 <= status < 300 else self.negative_ttl):
            return None
        return Response(
            url=blob["url"], status=blob["status"], text=blob["text"],
            from_cache=True, fetched_at=fetched,
        )

    def _write_cache(self, resp: Response) -> None:
        p = self._cache_path(resp.url)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "url": resp.url, "status": resp.status, "text": resp.text,
            "fetched_at": resp.fetched_at.isoformat(),
        })
        # Write-then-rename: a reader never sees a half-written cache entry,
        # even if two threads race on the same URL.
        tmp = p.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, p)

    # -- politeness ----------------------------------------------------------

    def _host_lock(self, host: str) -> threading.Lock:
        with self._registry_lock:
            lock = self._host_locks.get(host)
            if lock is None:
                lock = self._host_locks[host] = threading.Lock()
            return lock

    def _throttle(self, host: str) -> None:
        """Sleep so this host is not hit more often than ``min_interval``.

        Callers must hold the host's lock, so the read-sleep-write sequence
        cannot interleave with another thread targeting the same host.
        """
        last = self._last_hit.get(host)
        if last is not None:
            wait = self.min_interval - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def _robots_allows(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urlparse(url)
        host = parts.netloc
        if host not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            robots_url = f"{parts.scheme}://{host}/robots.txt"
            try:
                with self._host_lock(host):
                    self._throttle(host)
                    r = self.session.get(robots_url, timeout=self.timeout)
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                else:
                    rp = None  # no robots.txt => allowed
            except requests.RequestException as exc:
                if _is_egress_block(exc):
                    raise EgressBlocked(f"egress blocked fetching {robots_url}: {exc}") from exc
                rp = None
            self._robots[host] = rp
        rp = self._robots[host]
        return True if rp is None else rp.can_fetch(USER_AGENT, url)

    # -- public --------------------------------------------------------------

    def get(self, url: str, *, force: bool = False) -> Response:
        """Fetch a URL, using the on-disk cache unless ``force``."""
        if not force:
            cached = self._read_cache(url)
            if cached is not None:
                return cached

        if not self._robots_allows(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")

        host = urlparse(url).netloc
        try:
            with self._host_lock(host):
                self._throttle(host)
                r = self.session.get(url, timeout=self.timeout, allow_redirects=True)
        except requests.RequestException as exc:
            if _is_egress_block(exc):
                raise EgressBlocked(f"egress blocked fetching {url}: {exc}") from exc
            self._write_cache(
                Response(url=url, status=0, text="", from_cache=False,
                         fetched_at=datetime.utcnow())
            )
            raise FetchError(f"failed fetching {url}: {exc}") from exc

        resp = Response(
            url=r.url, status=r.status_code, text=r.text,
            from_cache=False, fetched_at=datetime.utcnow(),
        )
        self._write_cache(resp)
        if resp.url != url:
            # Remember the redirect source too, so the next run short-circuits.
            self._write_cache(Response(url, resp.status, resp.text, False, resp.fetched_at))
        return resp


#: Hosts this pipeline needs. Used by `dcp doctor` to report exactly what an
#: operator has to allowlist before a run can succeed.
REQUIRED_HOSTS: tuple[str, ...] = (
    "en.wikipedia.org",
    "ballotpedia.org",
    "api.open.fec.gov",
    "www.fec.gov",
    "api.congress.gov",
)


def doctor(hosts: tuple[str, ...] = REQUIRED_HOSTS, timeout: int = 10) -> dict[str, str]:
    """Probe each required host and report reachability.

    Run this first. In a restricted environment every host reports
    ``egress-blocked`` and there is no point starting a collection run.
    """
    results: dict[str, str] = {}
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    for host in hosts:
        url = f"https://{host}/"
        try:
            r = session.get(url, timeout=timeout)
            results[host] = f"ok ({r.status_code})"
        except requests.RequestException as exc:
            results[host] = "egress-blocked" if _is_egress_block(exc) else f"error: {type(exc).__name__}"
    return results
