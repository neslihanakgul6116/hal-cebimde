#!/usr/bin/env python3
"""
Pazar yerlerini veritabanına aktarır: OpenStreetMap (otomatik) + CSV (saha düzeltmesi).

    python osm_pazar.py --sehir Bursa                 # OSM'den çek (Overpass API)
    python osm_pazar.py --sehir Bursa --dump          # yazmadan göster
    python osm_pazar.py --sehir Bursa --csv saha.csv  # elle toplanan pazarları ekle/düzelt

Pazarın kurulduğu gün üç kaynaktan gelir, öncelik sırasıyla:
  1) elle  : CSV'den (saha ziyareti) — OSM içe aktarması bunu ASLA ezmez
  2) osm   : OSM `opening_hours` etiketi (örn. "Tu 08:00-18:00", "Mo,Th")
  3) isim  : adından çıkarım ("Salı Pazarı" → Salı)

CSV biçimi (Excel'den ';' ya da ',' ayraçlı, UTF-8):
    ad;ilce;lat;lon;gunler;saat
    Gün yazımı: "Sal,Cum" ya da "Salı Cuma" ya da "2,5"  (1=Pazartesi … 7=Pazar)

Veri lisansı: OpenStreetMap verisi ODbL'dir. Uygulamada "© OpenStreetMap katkıcıları"
atfı gösterilmelidir (arayüz bunu yapıyor).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "HalCebimde/0.1 (pazar yeri iceri aktarma)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pazar_yeri (
    id          TEXT PRIMARY KEY,        -- osm:node/123 | elle:nilufer-sali-pazari
    sehir       TEXT NOT NULL,
    ad          TEXT NOT NULL,
    ilce        TEXT,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    kaynak      TEXT NOT NULL,           -- osm | elle
    guncelleme  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pazar_gunu (
    pazar_id    TEXT NOT NULL REFERENCES pazar_yeri(id),
    gun         INTEGER NOT NULL CHECK (gun BETWEEN 1 AND 7),   -- 1=Pzt … 7=Paz
    saat        TEXT,
    kaynak      TEXT NOT NULL,           -- elle | osm | isim
    PRIMARY KEY (pazar_id, gun)
);
CREATE INDEX IF NOT EXISTS ix_pazar_sehir ON pazar_yeri (sehir);
"""


# =============================================================================
# DOMAIN
# =============================================================================
@dataclass(frozen=True, slots=True)
class Market:
    id: str
    city: str
    name: str
    district: str
    lat: float
    lon: float
    source: str                                   # osm | elle
    days: frozenset[int] = field(default_factory=frozenset)
    hours: str = ""
    day_source: str = ""                          # elle | osm | isim | ""


# =============================================================================
# GÜN ÇIKARIMI
# =============================================================================
OSM_DAYS = {"Mo": 1, "Tu": 2, "We": 3, "Th": 4, "Fr": 5, "Sa": 6, "Su": 7}
OSM_DAY_RE = re.compile(r"\b(Mo|Tu|We|Th|Fr|Sa|Su)(?:\s*-\s*(Mo|Tu|We|Th|Fr|Sa|Su))?\b")
HOURS_RE = re.compile(r"\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}")

# ASCII'ye katlanmış Türkçe gün adları. Uzun olan önce: "cumartesi" "cuma"dan, "pazartesi" "pazar"dan önce.
TR_DAYS = [("pazartesi", 1), ("sali", 2), ("carsamba", 3), ("persembe", 4),
           ("cumartesi", 6), ("cuma", 5), ("pazar", 7)]
TR_SHORT = {"pzt": 1, "sal": 2, "car": 3, "per": 4, "cum": 5, "cmt": 6, "paz": 7}


def fold(text: str) -> str:
    """Türkçe → küçük harf ASCII: 'SALI Pazarı' → 'sali pazari'"""
    text = text.replace("İ", "i").replace("I", "i").replace("ı", "i").lower()
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def days_from_opening_hours(value: str) -> tuple[frozenset[int], str]:
    """OSM opening_hours → (günler, saat). Desteklenen alt küme: 'Mo-Fr 08:00-17:00; Sa 09:00-13:00',
    'Tu,Fr', '24/7'. 'off' / 'closed' içeren kurallar atlanır."""
    value = value.strip()
    if not value:
        return frozenset(), ""
    if value == "24/7":
        return frozenset(range(1, 8)), "24 saat"
    days: set[int] = set()
    hours = ""
    for rule in value.split(";"):
        if re.search(r"\b(off|closed)\b", rule, re.I):
            continue
        for start, end in OSM_DAY_RE.findall(rule):
            a = OSM_DAYS[start]
            b = OSM_DAYS[end] if end else a
            days.update(range(a, b + 1) if a <= b else [*range(a, 8), *range(1, b + 1)])
        if not hours and (m := HOURS_RE.search(rule)):
            hours = m.group(0).replace(" ", "")
    return frozenset(days), hours


def days_from_name(name: str) -> frozenset[int]:
    """'Nilüfer Salı Pazarı' → {2}. Yalnızca gün adı 'pazar' kelimesinden hemen önce geliyorsa."""
    folded = fold(name)
    for word, num in TR_DAYS:
        if re.search(rf"\b{word}\s+pazar", folded):
            return frozenset({num})
    return frozenset()


def days_from_text(value: str) -> frozenset[int]:
    """CSV gün alanı: 'Sal,Cum' | 'Salı Cuma' | '2,5' | 'Cumartesi'"""
    result: set[int] = set()
    for token in re.split(r"[\s,;/]+", fold(value)):
        if not token:
            continue
        if token.isdigit() and 1 <= int(token) <= 7:
            result.add(int(token))
            continue
        full = dict(TR_DAYS)
        if token in full:              # 'sali', 'cumartesi', ...
            result.add(full[token])
        elif token in TR_SHORT:        # 'sal', 'cmt', 'cum' (=cuma), ...
            result.add(TR_SHORT[token])
    return frozenset(result)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", fold(text)).strip("-")[:60]


# =============================================================================
# OSM (Overpass)
# =============================================================================
def overpass_query(city: str) -> str:
    # Türkiye'de il sınırları admin_level=4. Şehir adı beyaz listeden gelir (argparse choices).
    return f"""
[out:json][timeout:90];
area["name"="{city}"]["boundary"="administrative"]["admin_level"="4"]->.il;
(
  node["amenity"="marketplace"](area.il);
  way["amenity"="marketplace"](area.il);
  relation["amenity"="marketplace"](area.il);
);
out center tags;
"""


def fetch_overpass(query: str) -> dict:
    data = urllib.parse.urlencode({"data": query}).encode()
    req = urllib.request.Request(OVERPASS_URL, data=data, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def parse_overpass(payload: dict, city: str) -> list[Market]:
    markets: list[Market] = []
    for el in payload.get("elements", []):
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        if lat is None or lon is None:
            continue
        tags = el.get("tags", {})
        name = (tags.get("name:tr") or tags.get("name") or "").strip() or "Pazar yeri (isimsiz)"
        district = tags.get("addr:district") or tags.get("addr:suburb") or tags.get("is_in:district") or ""

        days, hours = days_from_opening_hours(tags.get("opening_hours", ""))
        day_source = "osm" if days else ""
        if not days and (days := days_from_name(name)):
            day_source = "isim"

        markets.append(Market(
            id=f"osm:{el['type']}/{el['id']}", city=city, name=name, district=district,
            lat=float(lat), lon=float(lon), source="osm",
            days=days, hours=hours, day_source=day_source,
        ))
    return markets


# =============================================================================
# CSV (saha)
# =============================================================================
def read_csv(path: Path, city: str) -> list[Market]:
    text = path.read_text(encoding="utf-8-sig")
    dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=";,")
    markets: list[Market] = []
    for line_no, row in enumerate(csv.DictReader(text.splitlines(), dialect=dialect), start=2):
        row = {fold(k or "").strip(): (v or "").strip() for k, v in row.items()}
        try:
            name, lat, lon = row["ad"], float(row["lat"].replace(",", ".")), float(row["lon"].replace(",", "."))
        except (KeyError, ValueError):
            print(f"  [atlandı] satır {line_no}: ad/lat/lon eksik ya da hatalı", file=sys.stderr)
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or not name:
            print(f"  [atlandı] satır {line_no}: geçersiz koordinat", file=sys.stderr)
            continue
        district = row.get("ilce", "")
        days = days_from_text(row.get("gunler", ""))
        markets.append(Market(
            id=f"elle:{slug(name + '-' + district)}", city=city, name=name, district=district,
            lat=lat, lon=lon, source="elle", days=days, hours=row.get("saat", ""),
            day_source="elle" if days else "",
        ))
    return markets


# =============================================================================
# KAYIT
# =============================================================================
def save(db_path: Path, markets: list[Market]) -> tuple[int, int]:
    now = datetime.now().isoformat(timespec="seconds")
    conn = sqlite3.connect(db_path)
    try:
        with conn:  # tek transaction
            conn.executescript(SCHEMA)
            for m in markets:
                conn.execute(
                    """INSERT INTO pazar_yeri (id, sehir, ad, ilce, lat, lon, kaynak, guncelleme)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET ad=excluded.ad, ilce=excluded.ilce,
                           lat=excluded.lat, lon=excluded.lon, guncelleme=excluded.guncelleme""",
                    (m.id, m.city, m.name, m.district, m.lat, m.lon, m.source, now))
                if m.day_source == "elle":
                    # Saha bilgisi en güvenilir: bu pazarın tüm günlerini yeniden yaz
                    conn.execute("DELETE FROM pazar_gunu WHERE pazar_id = ?", (m.id,))
                else:
                    # Otomatik günleri tazele, elle girilenlere dokunma
                    conn.execute("DELETE FROM pazar_gunu WHERE pazar_id = ? AND kaynak <> 'elle'", (m.id,))
                conn.executemany(
                    "INSERT OR IGNORE INTO pazar_gunu (pazar_id, gun, saat, kaynak) VALUES (?, ?, ?, ?)",
                    [(m.id, d, m.hours, m.day_source) for d in sorted(m.days)])
        with_days = sum(1 for m in markets if m.days)
    finally:
        conn.close()
    return len(markets), with_days


def dump(markets: list[Market]) -> None:
    names = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]
    for m in sorted(markets, key=lambda m: m.name):
        days = ",".join(names[d - 1] for d in sorted(m.days)) or "gün bilinmiyor"
        print(f"  {m.name[:38]:<38} {m.district[:14]:<14} {m.lat:9.5f} {m.lon:9.5f}  {days} "
              f"{('(' + m.day_source + ')') if m.day_source else ''}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Pazar yerlerini içe aktar (OSM + CSV)")
    ap.add_argument("--sehir", choices=["Bursa", "İzmir"], default="Bursa")
    ap.add_argument("--db", type=Path, default=Path("hal.db"))
    ap.add_argument("--csv", type=Path, help="saha CSV dosyası (OSM yerine)")
    ap.add_argument("--dump", action="store_true", help="yazmadan listele")
    args = ap.parse_args()

    if args.csv:
        markets = read_csv(args.csv, args.sehir)
        print(f"CSV: {len(markets)} pazar okundu")
    else:
        print(f"OSM'den {args.sehir} pazar yerleri çekiliyor (Overpass)…")
        markets = parse_overpass(fetch_overpass(overpass_query(args.sehir)), args.sehir)
        print(f"OSM: {len(markets)} pazar yeri bulundu")

    if args.dump:
        dump(markets)
        return
    total, with_days = save(args.db, markets)
    print(f"Kaydedildi: {total} pazar, {with_days} tanesinin günü belli. "
          f"Günü bilinmeyenleri CSV ile tamamlayın.")


if __name__ == "__main__":
    main()
