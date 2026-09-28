"""Scraper tests using HTML that mirrors the live site's markup (inspected 2026-09-27)."""

import pytest

from scraper.bellhaven import ScrapeError, parse_detail, scrape_locations

BASE = "https://site.test"

HEADER = ('<header><a class="logo" href="/">Bellhaven <b>Senior Living</b></a><nav><a href="/">Home</a>'
          '<a href="/communities">Our Communities</a><a href="/about">About Us</a></nav></header>')


def card(slug, name, city, *care):
    badges = "".join(f'<span class="badge">{c}</span>' for c in care)
    return (f'<div class="card"><h3><a href="/communities/{slug}">{name}</a></h3>'
            f'<div class="city">{city}</div><div>{badges}</div></div>')


def directory(page, total_pages, count, cards):
    nxt = f'<a href="/communities?page={page + 1}">Next →</a>' if page < total_pages else ""
    prev = f'<a href="/communities?page={page - 1}">← Prev</a>' if page > 1 else ""
    return (f'<html><body>{HEADER}<div class="wrap"><h1>Our Communities</h1>'
            f'<p>Page {page} of {total_pages} · {count} communities listed</p><div class="cardgrid">{"".join(cards)}</div>'
            f'<div class="pager">{prev}<span>Page {page} / {total_pages}</span>{nxt}</div></div></body></html>')


def detail(name, street, locality, *care):
    badges = "".join(f'<span class="badge">{c}</span>' for c in care)
    return (f'<html><body>{HEADER}<div class="wrap"><h1>{name}</h1><dl class="detail">'
            f'<dt>Address</dt><dd>{street}<br>{locality}</dd><dt>Care Offerings</dt><dd>{badges}</dd>'
            f'<dt>Administrator</dt><dd>Nadia Duval</dd><dt>Phone</dt><dd>(614) 250-9447</dd></dl>'
            f'<p><a href="/communities">← Back to all communities</a></p></div></body></html>')


PAGES = {
    "/": f'<html><body>{HEADER}<p>35 communities and growing</p>'
         f'<a href="/communities">See all</a> <a href="/communities/bellhaven-meadows-of-findlay">Findlay</a></body></html>',
    "/about": f"<html><body>{HEADER}<p>About</p></body></html>",
    "/communities": directory(1, 2, 3, [card("bellhaven-of-maplewood", "Bellhaven of Maplewood", "Maplewood, OH", "Assisted Living"),
                                        card("bellhaven-of-wooster", "Bellhaven of Wooster", "Wooster, OH",
                                             "Short-Term Rehabilitation &amp; Nursing")]),
    "/communities?page=2": directory(2, 2, 3, [card("bellhaven-shores-of-erie", "Bellhaven Shores of Erie", "Erie, PA",
                                                    "Assisted Living", "Memory Support")]),
    "/communities/bellhaven-of-maplewood": detail("Bellhaven of Maplewood", "210 Orchard Lane", "Maplewood, OH 44280", "Assisted Living"),
    "/communities/bellhaven-of-wooster": detail("Bellhaven of Wooster", "2301 Cleveland Road", "Wooster, OH 44691",
                                                "Short-Term Rehabilitation &amp; Nursing"),
    "/communities/bellhaven-shores-of-erie": detail("Bellhaven Shores of Erie", "100 Lake Rd", "Erie, PA 16505",
                                                    "Assisted Living", "Memory Support"),
    "/communities/bellhaven-meadows-of-findlay": detail("Bellhaven Meadows of Findlay", "1800 N Blanchard St", "Findlay, OH 45840",
                                                        "Assisted Living", "Memory Support"),
}


class FakeResponse:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))


class FakeSession:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, **_):
        path = url.replace(BASE, "") or "/"
        self.calls.append(path)
        return FakeResponse(self.pages[path]) if path in self.pages else FakeResponse("not found", 404)


def test_scrape_follows_pagination_and_finds_offdirectory_community():
    locs = {l.name: l for l in scrape_locations(BASE, session=FakeSession(PAGES))}
    assert set(locs) == {"Bellhaven of Maplewood", "Bellhaven of Wooster", "Bellhaven Shores of Erie",
                         "Bellhaven Meadows of Findlay"}
    findlay = locs["Bellhaven Meadows of Findlay"]
    assert findlay.listed_in_directory is False
    assert (findlay.address, findlay.city, findlay.state, findlay.zip_code) == ("1800 N Blanchard St", "Findlay", "OH", "45840")
    assert findlay.care_offerings == ("Assisted Living", "Memory Support")
    assert locs["Bellhaven of Wooster"].care_offerings == ("Short-Term Rehabilitation & Nursing",)
    assert locs["Bellhaven of Maplewood"].listed_in_directory is True
    assert locs["Bellhaven of Maplewood"].source_url == f"{BASE}/communities/bellhaven-of-maplewood"


def test_scrape_refuses_partial_data():
    pages = dict(PAGES)
    del pages["/communities/bellhaven-of-wooster"]
    with pytest.raises(Exception):
        scrape_locations(BASE, session=FakeSession(pages))


def test_parse_detail_rejects_bad_address():
    with pytest.raises(ScrapeError):
        parse_detail(detail("X", "1 Main St", "Nowhere", "Assisted Living"), "u")


def test_parse_detail_zip_plus_four():
    loc = parse_detail(detail("X", "1 Main St", "Akron, OH 44313-1234", "Assisted Living"), "u")
    assert loc.zip_code == "44313"
