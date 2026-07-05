# Panduan Deploy ke VPS Sendiri

Panduan ini untuk memindahkan Sportsbook Auto Betting Agent dari Replit ke VPS
kamu sendiri (Ubuntu/Debian) supaya jalan 24/7 secara mandiri, otomatis restart
kalau crash atau server reboot.

---

## 1. Siapkan File Project

Download/copy semua file berikut dari Replit ke VPS kamu (misalnya via `scp`,
`git`, atau upload manual). **Jangan lupa** — file `bet_history.json` dan
`pending_signals.json` boleh dikosongkan/tidak disertakan, biar bot mulai
bersih di VPS.

```
main.py
bot_main.py
bot_config.py
arbitrage_finder.py
bet_sizer.py
executor.py
fetcher.py
predictor.py
telegram_notifier.py
telegram_listener.py
requirements.txt
.env.example
sportsbook-bot.service
```

---

## 2. Login ke VPS & Buat User Khusus (opsional tapi disarankan)

```bash
ssh root@ip-vps-kamu

adduser botuser
su - botuser
mkdir -p ~/sportsbook-bot
cd ~/sportsbook-bot
# upload/copy semua file project ke sini
```

---

## 3. Install Python & Virtual Environment

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip

cd ~/sportsbook-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## 4. Konfigurasi Environment Variables (`.env`)

Di VPS tidak ada "Secrets" seperti Replit, jadi kredensial disimpan di file `.env`:

```bash
cp .env.example .env
nano .env
```

Isi dengan nilai asli kamu:

```
ODDS_API_KEY=isi_dengan_key_asli
STAKE_API_KEY=isi_dengan_key_asli
TELEGRAM_BOT_TOKEN=isi_dengan_token_asli
TELEGRAM_CHAT_ID=isi_dengan_chat_id_asli
```

Simpan (`Ctrl+O`, Enter, `Ctrl+X`).

> ⚠️ **Jangan pernah upload file `.env` ke Git/GitHub** — sudah otomatis
> diabaikan lewat `.gitignore`.

Bot otomatis membaca file `.env` ini berkat `python-dotenv` (sudah termasuk
di `requirements.txt`).

---

## 5. Tes Jalankan Manual Dulu

```bash
source venv/bin/activate
python main.py
```

Kalau muncul log seperti ini, artinya konfigurasi sudah benar:

```
[INFO] bot_main: Sportsbook Prediction Bot — START
[INFO] bot_main: Mode: SIMULASI
[INFO] telegram_notifier: ✅ Telegram connection test berhasil.
[INFO] telegram_listener: 📡 Telegram button listener aktif — menunggu klik tombol...
```

Cek juga HP kamu — harus ada pesan "✅ Sportsbook Bot terhubung!" dari Telegram.

Tekan `Ctrl+C` untuk berhenti setelah dites.

---

## 6. Jalankan Otomatis 24/7 dengan systemd

File `sportsbook-bot.service` sudah disiapkan sebagai template. Sesuaikan
path-nya kalau username atau lokasi folder kamu beda, lalu pasang:

```bash
exit   # kembali ke user root/sudo
sudo cp /home/botuser/sportsbook-bot/sportsbook-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable sportsbook-bot
sudo systemctl start sportsbook-bot
```

Cek statusnya:

```bash
sudo systemctl status sportsbook-bot
```

Lihat log real-time:

```bash
journalctl -u sportsbook-bot -f
# atau
tail -f /home/botuser/sportsbook-bot/bot.log
```

Dengan systemd:
- Bot otomatis **restart sendiri** kalau crash (`Restart=always`)
- Bot otomatis **jalan lagi setelah VPS reboot** (`systemctl enable`)
- Tidak perlu buka terminal/SSH terus-menerus — benar-benar jalan di background

---

## 7. Perintah Berguna

| Perintah | Fungsi |
|---|---|
| `sudo systemctl restart sportsbook-bot` | Restart bot (misal setelah edit `.env` atau kode) |
| `sudo systemctl stop sportsbook-bot` | Hentikan bot |
| `sudo systemctl status sportsbook-bot` | Cek apakah bot sedang jalan |
| `journalctl -u sportsbook-bot -f` | Lihat log secara live |

---

## 8. Update Kode di Kemudian Hari

```bash
su - botuser
cd ~/sportsbook-bot
# ganti/upload file yang diperbarui
source venv/bin/activate
pip install -r requirements.txt   # kalau ada dependensi baru
exit
sudo systemctl restart sportsbook-bot
```

---

## Troubleshooting

| Masalah | Solusi |
|---|---|
| `ModuleNotFoundError` | Pastikan `source venv/bin/activate` dijalankan sebelum `pip install` |
| Bot tidak kirim ke Telegram | Cek isi `.env` — pastikan token/chat_id benar, tidak ada spasi ekstra |
| Service gagal start (`systemctl status` merah) | Cek path `WorkingDirectory` & `ExecStart` di file `.service` sesuai lokasi asli di VPS |
| Bot berhenti setelah SSH ditutup (tanpa systemd) | Gunakan systemd (langkah 6) — jangan hanya jalankan `python main.py` langsung di terminal |
