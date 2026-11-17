# PAGORA Price Service

Microservice Python (FastAPI) untuk PAGORA UC-11: mengambil harga pangan harian
dari PIHPS Bank Indonesia dan menghitung prediksi harga dengan **regresi linier**.

Service menyimpan riwayat BI 2 tahun sendiri (file JSON, bukan database), sinkron
tiap 24 jam, dan memakai model hibrida (Theta + regresi linier autoregresif). Error
prediksinya ±8% lebih kecil dari tebakan naive dan ±55% lebih kecil dari model OLS
garis lurus yang dipakai sebelumnya.

Laravel menghitung Harga Pagu (`predicted_price × margin`) dan menjadi fallback
ketika service ini atau API BI tidak tersedia (PRD BR-24 / NFR-09).

## Sumber data

PIHPS tidak punya API publik resmi. Service memakai endpoint JSON di balik halaman
*Tabel Harga per Komoditas* (`/hargapangan/WebSite/TabelHarga/*`), diverifikasi
2026-11-17. Harga yang diambil = baris **"Semua Provinsi"** (rata-rata nasional),
laporan harian, jenis harga sesuai `BI_PRICE_TYPE_ID`. Hanya hari kerja yang berisi data.

Jenis harga yang dipakai: **3 = Pedagang Besar** (harga grosir), karena Vendor PAGORA
menjual ke Purchasing dalam jumlah besar. Pilihan lain: 1 = Pasar Tradisional,
2 = Pasar Modern, 4 = Produsen. Riwayat disimpan per jenis harga
(`DATA_DIR/prices_type{N}.json`), jadi mengganti jenis memicu backfill baru dan
tidak mencampur data.

Rentang 1 tahun memakan waktu ±50 detik per komoditas (BI mengirim semua provinsi).
Service melakukan backfill `HISTORY_DAYS` (default 730 hari) sekali saat pertama jalan
(±35 menit), lalu sinkronisasi inkremental tiap `SYNC_INTERVAL_HOURS` (dihitung dari
waktu service start). Request ke BI selalu dikirim berurutan, satu per komoditas. Bila
belum ada data sama sekali (mis. BI down saat backfill), sinkronisasi dicoba lagi tiap 15 menit.

## API

Semua endpoint `/v1/*` wajib header `X-API-Key`.

| Method | Path | Keterangan |
|---|---|---|
| GET | `/health` | Cek hidup, tanpa auth |
| GET | `/v1/commodities` | Daftar komoditas BI: `code` (`com_N`) dipakai sebagai `commodities.api_reference_code` di Laravel |
| GET | `/v1/prices?code=com_1&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD` | Harga harian nasional langsung dari BI; rentang maks. `MAX_RANGE_DAYS` |
| GET | `/v1/forecast?code=com_1&horizon_days=30` | Prediksi model hibrida → `code`, `model`, `volatility`, `history_end`, `sample_size`, `trained_at`, `syncing`, `predictions[]` |
| GET | `/v1/status` | Fase & progres sinkronisasi, `ready`, waktu sinkron/latih terakhir & berikutnya, rentang data per komoditas |

Kode status: `401` API key salah, `422` input tidak valid atau riwayat komoditas
kurang (UC-11 alternatif 3a), `502` API BI tidak dapat diakses atau formatnya berubah,
`503` prediksi belum tersedia (lihat di bawah).

### Status sinkronisasi (untuk tampilan frontend)

`/v1/forecast` **tidak** memblokir selama sinkronisasi harian: model sebelumnya tetap
dipakai dan respons berisi `"syncing": true`. `503` hanya muncul bila model belum ada,
dengan header `Retry-After` dan body terstruktur:

```json
{
  "status": "syncing",
  "message": "Sedang mengunduh riwayat harga pertama dari Bank Indonesia (±35 menit). Prediksi tersedia setelah selesai.",
  "phase": "syncing",
  "progress": {"started_at": "...", "done": 5, "total": 21, "current": "com_6", "backfill": true},
  "next_sync_at": null
}
```

| `status` | Arti | Saran tampilan |
|---|---|---|
| `syncing` | Normal: data sedang diunduh / model sedang dilatih | Indikator sinkronisasi + progres `done/total`, coba lagi setelah `Retry-After` detik |
| `unavailable` | Sinkronisasi gagal (mis. BI down), dicoba lagi pada `next_sync_at` | Pesan "data belum tersedia", pakai data lokal Laravel |

Body tanpa field `status` (atau error dari proxy) berarti gangguan sungguhan.

### Model prediksi (hibrida)

Dipilih dari backtest walk-forward 2 tahun data PIHPS (21 komoditas, Nov 2024–Nov 2026;
tahun pertama hanya untuk latih, tahun kedua untuk uji):

- **Komoditas stabil** (volatilitas harian log harga < 0,4%, mis. beras, gula, minyak,
  daging sapi, telur): **metode Theta**, yaitu *simple exponential smoothing* ditambah
  setengah slope regresi linier 60 hari terakhir (Hyndman & Billah, 2003).
- **Komoditas lain** (cabai, bawang, ayam): **regresi linier autoregresif (ridge)** pada
  log harga. Variabel: perubahan harga 1/5/20 hari kerja dan jarak harga dari rata-rata
  20 & 60 hari kerja. Satu model per horizon, dilatih gabungan dari semua komoditas.

Prediksi dibuat per hari kerja. Sabtu/Minggu memakai nilai hari kerja sebelumnya.

| Model | MAPE ≤1 minggu | MAPE ≤1 bulan | Error relatif thd. naive | Lebih baik dari naive |
|---|---|---|---|---|
| **Hibrida (dipakai)** | **0,71%** | **2,27%** | **0,92** | **18/21 komoditas** |
| Naive (harga terakhir) | 0,83% | 2,49% | 1,00 | — |
| OLS garis lurus semua riwayat (sebelumnya) | 4,55% | 5,00% | 1,98 | 0/21 |

Backtest di atas memakai harga Pasar Tradisional (jenis 1). Akurasi untuk Pedagang
Besar (jenis 3) belum diukur ulang. Akurasi sangat bergantung pada komoditas: beras/gula ±0,2–0,4%, cabai ±6–10% (MAPE ≤1 bulan).
Prediksi makin tidak pasti makin jauh horizonnya.

## Menjalankan

Prasyarat: salin file env dan isi `API_KEY` (wajib, minimal 32 karakter).

```bash
cp .env.example .env
openssl rand -hex 32   # tempel hasilnya ke API_KEY di .env
```

Variabel lain di `.env.example` punya nilai default dan boleh dibiarkan.
`DOMAIN` hanya dipakai oleh Caddy (Docker), tidak perlu untuk run lokal.

Saat pertama jalan, service mengunduh riwayat BI 2 tahun di latar belakang (±35 menit).
Selama itu endpoint lain sudah bisa dipakai, tetapi `/v1/forecast` menjawab `503`
dengan `"status": "syncing"`. Pantau progresnya lewat log atau `GET /v1/status`.
Data disimpan di `DATA_DIR/prices_type{N}.json` (default `data/`), jadi backfill tidak diulang
saat service di-restart.

### Opsi A: Lokal (Python 3.12)

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

uvicorn app.main:app --reload --port 8000
```

### Opsi B: Docker

Untuk mencoba di komputer sendiri:

```bash
docker compose up -d --build
docker compose logs -f app       # lihat log
docker compose down              # hentikan
```

Container `app` dapat diakses di `http://localhost:8000`. Riwayat harga disimpan di
volume `price_data`, sehingga tetap ada walau container dibuat ulang. Biarkan `DOMAIN` kosong
(default `localhost`) agar Caddy tidak mencoba menerbitkan sertifikat publik.
Untuk deploy dengan domain publik, lihat bagian [Deploy](#deploy-aws-ec2).

Tanpa Compose (hanya service API, tanpa Caddy):

```bash
docker build -t pagora-price-service .
docker run --rm -p 8000:8000 --env-file .env -v pagora-data:/srv/data pagora-price-service
```

### Verifikasi

```bash
curl http://localhost:8000/health

curl -H "X-API-Key: <API_KEY>" http://localhost:8000/v1/commodities

curl -H "X-API-Key: <API_KEY>" \
  "http://localhost:8000/v1/prices?code=com_1&start_date=2026-11-01&end_date=2026-11-14"

curl -H "X-API-Key: <API_KEY>" http://localhost:8000/v1/status

curl -H "X-API-Key: <API_KEY>" "http://localhost:8000/v1/forecast?code=com_1&horizon_days=30"
```

Dokumentasi interaktif (Swagger) tersedia di `http://localhost:8000/docs`.

## Testing

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Atau tanpa instalasi lokal:

```bash
docker run --rm -v "$PWD":/srv -w /srv python:3.12-slim \
  sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q"
```

## Deploy (AWS EC2)

1. EC2 Ubuntu, region **ap-southeast-3 (Jakarta)**, `t3.micro` cukup. Security group: buka 22 (IP Anda), 80, 443.
2. Arahkan DNS A record (mis. `price.domain-anda`) ke Elastic IP instance.
3. Instal Docker, clone repo ini, lalu:
   ```bash
   cp .env.example .env   # isi API_KEY (openssl rand -hex 32) dan DOMAIN
   docker compose up -d --build
   ```
   Caddy menerbitkan sertifikat HTTPS otomatis.
4. Di Laravel: `PRICE_SERVICE_URL=https://price.domain-anda` dan `PRICE_SERVICE_API_KEY=<API_KEY yang sama>`.
