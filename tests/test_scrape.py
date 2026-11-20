from pathlib import Path

import pytest

from bhsync.scrape import gate, scrape

SITE = Path(__file__).parent / "fixtures" / "site"
BASE = "https://example.test"


def make_get(overrides=None, calls=None):
    overrides = overrides or {}

    def get(url):
        if calls is not None:
            calls.append(url)
        path = url[len(BASE):]
        if path in overrides:
            return overrides[path]
        if path.startswith("/communities?page="):
            n = min(int(path.rsplit("=", 1)[1]), 3)  # site clamps beyond last page
            return (SITE / f"communities_p{n}.html").read_text(encoding="utf-8")
        name = {"/": "index", "/about": "about"}.get(path) or path.lstrip("/")
        return (SITE / f"{name}.html").read_text(encoding="utf-8")
    return get


@pytest.fixture(scope="module")
def result():
    calls = []
    locs, meta = scrape(BASE, make_get(calls=calls))
    return locs, meta, calls


def by_slug(locs):
    return {loc.slug: loc for loc in locs}


def test_counts_and_terminates(result):
    locs, meta, calls = result
    assert len(locs) == 35
    assert meta.stated_count == 35 and meta.directory_count == 34
    assert meta.pages == 3 and meta.warnings == []
    assert not any("page=4" in c for c in calls)


def test_no_duplicates_sorted(result):
    slugs = [loc.slug for loc in result[0]]
    assert len(slugs) == len(set(slugs)) and slugs == sorted(slugs)


def test_findlay_homepage_only(result):
    f = by_slug(result[0])["bellhaven-meadows-of-findlay"]
    assert f.offerings == ["Assisted Living", "Memory Support"]
    assert f.url == f"{BASE}/communities/bellhaven-meadows-of-findlay"
    assert (f.street, f.city, f.state, f.zip) == ("1800 N Blanchard St", "Findlay", "OH", "45840")


def test_entities_and_zip(result):
    d = by_slug(result[0])
    assert d["bellhaven-rehabilitation-nursing-of-grove-city"].name == \
        "Bellhaven Rehabilitation & Nursing of Grove City"
    assert d["bellhaven-of-portsmouth"].zip == "45662"


def test_repeated_page_stops_with_warning():
    p1 = (SITE / "communities_p1.html").read_text(encoding="utf-8")
    locs, meta = scrape(BASE, make_get({"/communities?page=2": p1}))
    assert meta.pages == 1
    assert any("repeated" in w for w in meta.warnings)
    assert gate(locs, meta, None)[0] is False


def test_gate(result):
    locs, meta, _ = result
    assert gate(locs, meta, None) == (True, [])
    assert gate(locs, meta, 40)[0] is False
    assert gate(locs[:-1], meta, None)[0] is False


def test_malformed_detail_warns():
    bad = "<html><body><h1>Broken</h1><dl class='detail'><dt>Phone</dt><dd>1</dd></dl></body></html>"
    locs, meta = scrape(BASE, make_get({"/communities/bellhaven-of-tiffin": bad}))
    assert len(locs) == 34
    assert any("bellhaven-of-tiffin" in w for w in meta.warnings)
    ok, reasons = gate(locs, meta, None)
    assert not ok and reasons
