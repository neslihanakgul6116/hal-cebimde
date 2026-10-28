#!/usr/bin/env python3
"""
Hal Cebimde - Tek Tıkla Güncelleme Sistemi
Bu script; Bursa, İzmir hal fiyatlarını ve pazar yerlerini sırayla çeker.
Kullanım: python update.py
"""
import subprocess
import sys
import time

def run_step(name, command):
    print(f"\n--- {name} Başlıyor ---")
    try:
        # Komutu çalıştır ve çıktıyı canlı olarak terminale bas
        subprocess.run(command, check=True, shell=True)
        print(f"✅ {name} başarıyla tamamlandı.")
    except subprocess.CalledProcessError:
        print(f"❌ {name} sırasında bir hata oluştu! Diğer adıma geçiliyor...")

def main():
    start_time = time.time()
    print("🚀 HAL CEBİMDE - VERİ GÜNCELLEME BAŞLADI\n")

    # 1. Bursa Hal Verileri
    run_step("Bursa Hal Fiyatları", "python bursa_hal.py --ipv4 --db hal_app/hal.db")

    # 2. İzmir Hal Verileri (Bizim eklediğimiz geliştirme)
    run_step("İzmir Hal Fiyatları", "python izmir_hal.py")

    # 3. Pazar Yerleri (OSM)
    # osm_pazar.py hal_app içinde olduğu için o klasöre girip çalıştırıyoruz
    run_step("Pazar Yerleri (OSM)", "cd hal_app && python osm_pazar.py --sehir Bursa")

    sure = time.time() - start_time
    print(f"\n🎉 Tüm işlemler {sure:.1f} saniyede bitti. Uygulamayı başlatabilirsiniz.")

if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()