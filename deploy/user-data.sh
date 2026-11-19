#!/bin/bash
# User data EC2 (Ubuntu 24.04, arm64/x86_64): dijalankan sekali oleh cloud-init saat server pertama kali menyala.
# Menyiapkan server, tetapi TIDAK menjalankan service: API_KEY dan DOMAIN diisi manual lewat
# Session Manager setelah Elastic IP terpasang (lihat README, bagian Deploy), agar rahasia
# tidak tersimpan di user data yang bisa dibaca dari konsol EC2.
#
# Log: /var/log/pagora-setup.log   Hasil cek akses BI: /var/log/pagora-bi-check.log
set -euo pipefail
exec > >(tee -a /var/log/pagora-setup.log) 2>&1

REPO_URL="https://github.com/dimasaditthaliaputra/pagora-price-service.git"
REPO_BRANCH="main"
APP_DIR="/opt/pagora-price-service"
SWAP_FILE="/swapfile"
BI_BASE="https://www.bi.go.id/hargapangan"

echo "== $(date -Is) mulai setup PAGORA price service"

# Swap 1 GB: t4g.micro hanya punya RAM 1 GB; swap mencegah proses mati saat docker build / backfill.
if [ ! -f "$SWAP_FILE" ]; then
  fallocate -l 1G "$SWAP_FILE"
  chmod 600 "$SWAP_FILE"
  mkswap "$SWAP_FILE"
  swapon "$SWAP_FILE"
  echo "$SWAP_FILE none swap sw 0 0" >> /etc/fstab
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y docker.io docker-compose-v2 git curl ca-certificates
systemctl enable --now docker

if [ ! -d "$APP_DIR/.git" ]; then
  git clone --branch "$REPO_BRANCH" --depth 1 "$REPO_URL" "$APP_DIR"
fi

# .env dari template; API_KEY dan DOMAIN sengaja dikosongkan sampai diisi manual.
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
fi
chmod 600 "$APP_DIR/.env"

# Cek akses ke PIHPS BI dari IP server ini (Sydney = IP luar negeri). Dua request kecil, berurutan.
bi_request() {
  curl -s -o /dev/null -m 60 -w "%{http_code}" \
    -A "Mozilla/5.0 (compatible; PAGORA-PriceService/1.0)" \
    -H "X-Requested-With: XMLHttpRequest" \
    -H "Referer: $BI_BASE/TabelHarga/PasarTradisionalKomoditas" \
    "$BI_BASE/WebSite/TabelHarga/$1" || echo "000"
}
today=$(date +%F)
week_ago=$(date -d "7 days ago" +%F)
commodities=$(bi_request "GetRefCommodityAndCategory")
grid=$(bi_request "GetGridDataKomoditas?price_type_id=3&comcat_id=com_1&province_id=&regency_id=&showKota=false&showPasar=false&tipe_laporan=1&start_date=$week_ago&end_date=$today")
if [ "$commodities" = "200" ] && [ "$grid" = "200" ]; then
  verdict="OK: BI dapat diakses dari server ini"
else
  verdict="GAGAL: BI tidak dapat diakses (kemungkinan IP diblokir) - jangan lanjut deploy"
fi
printf '%s\nGetRefCommodityAndCategory: HTTP %s\nGetGridDataKomoditas: HTTP %s\n%s\n' \
  "$(date -Is)" "$commodities" "$grid" "$verdict" | tee /var/log/pagora-bi-check.log

echo "== $(date -Is) setup selesai. Langkah berikutnya: isi API_KEY dan DOMAIN di $APP_DIR/.env, lalu docker compose up -d --build"
