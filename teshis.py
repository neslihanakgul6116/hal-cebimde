#!/usr/bin/env python3
"""
Bağlantı teşhisi: bursa_hal.py zaman aşımına uğruyorsa sebebini bulur.

    python teshis.py
    python teshis.py https://www.bursa.bel.tr/hal_fiyatlari

Denenenler:
  1) DNS: hangi IPv4 / IPv6 adresleri dönüyor
  2) Her adrese TCP bağlantısı (443)
  3) aiohttp ile sayfa: önce IPv4+IPv6 (varsayılan), sonra yalnızca IPv4
  4) urllib (standart kütüphane) ile sayfa
Sonunda hangi ayarın çalıştığını ve ne yapılacağını yazar.
"""
from __future__ import annotations

import asyncio
import socket
import sys
import time
import urllib.request
from urllib.parse import urlparse

import aiohttp

URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.bursa.bel.tr/hal_fiyatlari"
UA = "FiyatTakipDers/1.0 (+egitim amacli)"
TIMEOUT = 40
MARKER = "Tarihli"  # sayfa başlığı: "dd.mm.yyyy Tarihli Hal Fiyatları"


def line(ok: bool | None, text: str) -> None:
    mark = {True: "[ OK ]", False: "[HATA]", None: "[ -- ]"}[ok]
    print(f"{mark} {text}")


def dns(host: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {"IPv4": [], "IPv6": []}
    for family, label in ((socket.AF_INET, "IPv4"), (socket.AF_INET6, "IPv6")):
        try:
            infos = socket.getaddrinfo(host, 443, family, socket.SOCK_STREAM)
            result[label] = sorted({i[4][0] for i in infos})
        except socket.gaierror:
            pass
    return result


def tcp(addr: str, family: int) -> float | None:
    t0 = time.perf_counter()
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.settimeout(8)
            s.connect((addr, 443))
        return time.perf_counter() - t0
    except OSError:
        return None


async def aio_get(family: int) -> tuple[bool, str]:
    connector = aiohttp.TCPConnector(family=family)
    timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=TIMEOUT)
    t0 = time.perf_counter()
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout,
                                         headers={"User-Agent": UA}) as session:
            async with session.get(URL) as resp:
                body = await resp.read()
        took = time.perf_counter() - t0
        found = MARKER.encode() in body
        return (resp.status == 200 and found,
                f"HTTP {resp.status}, {len(body) / 1024:.0f} KB, {took:.1f} sn, "
                f"bülten başlığı {'var' if found else 'YOK'}")
    except Exception as exc:  # teşhis: her hatayı raporla
        return False, f"{type(exc).__name__} {exc} ({time.perf_counter() - t0:.1f} sn)"


def urllib_get() -> tuple[bool, str]:
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(URL, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read()
            status = resp.status
        found = MARKER.encode() in body
        return (status == 200 and found,
                f"HTTP {status}, {len(body) / 1024:.0f} KB, {time.perf_counter() - t0:.1f} sn")
    except Exception as exc:
        return False, f"{type(exc).__name__} {exc} ({time.perf_counter() - t0:.1f} sn)"


async def main() -> None:
    host = urlparse(URL).hostname or ""
    print(f"Hedef: {URL}\n")

    addrs = dns(host)
    line(bool(addrs["IPv4"] or addrs["IPv6"]),
         f"DNS  IPv4: {', '.join(addrs['IPv4']) or 'yok'} | IPv6: {', '.join(addrs['IPv6']) or 'yok'}")
    for label, family in (("IPv4", socket.AF_INET), ("IPv6", socket.AF_INET6)):
        for addr in addrs[label][:2]:
            t = tcp(addr, family)
            line(t is not None, f"TCP  {label} {addr}:443 " + (f"{t * 1000:.0f} ms" if t else "bağlanamadı"))

    print("\nSayfa indiriliyor (her deneme en fazla ~40 sn)…")
    both_ok, both_msg = await aio_get(0)
    line(both_ok, f"aiohttp (IPv4+IPv6): {both_msg}")
    v4_ok, v4_msg = await aio_get(socket.AF_INET)
    line(v4_ok, f"aiohttp (yalnızca IPv4): {v4_msg}")
    url_ok, url_msg = urllib_get()
    line(url_ok, f"urllib: {url_msg}")

    print("\nSonuç:")
    if both_ok:
        print("  Bağlantı şu an sorunsuz. Önceki hata geçici olabilir; bursa_hal.py'yi tekrar çalıştırın.")
    elif v4_ok:
        print("  IPv6 yolu bozuk, IPv4 çalışıyor. Çözüm:  python bursa_hal.py --ipv4")
    elif url_ok:
        print("  urllib çalışıyor, aiohttp çalışmıyor. Bu çıktıyı Claude'a gönderin.")
    elif not (addrs["IPv4"] or addrs["IPv6"]):
        print("  DNS çözülemedi: internet bağlantısını / DNS ayarını kontrol edin.")
    else:
        print("  Hiçbir yöntem çalışmadı. Sayfa tarayıcıda açılıyor mu?")
        print("  - Tarayıcıda da açılmıyorsa: site geçici olarak kapalı, sonra deneyin.")
        print("  - Tarayıcıda açılıyorsa: site otomatik istekleri engelliyor olabilir.")
        print("    Bu durumda engeli aşmaya çalışmayın; Bursa BB'den veri erişim izni/API isteyin.")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())
