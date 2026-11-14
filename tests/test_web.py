# ruff: noqa: F811  (fixtures are imported from test_api)
"""Landing page: search on the left, random aircraft on the right; aircraft facts only, no registrant data."""

from __future__ import annotations

from tests.test_api import client, env, world  # noqa: F401  (fixtures)

REGISTRANT_KEYS = {"registrant_name", "street", "street2", "city", "state", "zip_code", "country",
                   "registrant_type"}


def test_home_page_is_public_and_has_both_panels(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    html = r.text
    assert 'id="search-form"' in html and 'class="panel search"' in html and 'class="panel result"' in html
    assert html.index('class="panel search"') < html.index('class="panel result"')      # search left, result right
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp
    assert "<script>" not in html                                                      # no inline script
    for asset in ("/ui/app.js", "/ui/style.css"):
        a = client.get(asset)
        assert a.status_code == 200 and a.headers["cache-control"] == "no-cache"


def test_random_aircraft_has_facts_and_no_registrant_data(client):
    seen = set()
    for _ in range(6):
        r = client.get("/ui/random")
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        body = r.json()
        assert body["found"] is True and body["n_number"].startswith("N") and body["headline"]
        assert body["facts"] and all(set(f) == {"label", "value"} for f in body["facts"])
        text = r.text.lower()
        assert not (REGISTRANT_KEYS & set(body))                                       # keys
        assert "registrant" not in text and "street" not in text and "zip_code" not in text
        seen.add(body["n_number"])
    assert seen                                                                        # something was returned


def test_search_returns_the_searched_aircraft(client, env, world):
    first = client.get("/ui/random").json()["n_number"]
    r = client.get("/ui/search", params={"q": first.lower()})                          # case and format forgiving
    body = r.json()
    assert r.status_code == 200 and body["found"] and body["n_number"] == first
    assert body["meta"]["as_of"] and "confidence" in body["meta"]
    labels = {f["label"] for f in body["facts"]}
    assert {"Manufacturer", "Model"} & labels


def test_search_not_found_and_validation(client):
    r = client.get("/ui/search", params={"q": "N99999"})
    assert r.status_code == 200 and r.json()["found"] is False and r.json()["message"]
    assert client.get("/ui/search", params={"q": "!!"}).status_code == 400
    assert client.get("/ui/search", params={"q": ""}).status_code in (400, 422)
    assert client.get("/ui/search", params={"q": "N1", "date": "garbage"}).status_code == 400
    assert client.get("/ui/search", params={"q": "N1", "date": "2999-01-01"}).status_code == 400    # future
    assert client.get("/ui/search").status_code == 422                                  # q is required


def test_api_endpoints_still_require_keys(client):
    assert client.get("/v1/snapshots").status_code == 401
    assert client.get("/v1/tail-numbers/N1").status_code == 401
    assert client.get("/healthz").status_code == 200
