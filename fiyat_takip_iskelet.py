#!/usr/bin/env python3
"""
PROJE: Asenkron E-Ticaret Fiyat Takip Motoru (Async Price Tracker)
ÖĞRENCİ İSKELETİ — "TODO" işaretli yerleri doldurun.
Test: FIYAT_MODUL=fiyat_takip_iskelet python -m unittest -v
Altyapı (ADIM 3 sahte ağ, SQL, ana akış) hazır verilmiştir.
 — Python 3.11+, yalnızca standart kütüphane.

Çalıştırma:
    python fiyat_takip.py                  # 3 tur tarama, 50 ürün
    python fiyat_takip.py --fresh -v       # temiz veritabanı + retry logları
    python fiyat_takip.py --bench          # + eşzamanlılık kıyası ve "bloklama tuzağı" demosu

Dosya, ders adımlarına göre bölümlenmiştir (ADIM 1 ... ADIM 10).
Kod sırası bağımlılık sırasıdır; bazı adımlar ikiye bölünmüştür (örn. ADIM 2a / 2b).
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import inspect
import json
import logging
import random
import re
import sqlite3
import sys
import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, ParamSpec, Protocol, TypeVar, runtime_checkable

log = logging.getLogger("fiyat_takip")

# Basit sayaçlar: kaç istek atıldı, kaç retry oldu, hangi hatalar alındı.
METRICS: Counter[str] = Counter()

TWO_PLACES = Decimal("0.01")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# =============================================================================
# ADIM 1 — DOMAIN MODELİ (değişmez veri)
# -----------------------------------------------------------------------------
# Önce iş kavramları: Ürün, Fiyat Kaydı, Alarm. Ağ, veritabanı vs. sonra gelir.
#   frozen=True → kayıt oluştuktan sonra değiştirilemez (tarihsel veri bozulmaz,
#                 hash'lenebilir, set/dict anahtarı olabilir)
#   slots=True  → örnek başına __dict__ yok → daha az bellek, daha hızlı erişim
#   Decimal     → para asla float tutulmaz: 0.1 + 0.2 != 0.3
# =============================================================================
# TODO ADIM 1: @dataclass(frozen=True, slots=True) ile tanımlayın.
class Product:
    """Alanlar: sku: str, name: str, source: str"""


# TODO ADIM 1: frozen + slots dataclass.
class PriceSnapshot:
    """Alanlar: sku: str, price: Decimal, currency: str, captured_at: datetime

    __post_init__ içinde:
      - price Decimal değilse TypeError
      - price <= 0 ise ValueError
      - price'ı 2 haneye yuvarlayın (ipucu: frozen sınıfta object.__setattr__)
    """


class AlertKind(StrEnum):
    DUSUS = "FİYAT DÜŞTÜ"
    DIP = "YENİ DİP"


# TODO ADIM 1: frozen + slots dataclass.
class PriceAlert:
    """Alanlar: sku: str, kind: AlertKind, reference_price: Decimal,
               new_price: Decimal, drop_pct: Decimal"""

    def __str__(self) -> str:
        return (f"{self.kind}: {self.sku} {self.reference_price} -> {self.new_price} TL "
                f"(-%{self.drop_pct})")


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Bir ürün için çekme sonucu: ya snapshot ya error dolu olur."""
    product: Product
    snapshot: PriceSnapshot | None = None
    error: str | None = None
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return self.snapshot is not None


# Hata hiyerarşisi: retry kararı hata TİPİNE göre verilir.
class FetchError(Exception):
    """Fiyat çekme hatalarının kökü."""


class TransientError(FetchError):
    """HTTP 429/503 — geçici; tekrar denemek mantıklı."""


class PermanentError(FetchError):
    """HTTP 404/410 — kalıcı; tekrar denemek boşa istek."""


class ParseError(FetchError):
    """Sayfa yapısı değişmiş — retry çözmez, parser güncellenmeli."""


# =============================================================================
# ADIM 2a — KAYNAK SÖZLEŞMESİ (Protocol) + KAYIT DEFTERİ (registry)
# -----------------------------------------------------------------------------
# Protocol = yapısal tipleme. Bir sınıfın PriceSource sayılması için ondan
# kalıtım alması GEREKMEZ; aynı imzalı `name` ve `fetch` yeterli (duck typing,
# ama tip denetleyicinin anlayacağı şekilde).
# =============================================================================
@runtime_checkable
class PriceSource(Protocol):
    # TODO ADIM 2: `name: str` ve `async def fetch(self, product: Product) -> PriceSnapshot`
    ...


SOURCE_REGISTRY: dict[str, type] = {}

T = TypeVar("T")


def register_source(name: str) -> Callable[[type[T]], type[T]]:
    """Parametreli sınıf decorator'ı: sınıfı SOURCE_REGISTRY'ye ekler,
    cls.name = name atar. Aynı isim iki kez kaydedilirse ValueError."""
    # TODO ADIM 2
    raise NotImplementedError


# =============================================================================
# ADIM 3 — SAHTE AĞ KATMANI (deterministik simülasyon)
# -----------------------------------------------------------------------------
# Gerçek siteler bot engeli koyar, sonuçlar tekrarlanamaz. Burada "internet"i
# simüle ediyoruz: gecikme, %15 geçici hata, ara sıra hiç cevap vermeyen sunucu.
# Rastgelelik (seed, tur, mağaza, sku, deneme_no) ikilisinden türetilir:
# görevler hangi sırayla çalışırsa çalışsın, her çalıştırmada AYNI senaryo oluşur.
# =============================================================================
def format_tr(amount: Decimal) -> str:
    """Decimal('1299.90') -> '1.299,90'"""
    return f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


class FakeNetwork:
    def __init__(
        self,
        catalog: dict[str, Decimal],
        *,
        seed: int = 42,
        fail_rate: float = 0.15,
        hang_rate: float = 0.03,
        latency: tuple[float, float] = (0.2, 1.0),
        discontinued: Iterable[str] = (),
    ) -> None:
        self._catalog = catalog
        self._seed = seed
        self._fail_rate = fail_rate
        self._hang_rate = hang_rate
        self._latency = latency
        self._discontinued = frozenset(discontinued)
        self._attempts: Counter[tuple[int, str, str]] = Counter()
        self.tick = 0  # "zaman": her tarama turundan sonra ilerler, fiyatlar değişir

    def advance(self) -> None:
        self.tick += 1

    def _rng(self, *parts: object) -> random.Random:
        return random.Random(":".join(map(str, (self._seed, *parts))))

    def current_price(self, sku: str) -> Decimal:
        base = self._catalog[sku]
        if self.tick == 0:
            return base
        p = self._rng("fiyat", sku, self.tick).random()
        rng = self._rng("oran", sku, self.tick)
        if p < 0.65:
            factor = 1.0                        # değişmedi
        elif p < 0.85:
            factor = rng.uniform(0.95, 1.05)    # küçük oynama
        elif p < 0.95:
            factor = rng.uniform(0.75, 0.90)    # kampanya
        else:
            factor = rng.uniform(1.05, 1.15)    # zam
        return (base * Decimal(str(round(factor, 4)))).quantize(TWO_PLACES, ROUND_HALF_UP)

    async def get(self, store: str, sku: str) -> str:
        METRICS["istek"] += 1
        key = (self.tick, store, sku)
        self._attempts[key] += 1
        rng = self._rng("istek", *key, self._attempts[key])

        latency = rng.uniform(*self._latency)
        if rng.random() < self._hang_rate:
            latency = 30.0  # sunucu cevap vermiyor → timeout kesmeli
        await asyncio.sleep(latency)

        if sku in self._discontinued or sku not in self._catalog:
            raise PermanentError(f"HTTP 404 {store}/{sku}")
        if rng.random() < self._fail_rate:
            raise TransientError(f"HTTP {rng.choice((429, 503))} {store}/{sku}")
        return self._render(store, sku, self.current_price(sku))

    @staticmethod
    def _render(store: str, sku: str, price: Decimal) -> str:
        if store == "magaza_a":  # JSON API
            return json.dumps({"sku": sku, "price": str(price), "currency": "TRY"})
        if store == "magaza_b":  # HTML sayfa, Türkçe sayı biçimi
            return (f'<div class="urun" data-sku="{sku}">'
                    f'<span class="price">{format_tr(price)} TL</span></div>')
        raise PermanentError(f"Bilinmeyen mağaza: {store}")


# =============================================================================
# ADIM 4 — DECORATOR'LAR: @retry (exponential backoff + jitter) ve @timed
# -----------------------------------------------------------------------------
# retry = decorator FACTORY → 3 katman: retry(ayarlar) → decorator(func) → wrapper(*args)
#   bekleme_n = base * 2^(n-1) + U(0, base)
# Jitter (U) olmazsa aynı anda hata alan 100 istek aynı milisaniyede tekrar saldırır
# (thundering herd). ParamSpec, sarılan fonksiyonun imzasını tip denetleyiciye korur.
# =============================================================================
P = ParamSpec("P")
R = TypeVar("R")


def retry(
    *,
    attempts: int = 3,
    base_delay: float = 0.2,
    exceptions: tuple[type[BaseException], ...] = (TransientError, TimeoutError),
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    # TODO ADIM 4:
    #   - decorator(func): func async değilse TypeError (inspect.iscoroutinefunction)
    #   - wrapper: @functools.wraps(func); attempts kadar dene
    #   - yalnızca `exceptions` içindeki hatalarda tekrar dene; son denemede raise
    #   - bekleme: base_delay * 2 ** (attempt - 1) + random.uniform(0, base_delay)
    #   - METRICS[f"retry:{type(exc).__name__}"] += 1
    raise NotImplementedError


def timed(func: Callable[..., Any]) -> Callable[..., Any]:
    """Hem sync hem async fonksiyonu sarabilen süre ölçer."""
    label = func.__qualname__

    if inspect.iscoroutinefunction(func):
        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            t0 = time.perf_counter()
            try:
                return await func(*args, **kwargs)
            finally:
                print(f"   [süre] {label}: {time.perf_counter() - t0:.2f} sn")
        return async_wrapper

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        t0 = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            print(f"   [süre] {label}: {time.perf_counter() - t0:.2f} sn")
    return sync_wrapper


# =============================================================================
# ADIM 5 — HIZ SINIRLAMA: Token Bucket
# -----------------------------------------------------------------------------
# Semaphore "aynı anda kaç istek" sorusunu çözer; "saniyede kaç istek" sorusunu
# çözmez. Token bucket: kapasite b, dolum hızı r token/sn.
#   tokens = min(b, tokens + (şimdi - son) * r)
# Token yoksa (1 - tokens) / r saniye beklenir.
# asyncio.Lock kullanılır (threading.Lock event loop'u kilitler!). Lock FIFO
# adildir; bekleyen biri lock'u tutarken uyuması bilinçli: sıra korunur.
# =============================================================================
class TokenBucket:
    def __init__(self, rate: float, capacity: int) -> None:
        if rate <= 0 or capacity < 1:
            raise ValueError("rate > 0 ve capacity >= 1 olmalı")
        self.rate = rate
        self.capacity = capacity
        self._tokens = float(capacity)
        self._last = time.monotonic()  # monotonic: sistem saati geri alınsa da bozulmaz
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        # TODO ADIM 5: self._lock altında
        #   tokens = min(capacity, tokens + (now - last) * rate); last = now
        #   tokens >= 1 → bir token harca, dön
        #   değilse (1 - tokens) / rate saniye await asyncio.sleep, tekrar dene
        raise NotImplementedError

    # async context manager olarak da kullanılabilsin: `async with bucket:`
    async def __aenter__(self) -> TokenBucket:
        await self.acquire()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


# =============================================================================
# ADIM 2b — SOMUT KAYNAKLAR (iki farklı mağaza, iki farklı format)
# -----------------------------------------------------------------------------
# Ortak HTTP davranışı (rate limit + timeout) küçük bir taban sınıfta;
# her mağaza sadece kendi PARSE mantığını yazar.
# =============================================================================
class _HttpSource:
    name: str

    def __init__(self, network: FakeNetwork, bucket: TokenBucket, timeout: float = 2.0) -> None:
        self._network = network
        self._bucket = bucket
        self._timeout = timeout

    async def _get(self, sku: str) -> str:
        # TODO ADIM 5+6: önce `async with self._bucket`, içinde
        #   `async with asyncio.timeout(self._timeout)` ile self._network.get(self.name, sku)
        raise NotImplementedError


# TODO ADIM 2: @register_source("magaza_a") ekleyin
class MagazaA(_HttpSource):
    """JSON API dönen mağaza: {"sku": "...", "price": "1299.90", "currency": "TRY"}"""

    # TODO ADIM 4 bitince: @retry(attempts=3, base_delay=0.2)
    async def fetch(self, product: Product) -> PriceSnapshot:
        # TODO ADIM 2: self._get → json.loads → PriceSnapshot
        #   JSON/anahtar/Decimal hatalarını ParseError'a çevirin (raise ... from exc)
        raise NotImplementedError


PRICE_RE = re.compile(
    r'<span class="price">\s*(?P<amount>\d{1,3}(?:\.\d{3})*,\d{2})\s*TL\s*</span>'
)


def parse_tr_price(text: str) -> Decimal:
    """'1.299,90' -> Decimal('1299.90')"""
    # TODO ADIM 2
    raise NotImplementedError


# TODO ADIM 2: @register_source("magaza_b") ekleyin
class MagazaB(_HttpSource):
    """HTML dönen mağaza: <span class="price">1.299,90 TL</span>"""

    # TODO ADIM 4 bitince: @retry(attempts=3, base_delay=0.2)
    async def fetch(self, product: Product) -> PriceSnapshot:
        # TODO ADIM 2: PRICE_RE ile fiyatı bul, yoksa ParseError; parse_tr_price
        raise NotImplementedError


def build_sources(network: FakeNetwork, *, rate: float, burst: int,
                  timeout: float) -> dict[str, PriceSource]:
    """Registry'deki her mağaza için kendi token bucket'ıyla bir örnek oluşturur."""
    return {name: cls(network, TokenBucket(rate, burst), timeout)
            for name, cls in SOURCE_REGISTRY.items()}


# =============================================================================
# ADIM 6 + 7 — TARAMA MOTORU: TaskGroup, Semaphore, Async Generator
# -----------------------------------------------------------------------------
# Semaphore(N): aynı anda en fazla N istek. N=1 → senkron döngü gibi davranır.
# TaskGroup: bir görev patlarsa diğerlerini iptal eder. Bu yüzden ürün bazlı
#   hatalar _fetch_one içinde yakalanıp FetchResult'a çevrilir: tek ürünün
#   hatası taramayı düşürmez.
# stream(): as_completed ile "hangisi önce biterse" sırasıyla yield eder.
# =============================================================================
class ScanEngine:
    def __init__(self, sources: dict[str, PriceSource], *, max_concurrency: int = 10) -> None:
        for name, src in sources.items():
            # runtime_checkable sadece üye VARLIĞINI kontrol eder, imzayı değil.
            if not isinstance(src, PriceSource):
                raise TypeError(f"{name} PriceSource sözleşmesine uymuyor")
        self._sources = sources
        self._sem = asyncio.Semaphore(max_concurrency)

    async def _fetch_one(self, product: Product) -> FetchResult:
        # TODO ADIM 6: kaynağı bul (yoksa FetchResult(error=...)),
        #   self._sem altında source.fetch; Exception'ı FetchResult(error=...) yap.
        #   elapsed süresini doldurun.
        raise NotImplementedError

    async def scan_all(self, products: Iterable[Product]) -> list[FetchResult]:
        """Hepsini bekle, giriş sırasıyla döndür (ADIM 6)."""
        # TODO ADIM 6: asyncio.TaskGroup
        raise NotImplementedError

    async def stream(self, products: Iterable[Product]) -> AsyncIterator[FetchResult]:
        """Sonuçları bitiş sırasıyla akıt (ADIM 7)."""
        # TODO ADIM 7: create_task + asyncio.as_completed + yield;
        #   finally bloğunda kalan görevleri cancel() edin.
        raise NotImplementedError
        yield  # bu satır fonksiyonu async generator yapar


# =============================================================================
# ADIM 9 — ALARM KURALI (saf fonksiyon: I/O yok → kolay test edilir)
# -----------------------------------------------------------------------------
#   Δ% = (p_eski - p_yeni) / p_eski * 100
# Önce eşik: Δ% < eşik ise alarm yok (gürültü filtresi).
# Eşik aşıldıysa: fiyat penceredeki en düşüğün de altındaysa → YENİ DİP,
#                 değilse → FİYAT DÜŞTÜ.
# =============================================================================
def drop_percent(old: Decimal, new: Decimal) -> Decimal:
    # TODO ADIM 9: (old - new) / old * 100, 2 haneye yuvarla
    raise NotImplementedError


def evaluate_price(
    sku: str,
    new: Decimal,
    *,
    last: Decimal | None,
    window_min: Decimal | None,
    history_count: int,
    threshold: Decimal,
    min_history: int = 2,
) -> PriceAlert | None:
    # TODO ADIM 9:
    #   last yok veya new >= last → None
    #   Δ% < threshold → None
    #   history_count >= min_history ve new < window_min → DIP (referans window_min)
    #   aksi halde → DUSUS (referans last)
    raise NotImplementedError


# =============================================================================
# ADIM 8 — KAYIT: Async Context Manager + bloklayan I/O'yu thread'e devretme
# -----------------------------------------------------------------------------
# sqlite3 bloklayan bir kütüphanedir. Event loop'ta doğrudan çağırırsak tüm
# istekler donar. Çözüm: asyncio.to_thread(...). Aynı bağlantıya iki thread
# aynı anda yazmasın diye asyncio.Lock ile sıraya sokuyoruz.
# Fiyat TEXT olarak saklanır: Decimal hassasiyeti kaybolmaz.
# =============================================================================
class PriceTracker:
    SCHEMA = """
        CREATE TABLE IF NOT EXISTS price_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sku         TEXT NOT NULL,
            price       TEXT NOT NULL,
            currency    TEXT NOT NULL,
            captured_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_price_history_sku ON price_history (sku, id);
    """

    def __init__(self, db_path: str | Path, *, drop_threshold: Decimal = Decimal("10"),
                 window: int = 30, min_history: int = 2) -> None:
        self._db_path = str(db_path)
        self._threshold = drop_threshold
        self._window = window
        self._min_history = min_history
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # ---- async context manager protokolü --------------------------------------
    async def __aenter__(self) -> PriceTracker:
        # TODO ADIM 8: self._conn = await asyncio.to_thread(self._open, ...)
        raise NotImplementedError

    async def __aexit__(self, exc_type: type[BaseException] | None,
                        exc: BaseException | None, tb: object) -> bool:
        # TODO ADIM 8: bağlantıyı _close ile (thread'de) kapatın; hata yoksa commit.
        #   Dönüş değeri hatayı yutup yutmayacağını belirler — ne dönmeli?
        raise NotImplementedError

    @staticmethod
    def _open(path: str, schema: str) -> sqlite3.Connection:
        # check_same_thread=False: to_thread farklı worker thread'ler kullanır.
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.executescript(schema)
        return conn

    @staticmethod
    def _close(conn: sqlite3.Connection, success: bool) -> None:
        try:
            conn.commit() if success else conn.rollback()
        finally:
            conn.close()

    async def _run(self, fn: Callable[..., R], *args: Any) -> R:
        if self._conn is None:
            raise RuntimeError("PriceTracker 'async with' bloğu içinde kullanılmalı")
        async with self._lock:
            return await asyncio.to_thread(fn, self._conn, *args)

    # ---- thread içinde çalışan senkron SQL fonksiyonları ----------------------
    @staticmethod
    def _load_context(conn: sqlite3.Connection, skus: list[str],
                      window: int) -> dict[str, tuple[Decimal, Decimal, int]]:
        """Her SKU için (son fiyat, son `window` kaydın en düşüğü, kayıt sayısı)."""
        if not skus:
            return {}
        # f-string sadece '?' yer tutucularını üretir; DEĞERLER parametre olarak
        # gider → SQL injection yok.
        placeholders = ",".join("?" * len(skus))
        rows = conn.execute(
            f"""
            SELECT sku, price, rn FROM (
                SELECT sku, price,
                       ROW_NUMBER() OVER (PARTITION BY sku ORDER BY id DESC) AS rn
                FROM price_history
                WHERE sku IN ({placeholders})
            ) WHERE rn <= ?
            """,
            (*skus, window),
        ).fetchall()

        ctx: dict[str, tuple[Decimal, Decimal, int]] = {}
        for sku, price_text, rn in rows:
            price = Decimal(price_text)
            last, low, count = ctx.get(sku, (price, price, 0))
            if rn == 1:
                last = price
            ctx[sku] = (last, min(low, price), count + 1)
        return ctx

    @staticmethod
    def _insert(conn: sqlite3.Connection, snaps: list[PriceSnapshot]) -> None:
        conn.executemany(
            "INSERT INTO price_history (sku, price, currency, captured_at) VALUES (?, ?, ?, ?)",
            [(s.sku, str(s.price), s.currency, s.captured_at.isoformat()) for s in snaps],
        )
        conn.commit()  # her tur bir iş birimi (unit of work)

    # ---- dış API --------------------------------------------------------------
    async def process(self, results: Iterable[FetchResult]) -> list[PriceAlert]:
        """Başarılı sonuçları geçmişle kıyasla, alarmları üret, sonra kaydet."""
        # TODO ADIM 8+9:
        #   1) başarılı snapshot'ları ayıkla
        #   2) ctx = await self._run(self._load_context, skus, self._window)
        #   3) her snapshot için evaluate_price
        #   4) await self._run(self._insert, snaps)
        raise NotImplementedError


# =============================================================================
# ADIM 10 — ÖLÇÜM VE KIYAS
# -----------------------------------------------------------------------------
#   T_sync  ≈ Σ gecikme_i
#   T_async ≈ ⌈n / N_eşzamanlı⌉ · max(gecikme)
# =============================================================================
@timed
async def blocking_trap_demo(n: int = 10, delay: float = 0.3) -> None:
    """async içinde bloklayan çağrı yapmanın bedeli."""
    async def good() -> None:
        await asyncio.sleep(delay)  # kontrolü event loop'a bırakır

    async def bad() -> None:
        time.sleep(delay)           # event loop'u DONDURUR; kimse ilerleyemez

    for label, fn in (("asyncio.sleep", good), ("time.sleep   ", bad)):
        t0 = time.perf_counter()
        await asyncio.gather(*(fn() for _ in range(n)))
        print(f"   {n} görev × {delay} sn, {label} → {time.perf_counter() - t0:.2f} sn")


async def benchmark(products: list[Product], prices: dict[str, Decimal], args: argparse.Namespace) -> None:
    print(f"\n=== ADIM 10: KIYAS ({len(products)} ürün) ===")
    print(f"   Teorik senkron süre ≈ n × ort. gecikme = {len(products)} × 0.6 "
          f"≈ {len(products) * 0.6:.0f} sn (+ retry/timeout)")
    baseline: float | None = None
    for conc in (1, 10, 25):
        # Her koşu için taze ağ: aynı seed → aynı hatalar → adil kıyas.
        network = FakeNetwork(prices, seed=args.seed)
        engine = ScanEngine(build_sources(network, rate=args.rate, burst=args.burst,
                                          timeout=args.timeout), max_concurrency=conc)
        t0 = time.perf_counter()
        results = await engine.scan_all(products)
        elapsed = time.perf_counter() - t0
        baseline = baseline or elapsed
        ok = sum(r.ok for r in results)
        note = "  (≈ senkron döngü)" if conc == 1 else f"  → {baseline / elapsed:.1f}x hızlı"
        print(f"   eşzamanlılık={conc:>2}: {elapsed:6.2f} sn  başarılı {ok}/{len(results)}{note}")

    print("\n=== Bloklama tuzağı ===")
    await blocking_trap_demo()


# =============================================================================
# KATALOG + ANA AKIŞ
# =============================================================================
PRODUCT_NAMES = [
    "Kablosuz Kulaklık", "Mekanik Klavye", "Oyuncu Mouse", "27 inç Monitör",
    "USB-C Hub", "Harici SSD 1TB", "Webcam 1080p", "Laptop Standı",
    "Akıllı Saat", "Bluetooth Hoparlör", "Powerbank 20000mAh", "Wi-Fi 6 Router",
]


def build_catalog(n: int, seed: int) -> tuple[list[Product], dict[str, Decimal]]:
    rng = random.Random(f"{seed}:katalog")
    products: list[Product] = []
    prices: dict[str, Decimal] = {}
    for i in range(1, n + 1):
        source = "magaza_a" if i % 2 else "magaza_b"
        sku = f"{'A' if source == 'magaza_a' else 'B'}-{i:04d}"
        products.append(Product(sku, f"{PRODUCT_NAMES[(i - 1) % len(PRODUCT_NAMES)]} #{i}", source))
        prices[sku] = Decimal(rng.randint(199, 24_999)) + Decimal("0.90")
    return products, prices


async def run_round(engine: ScanEngine, tracker: PriceTracker, products: list[Product],
                    round_no: int, show: int) -> None:
    names = {p.sku: p.name for p in products}
    print(f"\n=== TUR {round_no} ===  (sonuçlar BİTİŞ sırasıyla geliyor)")
    t0 = time.perf_counter()
    results: list[FetchResult] = []

    async with aclosing(engine.stream(products)) as stream:
        async for res in stream:
            results.append(res)
            if len(results) <= show:
                status = f"{res.snapshot.price:>10} TL" if res.snapshot else f"HATA ({res.error})"
                print(f"  {len(results):>3}. {res.product.sku:<7} {res.elapsed:5.2f} sn  {status}")
    if len(results) > show:
        print(f"  ... (+{len(results) - show} sonuç)")

    elapsed = time.perf_counter() - t0
    alerts = await tracker.process(results)
    failed = [r for r in results if not r.ok]
    print(f"  Başarılı: {len(results) - len(failed)}/{len(results)} | "
          f"Hata: {len(failed)} | Süre: {elapsed:.2f} sn")
    for r in failed:
        print(f"  [HATA]  {r.product.sku}: {r.error}")
    for a in alerts:
        print(f"  [ALARM] {a}  — {names[a.sku]}")


async def run(args: argparse.Namespace) -> None:
    random.seed(args.seed)  # jitter da tekrarlanabilir olsun
    products, prices = build_catalog(args.products, args.seed)
    discontinued = {products[6].sku} if len(products) > 6 else set()
    network = FakeNetwork(prices, seed=args.seed, discontinued=discontinued)
    engine = ScanEngine(build_sources(network, rate=args.rate, burst=args.burst,
                                      timeout=args.timeout),
                        max_concurrency=args.concurrency)

    if args.fresh:
        Path(args.db).unlink(missing_ok=True)

    print(f"Kaynaklar: {', '.join(SOURCE_REGISTRY)} | ürün: {len(products)} | "
          f"eşzamanlılık: {args.concurrency} | limit: {args.rate} istek/sn/mağaza")

    async with PriceTracker(args.db, drop_threshold=Decimal(str(args.threshold))) as tracker:
        for round_no in range(1, args.rounds + 1):
            await run_round(engine, tracker, products, round_no, args.show)
            network.advance()  # zaman geçti, fiyatlar değişti

    print("\nMetrikler:", ", ".join(f"{k}={v}" for k, v in sorted(METRICS.items())))

    if args.bench:
        await benchmark(products[: args.bench_products], prices, args)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Async E-Ticaret Fiyat Takip Motoru")
    ap.add_argument("--products", type=int, default=50)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--rate", type=float, default=15, help="mağaza başına istek/sn")
    ap.add_argument("--burst", type=int, default=5, help="token bucket kapasitesi")
    ap.add_argument("--timeout", type=float, default=2.0)
    ap.add_argument("--threshold", type=float, default=10, help="alarm eşiği (%%)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--db", default="fiyat.db")
    ap.add_argument("--fresh", action="store_true", help="veritabanını sıfırla")
    ap.add_argument("--show", type=int, default=10, help="tur başına listelenecek sonuç")
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--bench-products", type=int, default=20)
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows konsolu için
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="  [log] %(message)s")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
