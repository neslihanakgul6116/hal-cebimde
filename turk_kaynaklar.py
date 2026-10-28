#!/usr/bin/env python3
"""
TÜRK KAYNAKLARINDAN FİYAT TAKİBİ — fiyat_takip.py + gercek_kaynak.py eklentisi

1) TCMB döviz kurları   https://www.tcmb.gov.tr/kurlar/today.xml   (resmi, açık, günlük)
2) İzmir BB hal fiyatları  openapi.izmir.bel.tr  (açık veri, sebze/meyve, günlük)

Neden Trendyol/Hepsiburada değil? Kullanım koşulları otomatik erişimi kısıtlıyor,
bot korumaları (Cloudflare/Akamai) var. Açık veri kaynakları hem yasal hem kararlı,
üstelik fiyatlar GERÇEKTEN değişiyor → alarm mekanizması gerçek veriyle çalışır.

Kurulum : pip install aiohttp
Çalıştır:
    python turk_kaynaklar.py kur --codes USD EUR GBP CHF --threshold 0.5
    python turk_kaynaklar.py hal --limit 15 --dump     # --dump: ham ilk kaydı göster

YENİ KAVRAM — Single-flight (istek birleştirme):
  Bu kaynaklarda TEK doküman (today.xml / günün bülteni) içinde YÜZLERCE ürün var.
  Motor 10 dövizi eşzamanlı isterse saf yaklaşım 10 kez aynı XML'i indirir.
  Single-flight: aynı anahtar için uçuştaki isteği paylaş → 10 ürün, 1 HTTP isteği.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import xml.etree.ElementTree as ET  # Güvenlik notu: üretimde `defusedxml` tercih edin
from contextlib import aclosing
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from fiyat_takip import (
    METRICS, ParseError, PermanentError, PriceSnapshot, PriceTracker, Product,
    ScanEngine, TokenBucket, _HttpSource, register_source, retry, utcnow,
)
from gercek_kaynak import RealNetwork

BASE_URLS = {
    "tcmb": "https://www.tcmb.gov.tr/kurlar/",
    "izmir_hal": "https://openapi.izmir.bel.tr/api/ibb/halfiyatlari/sebzemeyve/",
}


# =============================================================================
# SINGLE-FLIGHT + TTL ÖNBELLEK
# -----------------------------------------------------------------------------
# - İlk gelen istek bir Task başlatır; arkadan gelenler AYNI Task'ı bekler.
# - asyncio.shield: bekleyenlerden biri iptal edilirse ortak Task iptal olmaz.
# - Hatalı/iptal Task önbellekte kalmaz → @retry bir sonraki denemede yeniden çeker.
# =============================================================================
class SharedDocumentSource(_HttpSource):
    ttl: float = 300.0  # sn

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._cache: dict[str, tuple[float, asyncio.Task[str]]] = {}

    async def _document(self, path: str) -> str:
        now = time.monotonic()
        entry = self._cache.get(path)
        stale = (
            entry is None
            or now - entry[0] > self.ttl
            or (entry[1].done() and (entry[1].cancelled() or entry[1].exception() is not None))
        )
        if stale:
            METRICS[f"dokuman_indir:{self.name}"] += 1
            self._cache[path] = (now, asyncio.create_task(self._get(path)))
        else:
            METRICS[f"dokuman_paylas:{self.name}"] += 1
        return await asyncio.shield(self._cache[path][1])


# =============================================================================
# 1) TCMB DÖVİZ KURLARI
# -----------------------------------------------------------------------------
# <Tarih_Date Tarih="dd.mm.yyyy" ...>
#   <Currency Kod="USD" CurrencyCode="USD">
#     <Unit>1</Unit><Isim>ABD DOLARI</Isim>
#     <ForexBuying>..</ForexBuying><ForexSelling>..</ForexSelling> ...
# Not: JPY gibi bazı dövizlerde Unit=100 → fiyat "100 birim" içindir.
# =============================================================================
def parse_tcmb(xml_text: str) -> dict[str, tuple[Decimal, int, str]]:
    """Kod → (döviz satış kuru, birim, Türkçe isim)"""
    try:
        root = ET.fromstring(xml_text.encode("utf-8"))
    except ET.ParseError as exc:
        raise ParseError("TCMB XML çözümlenemedi") from exc
    rates: dict[str, tuple[Decimal, int, str]] = {}
    for cur in root.iter("Currency"):
        code = cur.get("Kod") or cur.get("CurrencyCode")
        selling = (cur.findtext("ForexSelling") or "").strip()
        if not code or not selling:  # bazı kodlarda (örn. XDR) alan boş gelir
            continue
        try:
            rates[code] = (Decimal(selling), int(cur.findtext("Unit") or 1),
                           (cur.findtext("Isim") or code).strip())
        except (InvalidOperation, ValueError):
            continue
    if not rates:
        raise ParseError("TCMB XML içinde kur bulunamadı")
    return rates


@register_source("tcmb")
class TcmbKur(SharedDocumentSource):
    """sku = döviz kodu (USD, EUR, ...)"""

    @retry(attempts=3, base_delay=1.0)
    async def fetch(self, product: Product) -> PriceSnapshot:
        rates = parse_tcmb(await self._document("today.xml"))
        if product.sku not in rates:
            raise PermanentError(f"TCMB listesinde yok: {product.sku}")
        price, _unit, _name = rates[product.sku]
        return PriceSnapshot(product.sku, price, "TRY", utcnow())


# =============================================================================
# 2) İZMİR BÜYÜKŞEHİR HAL FİYATLARI (sebze/meyve)
# -----------------------------------------------------------------------------
# GET .../sebzemeyve/YYYY-MM-DD → JSON bülten (ürün listesi; asgari/azami/ortalama)
# Alan adları kaynağa göre "MalAdi" ya da "MAL_ADI" biçiminde olabilir.
# Bu yüzden anahtarları normalize ederek (küçük harf, '_' yok) arıyoruz:
# alan adı değişikliğine karşı savunmacı parse.
# =============================================================================
def _norm(key: str) -> str:
    return key.replace("_", "").casefold()


def _pick(item: dict[str, Any], *names: str) -> Any:
    lookup = {_norm(k): v for k, v in item.items()}
    for name in names:
        if (value := lookup.get(_norm(name))) not in (None, ""):
            return value
    return None


def parse_hal(json_text: str) -> dict[str, tuple[Decimal, str]]:
    """'MAL ADI (BİRİM)' → (ortalama fiyat, tür)"""
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise ParseError("Hal bülteni JSON değil") from exc

    items = data if isinstance(data, list) else _pick(data, "HalFiyatListesi")
    if items is None and isinstance(data, dict):  # liste anahtarı farklıysa ilk listeyi al
        items = next((v for v in data.values() if isinstance(v, list)), [])

    prices: dict[str, tuple[Decimal, str]] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = _pick(item, "MalAdi", "MAL_ADI")
        unit = _pick(item, "Birim", "BIRIM") or "?"
        avg = _pick(item, "OrtalamaUcret", "ORTALAMA_UCRET")
        if name is None or avg is None:
            continue
        try:
            price = Decimal(str(avg).replace(",", "."))
        except InvalidOperation:
            continue
        if price > 0:
            prices[f"{str(name).strip()} ({unit})"] = (price, str(_pick(item, "MalTipAdi", "MAL_TURU") or ""))
    return prices


@register_source("izmir_hal")
class IzmirHal(SharedDocumentSource):
    """sku = 'MAL ADI (BİRİM)'. Hangi günün bülteni okunacağı dışarıdan verilir."""

    def __init__(self, *args: Any, bulletin_date: date, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.bulletin_date = bulletin_date

    @retry(attempts=3, base_delay=1.0)
    async def fetch(self, product: Product) -> PriceSnapshot:
        prices = parse_hal(await self._document(self.bulletin_date.isoformat()))
        if product.sku not in prices:
            raise PermanentError(f"Bültende yok: {product.sku}")
        return PriceSnapshot(product.sku, prices[product.sku][0], "TRY", utcnow())


async def latest_hal_bulletin(net: RealNetwork, max_days_back: int = 7) -> tuple[date, str]:
    """Bugünün bülteni boş olabilir (sabah erken, pazar, bayram) → geriye doğru ara."""
    day = date.today()
    for _ in range(max_days_back + 1):
        try:
            text = await net.get("izmir_hal", day.isoformat())
            if parse_hal(text):
                return day, text
        except (PermanentError, ParseError):
            pass
        day -= timedelta(days=1)
    raise PermanentError(f"Son {max_days_back} günde hal bülteni bulunamadı")


# =============================================================================
# ANA AKIŞ
# =============================================================================
async def scan(engine: ScanEngine, products: list[Product], db: str,
               threshold: Decimal) -> None:
    async with PriceTracker(db, drop_threshold=threshold) as tracker:
        t0 = time.perf_counter()
        results = []
        async with aclosing(engine.stream(products)) as stream:
            async for res in stream:
                results.append(res)
                value = f"{res.snapshot.price:>10} TL" if res.snapshot else f"HATA ({res.error})"
                print(f"  {res.product.name:<32} {value}")
        alerts = await tracker.process(results)
    print(f"Süre: {time.perf_counter() - t0:.2f} sn | alarm: {len(alerts)}")
    for a in alerts:
        print(f"  [ALARM] {a}")
    print("Metrikler:", ", ".join(f"{k}={v}" for k, v in sorted(METRICS.items())))


async def run_kur(args: argparse.Namespace) -> None:
    async with RealNetwork({"tcmb": BASE_URLS["tcmb"]}) as net:
        source = TcmbKur(net, TokenBucket(rate=1, capacity=1), timeout=20)
        products = [Product(code, f"{code} döviz satış", "tcmb") for code in args.codes]
        await scan(ScanEngine({"tcmb": source}, max_concurrency=10), products,
                   args.db, Decimal(str(args.threshold)))


async def run_hal(args: argparse.Namespace) -> None:
    async with RealNetwork({"izmir_hal": BASE_URLS["izmir_hal"]}) as net:
        day, text = await latest_hal_bulletin(net)
        print(f"Bülten tarihi: {day.isoformat()}")
        if args.dump:
            data = json.loads(text)
            first = (data if isinstance(data, list) else
                     next((v for v in data.values() if isinstance(v, list)), [None]))[0]
            print("Ham ilk kayıt:", json.dumps(first, ensure_ascii=False, indent=2))

        source = IzmirHal(net, TokenBucket(rate=1, capacity=1), timeout=20, bulletin_date=day)
        names = list(parse_hal(text))[: args.limit]
        products = [Product(n, n[:32], "izmir_hal") for n in names]
        await scan(ScanEngine({"izmir_hal": source}, max_concurrency=10), products,
                   args.db, Decimal(str(args.threshold)))


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Türk açık veri kaynaklarından fiyat takibi")
    sub = ap.add_subparsers(dest="cmd", required=True)

    kur = sub.add_parser("kur", help="TCMB döviz kurları")
    kur.add_argument("--codes", nargs="+", default=["USD", "EUR", "GBP", "CHF", "JPY"])
    kur.add_argument("--threshold", type=float, default=0.5, help="alarm eşiği (%%)")
    kur.add_argument("--db", default="kur.db")

    hal = sub.add_parser("hal", help="İzmir hal fiyatları")
    hal.add_argument("--limit", type=int, default=15)
    hal.add_argument("--threshold", type=float, default=10)
    hal.add_argument("--dump", action="store_true", help="ham ilk kaydı yazdır")
    hal.add_argument("--db", default="hal.db")

    args = ap.parse_args()
    asyncio.run(run_kur(args) if args.cmd == "kur" else run_hal(args))


if __name__ == "__main__":
    main()
