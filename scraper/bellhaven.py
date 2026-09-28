"""Scraper for Bellhaven Senior Living communities on the assessment website.

Site structure (inspected 2026-09-27 at https://analyst-assessment-production.up.railway.app):

* ``/communities`` is a paginated directory ("Page 1 of 3 · 34 communities listed").
  Each ``div.card`` has ``h3 > a[href="/communities/<slug>"]``, ``div.city`` and
  ``span.badge`` care types. Pagination is ``div.pager`` with a "Next →" link.
* The home page links to a community that is NOT in the directory
  (``/communities/bellhaven-meadows-of-findlay``, "newly added"), and says
  "35 communities". So links are collected from the home page, the about page and
  every directory page, not just the directory.
* Each detail page has ``h1`` (name) and ``dl.detail`` with ``dt``/``dd`` pairs:
  ``Address`` (``street<br>City, ST ZIP``), ``Care Offerings`` (``span.badge``),
  ``Administrator`` and ``Phone``.

Run ``python -m scraper.bellhaven`` to scrape and write ``data/locations.csv``.
"""

from __future__ import annotations

import csv
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://analyst-assessment-production.up.railway.app"
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_RETRIES = 3
USER_AGENT = "clipboard-crm-reconciliation/0.2 (assessment scraper)"

SEED_PATHS = ("/", "/communities", "/about")
COMMUNITY_PATH = re.compile(r"^/communities/[a-z0-9][a-z0-9-]*/?$")
CITY_STATE_ZIP = re.compile(r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5})(?:-\d{4})?$")


class ScrapeError(RuntimeError):
    """Raised when a page does not have the expected structure."""


@dataclass(frozen=True)
class Location:
    """One Bellhaven community as published on the website."""

    name: str
    address: str
    city: str
    state: str
    zip_code: str
    care_offerings: tuple[str, ...] = field(default_factory=tuple)
    source_url: str = ""
    listed_in_directory: bool = True
    administrator: str = ""
    phone: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["care_offerings"] = list(self.care_offerings)
        return data


def _clean(text: Optional[str]) -> str:
    return " ".join((text or "").split())


def fetch_html(url: str, session: requests.Session, retries: int = DEFAULT_RETRIES,
               timeout: int = DEFAULT_TIMEOUT_SECONDS) -> str:
    """GET a page with retry and exponential backoff on network errors and 5xx."""
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
            if response.status_code >= 500:
                raise requests.HTTPError(f"{response.status_code} from {url}", response=response)
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            logger.warning("Fetch failed for %s (attempt %d/%d): %s", url, attempt, retries, exc)
            if attempt < retries:
                time.sleep(2 ** attempt)
    assert last_error is not None
    raise last_error


def extract_community_links(html: str, page_url: str) -> set[str]:
    """Return absolute URLs of every community detail page linked from ``html``."""
    soup = BeautifulSoup(html, "html.parser")
    host = urlparse(page_url).netloc
    links: set[str] = set()
    for a in soup.select("a[href]"):
        absolute = urljoin(page_url, str(a["href"]))
        parsed = urlparse(absolute)
        if parsed.netloc == host and COMMUNITY_PATH.match(parsed.path):
            links.add(f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}")
    return links


def extract_next_page(html: str, page_url: str) -> Optional[str]:
    """Return the absolute URL of the directory's "Next" page, if any."""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.select(".pager a[href]"):
        if "next" in a.get_text(" ").lower():
            return urljoin(page_url, str(a["href"]))
    return None


def parse_directory_count(html: str) -> Optional[int]:
    """Read "N communities listed" from a directory page, if present."""
    match = re.search(r"(\d+)\s+communities\s+listed", BeautifulSoup(html, "html.parser").get_text(" "))
    return int(match.group(1)) if match else None


def parse_detail(html: str, url: str, listed_in_directory: bool = True) -> Location:
    """Parse a community detail page.

    Raises:
        ScrapeError: if the name, address or care offerings cannot be parsed.
    """
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.select_one("h1")
    name = _clean(h1.get_text(" ")) if h1 else ""
    if not name:
        raise ScrapeError(f"No community name (h1) on {url}")

    fields: dict[str, object] = {}
    for dt in soup.select("dl.detail dt"):
        dd = dt.find_next_sibling("dd")
        if dd is not None:
            fields[_clean(dt.get_text(" ")).lower()] = dd

    address_dd = fields.get("address")
    if address_dd is None:
        raise ScrapeError(f"No Address on {url}")
    lines = [_clean(s) for s in address_dd.get_text("\n").split("\n") if _clean(s)]  # type: ignore[union-attr]
    if len(lines) < 2:
        raise ScrapeError(f"Unexpected address format on {url}: {lines!r}")
    street, locality = " ".join(lines[:-1]), lines[-1]
    match = CITY_STATE_ZIP.match(locality)
    if not match:
        raise ScrapeError(f"Cannot parse city/state/ZIP {locality!r} on {url}")

    care_dd = fields.get("care offerings")
    offerings = tuple(sorted({_clean(b.get_text(" ")) for b in care_dd.select(".badge")})) if care_dd else ()  # type: ignore[union-attr]
    if not offerings:
        raise ScrapeError(f"No care offerings on {url}")

    def text(key: str) -> str:
        node = fields.get(key)
        return _clean(node.get_text(" ")) if node is not None else ""  # type: ignore[union-attr]

    return Location(
        name=name,
        address=street,
        city=match.group("city").strip(),
        state=match.group("state"),
        zip_code=match.group("zip"),
        care_offerings=offerings,
        source_url=url,
        listed_in_directory=listed_in_directory,
        administrator=text("administrator"),
        phone=text("phone"),
    )


def scrape_locations(base_url: Optional[str] = None, max_pages: int = 50,
                     session: Optional[requests.Session] = None) -> list[Location]:
    """Scrape every Bellhaven community.

    Crawls the home and about pages plus every directory page, collects all
    community links, then parses each detail page. Communities linked outside
    the directory are kept and marked ``listed_in_directory=False``.
    """
    base_url = (base_url or os.getenv("BELLHAVEN_URL") or DEFAULT_BASE_URL).rstrip("/")
    session = session or requests.Session()

    directory_links: set[str] = set()
    other_links: set[str] = set()
    expected_count: Optional[int] = None

    for path in SEED_PATHS:
        if path == "/communities":
            continue
        other_links |= extract_community_links(fetch_html(base_url + path, session), base_url + path)

    url: Optional[str] = base_url + "/communities"
    seen: set[str] = set()
    while url and url not in seen and len(seen) < max_pages:
        seen.add(url)
        html = fetch_html(url, session)
        expected_count = expected_count or parse_directory_count(html)
        directory_links |= extract_community_links(html, url)
        url = extract_next_page(html, url)

    if expected_count is not None and len(directory_links) != expected_count:
        logger.warning("Directory says %d communities but %d links were found", expected_count, len(directory_links))
    extra = other_links - directory_links
    if extra:
        logger.info("Found %d community page(s) linked outside the directory: %s", len(extra), sorted(extra))

    locations: list[Location] = []
    failures: list[str] = []
    for link in sorted(directory_links | other_links):
        try:
            locations.append(parse_detail(fetch_html(link, session), link, listed_in_directory=link in directory_links))
        except (ScrapeError, requests.RequestException) as exc:
            failures.append(f"{link}: {exc}")
            logger.error("Could not scrape %s: %s", link, exc)
    if failures:
        raise ScrapeError(f"{len(failures)} community page(s) failed; refusing to reconcile on partial data: {failures}")

    logger.info("Scraped %d communities (%d from the directory, %d linked elsewhere)",
                len(locations), len(directory_links), len(extra))
    return locations


def write_csv(locations: Iterable[Location], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [loc.to_dict() for loc in locations]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(Location.__dataclass_fields__))
        writer.writeheader()
        for row in rows:
            row["care_offerings"] = ";".join(row["care_offerings"])
            writer.writerow(row)


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    result = scrape_locations()
    write_csv(result, "data/locations.csv")
    print(f"Scraped {len(result)} communities -> data/locations.csv")
