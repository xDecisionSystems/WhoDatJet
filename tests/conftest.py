from __future__ import annotations

import io
import zipfile
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from whodatjet import db
from whodatjet.archive_store import S3ArchiveStore
from whodatjet.config import Config, S3Config

FAA_FILES = {
    "MASTER.txt": "﻿N-NUMBER,SERIAL NUMBER,MFR MDL CODE,UNIQUE ID,\r\n12345,1001,2072738,00600060,\r\n",
    "ACFTREF.txt": "﻿CODE,MFR,MODEL,\r\n2072738,CESSNA,172S,\r\n",
    "DEREG.txt": "﻿N-NUMBER,SERIAL-NUMBER,CANCEL-DATE,\r\n999AB,77,20200101,\r\n",
    "ENGINE.txt": "﻿CODE,MFR,\r\n",
}


def make_zip(files: dict[str, str] | None = None, mtime=(2026, 11, 11, 23, 30, 0)) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in (files or FAA_FILES).items():
            zf.writestr(zipfile.ZipInfo(name, date_time=mtime), text)
    return buf.getvalue()


@pytest.fixture
def s3cfg() -> S3Config:
    return S3Config(endpoint_url=None, region="us-east-1", bucket="archives", prefix="whodatjet/",
                    addressing_style="path", ca_bundle=None, access_key_id="test", secret_access_key="test")


@pytest.fixture
def store(s3cfg, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="archives")
        yield S3ArchiveStore(s3cfg, client=client)


@pytest.fixture
def cfg(tmp_path: Path, s3cfg) -> Config:
    return Config(faa_url="https://faa.test/ReleasableAircraft.zip", user_agent="whodatjet-test",
                  required_files=("MASTER.txt", "ACFTREF.txt", "DEREG.txt"), data_dir=tmp_path / "data",
                  s3=s3cfg, download_attempts=2, download_timeout_s=5)


@pytest.fixture
def conn(tmp_path):
    c = db.connect_writer(tmp_path / "whodatjet.db")
    db.migrate(c, db.load_migrations("registry"))
    return c


@pytest.fixture
def store_id(conn):
    return int(conn.execute("INSERT INTO archive_store (role, kind, location, created_at) "
                            "VALUES ('primary','filesystem','/test','now')").lastrowid)
