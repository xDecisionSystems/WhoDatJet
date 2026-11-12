from __future__ import annotations

from datetime import date, datetime

import httpx
import pytest

from tests.conftest import FAA_FILES, make_zip
from whodatjet.faa_client import (
    CHICAGO,
    ArchiveError,
    Download,
    DownloadError,
    download,
    inspect_archive,
    source_date_for,
)

REQ = ("MASTER.txt", "ACFTREF.txt", "DEREG.txt")
DL_AT = datetime(2026, 11, 12, 0, 20, tzinfo=CHICAGO)


def _dl(path, last_modified="Thu, 12 Nov 2026 05:58:13 GMT"):
    return Download(path, 200, last_modified, '"abc"', DL_AT)


def test_last_modified_converted_to_chicago_date():
    # 05:58 UTC on the 12th is 11:58 p.m. Central on the 11th (Phase 0 observation)
    assert source_date_for("Thu, 12 Nov 2026 05:58:13 GMT", None, DL_AT) == (date(2026, 11, 11), "http_last_modified")


def test_source_date_fallbacks():
    assert source_date_for(None, datetime(2026, 11, 10, 23, 30), DL_AT) == (date(2026, 11, 10), "zip_member_mtime")
    assert source_date_for("garbage", None, DL_AT) == (date(2026, 11, 12), "download_date")


def test_content_hash_ignores_zip_bytes(tmp_path):
    a, b = tmp_path / "a.zip", tmp_path / "b.zip"
    a.write_bytes(make_zip(mtime=(2026, 11, 11, 23, 30, 0)))
    b.write_bytes(make_zip(mtime=(2026, 11, 11, 23, 45, 0)))  # re-zipped: different bytes, same content
    ia, ib = inspect_archive(a, REQ, _dl(a)), inspect_archive(b, REQ, _dl(b))
    assert ia.archive_sha256 != ib.archive_sha256
    assert ia.content_sha256 == ib.content_sha256


def test_content_hash_changes_with_content(tmp_path):
    a, b = tmp_path / "a.zip", tmp_path / "b.zip"
    a.write_bytes(make_zip())
    b.write_bytes(make_zip({**FAA_FILES, "MASTER.txt": FAA_FILES["MASTER.txt"] + "54321,9,2072738,1,\r\n"}))
    assert inspect_archive(a, REQ, _dl(a)).content_sha256 != inspect_archive(b, REQ, _dl(b)).content_sha256


def test_missing_required_file_rejected(tmp_path):
    p = tmp_path / "x.zip"
    p.write_bytes(make_zip({k: v for k, v in FAA_FILES.items() if k != "DEREG.txt"}))
    with pytest.raises(ArchiveError, match="DEREG.txt"):
        inspect_archive(p, REQ, _dl(p))


def test_non_zip_rejected(tmp_path):
    p = tmp_path / "x.zip"
    p.write_bytes(b"<html>Service Unavailable</html>")
    with pytest.raises(ArchiveError):
        inspect_archive(p, REQ, _dl(p))


def test_download_uses_get_and_user_agent(tmp_path):
    body = make_zip()
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.headers["user-agent"]))
        return httpx.Response(200, content=body, headers={"last-modified": "Thu, 12 Nov 2026 05:58:13 GMT"})

    dl = download("https://faa.test/x.zip", tmp_path / "x.zip", user_agent="whodatjet-test",
                  transport=httpx.MockTransport(handler))
    assert seen == [("GET", "whodatjet-test")]
    assert dl.path.read_bytes() == body
    assert dl.last_modified == "Thu, 12 Nov 2026 05:58:13 GMT"


def test_download_retries_then_fails(tmp_path):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503)

    with pytest.raises(DownloadError, match="after 3 attempts"):
        download("https://faa.test/x.zip", tmp_path / "x.zip", user_agent="t", attempts=3, backoff_s=0,
                 transport=httpx.MockTransport(handler))
    assert len(calls) == 3
    assert not list(tmp_path.iterdir())  # no partial file left behind
