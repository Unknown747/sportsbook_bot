# Panduan Instalasi — Sportsbook Auto Betting Agent

Bot taruhan olahraga otomatis yang terhubung ke Stake.com GraphQL API menggunakan mata uang IDR.

---

## Persyaratan Sistem

| Kebutuhan | Versi Minimum |
|---|---|
| Python | 3.9+ |
| pip | terbaru |
| Koneksi internet | stabil |
| Akun Stake.com | aktif |

---

## Langkah 1 — Clone / Download Project

```bash
git clone <url-repo-kamu>
cd sportsbook-bot
```

Atau download ZIP lalu ekstrak ke folder pilihan kamu.

---

## Langkah 2 — Install Dependensi Python

```bash
pip install -r requirements.txt
```

Dependensi yang diinstall:
- `requests` — HTTP client untuk GraphQL API Stake.com

---

## Langkah 3 — Dapatkan API Key Stake.com

1. Login ke [stake.com](https://stake.com)
2. Buka **Settings** → **API**
3. Klik **Generate API Key**
4. Salin key yang muncul

> ⚠️ Simpan API key dengan aman. Jangan bagikan ke siapa pun.

---

## Langkah 3b — (Opsional) Daftar OddsAPI untuk Data Odds Live

Stake.com membatasi akses odds pertandingan melalui API key — odds live hanya bisa diambil melalui penyedia data eksternal. Bot mendukung **the-odds-api.com** (gratis 500 request/bulan):

1. Daftar di [the-odds-api.com](https://the-odds-api.com)
2. Salin API key kamu
3. Tambahkan ke environment variable (lihat Langkah 4):
   ```
   Key:   ODDS_API_KEY
   Value: <odds-api-key-kamu>
   ```

Tanpa `ODDS_API_KEY`, bot tetap berjalan menggunakan **sample data** (aman untuk simulasi dan testing).

| Mode | ODDS_API_KEY | Sumber Data |
|---|---|---|
| Simulasi penuh | Tidak perlu | Sample data bawaan |
| Live odds | Wajib diset | the-odds-api.com |

---

## Langkah 4 — Set API Key sebagai Environment Variable

### Di Replit (Rekomendasi)
Buka tab **Secrets** di sidebar Replit, lalu tambahkan:
```
Key:   STAKE_API_KEY
Value: <api-key-kamu>
```

### Di Terminal Lokal (Linux/macOS)
```bash
export STAKE_API_KEY="api-key-kamu-di-sini"
```

### Di Terminal Lokal (Windows)
```cmd
set STAKE_API_KEY=api-key-kamu-di-sini
```

### Permanen di Linux/macOS (opsional)
Tambahkan baris berikut ke `~/.bashrc` atau `~/.zshrc`:
```bash
export STAKE_API_KEY="api-key-kamu-di-sini"
```

---

## Langkah 5 — Konfigurasi Bot

Edit file `bot_config.py` sesuai kebutuhan:

```python
# Saldo awal simulasi (dalam IDR)
INITIAL_BANKROLL: float = 100000.0

# Multiplier Kelly (lebih kecil = lebih konservatif)
KELLY_MULTIPLIER: float = 0.10

# Minimal edge untuk masuk taruhan (5% = 0.05)
MIN_VALUE_EDGE: float = 0.05

# Batas taruhan per bet (IDR)
MIN_BET_IDR: float = 2000.0
MAX_BET_IDR: float = 5000.0

# Batas rugi harian (10% dari bankroll)
MAX_DAILY_DRAWDOWN: float = 0.10

# Mata uang
STAKE_CURRENCY: str = "idr"

# TRUE = simulasi (aman, tidak taruhan sungguhan)
# FALSE = taruhan live ke akun Stake.com kamu
SIMULATION_MODE: bool = True
```

> ⚠️ **JANGAN ubah `SIMULATION_MODE` ke `False` sebelum kamu yakin sepenuhnya bot bekerja dengan benar.**

---

## Langkah 6 — Jalankan Bot

```bash
python main.py
```

Bot akan berjalan otomatis setiap **10 menit** dan menampilkan log seperti:

```
2026-07-05 09:27:23 [INFO] Sportsbook Auto Betting Agent starting.
2026-07-05 09:27:23 [INFO] Simulation mode: True
2026-07-05 09:27:23 [INFO] Initial bankroll: Rp100000.00
2026-07-05 09:27:23 [INFO] --- Starting new betting cycle ---
2026-07-05 09:27:23 [INFO] [match_001] AI Probs => Home:0.491 Draw:0.207 Away:0.303
2026-07-05 09:27:23 [INFO] [match_001] Opportunity: value_bet | Away | Odds:4.20 | Edge:0.0644
2026-07-05 09:27:23 [INFO] Cycle complete. Sleeping 600 seconds until next run.
```

---

## Struktur File Project

Semua modul diletakkan langsung di root (tidak dalam folder) agar tidak ada folder yang namanya bentrok dengan script:

```
main.py                 # Entry point root
bot_main.py             # Orkestrator utama (loop 10 menit)
bot_config.py           # Semua konfigurasi & konstanta
predictor.py            # Prediksi AI (Ensemble ML + Multi-Agent LLM)
arbitrage_finder.py     # Pencari value bet & arbitrase
bet_sizer.py            # Kalkulator Kelly Criterion (IDR)
executor.py             # Klien GraphQL Stake.com + logger taruhan
fetcher.py              # Pengambil data odds (OddsAPI / Stake)
telegram_notifier.py    # Pengirim notifikasi Telegram
requirements.txt        # Dependensi Python
bet_history.json        # Log otomatis semua taruhan (dibuat saat pertama run)
INSTALL.md              # Panduan ini
```

---

## Riwayat Taruhan

Setiap taruhan (simulasi maupun live) otomatis tersimpan di `bet_history.json`:

```json
[
  {
    "timestamp": "2026-07-05T09:27:23+00:00",
    "active_odds_id": "odds_001_away",
    "stake_idr": 5000.0,
    "currency": "idr",
    "simulation": true,
    "status": "SIMULATED",
    "match_info": {
      "match_id": "match_001",
      "home_team": "Team Alpha",
      "away_team": "Team Beta",
      "outcome": "Away",
      "odds": 4.2,
      "edge": 0.0644
    }
  }
]
```

---

## Troubleshooting

| Masalah | Solusi |
|---|---|
| `STAKE_API_KEY tidak ditemukan` | Pastikan environment variable sudah di-set dengan benar |
| `ModuleNotFoundError: requests` | Jalankan `pip install -r requirements.txt` |
| Bot tidak menemukan value bet | Normal — bot hanya taruhan jika edge ≥ 5% |
| Bot berhenti di tengah jalan | Cek koneksi internet; bot otomatis resume di siklus berikutnya |
| `bet_history.json` tidak ada | File dibuat otomatis saat pertama kali bot dijalankan |

---

## Keamanan

- ✅ API key **tidak pernah** disimpan langsung di kode
- ✅ API key dibaca dari environment variable
- ✅ Mode simulasi aktif by default — tidak ada risiko taruhan tidak sengaja
- ✅ Setiap taruhan tercatat lengkap di `bet_history.json`
- ✅ Drawdown protection: bot otomatis berhenti jika rugi harian ≥ 10% bankroll

---

## Dari Simulasi ke Live Betting

Ketika kamu sudah yakin bot berjalan dengan benar:

1. Pastikan `STAKE_API_KEY` sudah diisi dengan key yang valid
2. Edit `bot_config.py`:
   ```python
   SIMULATION_MODE: bool = False
   ```
3. Jalankan ulang bot:
   ```bash
   python main.py
   ```

> ⚠️ **Peringatan:** Taruhan mengandung risiko kehilangan uang. Gunakan bot ini dengan bijak dan hanya dengan dana yang siap kamu tanggung risikonya.
