"""
Hal Cebimde — PWA sunucusu (FastAPI)
"""
from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import closing
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

log = logging.getLogger("hal_cebimde")
BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
DB_PATH = Path(os.environ.get("HAL_DB", BASE / "hal.db"))

CITIES: dict[str, str] = {
    "Bursa": "Bursa Büyükşehir Belediyesi",
    "İzmir": "İzmir Büyükşehir Belediyesi",
}
TWO = Decimal("0.01")

app = FastAPI(title="Hal Cebimde", docs_url=None, redoc_url=None, openapi_url=None)

CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data:; connect-src 'self'; "
    "manifest-src 'self'; worker-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(self), camera=(), microphone=()"
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response

@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    log.exception("Beklenmeyen hata: %s", request.url.path)
    return JSONResponse({"hata": "Bir hata oluştu."}, status_code=500)

def connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise HTTPException(503, "Veri henüz yok.")
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn

def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

def mid(low: str, high: str) -> Decimal:
    return ((Decimal(low) + Decimal(high)) / 2).quantize(TWO, ROUND_HALF_UP)

CityParam = Annotated[str, Query(min_length=2, max_length=20)]

def check_city(sehir: str) -> str:
    if sehir not in CITIES:
        raise HTTPException(400, f"Desteklenen şehirler: {', '.join(CITIES)}")
    return sehir

@app.get("/api/bulten")
def bulten(sehir: CityParam = "Bursa") -> dict[str, Any]:
    sehir = check_city(sehir)
    with closing(connect()) as conn:
        if not table_exists(conn, "hal_bulten"):
            return {"sehir": sehir, "tarih": None, "onceki_tarih": None, "kaynak": CITIES[sehir], "urunler": []}
        dates = [r[0] for r in conn.execute("SELECT DISTINCT tarih FROM hal_bulten WHERE sehir = ? ORDER BY tarih DESC LIMIT 2", (sehir,))]
        if not dates:
            return {"sehir": sehir, "tarih": None, "onceki_tarih": None, "kaynak": CITIES[sehir], "urunler": []}

        previous: dict[tuple[str, str, str], Decimal] = {}
        if len(dates) == 2:
            for r in conn.execute("SELECT kategori, urun, birim, asgari, azami FROM hal_bulten WHERE sehir = ? AND tarih = ?", (sehir, dates[1])):
                previous[(r["kategori"], r["urun"], r["birim"])] = mid(r["asgari"], r["azami"])

        items = []
        for r in conn.execute("SELECT kategori, urun, birim, asgari, azami FROM hal_bulten WHERE sehir = ? AND tarih = ? ORDER BY rowid", (sehir, dates[0])):
            avg = mid(r["asgari"], r["azami"])
            old = previous.get((r["kategori"], r["urun"], r["birim"]))
            change = None if old is None else float(((avg - old) / old * 100).quantize(Decimal("0.1")))
            items.append({
                "kategori": r["kategori"], "urun": r["urun"], "birim": r["birim"],
                "asgari": float(Decimal(r["asgari"])), "azami": float(Decimal(r["azami"])),
                "ortalama": float(avg), "degisim_yuzde": change,
            })
    return {"sehir": sehir, "tarih": dates[0], "onceki_tarih": dates[1] if len(dates) == 2 else None, "kaynak": CITIES[sehir], "urunler": items}

@app.get("/api/gecmis")
def gecmis(sehir: CityParam = "Bursa", urun: str = "") -> dict[str, Any]:
    """Son 30 günün fiyat geçmişini döndürür."""
    sehir = check_city(sehir)
    with closing(connect()) as conn:
        if not table_exists(conn, "hal_bulten") or not urun:
            return {"urun": urun, "gecmis": []}
        rows = conn.execute(
            """SELECT tarih, (CAST(asgari AS REAL) + CAST(azami AS REAL)) / 2 AS ortalama 
               FROM hal_bulten 
               WHERE sehir = ? AND urun = ? 
               ORDER BY tarih ASC LIMIT 30""", (sehir, urun)
        ).fetchall()
    return {"urun": urun, "gecmis": [{"tarih": r["tarih"], "fiyat": round(r["ortalama"], 2)} for r in rows]}

@app.get("/api/pazarlar")
def pazarlar(sehir: CityParam = "Bursa") -> dict[str, Any]:
    sehir = check_city(sehir)
    with closing(connect()) as conn:
        if not (table_exists(conn, "pazar_yeri") and table_exists(conn, "pazar_gunu")):
            return {"sehir": sehir, "pazarlar": []}
        rows = conn.execute(
            """SELECT p.id, p.ad, p.ilce, p.lat, p.lon, GROUP_CONCAT(g.gun) AS gunler, MAX(g.saat) AS saat
               FROM pazar_yeri p LEFT JOIN pazar_gunu g ON g.pazar_id = p.id
               WHERE p.sehir = ? GROUP BY p.id ORDER BY p.ad""", (sehir,)).fetchall()
    return {"sehir": sehir, "pazarlar": [{
        "id": r["id"], "ad": r["ad"], "ilce": r["ilce"] or "", "lat": r["lat"], "lon": r["lon"],
        "gunler": sorted({int(g) for g in (r["gunler"] or "").split(",") if g}), "saat": r["saat"] or "",
    } for r in rows]}

HEAD = """<!doctype html><html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#1D4531"><link rel="manifest" href="/manifest.webmanifest"><link rel="icon" href="/static/icon.svg" type="image/svg+xml"><link rel="apple-touch-icon" href="/static/icon-192.png"><meta name="apple-mobile-web-app-capable" content="yes"><style>:root{color-scheme:light;padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}body{margin:0;font:14px system-ui,sans-serif}img{max-width:100%}[hidden]{display:none!important}</style></head><body>"""

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    body = (STATIC / "app.html").read_text(encoding="utf-8")
    return HTMLResponse(HEAD + body + "\n</body>\n</html>")

@app.get("/manifest.webmanifest")
def manifest() -> FileResponse:
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

@app.get("/sw.js")
def service_worker() -> FileResponse:
    return FileResponse(STATIC / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})

@app.get("/static/{name}")
def static_file(name: str) -> FileResponse:
    allowed = {"icon.svg", "icon-192.png", "icon-512.png"}
    if name not in allowed:
        raise HTTPException(404)
    return FileResponse(STATIC / name)