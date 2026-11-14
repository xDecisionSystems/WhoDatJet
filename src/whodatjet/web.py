"""Landing page for humans: search on the left, a random aircraft (replaced by search results) on the right.

The page and its `/ui/*` JSON endpoints need no API key, so they deliberately show **aircraft-level facts only**
(make, model, year, type, engine, registration dates). Registrant names and addresses, and the history/aircraft
endpoints, stay behind API keys under `/v1`.
"""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response

from whodatjet import query
from whodatjet.config import Config
from whodatjet.normalize import InvalidNNumber, normalize_n_number

STATIC = Path(__file__).parent / "web_static"
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
AIRCRAFT_TYPE = {"1": "Glider", "2": "Balloon", "3": "Blimp / dirigible", "4": "Fixed wing, single engine",
                 "5": "Fixed wing, multi engine", "6": "Rotorcraft", "7": "Weight-shift control",
                 "8": "Powered parachute", "9": "Gyroplane", "H": "Hybrid lift", "O": "Other"}
ENGINE_TYPE = {"0": "None", "1": "Reciprocating", "2": "Turbo-prop", "3": "Turbo-shaft", "4": "Turbo-jet",
               "5": "Turbo-fan", "6": "Ramjet", "7": "Two-cycle", "8": "Four-cycle", "9": "Unknown",
               "10": "Electric", "11": "Rotary"}
CATEGORY = {"1": "Land", "2": "Sea", "3": "Amphibian"}
STATUS = {"V": "Valid", "E": "Expired", "R": "Registration pending", "D": "Expired (dealer)"}
NOT_FOUND_TEXT = {
    "no_records": "The registry has no record of this N-number.",
    "before_observed_coverage": "This date is before the data WhoDatJet holds for this N-number.",
    "in_gap": "This date falls in a gap in the collected data, so the registration cannot be confirmed.",
    "unassigned": "No aircraft held this N-number on this date.",
}


def _clean(value) -> str | None:
    text = "" if value is None else str(value).strip()
    return text or None


def describe(record: dict) -> dict:
    """Turn a lookup result into the card shown on the page (no registrant fields)."""
    make, model = _clean(record.get("manufacturer_name")), _clean(record.get("model_name"))
    kind = AIRCRAFT_TYPE.get(str(record.get("aircraft_type")), _clean(record.get("aircraft_type")))
    cat = CATEGORY.get(str(record.get("aircraft_category")))
    engine = ENGINE_TYPE.get(str(record.get("engine_type")), _clean(record.get("engine_type")))
    count = record.get("engine_count")
    status = _clean(record.get("status_code"))
    facts = [
        ("Manufacturer", make), ("Model", model), ("Year built", _clean(record.get("year_mfr"))),
        ("Type", ", ".join(x for x in (kind, cat) if x) or None),
        ("Engine", " · ".join(x for x in (engine, f"{count} engine{'s' if count != 1 else ''}"
                                          if count is not None else None) if x) or None),
        ("Seats", _clean(record.get("seats"))), ("Serial number", _clean(record.get("serial_number"))),
        ("Registration", STATUS.get(status or "", f"status {status}" if status else None)),
        ("Certificate issued", _clean(record.get("certificate_issue_date"))),
        ("Registration expires", _clean(record.get("expiration_date"))),
        ("Mode S (hex)", _clean(record.get("mode_s_code_hex"))),
        ("Registered since", _clean(record.get("valid_from"))),
        ("Last FAA action", _clean(record.get("last_action_date"))),
    ]
    return {
        "found": True, "n_number": record["n_number"],
        "headline": " ".join(x for x in (make, model) if x) or "Aircraft",
        "facts": [{"label": k, "value": v} for k, v in facts if v],
        "meta": {"as_of": record.get("date"), "observed": record.get("observation_date"),
                 "basis": record.get("basis"), "confidence": record.get("data_confidence"),
                 "flags": record.get("data_quality_flags") or []},
    }


def register(app: FastAPI, cfg: Config, registry: Callable[[], sqlite3.Connection],
             today_fn: Callable[[], date]) -> None:
    def page(name: str, media: str) -> Response:
        r = FileResponse(STATIC / name, media_type=media)
        r.headers["Cache-Control"] = "no-cache"
        r.headers["Content-Security-Policy"] = CSP
        r.headers["X-Content-Type-Options"] = "nosniff"
        return r

    @app.get("/", include_in_schema=False)
    def home():
        return page("index.html", "text/html; charset=utf-8")

    @app.get("/ui/app.js", include_in_schema=False)
    def js():
        return page("app.js", "text/javascript; charset=utf-8")

    @app.get("/ui/style.css", include_in_schema=False)
    def css():
        return page("style.css", "text/css; charset=utf-8")

    def card(n: str, d: date) -> dict:
        try:
            result = query.lookup(registry(), n, d, today=today_fn(), gap_threshold=cfg.gap_threshold,
                                  stale_days=cfg.stale_days)
        except query.QueryError as exc:
            raise HTTPException(400, str(exc)) from exc
        if isinstance(result, query.NotFound):
            return {"found": False, "n_number": result.n_number, "date": result.date, "reason": result.reason,
                    "message": NOT_FOUND_TEXT.get(result.reason, "No registration found."),
                    "coverage_start": result.coverage_start, "coverage_end": result.coverage_end}
        return describe(result)

    @app.get("/ui/search", include_in_schema=False)
    def search(q: str, as_of: str | None = Query(None, alias="date")):
        try:
            n = normalize_n_number(q)
        except InvalidNNumber as exc:
            raise HTTPException(400, str(exc)) from exc
        try:
            d = date.fromisoformat(as_of) if as_of else today_fn()
        except ValueError as exc:
            raise HTTPException(400, f"invalid date {as_of!r}; expected YYYY-MM-DD") from exc
        return _no_store(card(n, d))

    @app.get("/ui/random", include_in_schema=False)
    def random_aircraft():
        conn = registry()
        top = conn.execute("SELECT max(id) FROM tail_number").fetchone()[0]
        if not top:
            return _no_store({"found": False, "n_number": "", "message": "The registry is empty.", "reason": "empty"})
        for _ in range(12):  # most random picks are current registrations; retry past deregistered ones
            row = conn.execute("SELECT n_number FROM tail_number WHERE id >= ? ORDER BY id LIMIT 1",
                               (random.randint(1, top),)).fetchone()
            if row is None:
                continue
            result = card(row["n_number"], today_fn())
            if result["found"]:
                return _no_store(result)
        return _no_store({"found": False, "n_number": "", "message": "Could not pick an aircraft; try again.",
                          "reason": "retry"})


def _no_store(body: dict) -> JSONResponse:
    return JSONResponse(body, headers={"Cache-Control": "no-store"})
