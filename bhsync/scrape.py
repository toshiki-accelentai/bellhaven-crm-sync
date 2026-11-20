"""Scrape the Bellhaven website into Location records."""
from __future__ import annotations

import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .models import Location, ScrapeMeta

MAX_PAGES = 20
SLUG_RE = re.compile(r"^/communities/([A-Za-z0-9-]+)/?$")
CITY_RE = re.compile(r"^(.+?),\s*([A-Z]{2})\s+(\d{5})")


def _default_get():
    s = requests.Session()
    s.headers["User-Agent"] = "bhsync/1.0"

    def get(url: str) -> str:
        for attempt in range(3):
            try:
                r = s.get(url, timeout=20)
            except requests.ConnectionError:
                if attempt == 2:
                    raise
            else:
                if r.status_code != 429 and r.status_code < 500:
                    r.raise_for_status()  # other 4xx raise immediately
                    return r.text
                if attempt == 2:
                    r.raise_for_status()
            time.sleep(2 ** attempt)
    return get


def _slugs(html: str) -> list[str]:
    out = []
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        m = SLUG_RE.match(a["href"].split("?")[0])
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def _int(pattern: str, html: str) -> int | None:
    m = re.search(pattern, BeautifulSoup(html, "html.parser").get_text(" "))
    return int(m.group(1)) if m else None


def _parse(slug: str, url: str, html: str) -> Location:
    soup = BeautifulSoup(html, "html.parser")
    h1, dl = soup.find("h1"), soup.find("dl", class_="detail")
    if not (h1 and h1.get_text(strip=True) and dl):
        raise ValueError("missing name or detail list")
    fields = {dt.get_text(strip=True).lower(): dd
              for dt, dd in zip(dl.find_all("dt"), dl.find_all("dd"))}
    if "address" not in fields:
        raise ValueError("missing address")
    lines = [s.strip() for s in fields["address"].stripped_strings]
    m = CITY_RE.match(lines[-1]) if len(lines) >= 2 else None
    if not m:
        raise ValueError(f"unparseable address {lines!r}")

    def text(k):
        return fields[k].get_text(strip=True) if k in fields else ""

    badges = fields["care offerings"].find_all("span", class_="badge") \
        if "care offerings" in fields else []
    return Location(
        slug=slug, url=url, name=h1.get_text(strip=True),
        street=" ".join(lines[:-1]), city=m.group(1).strip(), state=m.group(2),
        zip=m.group(3), offerings=[b.get_text(strip=True) for b in badges],
        administrator=text("administrator"), phone=text("phone"))


def scrape(base_url: str, get=None) -> tuple[list[Location], ScrapeMeta]:
    get = get or _default_get()
    base = base_url.rstrip("/")
    meta = ScrapeMeta(None, None, 0)

    home = get(base + "/")
    meta.stated_count = _int(r"serve\s+(\d+)\s+communities", home)
    slugs = set(_slugs(home)) | set(_slugs(get(base + "/about")))

    seen: list[list[str]] = []
    total, page = 1, 1
    while page <= min(total, MAX_PAGES):
        html = get(f"{base}/communities?page={page}")
        if page == 1:
            total = _int(r"Page\s+1\s+of\s+(\d+)", html) or 1
            meta.directory_count = _int(r"(\d+)\s+communities listed", html)
            if total > MAX_PAGES:
                meta.warnings.append(f"directory has {total} pages; capped at {MAX_PAGES}")
        page_slugs = _slugs(html)
        if page_slugs in seen:
            meta.warnings.append(f"directory page {page} repeated an earlier page; stopped")
            break
        seen.append(page_slugs)
        meta.pages += 1
        slugs.update(page_slugs)
        page += 1

    locations = []
    for slug in sorted(slugs):
        url = urljoin(base + "/", f"communities/{slug}")
        try:
            locations.append(_parse(slug, url, get(url)))
        except ValueError as e:
            meta.warnings.append(f"skipped {slug}: {e}")
    return locations, meta


def gate(locations: list[Location], meta: ScrapeMeta, previous_count: int | None,
         max_drop: int = 2) -> tuple[bool, list[str]]:
    n, reasons = len(locations), []
    if meta.stated_count is not None and n < meta.stated_count:
        reasons.append(f"scraped {n} locations but site states {meta.stated_count}")
    if previous_count is not None and n < previous_count - max_drop:
        reasons.append(f"scraped {n} locations, down from {previous_count} (max drop {max_drop})")
    reasons += [f"warning: {w}" for w in meta.warnings]
    return not reasons, reasons
