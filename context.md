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

Microservice **FastAPI yang stateless** dan di-deploy di **VPS AWS** (EC2,
region Jakarta ap-southeast-3, Docker + Caddy). Tugasnya hanya dua:

1. Mengambil harga harian rata-rata nasional dari PIHPS BI, lalu
   menormalkannya.
2. Menghitung regresi linier (OLS) dari riwayat harga yang dikirim Laravel.

Service ini dipisah dari Laravel agar pekerjaan berat (scraping BI ±50
detik/komoditas/tahun) tidak memakan kredit Laravel Cloud, yang anggarannya
dibatasi ≈ $13/bulan.

### Pembagian tanggung jawab (wajib dijaga)

| Ada di repo ini | Ada di Laravel, jangan dipindah ke sini |
|---|---|
| HTTP ke PIHPS BI, parsing & normalisasi | Penyimpanan riwayat (`commodity_reference_prices`) & prediksi (`price_predictions`) |
| Regresi linier, R², prediksi per hari | **Rumus Harga Pagu** (`predicted_price × margin`, sementara 1.05; harus berada di satu Service Laravel) |
| Validasi input & kode error | Penjadwalan sinkronisasi (backfill & inkremental), tombol "Perbarui data" |
| Autentikasi API key | Fallback ke data lokal saat BI/service ini mati (BR-24) |
| | Hak akses (hanya Purchasing & Owner), multi-tenant, dashboard |
| | Larangan Harga Pagu terlihat Vendor (BR-02) |

Akibatnya:
- **Jangan menambah database, cache persisten, atau scheduler di sini.** Laravel
  adalah sumber kebenaran data. Satu-satunya state adalah cache daftar komoditas
  di memori (TTL `COMMODITY_CACHE_SECONDS`).
- **Jangan menghitung Harga Pagu di sini.** Service mengembalikan
  `predicted_price`, dan margin ditambahkan oleh Laravel.
- Service ini tidak mengenal tenant, user, atau vendor.

## 3. Alur integrasi dengan Laravel

```
Laravel (scheduler harian / tombol "Perbarui data")
  1. GET  /v1/prices   (rentang inkremental sejak tanggal terakhir di DB)
     → simpan ke commodity_reference_prices (source = 'bank_indonesia')
  2. POST /v1/forecast (riwayat dari DB lokal, horizon_days)
     → simpan ke price_predictions + hitung Harga Pagu
Dasbor Analitik hanya membaca DB Laravel, tidak pernah memanggil service ini.
```

Karena dasbor tidak memanggil service ini secara langsung, NFR-11 (≤ 5 detik)
dan fallback BR-24 terpenuhi di sisi Laravel. Service ini boleh lambat atau
sesekali gagal, asalkan gagalnya dengan kode status yang jelas (lihat §4).

Variabel di Laravel: `PRICE_SERVICE_URL`, `PRICE_SERVICE_API_KEY` (nilainya sama
dengan `API_KEY` di sini).

## 4. Kontrak API

Semua endpoint `/v1/*` wajib header `X-API-Key`, yang dibandingkan dengan
`secrets.compare_digest`. **Kontrak ini dipakai Laravel.** Mengubah nama field,
format, atau kode status berarti mengubah Laravel juga, jadi lakukan perubahan
yang kompatibel ke belakang atau buat versi baru (`/v2`).

| Method | Path | Respons |
|---|---|---|
| GET | `/health` | `{"status":"ok"}`, tanpa auth |
| GET | `/v1/commodities` | `[{code, name, category, unit}]`; `code` berformat `com_N` dan dipetakan ke `commodities.api_reference_code` di Laravel |
| GET | `/v1/prices?code&start_date&end_date` | `{code, source:"bank_indonesia", region:"national", start_date, end_date, prices:[{date, price}]}`, terurut naik |
| POST | `/v1/forecast` | Body `{history:[{date, price}], horizon_days}`. Respons `{model:"linear_regression", sample_size, history_start, history_end, slope_per_day, intercept, r_squared, predictions:[{date, price}]}` |

Kode status:
- `401`: API key salah atau tidak ada.
- `422`: input tidak valid. Contohnya kode bukan `com_N`, rentang terbalik atau
  > `MAX_RANGE_DAYS`, riwayat < `MIN_FORECAST_POINTS`, tanggal ganda, horizon >
  `MAX_HORIZON_DAYS`, atau tren menghasilkan harga ≤ 0. Laravel memetakan kode
  ini ke pesan UC-11 3a: "Prediksi harga untuk komoditas ini belum dapat
  dihitung."
- `502`: BI tidak dapat diakses, mengembalikan non-JSON, atau formatnya berubah.
  Laravel lalu menampilkan data lokal terakhir.

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
per komoditas per sinkronisasi sudah cukup. Bila BI mengembalikan 403/429/503,
gagal dengan `502`, jangan mencoba ulang dalam loop.

Bila format BI berubah: buka halaman di atas, periksa tab Network atau cari
`loadUrl` dan `OnBeforeSend` di HTML-nya, lalu perbarui `app/bi_client.py` dan
fixture di `tests/fixtures/` dari respons asli yang dipangkas.

## 6. Model regresi

`app/forecast.py`, OLS dengan numpy:

```
harga = intercept + slope × x,   x = selisih hari kalender sejak tanggal riwayat pertama
slope = Σ(x−x̄)(y−ȳ) / Σ(x−x̄)²,  intercept = ȳ − slope·x̄
R² = 1 − SS_res / SS_tot   (SS_tot = 0 → R² = 1)
```

- `x` memakai **hari kalender**, bukan indeks baris, supaya celah akhir pekan
  dan libur tidak mendistorsi kemiringan. Prediksi dibuat untuk setiap hari
  kalender `history_end + 1 … + horizon_days`.
- Riwayat diurutkan dulu. Tanggal ganda ditolak, dan prediksi ≤ 0 ditolak
  (bukan di-clamp diam-diam).
- Nilai dibulatkan: harga 2 desimal, koefisien 4 desimal.
- Model sengaja dibuat sederhana dan mudah dijelaskan untuk laporan akademik.
  Mengganti model (musiman, ARIMA, dll.) adalah keputusan produk, jadi
  tanyakan dulu.

## 7. Struktur & konvensi kode

```
app/
  main.py        # FastAPI app, routing, auth, pemetaan error → HTTP
  bi_client.py   # BankIndonesiaClient (httpx async) + parse_daily_row
  forecast.py    # linear_forecast + ForecastError
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
tanpanya. Default lainnya: `BI_PRICE_TYPE_ID=1`, `MAX_RANGE_DAYS=400`,
`MIN_FORECAST_POINTS=10`, `MAX_HORIZON_DAYS=90`. `DOMAIN` dipakai Caddy untuk
HTTPS otomatis. Container aplikasi hanya listen di `127.0.0.1:8000`.

## 9. Keputusan yang masih terbuka

Belum diputuskan pemilik proyek per 2026-11-17. Jangan diasumsikan:

1. **Titik prediksi yang menjadi Harga Pagu.** Usulan: nilai prediksi tertinggi
   dalam horizon 30 hari × 1.05. Pilihan lainnya: prediksi hari ke-30. Ini
   dihitung di Laravel, tetapi memengaruhi `horizon_days` yang dikirim.
2. **Panjang riwayat untuk regresi.** Usulan: 90 hari terakhir.
3. **Margin Harga Pagu 5%** masih sementara dan perlu divalidasi (keputusan
   teknis Laravel §12 no. 1).
4. Repo GitHub untuk service ini belum dibuat.
