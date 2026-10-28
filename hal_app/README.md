# Hal Cebimde 🍅

Günlük hal fiyatlarını takip eden, fiyat değişimlerini gösteren ve en yakın semt pazarını bulan mobil web uygulaması (PWA).

## Özellikler
- **Anlık Hal Fiyatları:** Bursa Büyükşehir Belediyesi hal bülteninden günlük fiyatlar (Meyve, Sebze, Su Ürünleri vb.).
- **Fiyat Değişimi:** Bir önceki güne göre fiyat artış/düşüş yüzdeleri.
- **Pazar Bulucu:** OpenStreetMap verilerini kullanarak konumunuza en yakın ve bugün kurulan pazarları listeler.
- **Gizlilik Odaklı:** Konumunuz sunucuya gönderilmez; mesafe tamamen tarayıcınızda hesaplanır.
- **Çevrimdışı Çalışma:** Service Worker sayesinde internet yokken bile son verileri görebilirsiniz (PWA).

## Kurulum

Projeyi klonladıktan sonra gerekli kütüphaneleri yükleyin:

pip install -r requirements.txt

Veriyi Hazırlama
Uygulamayı çalıştırmadan önce veritabanını doldurmanız gerekir.
(Not: bursa_hal.py dosyasının bulunduğu üst dizinden çalıştırın)

# Hal fiyatlarını çek (Bursa)
python bursa_hal.py --ipv4 --db hal_app/hal.db

# Pazar yerlerini çek (OpenStreetMap)
cd hal_app
python osm_pazar.py --sehir Bursa

Çalıştırma
hal_app klasörüne girin ve sunucuyu başlatın:

cd hal_app
uvicorn app:app --host 0.0.0.0 --port 8000

Tarayıcınızda http://localhost:8000 adresine gidin.

Proje Yapısı
hal_app/: FastAPI sunucusu ve PWA arayüzü (asıl uygulama)

bursa_hal.py: Bursa BB hal sayfası kazıyıcısı (async)

fiyat_takip.py: Asenkron fiyat takip motoru (alarm, retry, rate-limit)

turk_kaynaklar.py: TCMB ve İzmir açık veri entegrasyonu

osm_pazar.py: OpenStreetMap'ten pazar yerlerini çeken script

Lisans
Bu proje MIT lisansı ile lisanslanmıştır.

