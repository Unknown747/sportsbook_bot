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
MIN_VALUE_EDGE: float = 0.05
MIN_BET_IDR: float = 2_000.0
MAX_BET_IDR: float = 5_000.0
MAX_DAILY_DRAWDOWN: float = 0.10

# ── Stake API ─────────────────────────────────────────────────────────────────
STAKE_API_URL: str = "https://stake.com/_api/graphql"
STAKE_API_KEY: str = os.environ.get("STAKE_API_KEY", "")
STAKE_CURRENCY: str = "idr"

# ── OddsAPI (sumber data odds riil — wajib untuk mode live) ────────────────────
ODDS_API_KEY: str = os.environ.get("ODDS_API_KEY", "")

# ── Mode ──────────────────────────────────────────────────────────────────────
# Catatan penting: Stake.com TIDAK menyediakan market/odds ID sportsbook riil
# via API, dan situsnya diblok Cloudflare untuk scraping — jadi eksekusi bet
# otomatis ke Stake tidak bisa dijalankan sungguhan. Bot ini berjalan sebagai
# sinyal LIVE (data riil, tanpa fallback palsu) + konfirmasi taruhan MANUAL
# oleh user lewat tombol Telegram.
SIMULATION_MODE: bool = os.environ.get("SIMULATION_MODE", "True").strip().lower() != "false"

# ── Scheduling ────────────────────────────────────────────────────────────────
# Jam berapa bot scan pertama kali setiap hari (WIB = UTC+7).
# Set SCHEDULED_HOURS ke list jam; e.g. [8, 14, 20] = 3x sehari.
SCHEDULED_HOURS: list = [8, 14, 20]
LOOP_INTERVAL_SECONDS: int = 600        # cek ulang setiap 10 menit

# ── Telegram Alert ────────────────────────────────────────────────────────────
# Dapatkan token dari @BotFather di Telegram, chat_id dari @userinfobot.
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "")
# Minimum edge sebelum alert Telegram dikirim (bisa beda dari MIN_VALUE_EDGE).
TELEGRAM_MIN_EDGE: float = 0.05
