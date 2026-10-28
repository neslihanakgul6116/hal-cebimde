#!/usr/bin/env python3
"""
BURSA BÜYÜKŞEHİR HAL FİYATLARI — https://www.bursa.bel.tr/hal_fiyatlari

Sayfa yapısı (28.10.2026 itibarıyla):
  - Başlık: "dd.mm.yyyy Tarihli Hal Fiyatları"
  - 9 tablo, sütunlar: ÜRÜN | BİRİM | FİYAT
  - Kategoriler (sayfadaki sırayla): Meyve, Sebze, İthal, Pelajik / Dip / İç / Diğer /
    Kültür / İthal Su Ürünleri  (bağlantılar: #hal-meyve, #hal-sebze, ...)
  - Fiyat: "10,00 - 80,00 ₺" (aralık) ya da "30,00 ₺" (tek fiyat)

Veritabanına iki şekilde yazar:
  1) price_history  → PriceTracker (İzmir'deki gibi; alarm mekanizması çalışır)
                      fiyat = (asgari + azami) / 2
  2) hal_bulten     → bültenin tamamı: şehir, tarih, kategori, ürün, birim, asgari, azami
                      (uygulamanın asıl veri tablosu; aynı gün iki kez çalışırsa tekrar yazmaz)

Kurulum : pip install aiohttp
Çalıştır:
    python bursa_hal.py --dump               # parse kontrolü: kategori başına ilk 3 ürün
    python bursa_hal.py --db hal.db          # tüm bülteni kaydet + alarmları göster
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sqlite3
import sys
import time
from pathlib import Path
from collections import Counter
from contextlib import aclosing
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any

from fiyat_takip import (
    METRICS, FetchError, ParseError, PermanentError, PriceSnapshot, PriceTracker, Product,
    ScanEngine, TokenBucket, register_source, retry, utcnow,
)
from gercek_kaynak import RealNetwork
from turk_kaynaklar import SharedDocumentSource

BASE_URL = "https://www.bursa.bel.tr/"
PAGE_PATH = "hal_fiyatlari"
CITY = "Bursa"

# Sayfadaki sırayla. Anchor id'si bulunamazsa tablo sırası bu listeyle eşlenir.
CATEGORIES: list[tuple[str, str]] = [
    ("hal-meyve", "Meyve"),
    ("hal-sebze", "Sebze"),
    ("hal-ithal", "İthal"),
    ("hal-pelajik", "Pelajik Su Ürünleri"),
    ("hal-dip", "Dip Su Ürünleri"),
    ("hal-ic", "İç Su Ürünleri"),
    ("hal-diger", "Diğer Su Ürünleri"),
    ("hal-kultur", "Kültür Su Ürünleri"),
    ("hal-ithal-su", "İthal Su Ürünleri"),
]
_ANCHOR_TO_NAME = dict(CATEGORIES)
TWO_PLACES = Decimal("0.01")


# =============================================================================
# DOMAIN
# =============================================================================
@dataclass(frozen=True, slots=True)
class HalItem:
    category: str
    name: str
    unit: str
    min_price: Decimal
    max_price: Decimal

    @property
    def mid_price(self) -> Decimal:
        return ((self.min_price + self.max_price) / 2).quantize(TWO_PLACES, ROUND_HALF_UP)

    @property
    def sku(self) -> str:
        return f"{self.category}/{self.name} ({self.unit})"


@dataclass(frozen=True, slots=True)
class HalBulletin:
    city: str
    bulletin_date: date
    items: tuple[HalItem, ...]

    def by_sku(self) -> dict[str, HalItem]:
        return {i.sku: i for i in self.items}


# =============================================================================
# PARSE — stdlib HTMLParser ile durum makinesi
# -----------------------------------------------------------------------------
# Neden regex değil? Tablolar iç içe etiketler (span, strong) içerebilir;
# HTMLParser etiketleri doğru sırayla verir, hücre metnini birleştirmek kolaylaşır.
# Kategori tespiti iki katmanlı:
#   1) Tablodan önce görülen son "hal-..." id'li elemanın adı (anchor hedefi)
#   2) Bulunamazsa tablo sırası → CATEGORIES listesi
# =============================================================================
DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})\s*Tarihli", re.I)
PRICE_RE = re.compile(r"\d+(?:\.\d{3})*(?:,\d+)?")


class _TableCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[tuple[str | None, list[list[str]]]] = []
        self.text_chunks: list[str] = []
        self._anchor: str | None = None
        self._table_depth = 0
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        elem_id = dict(attrs).get("id") or ""
        if elem_id.startswith("hal-"):
            self._anchor = elem_id
        if tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self.tables.append((self._anchor, []))
        elif self._table_depth and tag == "tr":
            self._row = []
        elif self._table_depth and tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._table_depth:
            self._table_depth -= 1
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row and self.tables:
                self.tables[-1][1].append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)
        else:
            self.text_chunks.append(data)


def parse_tr_number(text: str) -> Decimal:
    """'1.250,50' -> 1250.50 | '30,00' -> 30.00 | '45' -> 45"""
    return Decimal(text.replace(".", "").replace(",", "."))


def parse_price_range(text: str) -> tuple[Decimal, Decimal]:
    """'10,00 - 80,00 ₺' -> (10.00, 80.00) | '30,00 ₺' -> (30.00, 30.00)"""
    numbers = PRICE_RE.findall(text)
    if not numbers:
        raise ParseError(f"Fiyat okunamadı: {text!r}")
    try:
        values = [parse_tr_number(n) for n in numbers[:2]]
    except InvalidOperation as exc:
        raise ParseError(f"Fiyat okunamadı: {text!r}") from exc
    low, high = min(values), max(values)
    return low, high


def parse_bursa_hal(html: str) -> HalBulletin:
    collector = _TableCollector()
    collector.feed(html)
    collector.close()

    match = DATE_RE.search(" ".join(collector.text_chunks))
    if match is None:
        raise ParseError("Bülten tarihi bulunamadı ('dd.mm.yyyy Tarihli' başlığı yok)")
    day, month, year = map(int, match.groups())
    bulletin_date = date(year, month, day)

    data_tables = [(a, rows) for a, rows in collector.tables
                   if any(len(r) >= 3 for r in rows)]
    items: list[HalItem] = []
    for index, (anchor, rows) in enumerate(data_tables):
        if anchor in _ANCHOR_TO_NAME:
            category = _ANCHOR_TO_NAME[anchor]
        elif len(data_tables) == len(CATEGORIES):
            category = CATEGORIES[index][1]
        else:
            category = f"Tablo {index + 1}"
        for row in rows:
            if len(row) < 3 or row[0].casefold() in ("ürün", "urun", ""):
                continue  # başlık satırı / boş satır
            name, unit, price_text = row[0], row[1] or "?", row[2]
            try:
                low, high = parse_price_range(price_text)
            except ParseError:
                METRICS["bursa_atlanan_satir"] += 1
                continue
            if high > 0:
                items.append(HalItem(category, name, unit, low, high))

    if not items:
        raise ParseError("Bursa hal sayfasında ürün bulunamadı (sayfa yapısı değişmiş olabilir)")
    return HalBulletin(CITY, bulletin_date, tuple(items))


# =============================================================================
# KAYNAK (motor ile uyumlu; single-flight sayesinde 300 ürün = 1 sayfa isteği)
# =============================================================================
@register_source("bursa_hal")
class BursaHal(SharedDocumentSource):
    """sku = 'Kategori/Ürün (Birim)'"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Doküman tek kez iniyor ama parse her fetch'te tekrarlanırsa 300 ürün =
        # 300 parse olur ve CPU işi event loop'u bloklar. Parse sonucunu da sakla:
        # aynı doküman nesnesi (identity) → aynı bülten.
        self._parsed: tuple[str, HalBulletin, dict[str, HalItem]] | None = None

    async def _parsed_bulletin(self) -> tuple[HalBulletin, dict[str, HalItem]]:
        html = await self._document(PAGE_PATH)
        if self._parsed is None or self._parsed[0] is not html:
            bulletin = parse_bursa_hal(html)
            self._parsed = (html, bulletin, bulletin.by_sku())
        return self._parsed[1], self._parsed[2]

    @retry(attempts=3, base_delay=2.0)
    async def bulletin(self) -> HalBulletin:
        return (await self._parsed_bulletin())[0]

    @retry(attempts=3, base_delay=1.0)
    async def fetch(self, product: Product) -> PriceSnapshot:
        item = (await self._parsed_bulletin())[1].get(product.sku)
        if item is None:
            raise PermanentError(f"Bültende yok: {product.sku}")
        return PriceSnapshot(product.sku, item.mid_price, "TRY", utcnow())


# =============================================================================
# BÜLTEN DEPOSU — uygulamanın asıl tablosu (şehir bağımsız şema)
# =============================================================================
BULLETIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS hal_bulten (
    sehir       TEXT NOT NULL,
    tarih       TEXT NOT NULL,          -- ISO: 2026-10-27
    kategori    TEXT NOT NULL,
    urun        TEXT NOT NULL,
    birim       TEXT NOT NULL,
    asgari      TEXT NOT NULL,          -- Decimal → TEXT (hassasiyet kaybı yok)
    azami       TEXT NOT NULL,
    kayit_zamani TEXT NOT NULL,
    PRIMARY KEY (sehir, tarih, kategori, urun, birim)
);
"""


def bulletin_exists(db_path: str, city: str, day: date) -> bool:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(BULLETIN_SCHEMA)
        row = conn.execute("SELECT 1 FROM hal_bulten WHERE sehir = ? AND tarih = ? LIMIT 1",
                           (city, day.isoformat())).fetchone()
    return row is not None


def save_bulletin(db_path: str, bulletin: HalBulletin) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    rows = [(bulletin.city, bulletin.bulletin_date.isoformat(), i.category, i.name, i.unit,
             str(i.min_price), str(i.max_price), now) for i in bulletin.items]
    conn = sqlite3.connect(db_path)
    try:
        with conn:  # transaction: ya hepsi ya hiçbiri
            conn.executescript(BULLETIN_SCHEMA)
            conn.executemany(
                "INSERT OR REPLACE INTO hal_bulten VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    finally:
        conn.close()
    return len(rows)


# =============================================================================
# ANA AKIŞ
# =============================================================================
def dump(bulletin: HalBulletin, per_category: int = 3) -> None:
    counts = Counter(i.category for i in bulletin.items)
    print(f"Bülten: {bulletin.city} {bulletin.bulletin_date:%d.%m.%Y} | "
          f"{len(bulletin.items)} ürün, {len(counts)} kategori")
    shown: Counter[str] = Counter()
    for item in bulletin.items:
        if shown[item.category] < per_category:
            shown[item.category] += 1
            print(f"  [{item.category} ({counts[item.category]})] {item.name:<22} "
                  f"{item.unit:<6} {item.min_price:>8} - {item.max_price:<8} → ort. {item.mid_price}")


def check_db_folder(db_path: str) -> None:
    """Veritabanı klasörü yoksa ağa çıkmadan önce açıkça söyle (SQLite klasör oluşturmaz)."""
    if db_path == ":memory:":
        return
    folder = Path(db_path).expanduser().resolve().parent
    if not folder.is_dir():
        print(f"Veritabanı klasörü bulunamadı: {folder}", file=sys.stderr)
        print("hal_app klasörünün yerini kontrol edin (zip açılırken iç içe klasör oluşmuş olabilir).",
              file=sys.stderr)
        raise SystemExit(1)


async def run(args: argparse.Namespace) -> None:
    if not args.dump:
        check_db_folder(args.db)
    async with RealNetwork({"bursa_hal": BASE_URL}, read_timeout=args.timeout, ipv4_only=args.ipv4) as net:
        source = BursaHal(net, TokenBucket(rate=1, capacity=1), timeout=args.timeout + 15)
        try:
            bulletin = await source.bulletin()
        except (FetchError, TimeoutError) as exc:
            print(f"\nHal sayfası alınamadı (3 deneme): {exc or type(exc).__name__}", file=sys.stderr)
            print("Tarayıcıda açılıyor mu kontrol edin, sonra: python teshis.py", file=sys.stderr)
            raise SystemExit(1) from None
        dump(bulletin)
        if args.dump:
            return

        if bulletin_exists(args.db, bulletin.city, bulletin.bulletin_date) and not args.force:
            print(f"Bu bülten zaten kayıtlı ({bulletin.bulletin_date}). Tekrar için --force.")
            return

        saved = await asyncio.to_thread(save_bulletin, args.db, bulletin)
        print(f"hal_bulten: {saved} satır yazıldı")

        products = [Product(i.sku, i.name, "bursa_hal") for i in bulletin.items]
        engine = ScanEngine({"bursa_hal": source}, max_concurrency=50)
        async with PriceTracker(args.db, drop_threshold=Decimal(str(args.threshold))) as tracker:
            t0 = time.perf_counter()
            results = []
            async with aclosing(engine.stream(products)) as stream:
                async for res in stream:
                    results.append(res)
            alerts = await tracker.process(results)
        ok = sum(r.ok for r in results)
        print(f"price_history: {ok}/{len(results)} kayıt, {time.perf_counter() - t0:.2f} sn, "
              f"alarm: {len(alerts)}")
        for a in alerts:
            print(f"  [ALARM] {a}")
    print("Metrikler:", ", ".join(f"{k}={v}" for k, v in sorted(METRICS.items())))


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Bursa hal fiyatları → SQLite")
    ap.add_argument("--db", default="hal.db")
    ap.add_argument("--threshold", type=float, default=10, help="alarm eşiği (%%)")
    ap.add_argument("--dump", action="store_true", help="sadece parse et ve göster")
    ap.add_argument("--force", action="store_true", help="aynı bülteni yeniden yaz")
    ap.add_argument("--timeout", type=float, default=45, help="sunucu yanıt bekleme süresi (sn)")
    ap.add_argument("--ipv4", action="store_true", help="yalnızca IPv4 kullan (IPv6 yolu bozuksa)")
    args: Any = ap.parse_args(argv)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
