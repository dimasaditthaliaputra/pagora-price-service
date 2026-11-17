# PAGORA Price Service

Microservice Python (FastAPI) untuk PAGORA UC-11: mengambil harga pangan harian
dari PIHPS Bank Indonesia dan menghitung prediksi harga dengan **Regresi Linier (OLS)**.

Service ini **stateless** (tanpa database). Laravel menyimpan riwayat harga &
hasil prediksi, menghitung Harga Pagu (`predicted_price × margin`), dan menjadi
fallback ketika service ini atau API BI tidak tersedia (PRD BR-24 / NFR-09).

## Sumber data

PIHPS tidak punya API publik resmi. Service memakai endpoint JSON di balik halaman
*Tabel Harga per Komoditas* (`/hargapangan/WebSite/TabelHarga/*`), diverifikasi
2026-11-17. Harga yang diambil = baris **"Semua Provinsi"** (rata-rata nasional),
laporan harian, jenis pasar sesuai `BI_PRICE_TYPE_ID`. Hanya hari kerja yang berisi data.

Rentang 1 tahun memakan waktu ±50 detik per komoditas (BI mengirim semua provinsi),
jadi Laravel melakukan backfill sekali lalu sinkronisasi inkremental.

## API

Semua endpoint `/v1/*` wajib header `X-API-Key`.

| Method | Path | Keterangan |
|---|---|---|
| GET | `/health` | Cek hidup, tanpa auth |
| GET | `/v1/commodities` | Daftar komoditas BI: `code` (`com_N`) dipakai sebagai `commodities.api_reference_code` di Laravel |
| GET | `/v1/prices?code=com_1&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD` | Harga harian nasional; rentang maks. `MAX_RANGE_DAYS` |
| POST | `/v1/forecast` | Body `{"history":[{"date","price"}], "horizon_days":30}` → `slope_per_day`, `intercept`, `r_squared`, `sample_size`, `predictions[]` |

Kode status: `401` API key salah, `422` input/riwayat tidak memenuhi syarat
(UC-11 alternatif 3a), `502` API BI tidak dapat diakses atau formatnya berubah.

Model: `harga = intercept + slope × x`, dengan `x` = jumlah hari kalender sejak tanggal
riwayat pertama, sehingga celah akhir pekan tidak mendistorsi kemiringan.

## Menjalankan

Prasyarat: salin file env dan isi `API_KEY` (wajib, minimal 32 karakter).

```bash
cp .env.example .env
openssl rand -hex 32   # tempel hasilnya ke API_KEY di .env
```

Variabel lain di `.env.example` punya nilai default dan boleh dibiarkan.
`DOMAIN` hanya dipakai oleh Caddy (Docker), tidak perlu untuk run lokal.

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

Container `app` dapat diakses di `http://localhost:8000`. Biarkan `DOMAIN` kosong
(default `localhost`) agar Caddy tidak mencoba menerbitkan sertifikat publik.
Untuk deploy dengan domain publik, lihat bagian [Deploy](#deploy-aws-ec2).

Tanpa Compose (hanya service API, tanpa Caddy):

```bash
docker build -t pagora-price-service .
docker run --rm -p 8000:8000 --env-file .env pagora-price-service
```

### Verifikasi

```bash
curl http://localhost:8000/health

curl -H "X-API-Key: <API_KEY>" http://localhost:8000/v1/commodities

curl -H "X-API-Key: <API_KEY>" \
  "http://localhost:8000/v1/prices?code=com_1&start_date=2026-11-01&end_date=2026-11-14"
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
