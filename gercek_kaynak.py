#!/usr/bin/env python3
"""
GERÇEK SİTEDEN FİYAT ÇEKME — fiyat_takip.py'ye eklenti.

Hedef: https://books.toscrape.com  (scraping PRATİĞİ için yapılmış yasal demo site)
Kurulum: pip install aiohttp
Çalıştırma: python gercek_kaynak.py --limit 10

Mimarideki kazanç: Domain, retry, token bucket, motor, tracker, alarm AYNEN
kullanılıyor. Değişen tek şey "ağ" katmanı ve yeni bir kaynak sınıfı.
FakeNetwork ile aynı imzaya sahip `get(store, path) -> str` yazdık → duck typing.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import socket
import sys
import time
from contextlib import aclosing
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import aiohttp

from fiyat_takip import (
    ParseError, PermanentError, PriceSnapshot, PriceTracker, Product, ScanEngine,
    TokenBucket, TransientError, _HttpSource, register_source, retry, utcnow,
)

USER_AGENT = "FiyatTakipDers/1.0 (+egitim amacli)"  # kim olduğunu dürüstçe söyle


# =============================================================================
# 1) GERÇEK AĞ KATMANI — FakeNetwork'ün yerini alır
# -----------------------------------------------------------------------------
# - Tek ClientSession: TCP bağlantıları ve TLS el sıkışması yeniden kullanılır.
#   (Her istekte yeni session açmak en sık yapılan hata.)
# - HTTP durum kodlarını bizim hata hiyerarşimize çeviriyoruz → @retry aynen çalışır.
# - robots.txt: site "buraya girme" diyorsa girmiyoruz.
# =============================================================================
class RealNetwork:
    def __init__(self, base_urls: dict[str, str], *, per_host: int = 4,
                 read_timeout: float = 45.0, ipv4_only: bool = False) -> None:
        self._base = base_urls
        self._family = socket.AF_INET if ipv4_only else 0  # 0 = IPv4 + IPv6
        self._per_host = per_host
        self._read_timeout = read_timeout
        self._session: aiohttp.ClientSession | None = None
        self._robots: dict[str, RobotFileParser] = {}

    async def __aenter__(self) -> RealNetwork:
        self._session = aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8"},
            # total yerine ayrı süreler: yavaş ama çalışan sunucu (belediye siteleri)
            # bağlanınca yanıtı geç başlatabilir; sock_read her okuma beklemesine uygulanır.
            timeout=aiohttp.ClientTimeout(total=None, connect=10, sock_connect=10,
                                          sock_read=self._read_timeout),
            connector=aiohttp.TCPConnector(limit_per_host=self._per_host, family=self._family),
        )
        for store, base in self._base.items():
            self._robots[store] = await self._load_robots(base)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._session:
            await self._session.close()

    async def _load_robots(self, base: str) -> RobotFileParser:
        rp = RobotFileParser()
        try:
            text = await self._fetch_text(urljoin(base, "/robots.txt"))
            rp.parse(text.splitlines())
        except (PermanentError, TransientError, aiohttp.ClientError, TimeoutError):
            rp.parse([])  # robots.txt yoksa ya da cevap vermiyorsa: kural yok kabul edilir
        return rp

    async def _fetch_text(self, url: str) -> str:
        assert self._session is not None, "'async with RealNetwork(...)' içinde kullanın"
        async with self._session.get(url) as resp:
            if resp.status == 429 or resp.status >= 500:
                # Retry-After başlığı varsa ona uymak gerekir (bkz. tartışma)
                raise TransientError(f"HTTP {resp.status} {url}")
            if resp.status >= 400:
                raise PermanentError(f"HTTP {resp.status} {url}")
            # Bazı siteler charset bildirmez → bayt al, UTF-8 dene
            raw = await resp.read()
            return raw.decode(resp.charset or "utf-8", errors="replace")

    async def get(self, store: str, path: str) -> str:
        """FakeNetwork.get ile AYNI imza → kaynak sınıfları farkı bilmez."""
        url = urljoin(self._base[store], path)
        if not self._robots[store].can_fetch(USER_AGENT, url):
            raise PermanentError(f"robots.txt izin vermiyor: {url}")
        try:
            return await self._fetch_text(url)
        except TimeoutError as exc:  # aiohttp zaman aşımları da TimeoutError'dır
            raise TransientError(f"Sunucu {self._read_timeout:.0f} sn içinde yanıt vermedi: {url}") from exc
        except aiohttp.ClientConnectionError as exc:
            raise TransientError(f"Bağlantı hatası: {url}") from exc


# =============================================================================
# 2) PARSE STRATEJİLERİ
# -----------------------------------------------------------------------------
# a) CSS sınıfına bakan regex: kolay ama kırılgan (site tasarımı değişince biter).
# b) JSON-LD (schema.org/Product): e-ticaret sitelerinin çoğu SEO için sayfaya
#    makine-okunur ürün verisi koyar. Tasarım değişse de genelde aynı kalır.
#    Gerçek projelerde ÖNCE buna bakın.
# =============================================================================
BOOK_PRICE_RE = re.compile(r'class="price_color">\s*[^\d<]*(?P<amount>\d+(?:\.\d{2})?)\s*<')
LISTING_RE = re.compile(r'<h3>\s*<a href="(?P<href>[^"]+)" title="(?P<title>[^"]+)"')
JSONLD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)


def price_from_jsonld(html: str) -> tuple[Decimal, str] | None:
    """schema.org Product → (fiyat, para birimi). Bulamazsa None."""
    for block in JSONLD_RE.findall(html):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        # Kök tek nesne, liste ya da {"@graph": [...]} olabilir
        nodes = data if isinstance(data, list) else data.get("@graph", [data])
        for node in nodes:
            types = node.get("@type")
            if "Product" not in (types if isinstance(types, list) else [types]):
                continue
            offers = node.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            price = offers.get("price") or offers.get("lowPrice")
            if price is None:
                continue
            try:
                return Decimal(str(price)), offers.get("priceCurrency", "TRY")
            except InvalidOperation:
                continue
    return None


# =============================================================================
# 3) YENİ KAYNAK — mevcut koda dokunmadan eklendi (Open/Closed)
# =============================================================================
@register_source("books")
class BooksToScrape(_HttpSource):
    """sku = katalog içi yol, örn. 'a-light-in-the-attic_1000/index.html'"""

    @retry(attempts=3, base_delay=1.0)  # gerçek sitede daha sabırlı backoff
    async def fetch(self, product: Product) -> PriceSnapshot:
        html = await self._get(product.sku)
        found = price_from_jsonld(html)          # önce sağlam yol
        if found is None:
            match = BOOK_PRICE_RE.search(html)   # yoksa CSS'e bak
            if match is None:
                raise ParseError(f"{self.name}: fiyat bulunamadı ({product.sku})")
            found = (Decimal(match["amount"]), "GBP")
        price, currency = found
        return PriceSnapshot(product.sku, price, currency, utcnow())


async def discover_products(network: RealNetwork, limit: int) -> list[Product]:
    """Listeleme sayfalarını gezip ürünleri bul (sayfa sayfa, limit dolana dek)."""
    products: list[Product] = []
    page = 1
    while len(products) < limit:
        try:
            html = await network.get("books", f"page-{page}.html")
        except PermanentError:  # son sayfayı geçtik (404)
            break
        found = LISTING_RE.findall(html)
        if not found:
            break
        products += [Product(href, title, "books") for href, title in found]
        page += 1
    return products[:limit]


async def main(args: argparse.Namespace) -> None:
    async with RealNetwork({"books": "https://books.toscrape.com/catalogue/"}) as net:
        products = await discover_products(net, args.limit)
        print(f"{len(products)} ürün bulundu, fiyatlar çekiliyor...")

        source = BooksToScrape(net, TokenBucket(rate=args.rate, capacity=2), timeout=20)
        engine = ScanEngine({"books": source}, max_concurrency=args.concurrency)

        async with PriceTracker(args.db) as tracker:
            t0 = time.perf_counter()
            results = []
            async with aclosing(engine.stream(products)) as stream:
                async for res in stream:
                    results.append(res)
                    label = res.product.name[:40]
                    value = (f"{res.snapshot.price} {res.snapshot.currency}"
                             if res.snapshot else f"HATA ({res.error})")
                    print(f"  {res.elapsed:5.2f} sn  {label:<40} {value}")
            alerts = await tracker.process(results)
            print(f"Süre: {time.perf_counter() - t0:.2f} sn | alarm: {len(alerts)}")
            for a in alerts:
                print(f"  [ALARM] {a}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--rate", type=float, default=2, help="istek/sn — kibar ol")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--db", default="gercek.db")
    asyncio.run(main(ap.parse_args()))
