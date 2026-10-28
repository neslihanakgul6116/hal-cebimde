#!/usr/bin/env python3
"""
İzmir hal verilerini çekip uygulamanın veritabanına yazar.
Hocanın turk_kaynaklar.py dosyasındaki hazır kodları kullanır.
"""
import argparse
import asyncio
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Hocanın yazdığı hazır İzmir ve Ağ kodlarını çağırıyoruz
from turk_kaynaklar import RealNetwork, latest_hal_bulletin, parse_hal, BASE_URLS

SCHEMA = """
CREATE TABLE IF NOT EXISTS hal_bulten (
    sehir       TEXT NOT NULL,
    tarih       TEXT NOT NULL,
    kategori    TEXT NOT NULL,
    urun        TEXT NOT NULL,
    birim       TEXT NOT NULL,
    asgari      TEXT NOT NULL,
    azami       TEXT NOT NULL,
    kayit_zamani TEXT NOT NULL,
    PRIMARY KEY (sehir, tarih, kategori, urun, birim)
);
"""

async def main(db_path: Path) -> None:
    print("İzmir hal verileri çekiliyor...")
    try:
        # Hocanın RealNetwork sınıfını kullanarak veriyi çekiyoruz
        async with RealNetwork({"izmir_hal": BASE_URLS["izmir_hal"]}) as net:
            day, text = await latest_hal_bulletin(net)
            print(f"Bülten tarihi: {day.isoformat()}")
            
            prices = parse_hal(text)
            if not prices:
                print("Hata: Bültende ürün bulunamadı.")
                return

            rows = []
            now = datetime.now().isoformat(timespec="seconds")
            
            for sku, (price, tip) in prices.items():
                # sku formatı "Domates (Kg)" şeklinde geliyor, ayırıyoruz
                if " (" in sku and sku.endswith(")"):
                    urun, birim = sku.rsplit(" (", 1)
                    birim = birim[:-1] # son parantezi sil
                else:
                    urun, birim = sku, "?"
                    
                kategori = tip if tip else "Sebze/Meyve"
                asgari = str(price)
                azami = str(price)
                
                rows.append(("İzmir", day.isoformat(), kategori, urun.strip(), birim.strip(), asgari, azami, now))

            conn = sqlite3.connect(db_path)
            try:
                with conn:
                    conn.executescript(SCHEMA)
                    conn.executemany("INSERT OR REPLACE INTO hal_bulten VALUES (?,?,?,?,?,?,?,?)", rows)
            finally:
                conn.close()
            
            print(f"Başarılı: {len(rows)} ürün İzmir veritabanına kaydedildi -> {db_path}")
            
    except Exception as e:
        print(f"Hata oluştu: {e}", file=sys.stderr)

if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path("hal_app/hal.db"))
    args = ap.parse_args()
    asyncio.run(main(args.db))