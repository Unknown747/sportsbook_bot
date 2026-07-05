"""
Telegram Notifier module untuk Sportsbook Prediction Bot.

Mengirim sinyal VALUE BET langsung ke HP via Telegram Bot API.
Pesan berisi: Tim, Prediksi, Odds Stake, Rekomendasi modal (Kelly), Link Stake.
"""

import logging
from typing import Optional

import requests

from sportsbook_bot import config

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"

SPORT_TO_STAKE_URL: dict = {
    "baseball_mlb":               "https://stake.com/sports/baseball/mlb",
    "baseball_kbo":               "https://stake.com/sports/baseball/kbo",
    "baseball_npb":               "https://stake.com/sports/baseball/npb",
    "basketball_wnba":            "https://stake.com/sports/basketball/wnba",
    "americanfootball_nfl_preseason": "https://stake.com/sports/american-football/nfl",
    "cricket_international_t20":  "https://stake.com/sports/cricket",
    "soccer_epl":                 "https://stake.com/sports/soccer/england/premier-league",
    "soccer_laliga":              "https://stake.com/sports/soccer/spain/laliga",
}

_OUTCOME_LABEL = {"Home": "🏠 Home (Tim Kandang)", "Away": "✈️ Away (Tim Tandang)", "Draw": "🤝 Seri"}
_OUTCOME_EMOJI = {"Home": "🟢", "Away": "🔵", "Draw": "🟡"}


def _build_stake_link(sport_key: str, home_team: str, away_team: str) -> str:
    """Return best-effort Stake.com link for the match sport page."""
    base = SPORT_TO_STAKE_URL.get(sport_key, "https://stake.com/sports")
    return base


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
) -> str:
    """Format a rich Telegram message for a value bet signal."""
    stake_url = _build_stake_link(sport_key, home_team, away_team)
    emoji = _OUTCOME_EMOJI.get(outcome, "⚡")
    label = _OUTCOME_LABEL.get(outcome, outcome)
    edge_pct = edge * 100
    ai_pct = ai_prob * 100
    implied_pct = (1 / odds) * 100

    tag = "🔥 VALUE BET" if opp_type == "value_bet" else "⚡ ARBITRAGE"
    kelly_str = f"Rp{kelly_stake:,.0f}"

    lines = [
        f"{tag} TERDETEKSI!",
        "",
        f"⚽ *{home_team}* vs *{away_team}*",
        "",
        f"{emoji} *Prediksi: {label}*",
        f"💰 Odds Stake  : `{odds:.2f}`",
        f"📊 Peluang AI  : `{ai_pct:.1f}%`",
        f"📉 Peluang Tersirat: `{implied_pct:.1f}%`",
        f"📈 Keunggulan (Edge): `+{edge_pct:.1f}%`",
        "",
        f"💵 *Rekomendasi Modal (Kelly):*",
        f"   → {kelly_str}",
        "",
        f"🔗 [Buka di Stake]({stake_url})",
        "",
        "⚠️ _Pasang MANUAL di Stake. Bot hanya memberi sinyal._",
    ]
    return "\n".join(lines)


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
) -> bool:
    """
    Kirim notifikasi value bet ke Telegram.

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
    )

    url = TELEGRAM_API_BASE.format(token=config.TELEGRAM_BOT_TOKEN, method="sendMessage")
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200 and resp.json().get("ok"):
            logger.info("✅ Telegram alert terkirim: %s vs %s → %s", home_team, away_team, outcome)
            return True
        else:
            logger.warning("Telegram gagal: %s — %s", resp.status_code, resp.text[:200])
            return False
    except requests.exceptions.RequestException as exc:
        logger.error("Telegram request error: %s", exc)
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
    mode = "🔴 LIVE" if not config.SIMULATION_MODE else "🟡 SIMULASI"

    lines = [
        "🤖 *Sportsbook Bot — Laporan Scan*",
        "",
        f"📅 Mode: {mode}",
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
