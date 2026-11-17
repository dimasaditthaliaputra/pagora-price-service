# PAGORA Price Service — Konteks untuk AI Agent

Baca dokumen ini sebelum mengubah kode di repo ini. Isinya: posisi service dalam
sistem PAGORA, batas tanggung jawab, kontrak API, perilaku sumber data, dan
keputusan desain yang sudah diambil. Jangan menambah fitur di luar cakupan di
bawah tanpa konfirmasi pemilik proyek.

## 1. Apa itu PAGORA

PAGORA adalah aplikasi web B2B pengadaan bahan pangan (multi-tenant). Aplikasi
utamanya ditulis dengan **Laravel + Inertia React** di repo terpisah
(`pagora-website`) dan di-deploy ke **Laravel Cloud**. Aktornya: Tenant (Owner,
Manajemen, Purchasing, QC Gudang) yang membeli, dan Vendor yang memasok.

Salah satu fitur utama PAGORA adalah **Harga Pagu**: rekomendasi batas harga
wajar tertinggi per komoditas. Purchasing memakainya sebagai acuan saat menyusun
Purchase Order dan bernegosiasi dengan Vendor. Harga Pagu dihitung dari data
**PIHPS Bank Indonesia** dengan **Regresi Linier**. SKPL proyek mewajibkan
bagian regresi ini dikerjakan dengan Python, dan karena itu repo ini ada.

Use case terkait di PRD Laravel: **UC-11 Pantau Tren Harga dan Rekomendasi Harga
Pagu** (FR-15, FR-16, BR-01, BR-02, BR-24, NFR-09, NFR-11).

## 2. Peran repo ini

Microservice **FastAPI** yang di-deploy di **VPS AWS** (EC2,
region Jakarta ap-southeast-3, Docker + Caddy). Tugasnya:

1. Mengambil harga harian rata-rata nasional dari PIHPS BI, lalu
   menormalkannya.
2. Menyimpan riwayat BI 2 tahun sendiri, sinkron tiap 24 jam, dan melatih
   model hibrida (lihat §6). Ini membuat service **stateful**.

**Keputusan 2026-11-17 (pemilik proyek):**
- Akurasi diprioritaskan di atas arsitektur stateless. Backtest 2 tahun
  menunjukkan OLS garis lurus (model awal) errornya ±2× tebakan naive. Model
  terbaik (regresi autoregresif gabungan) butuh riwayat **semua** komoditas
  sekaligus, jadi service menyimpan data dan melatih model sendiri. Alternatif
  "Laravel mengirim riwayat semua komoditas per request" sudah ditawarkan dan
  tidak dipilih.
- Model OLS awal (`POST /v1/forecast`) **dihapus**, karena belum dipakai
  Laravel. Model hibrida menggantikannya di `GET /v1/forecast`.

Service ini dipisah dari Laravel agar pekerjaan berat (scraping BI ±50
detik/komoditas/tahun) tidak memakan kredit Laravel Cloud, yang anggarannya
dibatasi ≈ $13/bulan.

### Pembagian tanggung jawab (wajib dijaga)

| Ada di repo ini | Ada di Laravel, jangan dipindah ke sini |
|---|---|
| HTTP ke PIHPS BI, parsing & normalisasi | Penyimpanan riwayat (`commodity_reference_prices`) & prediksi (`price_predictions`) untuk dasbor |
| Model prediksi hibrida, prediksi per hari | **Rumus Harga Pagu** (`predicted_price × margin`, sementara 1.05; harus berada di satu Service Laravel) |
| Validasi input & kode error | Penjadwalan sinkronisasi Laravel (backfill & inkremental), tombol "Perbarui data" |
| Salinan riwayat BI (`DATA_DIR/prices_type{N}.json`), sinkron & latih ulang berkala (loop latar di proses FastAPI) | |
| Autentikasi API key | Fallback ke data lokal saat BI/service ini mati (BR-24) |
| | Hak akses (hanya Purchasing & Owner), multi-tenant, dashboard |
| | Larangan Harga Pagu terlihat Vendor (BR-02) |

Akibatnya:
- State yang ada: cache daftar komoditas di memori (TTL
  `COMMODITY_CACHE_SECONDS`), file riwayat di `DATA_DIR`, model di memori
  (dilatih ulang dari file saat start), dan satu loop latar untuk sinkronisasi.
  Jangan menambah database atau scheduler lain tanpa persetujuan. Data ini adalah
  salinan untuk melatih model. Untuk dasbor dan riwayat, Laravel tetap sumber
  kebenaran.
- **Jangan menghitung Harga Pagu di sini.** Service mengembalikan
  `predicted_price`, dan margin ditambahkan oleh Laravel.
- Service ini tidak mengenal tenant, user, atau vendor.

## 3. Alur integrasi dengan Laravel

```
Laravel (scheduler harian / tombol "Perbarui data")
  1. GET  /v1/prices   (rentang inkremental sejak tanggal terakhir di DB)
     → simpan ke commodity_reference_prices (source = 'bank_indonesia')
  2. GET  /v1/forecast?code&horizon_days
     → simpan ke price_predictions + hitung Harga Pagu
     503 status=syncing     → tampilkan indikator sinkronisasi, coba lagi (Retry-After)
     503 status=unavailable → pakai prediksi terakhir di DB, atau harga BI terakhir
Dasbor Analitik hanya membaca DB Laravel, tidak pernah memanggil service ini.
```

Karena dasbor tidak memanggil service ini secara langsung, NFR-11 (≤ 5 detik)
dan fallback BR-24 terpenuhi di sisi Laravel. Service ini boleh lambat atau
sesekali gagal, asalkan gagalnya dengan kode status yang jelas (lihat §4).

Variabel di Laravel: `PRICE_SERVICE_URL`, `PRICE_SERVICE_API_KEY` (nilainya sama
dengan `API_KEY` di sini).

## 4. Kontrak API

Semua endpoint `/v1/*` wajib header `X-API-Key`, yang dibandingkan dengan
`secrets.compare_digest`. Per 2026-11-17 Laravel belum memanggil service ini.
Setelah Laravel memakainya, mengubah nama field, format, atau kode status berarti
mengubah Laravel juga, jadi lakukan perubahan yang kompatibel ke belakang atau
buat versi baru (`/v2`).

| Method | Path | Respons |
|---|---|---|
| GET | `/health` | `{"status":"ok"}`, tanpa auth |
| GET | `/v1/commodities` | `[{code, name, category, unit}]`; `code` berformat `com_N` dan dipetakan ke `commodities.api_reference_code` di Laravel |
| GET | `/v1/prices?code&start_date&end_date` | `{code, source:"bank_indonesia", region:"national", start_date, end_date, prices:[{date, price}]}`, terurut naik |
| GET | `/v1/forecast?code&horizon_days` | `{code, model:"theta"\|"linear_autoregression"\|"combination", volatility, history_end, sample_size, trained_at, syncing, predictions:[{date, price}]}`; satu harga per hari kalender |
| GET | `/v1/status` | `{phase:"starting"\|"syncing"\|"training"\|"idle", ready, progress:{started_at, done, total, current, backfill}\|null, last_sync, next_sync_at, model_trained_at, failed_codes, commodities:[{code, data_from, data_until, points}]}` |

Kode status:
- `401`: API key salah atau tidak ada.
- `422`: input tidak valid. Contohnya kode bukan `com_N`, rentang terbalik atau
  > `MAX_RANGE_DAYS`, komoditas belum punya data atau riwayatnya < 60 hari kerja,
  horizon > `MAX_HORIZON_DAYS`, atau tren menghasilkan harga ≤ 0. Laravel memetakan kode
  ini ke pesan UC-11 3a: "Prediksi harga untuk komoditas ini belum dapat
  dihitung."
- `502`: BI tidak dapat diakses, mengembalikan non-JSON, atau formatnya berubah.
  Laravel lalu menampilkan data lokal terakhir.
- `503` (`/v1/forecast`): model belum ada. Body `{status, message, phase,
  progress, next_sync_at}` + header `Retry-After`. `status:"syncing"` = normal,
  data sedang diunduh/model dilatih (frontend menampilkan indikator sinkronisasi,
  bukan error). `status:"unavailable"` = sinkronisasi gagal, dicoba lagi pada
  `next_sync_at`. Saat sinkronisasi harian, forecast **tidak** 503: model lama
  tetap dipakai dengan `syncing: true`.

`prices` boleh kosong (`[]`) bila BI tidak punya data nasional untuk rentang
tersebut. Kondisi ini bukan error.

## 5. Sumber data: PIHPS Bank Indonesia

**Tidak ada API publik resmi.** Endpoint di bawah adalah JSON internal di balik
halaman *Tabel Harga per Komoditas*
(`https://www.bi.go.id/hargapangan/TabelHarga/PasarTradisionalKomoditas`),
terakhir diverifikasi **2026-11-17**. Endpoint ini bisa berubah tanpa
pemberitahuan. Endpoint lama yang dikutip proyek-proyek di GitHub (`GetType`,
`GetProvinceAll`, `GetCommoditiesTree`) sudah **404**.

Base: `{BI_BASE_URL}/WebSite/TabelHarga/`. Header yang dikirim: `Referer`
(halaman di atas), `X-Requested-With: XMLHttpRequest`, dan `User-Agent`.

| Endpoint | Isi |
|---|---|
| `GetRefPriceType` | `1` Pasar Tradisional, `2` Pasar Modern, `3` Pedagang Besar, `4` Produsen |
| `GetRefCommodityAndCategory` | Pohon datar: kategori `cat_N` (`cat_id: null`) dan komoditas `com_N` (`cat_id` → kategori). Ada 21 komoditas. Sebagian nama berakhiran spasi, jadi selalu `strip()` |
| `GetGridDataKomoditas` | Parameter: `price_type_id`, `comcat_id`, `province_id`, `regency_id`, `showKota`, `showPasar`, `tipe_laporan`, `start_date`, `end_date` (ISO) |
| `GetChartKomoditas` | Mengembalikan `{"data":[{}]}` untuk request langsung. Tidak dipakai |

Format `GetGridDataKomoditas` dengan `tipe_laporan=1` (harian):
- `{"data":[{no, name, level, "dd/mm/yyyy": "14,900", ...}, ...]}`.
- Baris `level: 0` = **"Semua Provinsi"** (rata-rata nasional) dan inilah yang
  dipakai. Baris `level: 1` adalah provinsi.
- Harga berupa string dengan pemisah ribuan koma. `"-"` berarti tidak ada data.
- Hanya **hari kerja** yang punya kolom.
- BI selalu mengirim semua provinsi: rentang 1 tahun ≈ 200 KB dan **±50 detik**,
  90 hari ≈ 5 detik. Karena itu ada `BI_TIMEOUT_SECONDS=90` dan Laravel memakai
  sinkronisasi inkremental.

`tipe_laporan=2` (mingguan) **sengaja tidak dipakai**. Labelnya seperti
`"Nov 2026 (V)"`, tanpa tanggal pasti, dan urutannya tidak konsisten.

Etika akses: jangan melakukan request paralel atau berulang ke BI. Satu request
per komoditas per sinkronisasi sudah cukup. Sinkronisasi (`app/sync.py`)
mengikuti aturan ini: berurutan, potongan ≤ 365 hari, komoditas yang gagal
dicoba lagi sendirian tiap 15 menit, maksimal 4 kali (`MAX_RETRIES`), lalu kembali
ke jadwal 24 jam. Selama belum ada model, semua dicoba ulang tiap 15 menit. Bila BI mengembalikan 403/429/503,
gagal dengan `502`, jangan mencoba ulang dalam loop.

Bila format BI berubah: buka halaman di atas, periksa tab Network atau cari
`loadUrl` dan `OnBeforeSend` di HTML-nya, lalu perbarui `app/bi_client.py` dan
fixture di `tests/fixtures/` dari respons asli yang dipangkas.

## 6. Model regresi

### Hibrida (`app/hybrid.py`)

- Volatilitas = simpangan baku perubahan log harga harian, 250 hari kerja
  terakhir. Ambang **0,4%** ditetapkan dari data tahun pertama saja. Rentang
  campuran ±0,1% di sekitarnya ditetapkan sebelum diuji (tidak dicari dari data uji).
- Volatilitas ≤ 0,3% → **Theta**: level SES (alpha dipilih dari SSE 1-langkah
  terkecil, 120 titik terakhir) + ½ × slope OLS 60 hari kalender terakhir ×
  hari ke depan (Hyndman & Billah, 2003).
- 0,3–0,5% → **campuran** (`model: "combination"`): bobot regresi =
  (volatilitas − 0,3%) / 0,2%. Menggantikan pergantian mendadak di 0,4% yang
  membuat telur (Pedagang Besar) berganti metode tiap minggu.
- ≥ 0,5% → **regresi linier autoregresif (ridge, λ=10)** pada log harga.
  Fitur: perubahan log 1/5/20 hari kerja, jarak log harga dari rata-rata 20 &
  60 hari kerja. Satu model per horizon hari kerja (direct multi-step), dilatih
  gabungan dari semua komoditas. Prediksi = harga terakhir × exp(ŷ).
- Prediksi per hari kerja (Senin-Jumat; libur nasional dianggap hari kerja).
  Sabtu/Minggu = nilai hari kerja sebelumnya. Minimal 60 hari kerja riwayat.

Backtest walk-forward kode produksi (uji Nov 2025–Nov 2026, origin tiap 5 hari
kerja, horizon 1–21 hari kerja). Parameter dirancang dari data jenis 1, lalu
diuji tanpa penyesuaian pada jenis 3:

| Jenis harga | MAPE ≤5 hk | MAPE ≤21 hk | Error relatif thd. naive | Menang vs naive | OLS awal (MAPE ≤21 / rel.) |
|---|---|---|---|---|---|
| 3 Pedagang Besar (dipakai) | 0,93% | 2,66% | 0,95 | 16/21 | 5,65% / 2,03 |
| 1 Pasar Tradisional | 0,70% | 2,27% | 0,92 | 18/21 | 5,00% / 1,98 |

Campuran vs pergantian mendadak (error relatif total tidak berubah, ±0,002):
telur jenis 3 membaik dari 1,18 ke 1,06 (masih sedikit di atas naive).

Keterbatasan: keunggulan atas naive kecil dan tidak konsisten. Pada periode
Mei–Nov 2025 (jenis 3, data latih < 1 tahun) error relatifnya 1,01 (6/21
menang). Keunggulan yang konsisten adalah terhadap OLS awal. Mengubah parameter di
atas harus disertai backtest ulang.

Model OLS garis lurus awal sudah dihapus. Mengganti model (musiman, ARIMA,
dll.) adalah keputusan produk, jadi tanyakan dulu.

## 7. Struktur & konvensi kode

```
app/
  main.py        # FastAPI app, routing, auth, pemetaan error → HTTP, start loop latar
  bi_client.py   # BankIndonesiaClient (httpx async) + parse_daily_row
  hybrid.py      # Model: Theta + regresi autoregresif ridge, train, hybrid_forecast, ForecastError
  price_store.py # Riwayat harga lokal (file JSON, tulis atomik)
  sync.py        # ServiceState (fase/progres), sinkronisasi BI berurutan, latih ulang berkala
  schemas.py     # Model Pydantic (kontrak API)
  config.py      # Settings (pydantic-settings, dari env/.env)
tests/           # pytest; fixtures/ = potongan respons BI asli
```

- Python **3.12** (Docker `python:3.12-slim`). Python bawaan macOS (3.9) tidak
  didukung karena kode memakai `X | None` dan sintaks 3.10+.
- Error domain dilempar sebagai exception khusus (`BankIndonesiaError`,
  `ForecastError`) dan baru dipetakan ke HTTP di `main.py`.
- Dependensi dikunci versinya di `requirements*.txt`. Jangan menambah
  dependensi (mis. pandas, scikit-learn) tanpa persetujuan.
- Pesan error untuk pengguna ditulis dalam Bahasa Indonesia. Nama
  kode/identifier dalam Bahasa Inggris.
- Test **tidak boleh memanggil BI asli**. Pakai `httpx.MockTransport` lewat
  `make_bi_client()` dan dependency override di `tests/conftest.py`.

Menjalankan test:

```bash
docker run --rm -v "$PWD":/srv -w /srv python:3.12-slim \
  sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q -p no:cacheprovider"
```

Uji manual terhadap BI asli: build image, jalankan dengan
`-e API_KEY=$(openssl rand -hex 32)`, lalu panggil `/v1/prices` untuk rentang
pendek (≤ 90 hari).

## 8. Konfigurasi

Lihat `.env.example`. `API_KEY` wajib (≥ 32 karakter) dan service gagal start
tanpanya. Default lainnya: `BI_PRICE_TYPE_ID=3`, `MAX_RANGE_DAYS=400`,
`MAX_HORIZON_DAYS=90`, `DATA_DIR=data`,
`HISTORY_DAYS=730`, `SYNC_ENABLED=true`, `SYNC_INTERVAL_HOURS=24`. Di Docker,
`DATA_DIR` = `/srv/data` dan dipasang sebagai volume `price_data`. `DOMAIN` dipakai Caddy untuk
HTTPS otomatis. Container aplikasi hanya listen di `127.0.0.1:8000`.

## 9. Keputusan yang masih terbuka

Belum diputuskan pemilik proyek per 2026-11-17. Jangan diasumsikan:

1. **Titik prediksi yang menjadi Harga Pagu.** Usulan: nilai prediksi tertinggi
   dalam horizon 30 hari × 1.05. Pilihan lainnya: prediksi hari ke-30. Ini
   dihitung di Laravel, tetapi memengaruhi `horizon_days` yang dikirim.
2. ~~Panjang riwayat untuk regresi.~~ Diputuskan: 730 hari (`HISTORY_DAYS`).
5. ~~Jenis harga BI.~~ Diputuskan kelompok 2026-11-17: **3 = Pedagang Besar**
   (harga grosir, paling dekat dengan harga beli B2B). Format respons BI sama
   dengan jenis 1 dan datanya lengkap: 522 hari kerja untuk semua 21 komoditas
   (diverifikasi 2026-11-17). Backtest jenis 3 ada di §6.
3. **Margin Harga Pagu 5%** masih sementara dan perlu divalidasi (keputusan
   teknis Laravel §12 no. 1).
4. Repo GitHub untuk service ini belum dibuat.
