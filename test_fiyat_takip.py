"""
Kabul testleri — hem referans çözümü hem öğrenci iskeletini test eder.

    python -m unittest -v                                    # referans çözüm
    FIYAT_MODUL=fiyat_takip_iskelet python -m unittest -v    # öğrenci kodu (Linux/macOS)
    $env:FIYAT_MODUL="fiyat_takip_iskelet"; python -m unittest -v   # PowerShell
"""
import asyncio
import dataclasses
import importlib
import os
import time
import unittest
from decimal import Decimal

ft = importlib.import_module(os.environ.get("FIYAT_MODUL", "fiyat_takip"))


def snap(sku="A-0001", price="100.00"):
    return ft.PriceSnapshot(sku, Decimal(price), "TRY", ft.utcnow())


# ---------------------------------------------------------------- ADIM 1
class TestDomain(unittest.TestCase):
    def test_frozen(self):
        s = snap()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            s.price = Decimal("1")

    def test_slots(self):
        self.assertFalse(hasattr(snap(), "__dict__"))

    def test_float_rejected(self):
        snap()  # Decimal ile geçerli kayıt oluşabilmeli
        with self.assertRaises(TypeError):
            ft.PriceSnapshot("A", 99.9, "TRY", ft.utcnow())

    def test_non_positive_rejected(self):
        with self.assertRaises(ValueError):
            snap(price="0")

    def test_price_normalized(self):
        self.assertEqual(snap(price="10.005").price, Decimal("10.01"))


# ---------------------------------------------------------------- ADIM 2
class TestSources(unittest.TestCase):
    def test_registry(self):
        self.assertIn("magaza_a", ft.SOURCE_REGISTRY)
        self.assertIn("magaza_b", ft.SOURCE_REGISTRY)

    def test_duplicate_registration(self):
        with self.assertRaises(ValueError):
            ft.register_source("magaza_a")(type("Kopya", (), {}))

    def test_parse_tr_price(self):
        self.assertEqual(ft.parse_tr_price("1.299,90"), Decimal("1299.90"))
        self.assertEqual(ft.parse_tr_price("12.345.678,05"), Decimal("12345678.05"))
        self.assertEqual(ft.parse_tr_price("99,00"), Decimal("99.00"))

    def test_protocol_structural(self):
        class Duck:  # PriceSource'tan kalıtım YOK
            name = "duck"
            async def fetch(self, product): ...
        self.assertIsInstance(Duck(), ft.PriceSource)


# ---------------------------------------------------------------- ADIM 4
class TestRetry(unittest.IsolatedAsyncioTestCase):
    async def test_succeeds_after_transient(self):
        calls = 0

        @ft.retry(attempts=3, base_delay=0)
        async def flaky():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ft.TransientError("503")
            return "ok"

        self.assertEqual(await flaky(), "ok")
        self.assertEqual(calls, 3)

    async def test_gives_up(self):
        @ft.retry(attempts=2, base_delay=0)
        async def always_fail():
            raise ft.TransientError("429")

        with self.assertRaises(ft.TransientError):
            await always_fail()

    async def test_permanent_not_retried(self):
        calls = 0

        @ft.retry(attempts=5, base_delay=0)
        async def gone():
            nonlocal calls
            calls += 1
            raise ft.PermanentError("404")

        with self.assertRaises(ft.PermanentError):
            await gone()
        self.assertEqual(calls, 1)

    def test_rejects_sync_function(self):
        with self.assertRaises(TypeError):
            @ft.retry()
            def sync(): ...

    def test_wraps_preserves_name(self):
        @ft.retry()
        async def my_func():
            """belge"""
        self.assertEqual(my_func.__name__, "my_func")
        self.assertEqual(my_func.__doc__, "belge")


# ---------------------------------------------------------------- ADIM 5
class TestTokenBucket(unittest.IsolatedAsyncioTestCase):
    async def test_rate_is_enforced(self):
        bucket = ft.TokenBucket(rate=20, capacity=1)
        t0 = time.perf_counter()
        for _ in range(5):          # 1 anında + 4 × 50 ms
            await bucket.acquire()
        self.assertGreaterEqual(time.perf_counter() - t0, 0.18)

    async def test_burst_is_instant(self):
        bucket = ft.TokenBucket(rate=1, capacity=5)
        t0 = time.perf_counter()
        for _ in range(5):
            await bucket.acquire()
        self.assertLess(time.perf_counter() - t0, 0.05)


# ---------------------------------------------------------------- ADIM 6-7
class FakeSource:
    name = "sahte"

    def __init__(self, fail_skus=(), delays=None):
        self.fail_skus = set(fail_skus)
        self.delays = delays or {}
        self.started = []

    async def fetch(self, product):
        self.started.append(product.sku)
        await asyncio.sleep(self.delays.get(product.sku, 0.01))
        if product.sku in self.fail_skus:
            raise ft.PermanentError("404")
        return snap(product.sku)


def products(*skus):
    return [ft.Product(s, s, "sahte") for s in skus]


class TestEngine(unittest.IsolatedAsyncioTestCase):
    async def test_one_failure_does_not_kill_scan(self):
        engine = ft.ScanEngine({"sahte": FakeSource(fail_skus={"X"})})
        results = await engine.scan_all(products("A", "X", "B"))
        self.assertEqual([r.ok for r in results], [True, False, True])
        self.assertIn("PermanentError", results[1].error)

    async def test_stream_yields_in_completion_order(self):
        src = FakeSource(delays={"YAVAS": 0.2, "HIZLI": 0.01})
        engine = ft.ScanEngine({"sahte": src})
        order = [r.product.sku async for r in engine.stream(products("YAVAS", "HIZLI"))]
        self.assertEqual(order, ["HIZLI", "YAVAS"])

    async def test_concurrency_limit(self):
        engine = ft.ScanEngine({"sahte": FakeSource(delays={s: 0.1 for s in "ABCD"})},
                               max_concurrency=2)
        t0 = time.perf_counter()
        await engine.scan_all(products(*"ABCD"))
        self.assertGreaterEqual(time.perf_counter() - t0, 0.19)  # 2 dalga × 0.1


# ---------------------------------------------------------------- ADIM 8-9
class TestAlarm(unittest.TestCase):
    def kw(self, **over):
        base = dict(last=Decimal("100"), window_min=Decimal("90"), history_count=5,
                    threshold=Decimal("10"))
        base.update(over)
        return base

    def test_no_history(self):
        self.assertIsNone(ft.evaluate_price("A", Decimal("50"), **self.kw(last=None, window_min=None, history_count=0)))

    def test_below_threshold(self):
        self.assertIsNone(ft.evaluate_price("A", Decimal("95"), **self.kw()))

    def test_drop(self):
        a = ft.evaluate_price("A", Decimal("90"), **self.kw(window_min=Decimal("80")))
        self.assertEqual(a.kind, ft.AlertKind.DUSUS)
        self.assertEqual(a.drop_pct, Decimal("10.00"))

    def test_new_low(self):
        a = ft.evaluate_price("A", Decimal("72"), **self.kw(window_min=Decimal("80")))
        self.assertEqual(a.kind, ft.AlertKind.DIP)
        self.assertEqual(a.reference_price, Decimal("80"))


class TestTracker(unittest.IsolatedAsyncioTestCase):
    async def test_roundtrip_and_alert(self):
        ok = lambda sku, p: ft.FetchResult(ft.Product(sku, sku, "x"), snapshot=snap(sku, p))
        async with ft.PriceTracker(":memory:", drop_threshold=Decimal("10")) as tr:
            self.assertEqual(await tr.process([ok("A", "100"), ok("B", "50")]), [])
            alerts = await tr.process([ok("A", "80"), ok("B", "49")])
        self.assertEqual([(a.sku, a.drop_pct) for a in alerts], [("A", Decimal("20.00"))])

    async def test_requires_context_manager(self):
        with self.assertRaises(RuntimeError):
            await ft.PriceTracker(":memory:").process([])


if __name__ == "__main__":
    unittest.main()
