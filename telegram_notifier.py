"""
Telegram Notifier module untuk Sportsbook Prediction Bot.

Mengirim sinyal VALUE BET langsung ke HP via Telegram Bot API.
Pesan berisi: Tim, Prediksi, Odds Stake, Rekomendasi modal (Kelly), Link Stake,
dan tombol interaktif (inline keyboard) — TIDAK perlu ketik command apapun.
"""

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

import bot_config as config

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"

PENDING_SIGNALS_FILE = "pending_signals.json"

# pending_signals.json ditulis dari 2 thread berbeda (loop utama & Telegram
# listener) — lock ini mencegah race condition read-modify-write.
_pending_lock = threading.Lock()

SPORT_TO_STAKE_URL: dict = {
    # Baseball
    "baseball_mlb":                          "https://stake.com/sports/baseball/mlb",
    "baseball_kbo":                          "https://stake.com/sports/baseball/kbo",
    "baseball_npb":                          "https://stake.com/sports/baseball/npb",
    # Basketball
    "basketball_wnba":                       "https://stake.com/sports/basketball/wnba",
    "basketball_nba":                        "https://stake.com/sports/basketball/nba",
    # American Football
    "americanfootball_nfl_preseason":        "https://stake.com/sports/american-football/nfl",
    "americanfootball_nfl":                  "https://stake.com/sports/american-football/nfl",
    # Cricket
    "cricket_international_t20":             "https://stake.com/sports/cricket",
    "cricket_test_match":                    "https://stake.com/sports/cricket",
    # Soccer — semua liga yang dipantau bot
    "soccer_epl":                            "https://stake.com/sports/soccer/england/premier-league",
    "soccer_laliga":                         "https://stake.com/sports/soccer/spain/laliga",
    "soccer_bundesliga":                     "https://stake.com/sports/soccer/germany/bundesliga",
    "soccer_serie_a":                        "https://stake.com/sports/soccer/italy/serie-a",
    "soccer_ligue_one":                      "https://stake.com/sports/soccer/france/ligue-1",
    "soccer_brazil_campeonato":              "https://stake.com/sports/soccer/brazil/serie-a",
    "soccer_brazil_serie_b":                 "https://stake.com/sports/soccer/brazil/serie-b",
    "soccer_argentina_primera_division":     "https://stake.com/sports/soccer/argentina/primera-division",
    "soccer_austria_bundesliga":             "https://stake.com/sports/soccer/austria/bundesliga",
    "soccer_china_superleague":              "https://stake.com/sports/soccer/china/super-league",
    "soccer_conmebol_copa_libertadores":     "https://stake.com/sports/soccer/conmebol/copa-libertadores",
    "soccer_conmebol_copa_sudamericana":     "https://stake.com/sports/soccer/conmebol/copa-sudamericana",
    "soccer_uefa_champs_league":             "https://stake.com/sports/soccer/europe/champions-league",
    "soccer_uefa_europa_league":             "https://stake.com/sports/soccer/europe/europa-league",
    "soccer_usa_mls":                        "https://stake.com/sports/soccer/usa/mls",
    "soccer_japan_j_league":                 "https://stake.com/sports/soccer/japan/j1-league",
    "soccer_south_korea_kleague1":           "https://stake.com/sports/soccer/south-korea/k-league-1",
}

# Emoji olahraga per kategori (dipakai di pesan Telegram)
_SPORT_EMOJI: dict = {
    "soccer":             "⚽",
    "baseball":           "⚾",
    "baseball_kbo":       "⚾",
    "baseball_npb":       "⚾",
    "basketball":         "🏀",
    "american-football":  "🏈",   # slug dipakai di bot_main SPORTS_TO_SCAN
    "americanfootball":   "🏈",   # prefix dari OddsAPI sport keys (tanpa tanda hubung)
    "cricket":            "🏏",
}

def _sport_emoji(sport_key: str) -> str:
    """Kembalikan emoji yang tepat untuk cabang olahraga."""
    # Cek exact match dulu, lalu prefix match
    if sport_key in _SPORT_EMOJI:
        return _SPORT_EMOJI[sport_key]
    for prefix, emoji in _SPORT_EMOJI.items():
        if sport_key.startswith(prefix):
            return emoji
    return "🏟️"

_OUTCOME_LABEL = {"Home": "🏠 Home (Tim Kandang)", "Away": "✈️ Away (Tim Tandang)", "Draw": "🤝 Seri"}
_OUTCOME_EMOJI = {"Home": "🟢", "Away": "🔵", "Draw": "🟡"}


def _build_stake_link(sport_key: str) -> str:
    """Return best-effort Stake.com link for the match sport page."""
    return SPORT_TO_STAKE_URL.get(sport_key, "https://stake.com/sports")


def _format_signal_message(
    home_team: str,
    away_team: str,
    outcome: str,
    odds: float,
    edge: float,
    kelly_stake: float,
    ai_prob: float,
    sport_key: str,
    opp_type: str,
    platform: str = "market",
) -> str:
    """Format a rich Telegram message for a value bet signal."""
    stake_url = _build_stake_link(sport_key)
    edge_pct = edge * 100
    ai_pct = ai_prob * 100

    tag = "💎 VALUE BET" if opp_type == "value_bet" else "⚡ ARBITRAGE"
    if edge >= config.TELEGRAM_HOT_EDGE:
        tag = f"🔥 HOT {tag}"
    kelly_str = f"Rp{kelly_stake:,.0f}"
    s_emoji = _sport_emoji(sport_key)

    # Tentukan tim mana yang harus dipilih berdasarkan outcome
    if outcome == "Home":
        bet_team = home_team
        bet_label = "🏠 KANDANG"
    elif outcome == "Away":
        bet_team = away_team
        bet_label = "✈️ TANDANG"
    else:  # Draw
        bet_team = "SERI"
        bet_label = "🤝 SERI"

    # Odds minimum agar taruhan tetap menguntungkan secara statistik:
    # = 1 ÷ peluang_fair. Di bawah angka ini → tidak ada edge → JANGAN pasang.
    min_viable_odds = 1.0 / ai_prob if ai_prob > 0 else 0.0

    lines = [
        f"{tag} TERDETEKSI!",
        "",
        f"{s_emoji} *{home_team}* vs *{away_team}*",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"🎯 *PASANG TARUHAN PADA:*",
        f"   *{bet_team}* ({bet_label})",
        "",
        f"📋 *Susunan Tim:*",
        f"   🏠 Kandang : {home_team}",
        f"   ✈️  Tandang  : {away_team}",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"💰 *Odds referensi pasar : `{odds:.2f}`*",
        f"   _(dari {platform} — ini odds terbaik di pasar, bukan odds Stake)_",
        f"📊 Peluang menang (AI)  : `{ai_pct:.1f}%`",
        f"📈 Edge (keunggulan)    : `+{edge_pct:.1f}%`",
        "",
        f"💵 *Modal yang disarankan:*",
        f"   → *{kelly_str}*",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🚦 *Cek odds Stake sebelum pasang:*",
        f"   Cari *{bet_team}* di Stake (market: Pemenang/Moneyline)",
        f"   ✅ PASANG jika odds Stake ≥ `{min_viable_odds:.2f}`",
        f"   ❌ SKIP jika odds Stake < `{min_viable_odds:.2f}` (tidak ada keunggulan)",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📌 *Cara pasang di Stake:*",
        f"   1. Buka → [Stake Sports]({stake_url})",
        f"   2. Cari pertandingan *{home_team} vs {away_team}*",
        f"   3. Pilih *{bet_team}* ({bet_label})",
        f"   4. Masukkan nominal *{kelly_str}* lalu konfirmasi",
        "",
        "⚠️ _Sinyal ini berdasarkan konsensus pasar. Selalu cek batas odds di atas._",
    ]
    return "\n".join(lines)


def _load_pending_signals() -> dict:
    """Load pending signals (belum dikonfirmasi via tombol) dari file. Caller harus pegang _pending_lock."""
    if not os.path.exists(PENDING_SIGNALS_FILE):
        return {}
    try:
        with open(PENDING_SIGNALS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_pending_signals(signals: dict) -> None:
    """Persist pending signals ke file. Caller harus pegang _pending_lock."""
    with open(PENDING_SIGNALS_FILE, "w", encoding="utf-8") as f:
        json.dump(signals, f, indent=2, ensure_ascii=False)


def _prune_expired_signals(signals: dict) -> dict:
    """
    Buang sinyal pending yang sudah kedaluwarsa (lebih tua dari
    PENDING_SIGNAL_MAX_AGE_HOURS) — mencegah pending_signals.json menumpuk
    sinyal lama yang tidak akan pernah dikonfirmasi user lagi. Sinyal tanpa
    `created_at` valid tetap dipertahankan.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=config.PENDING_SIGNAL_MAX_AGE_HOURS)
    kept: dict = {}
    for signal_id, data in signals.items():
        created_at = data.get("created_at", "")
        try:
            dt = datetime.fromisoformat(created_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            kept[signal_id] = data
            continue
        if dt >= cutoff:
            kept[signal_id] = data
    return kept


def _add_pending_signal(signal_id: str, data: dict) -> None:
    """Simpan satu sinyal yang sedang menunggu konfirmasi tombol."""
    with _pending_lock:
        signals = _load_pending_signals()
        signals[signal_id] = data
        signals = _prune_expired_signals(signals)
        _save_pending_signals(signals)


def get_pending_signal(signal_id: str) -> Optional[dict]:
    """Ambil detail sinyal berdasarkan signal_id (dipakai oleh listener callback)."""
    with _pending_lock:
        return _load_pending_signals().get(signal_id)


def remove_pending_signal(signal_id: str) -> None:
    """Hapus sinyal dari daftar pending setelah dikonfirmasi/dilewati."""
    with _pending_lock:
        signals = _load_pending_signals()
        if signal_id in signals:
            del signals[signal_id]
            _save_pending_signals(signals)


def send_value_bet_alert(
    home_team: str,
    away_team: str,
    outcome: str,
    odds: float,
    edge: float,
    kelly_stake: float,
    ai_prob: float,
    sport_key: str = "unknown",
    opp_type: str = "value_bet",
    platform: str = "market",
) -> bool:
    """
    Kirim notifikasi value bet ke Telegram, lengkap dengan tombol interaktif
    "✅ Sudah Pasang" dan "⏭️ Lewati" — tidak perlu ketik command apapun.

    Returns:
        True jika berhasil, False jika gagal atau Telegram belum dikonfigurasi.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.debug("Telegram belum dikonfigurasi — notifikasi dilewati.")
        return False

    message = _format_signal_message(
        home_team=home_team,
        away_team=away_team,
        outcome=outcome,
        odds=odds,
        edge=edge,
        kelly_stake=kelly_stake,
        ai_prob=ai_prob,
        sport_key=sport_key,
        opp_type=opp_type,
        platform=platform,
    )

    signal_id = uuid.uuid4().hex[:12]
    _add_pending_signal(
        signal_id,
        {
            "home_team": home_team,
            "away_team": away_team,
            "outcome": outcome,
            "odds": odds,
            "edge": edge,
            "kelly_stake": kelly_stake,
            "ai_prob": ai_prob,
            "sport_key": sport_key,
            "opp_type": opp_type,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    url = TELEGRAM_API_BASE.format(token=config.TELEGRAM_BOT_TOKEN, method="sendMessage")
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
        "reply_markup": {
            "inline_keyboard": [
                [
                    {"text": "✅ Sudah Pasang", "callback_data": f"placed:{signal_id}"},
                    {"text": "⏭️ Lewati", "callback_data": f"skip:{signal_id}"},
                ]
            ]
        },
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200 and resp.json().get("ok"):
            logger.info("✅ Telegram alert terkirim: %s vs %s → %s", home_team, away_team, outcome)
            return True
        else:
            logger.warning("Telegram gagal: %s — %s", resp.status_code, resp.text[:200])
            remove_pending_signal(signal_id)
            return False
    except requests.exceptions.RequestException as exc:
        logger.error("Telegram request error: %s", exc)
        remove_pending_signal(signal_id)
        return False


def send_daily_summary(
    total_signals: int,
    total_matches: int,
    sports_scanned: list,
    bankroll: float,
) -> bool:
    """
    Kirim ringkasan harian ke Telegram (dipanggil di awal siklus).

    Returns:
        True jika berhasil.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False

    sports_str = ", ".join(s.replace("baseball_", "").upper() for s in sports_scanned)

    lines = [
        "🤖 *Sportsbook Bot — Laporan Scan*",
        "",
        "📅 Mode: 🔴 LIVE (data riil, konfirmasi taruhan MANUAL)",
        f"🏟️ Olahraga: {sports_str}",
        f"🔍 Total pertandingan dipindai: *{total_matches}*",
        f"🎯 Sinyal value bet ditemukan: *{total_signals}*",
        f"💼 Bankroll saat ini: *Rp{bankroll:,.0f}*",
    ]

    if total_signals == 0:
        lines.append("")
        lines.append("_Tidak ada value bet hari ini. Bot akan scan lagi nanti._")

    message = "\n".join(lines)
    url = TELEGRAM_API_BASE.format(token=config.TELEGRAM_BOT_TOKEN, method="sendMessage")
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp.status_code == 200 and resp.json().get("ok", False)
    except requests.exceptions.RequestException:
        return False


def test_telegram_connection() -> bool:
    """
    Kirim pesan tes ke Telegram untuk memverifikasi koneksi.

    Returns:
        True jika bot token & chat_id valid.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.warning("TELEGRAM_BOT_TOKEN atau TELEGRAM_CHAT_ID belum diset.")
        return False

    url = TELEGRAM_API_BASE.format(token=config.TELEGRAM_BOT_TOKEN, method="sendMessage")
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": (
            "✅ *Sportsbook Bot terhubung!*\n\n"
            "Bot siap mengirim sinyal value bet ke HP kamu.\n"
            "Taruhan dilakukan MANUAL — bot hanya memberi sinyal."
        ),
        "parse_mode": "Markdown",
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        ok = resp.status_code == 200 and resp.json().get("ok", False)
        if ok:
            logger.info("✅ Telegram connection test berhasil.")
        else:
            logger.warning("❌ Telegram test gagal: %s", resp.text[:300])
        return ok
    except requests.exceptions.RequestException as exc:
        logger.error("Telegram test error: %s", exc)
        return False
