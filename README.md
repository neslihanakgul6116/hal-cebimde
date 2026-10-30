# Hal Cebimde 🍅

Günlük hal fiyatlarını takip eden, fiyat değişimlerini gösteren ve en yakın semt pazarını bulan mobil web uygulaması (PWA).

## Özellikler
- **Anlık Hal Fiyatları:** Bursa ve İzmir Büyükşehir Belediyelerinin hal bültenlerinden günlük fiyatlar.
- **Fiyat Değişimi ve Trend Grafiği:** Bir önceki güne göre artış/düşüş yüzdeleri ve son günlerin fiyat grafiği.
- **Akıllı Alarm Sistemi:** Ürünlerin son 2 günkü fiyatını karşılaştırıp, fiyat düşüşü veya artışı olduğunda kullanıcıyı uyaran canlı alarm kontrolü.
- **Pazar Bulucu:** OpenStreetMap verilerini kullanarak konumunuza en yakın ve bugün kurulan pazarları listeler.
- **Karanlık/Aydınlık Tema:** Göz yormayan, kullanıcı tercihine göre değişen modern arayüz.
- **Gizlilik Odaklı:** Konumunuz sunucuya gönderilmez; mesafe tamamen tarayıcınızda hesaplanır.
- **Çevrimdışı Çalışma:** Service Worker sayesinde internet yokken bile son verileri görebilirsiniz (PWA).

## Kurulum

Projeyi klonladıktan sonra gerekli kütüphaneleri yükleyin:
```bash
pip install -r requirements.txt
Veriyi Hazırlama (Tek Tıkla Güncelleme)
Uygulamayı çalıştırmadan önce veritabanını doldurmanız gerekir. Ana dizinde (fiyat_takip klasöründe) şu komutu çalıştırın:

bash
python update.py
(Bu komut sırasıyla Bursa Hal, İzmir Hal ve Pazar Yerleri verilerini çeker.)

Çalıştırma
hal_app klasörüne girin ve sunucuyu başlatın:

bash
cd hal_app
uvicorn app:app --host 0.0.0.0 --port 8000
Tarayıcınızda http://localhost:8000 adresine gidin.

Proje Yapısı
hal_app/: FastAPI sunucusu ve PWA arayüzü (asıl uygulama)

update.py: Tüm verileri tek tıkla güncelleyen otomasyon scripti

bursa_hal.py: Bursa BB hal sayfası kazıyıcısı (async)

izmir_hal.py: İzmir hal verilerini çekip veritabanına yazan script

fiyat_takip.py: Asenkron fiyat takip motoru (alarm, retry, rate-limit)

osm_pazar.py: OpenStreetMap'ten pazar yerlerini çeken script

Lisans
Bu proje MIT lisansı ile lisanslanmıştır.
