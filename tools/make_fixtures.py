"""Build small, anonymized test fixtures from a real extracted FAA archive.

Usage: python -I tools/make_fixtures.py <extracted_dir> <out_dir>

Selects rows that exercise known edge cases (Phase 0 findings) and replaces every
registrant name, address, and "other names" field with synthetic values, because the
repository is public. Aircraft fields (N-number, serial, codes, dates, Mode S) stay real.
"""

from __future__ import annotations

import collections
import csv
import re
import sys
from pathlib import Path

src, out = Path(sys.argv[1]), Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)

PII = re.compile(r"^(NAME|STREET.*|CITY.*|ZIP.*|COUNTY.*|OTHER[ -]NAMES.*)$")


def read(name):
    with open(src / name, encoding="utf-8-sig", newline="") as f:
        r = csv.reader(f)
        header = next(r)
        rows = list(r)
    return header, rows


def col(header, name):
    return [h.strip() for h in header].index(name)


def anonymize(header, rows):
    idx = [i for i, h in enumerate(header) if PII.match(h.strip())]
    out_rows = []
    for n, row in enumerate(rows, 1):
        row = list(row)
        for i in idx:
            if i < len(row) and row[i].strip():
                h = header[i].strip()
                if h == "NAME":
                    row[i] = f"REGISTRANT {n:04d}"
                elif h.startswith("STREET"):
                    row[i] = f"{n} TEST ST"
                elif h.startswith("CITY"):
                    row[i] = "TESTVILLE"
                elif h.startswith("ZIP"):
                    row[i] = "00000"
                elif h.startswith("COUNTY"):
                    row[i] = "000"
                else:
                    row[i] = f"OTHER NAME {n:04d}"
        out_rows.append(row)
    return out_rows


def write(name, header, rows):
    # Reproduce the FAA format: UTF-8 BOM, CRLF, trailing comma column already in the data.
    with open(out / name, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, lineterminator="\r\n")
        w.writerow(header)
        w.writerows(rows)


mh, mrows = read("MASTER.txt")
gh, grows = read("DEREG.txt")
ah, arows = read("ACFTREF.txt")
M = {k: col(mh, k) for k in ("N-NUMBER", "SERIAL NUMBER", "MFR MDL CODE", "KIT MFR", "STATUS CODE", "CERT ISSUE DATE")}
G = {k: col(gh, k) for k in ("N-NUMBER", "SERIAL-NUMBER", "MFR-MDL-CODE", "CERT-ISSUE-DATE", "CANCEL-DATE")}

picked_m: dict[str, list[str]] = {}


def pick_m(pred, n, label):
    got = 0
    for r in mrows:
        if got >= n:
            break
        if r[M["N-NUMBER"]] not in picked_m and pred(r):
            picked_m[r[M["N-NUMBER"]]] = r
            got += 1
    print(f"MASTER {label}: {got}")


def ser(r):
    return r[M["SERIAL NUMBER"]].strip()


pick_m(lambda r: re.fullmatch(r"\d+", ser(r)) and not ser(r).startswith("0"), 6, "plain numeric serial")
pick_m(lambda r: " " in ser(r), 3, "serial with space")
pick_m(lambda r: "-" in ser(r), 4, "serial with hyphen")
pick_m(lambda r: ser(r).startswith("0"), 3, "serial with leading zero")
pick_m(lambda r: ser(r).upper() in {"0", "00", ""}, 3, "placeholder serial")
pick_m(lambda r: r[M["KIT MFR"]].strip(), 3, "kit aircraft")
pick_m(lambda r: r[M["STATUS CODE"]].strip() not in ("V",), 4, "non-valid status")
pick_m(lambda r: not r[M["CERT ISSUE DATE"]].strip(), 2, "no certificate date")
# Phase 0 collision: same 3-char mfr prefix + serial "1" on different aircraft
pick_m(lambda r: r[M["MFR MDL CODE"]].startswith("056") and ser(r) == "1", 4, "mfr3+serial collision")
# N-numbers containing letters
pick_m(lambda r: re.search(r"[A-Z]{2}$", r[M["N-NUMBER"]].strip()), 2, "two trailing letters")

# DEREG: rows for picked aircraft (history of the same airframe), plus edge cases
picked_g: list[list[str]] = []
mkeys = {(r[M["MFR MDL CODE"]].strip(), re.sub(r"[\s-]", "", ser(r).upper())) for r in picked_m.values()}
mns = set(picked_m)
for r in grows:
    k = (r[G["MFR-MDL-CODE"]].strip(), re.sub(r"[\s-]", "", r[G["SERIAL-NUMBER"]].strip().upper()))
    if k in mkeys or r[G["N-NUMBER"]].strip() in mns:
        picked_g.append(r)
print("DEREG rows linked to picked MASTER aircraft or N-numbers:", len(picked_g))
counts = collections.Counter()
for r in grows:
    cert, cancel = r[G["CERT-ISSUE-DATE"]].strip(), r[G["CANCEL-DATE"]].strip()
    if not cert and counts["nocert"] < 3:
        picked_g.append(r)
        counts["nocert"] += 1
    elif cert and cancel and cancel < cert and counts["inverted"] < 3:
        picked_g.append(r)
        counts["inverted"] += 1
    elif not cancel and counts["nocancel"] < 2:
        picked_g.append(r)
        counts["nocancel"] += 1
    elif cancel[:4] < "1960" and cancel and counts["old"] < 2:
        picked_g.append(r)
        counts["old"] += 1
print("DEREG edge cases:", dict(counts))

codes = {r[M["MFR MDL CODE"]].strip() for r in picked_m.values()} | {r[G["MFR-MDL-CODE"]].strip() for r in picked_g}
picked_a = [r for r in arows if r[0].strip() in codes]
print("ACFTREF rows:", len(picked_a))

write("MASTER.txt", mh, anonymize(mh, list(picked_m.values())))
write("DEREG.txt", gh, anonymize(gh, picked_g))
write("ACFTREF.txt", ah, picked_a)
print("wrote", out)
