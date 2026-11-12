"""Build FAA-format archives from the anonymized Phase 0 fixtures, with optional edits."""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import zipfile
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "faa-2026-11-11"


class Archive:
    def __init__(self) -> None:
        self.files: dict[str, tuple[list[str], list[list[str]]]] = {}
        for name in ("MASTER.txt", "DEREG.txt", "ACFTREF.txt"):
            with open(FIXTURE_DIR / name, encoding="utf-8-sig", newline="") as fh:
                r = csv.reader(fh)
                header = next(r)
                self.files[name] = (header, [row for row in r if row])

    def copy(self) -> Archive:
        return copy.deepcopy(self)

    def _col(self, name: str, column: str) -> int:
        return [h.strip() for h in self.files[name][0]].index(column)

    def rows(self, name: str) -> list[list[str]]:
        return self.files[name][1]

    def get(self, name: str, row: list[str], column: str) -> str:
        return row[self._col(name, column)].strip()

    def set(self, name: str, row: list[str], column: str, value: str) -> None:
        row[self._col(name, column)] = value

    def find(self, name: str, column: str, value: str) -> list[str]:
        return next(r for r in self.rows(name) if self.get(name, r, column) == value)

    def remove(self, name: str, row: list[str]) -> None:
        self.files[name][1].remove(row)

    def add(self, name: str, row: list[str]) -> None:
        self.files[name][1].append(row)

    def drop_column(self, name: str, column: str) -> None:
        i = self._col(name, column)
        header, rows = self.files[name]
        del header[i]
        for r in rows:
            del r[i]

    def zip_bytes(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, (header, rows) in self.files.items():
                text = io.StringIO()
                w = csv.writer(text, lineterminator="\r\n")
                w.writerow(header)
                w.writerows(rows)
                zf.writestr(zipfile.ZipInfo(name, date_time=(2026, 11, 11, 23, 30, 0)), "﻿" + text.getvalue())
        return buf.getvalue()

    def write(self, path: Path) -> tuple[Path, str]:
        data = self.zip_bytes()
        path.write_bytes(data)
        return path, hashlib.sha256(data).hexdigest()
