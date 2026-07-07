"""
Configuration module for Sportsbook Auto Betting Agent.
Contains all constants, API settings, and risk management parameters.

Di Replit, variabel diambil dari Secrets. Di VPS/server sendiri, taruh
variabel di file `.env` (lihat `.env.example`) — akan otomatis terbaca
lewat python-dotenv jika file itu ada.
"""

import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# ── Bankroll & risk management ────────────────────────────────────────────────
INITIAL_BANKROLL: float = 100_000.0
KELLY_MULTIPLIER: float = 0.10
MIN_VALUE_EDGE: float = 0.02
MIN_BET_IDR: float = 2_000.0
MAX_BET_IDR: float = 5_000.0
# Catatan: bot TIDAK tahu hasil menang/kalah taruhan (tidak ada data settlement),
# jadi angka ini sebenarnya adalah batas EKSPOSUR harian (total stake yang boleh
# dipasang per hari), bukan proteksi drawdown/kerugian riil. Nama variabel
# dipertahankan untuk kompatibilitas, tapi baca sebagai "MAX_DAILY_EXPOSURE".
MAX_DAILY_DRAWDOWN: float = 0.10

# ── Retensi data (housekeeping) ────────────────────────────────────────────────
# bet_history.json & pending_signals.json dipangkas otomatis supaya tidak
# tumbuh tanpa batas — record CONFIRMED_MANUAL/SIMULATED/dsb yang lebih tua
# dari ini akan dibuang saat file ditulis ulang. Sinyal pending (belum
# dikonfirmasi via tombol) yang kedaluwarsa juga dibersihkan terpisah
# (lihat PENDING_SIGNAL_MAX_AGE_HOURS).
BET_HISTORY_RETENTION_DAYS: int = 90
PENDING_SIGNAL_MAX_AGE_HOURS: int = 48

# ── Stake API ─────────────────────────────────────────────────────────────────
STAKE_API_URL: str = "https://stake.com/_api/graphql"
STAKE_API_KEY: str = os.environ.get("STAKE_API_KEY", "")
STAKE_CURRENCY: str = "idr"

# ── OddsAPI — multi-key rotation ──────────────────────────────────────────────
# Untuk rotasi otomatis: set ODDS_API_KEYS ke beberapa key dipisah koma.
#   Contoh: ODDS_API_KEYS=keyABC123,keyDEF456,keyGHI789
# Kalau ODDS_API_KEYS tidak diset, fallback ke ODDS_API_KEY tunggal.
# Bot otomatis pindah key saat kena 429 atau sisa quota ≤ LOW_QUOTA_THRESHOLD.
_raw_keys: str = os.environ.get("ODDS_API_KEYS", "")
ODDS_API_KEYS: list = [k.strip() for k in _raw_keys.split(",") if k.strip()]
if not ODDS_API_KEYS:
    _single = os.environ.get("ODDS_API_KEY", "")
    if _single:
        ODDS_API_KEYS = [_single]
# Backward compat — selalu menunjuk ke key pertama yang aktif.
ODDS_API_KEY: str = ODDS_API_KEYS[0] if ODDS_API_KEYS else ""

# Otomatis pindah key saat sisa quota mencapai batas ini (sebelum benar-benar habis).
ODDS_API_LOW_QUOTA_THRESHOLD: int = 15

# ── Mode ──────────────────────────────────────────────────────────────────────
# Catatan penting: Stake.com TIDAK menyediakan market/odds ID sportsbook riil
# via API, dan situsnya diblok Cloudflare untuk scraping — jadi eksekusi bet
# otomatis ke Stake tidak bisa dijalankan sungguhan. Bot ini SELALU berjalan
# sebagai sinyal LIVE (data riil, tanpa fallback palsu) + konfirmasi taruhan
# MANUAL oleh user lewat tombol Telegram — ini bukan sesuatu yang bisa
# di-toggle lewat config. (SIMULATION_MODE & StakeExecutor lama sudah dihapus
# karena tidak pernah dipanggil dari loop utama.)

# ── Scheduling ────────────────────────────────────────────────────────────────
# Jam berapa bot scan pertama kali setiap hari (WIB = UTC+7).
# Set SCHEDULED_HOURS ke list jam; e.g. [8, 14, 20] = 3x sehari.
SCHEDULED_HOURS: list = [8, 14, 20]
LOOP_INTERVAL_SECONDS: int = 600        # cek ulang setiap 10 menit

# Kirim pengingat otomatis N jam sebelum pertandingan dimulai.
# Contoh: 3 → notif dikirim ketika match tinggal ≤ 3 jam lagi.
MATCH_REMINDER_HOURS_BEFORE: int = 3
# Seberapa sering background thread memeriksa apakah ada match yang mau mulai.
REMINDER_CHECK_INTERVAL_SECONDS: int = 900  # 15 menit

# ── Telegram Alert ────────────────────────────────────────────────────────────
# Dapatkan token dari @BotFather di Telegram, chat_id dari @userinfobot.
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "")
# Minimum edge sebelum alert Telegram dikirim (bisa beda dari MIN_VALUE_EDGE).
TELEGRAM_MIN_EDGE: float = 0.02
# Ambang batas edge untuk label "🔥 HOT" di pesan Telegram — sinyal dengan
# edge setinggi ini dianggap jauh lebih meyakinkan dibanding sinyal biasa.
TELEGRAM_HOT_EDGE: float = 0.05
