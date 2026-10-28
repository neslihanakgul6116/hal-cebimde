# Async E-Ticaret Fiyat Takip Motoru

Python 3.11+ gerekir. Harici paket gerekmez (yalnızca standart kütüphane).

## Dosyalar

| Dosya | İçerik |
|---|---|
| `fiyat_takip.py` | Referans çözüm (10 adımın tamamı) |
| `fiyat_takip_iskelet.py` | Öğrenci iskeleti: altyapı hazır, adımlar `TODO` |
| `test_fiyat_takip.py` | 25 kabul testi (çözüme de iskelete de çalışır) |

## Çalıştırma

```bash
python fiyat_takip.py --fresh            # temiz DB, 3 tur, 50 ürün
python fiyat_takip.py --fresh -v         # + retry/backoff logları
python fiyat_takip.py --bench            # + eşzamanlılık kıyası, bloklama tuzağı
python fiyat_takip.py --rate 3 -v        # rate limit'i görünür kıl
python fiyat_takip.py --concurrency 1    # async ama sıralı: farkı hisset
python fiyat_takip.py --rounds 1         # DB'yi silmeden tekrar: kalıcılık
```

## Testler

```bash
python -m unittest -v                                         # referans çözüm
FIYAT_MODUL=fiyat_takip_iskelet python -m unittest -v         # Linux/macOS
$env:FIYAT_MODUL="fiyat_takip_iskelet"; python -m unittest -v # PowerShell
```

## Ders akışı (öneri)

| Süre | Adım | Kodda bakılacak yer |
|---|---|---|
| 15 dk | 1. Domain modeli | `Product`, `PriceSnapshot.__post_init__` (`object.__setattr__` hilesi) |
| 15 dk | 2. Protocol + registry | `PriceSource`, `register_source`, `parse_tr_price` |
| 5 dk | 3. Sahte ağ | `FakeNetwork._rng`: neden sıra bağımsız deterministik? |
| 20 dk | 4. Decorator'lar | `retry` 3 katman, `ParamSpec`, `functools.wraps`, `timed` |
| 15 dk | 5. Token bucket | Semaphore ile farkı, `asyncio.Lock` ile `threading.Lock` farkı |
| 20 dk | 6-7. Motor | `TaskGroup`, `except Exception` neden CancelledError'ı yutmaz, `as_completed`, `aclosing` |
| 20 dk | 8-9. Kayıt + alarm | `to_thread`, `__aexit__` dönüş değeri, window function SQL, saf fonksiyon |
| 10 dk | 10. Kıyas | `--bench`: 14 sn → 2 sn ve `time.sleep` tuzağı |

## Tartışma soruları

1. `retry`, `_get` yerine `fetch` üzerinde. Bu yüzden her denemede yeniden token alınıyor. Doğru mu?
2. `PermanentError` neden retry listesinde yok? `ParseError` için ne yapılmalı?
3. `stream()` içinde `finally` ne zaman çalışır? `aclosing` kaldırılırsa ne değişir?
4. `TokenBucket` lock'u tutarken uyuyor. Bu bir hata mı, tasarım kararı mı?
5. Fiyat neden `REAL` değil `TEXT` kolonda saklanıyor?
