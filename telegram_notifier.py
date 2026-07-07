"""
Telegram Notifier module untuk Sportsbook Prediction Bot.

Mengirim sinyal VALUE BET langsung ke HP via Telegram Bot API.
Pesan berisi: Tim, Prediksi, Odds, Jam mulai match (WIB), Rekomendasi modal (Kelly),
Link Stake, dan tombol interaktif — TIDAK perlu ketik command apapun.
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

_WIB = timezone(timedelta(hours=7))

# ── Bot state (diupdate setelah setiap siklus scan) ────────────────────────────
# Dipakai oleh tombol 📊 Status untuk menampilkan info terkini.
_bot_state: dict = {
    "last_scan_wib":    None,   # str "07 Jul 20:00"
    "next_scan_hour":   None,   # int jam WIB, mis. 8
    "signals_today":    0,
    "matches_today":    0,
    "scan_count_today": 0,
    "bankroll":         0.0,
    "key_slots":        0,
}
_signals_today_total: int = 0   # akumulasi lintas siklus, reset tiap hari WIB
_signals_today_date: Optional[str] = None


def update_bot_state(
    last_scan_wib: str,
    next_scan_hour: Optional[int],
    signals_this_cycle: int,
    matches_today: int,
    bankroll: float = 0.0,
    key_slots: int = 0,
) -> None:
    """Dipanggil setelah setiap siklus scan selesai untuk update status bot."""
    global _signals_today_total, _signals_today_date
    today_str = datetime.now(_WIB).strftime("%Y-%m-%d")
    if _signals_today_date != today_str:
        _signals_today_total = 0
        _signals_today_date = today_str
    _signals_today_total += signals_this_cycle
    _bot_state.update({
        "last_scan_wib":    last_scan_wib,
        "next_scan_hour":   next_scan_hour,
        "signals_today":    _signals_today_total,
        "matches_today":    matches_today,
        "bankroll":         bankroll,
        "key_slots":        key_slots,
    })


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
    "american-football":  "🏈",
    "americanfootball":   "🏈",
    "cricket":            "🏏",
}


def _sport_emoji(sport_key: str) -> str:
    """Kembalikan emoji yang tepat untuk cabang olahraga."""
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


def _format_commence_time(commence_time_iso: str) -> str:
    """
    Ubah ISO 8601 UTC string dari OddsAPI menjadi label WIB yang mudah dibaca.
    Contoh: "2026-07-08T13:00:00Z" → "08 Jul 20:00 WIB"
    Kembalikan string kosong jika parse gagal.
    """
    if not commence_time_iso:
        return ""
    try:
        dt = datetime.fromisoformat(commence_time_iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_wib = dt.astimezone(_WIB)
        return dt_wib.strftime("%d %b %H:%M WIB")
    except (ValueError, TypeError):
        return ""


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
    commence_time_iso: str = "",
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

    if outcome == "Home":
        bet_team = home_team
        bet_label = "🏠 KANDANG"
    elif outcome == "Away":
        bet_team = away_team
        bet_label = "✈️ TANDANG"
    else:
        bet_team = "SERI"
        bet_label = "🤝 SERI"

    min_viable_odds = 1.0 / ai_prob if ai_prob > 0 else 0.0

    match_time_str = _format_commence_time(commence_time_iso)

    lines = [
        f"{tag} TERDETEKSI!",
        "",
        f"{s_emoji} *{home_team}* vs *{away_team}*",
    ]
    if match_time_str:
        lines.append(f"⏰ *Match mulai: {match_time_str}*")
    lines += [
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


# ── Pending signals helpers ────────────────────────────────────────────────────

def _load_pending_signals() -> dict:
    """Load pending signals dari file. Caller harus pegang _pending_lock."""
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
    PENDING_SIGNAL_MAX_AGE_HOURS). Sinyal tanpa `created_at` valid dipertahankan.
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


def mark_reminder_sent(signal_id: str) -> None:
    """Tandai sinyal sebagai sudah dikirim pengingatnya — tidak akan dikirimi ulang."""
    with _pending_lock:
        signals = _load_pending_signals()
        if signal_id in signals:
            signals[signal_id]["reminder_sent"] = True
            _save_pending_signals(signals)


# ── Public senders ─────────────────────────────────────────────────────────────

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
    commence_time_iso: str = "",
) -> bool:
    """
    Kirim notifikasi value bet ke Telegram, lengkap dengan jam mulai match (WIB)
    dan tombol interaktif "✅ Sudah Pasang" / "⏭️ Lewati".

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
        commence_time_iso=commence_time_iso,
    )

    signal_id = uuid.uuid4().hex[:12]
    _add_pending_signal(
        signal_id,
        {
            "home_team":       home_team,
            "away_team":       away_team,
            "outcome":         outcome,
            "odds":            odds,
            "edge":            edge,
            "kelly_stake":     kelly_stake,
            "ai_prob":         ai_prob,
            "sport_key":       sport_key,
            "opp_type":        opp_type,
            "commence_time":   commence_time_iso,
            "reminder_sent":   False,
            "created_at":      datetime.now(timezone.utc).isoformat(),
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


def send_reminder_alert(
    signal_id: str,
    home_team: str,
    away_team: str,
    outcome: str,
    odds: float,
    commence_time_iso: str,
    kelly_stake: float,
    minutes_until: int,
    sport_key: str = "",
) -> bool:
    """
    Kirim pengingat otomatis sebelum match dimulai untuk sinyal yang sudah dikirim
    sebelumnya tetapi belum dikonfirmasi user.

    Args:
        minutes_until: Berapa menit lagi match dimulai.

    Returns:
        True jika berhasil dikirim.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False

    match_time_str = _format_commence_time(commence_time_iso)
    s_emoji = _sport_emoji(sport_key)

    if outcome == "Home":
        bet_team = home_team
        bet_label = "🏠 KANDANG"
    elif outcome == "Away":
        bet_team = away_team
        bet_label = "✈️ TANDANG"
    else:
        bet_team = "SERI"
        bet_label = "🤝 SERI"

    stake_url = _build_stake_link(sport_key)
    kelly_str = f"Rp{kelly_stake:,.0f}"

    if minutes_until >= 60:
        waktu_str = f"~{minutes_until // 60} jam {minutes_until % 60} menit lagi"
    else:
        waktu_str = f"~{minutes_until} menit lagi"

    lines = [
        f"⏰ *PENGINGAT — Match Segera Mulai!*",
        "",
        f"{s_emoji} *{home_team}* vs *{away_team}*",
        f"🕐 Mulai: *{match_time_str}* ({waktu_str})",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🎯 Pasang: *{bet_team}* ({bet_label})",
        f"💰 Odds referensi: `{odds:.2f}` | Modal: *{kelly_str}*",
        "",
        f"👉 [Buka Stake Sekarang]({stake_url}) dan pasang sebelum match mulai!",
        "",
        "_Tekan tombol di bawah setelah taruhan dipasang:_",
    ]
    message = "\n".join(lines)

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
        ok = resp.status_code == 200 and resp.json().get("ok", False)
        if ok:
            logger.info(
                "⏰ Reminder terkirim: %s vs %s (%d menit lagi)",
                home_team, away_team, minutes_until,
            )
        return ok
    except requests.exceptions.RequestException as exc:
        logger.error("Telegram reminder error: %s", exc)
        return False


def check_and_send_reminders(hours_before: int = 3) -> int:
    """
    Cek semua sinyal pending — kirim pengingat untuk match yang akan mulai
    dalam `hours_before` jam ke depan. Setiap sinyal hanya dikirimi 1× reminder.

    Returns:
        Jumlah reminder yang berhasil dikirim dalam pemanggilan ini.
    """
    with _pending_lock:
        signals = dict(_load_pending_signals())  # shallow copy sebelum lepas lock

    if not signals:
        return 0

    now_utc = datetime.now(timezone.utc)
    threshold_secs = hours_before * 3600
    sent_count = 0

    for signal_id, signal in signals.items():
        if signal.get("reminder_sent"):
            continue
        commence_iso = signal.get("commence_time", "")
        if not commence_iso:
            continue
        try:
            commence_dt = datetime.fromisoformat(commence_iso.replace("Z", "+00:00"))
            if commence_dt.tzinfo is None:
                commence_dt = commence_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue

        time_left = (commence_dt - now_utc).total_seconds()
        if 0 < time_left <= threshold_secs:
            minutes_until = int(time_left / 60)
            ok = send_reminder_alert(
                signal_id=signal_id,
                home_team=signal.get("home_team", "?"),
                away_team=signal.get("away_team", "?"),
                outcome=signal.get("outcome", "?"),
                odds=float(signal.get("odds", 0.0)),
                commence_time_iso=commence_iso,
                kelly_stake=float(signal.get("kelly_stake", 0.0)),
                minutes_until=minutes_until,
                sport_key=signal.get("sport_key", ""),
            )
            if ok:
                mark_reminder_sent(signal_id)
                sent_count += 1

    return sent_count


# ── Summary & status ───────────────────────────────────────────────────────────

_SPORT_DISPLAY: dict = {
    "soccer":            "⚽ Soccer",
    "baseball":          "⚾ Baseball",
    "baseball_kbo":      "⚾ KBO",
    "baseball_npb":      "⚾ NPB",
    "basketball":        "🏀 Basketball",
    "american-football": "🏈 Am. Football",
    "cricket":           "🏏 Cricket",
}


def send_daily_summary(
    total_signals: int,
    total_matches: int,
    sport_counts: dict,
    sport_signals: dict,
    sport_skipped_no_consensus: dict,
    sport_skipped_no_value: dict,
    bankroll: float,
) -> bool:
    """
    Kirim ringkasan siklus scan ke Telegram dengan breakdown per-sport.

    Returns:
        True jika berhasil.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False

    now_wib = datetime.now(_WIB)
    now_str = now_wib.strftime("%H:%M WIB")

    lines = [
        "🤖 *Sportsbook Bot — Laporan Scan*",
        f"🕐 Waktu: {now_str}  |  Mode: 🔴 LIVE",
        "",
        f"🔍 *Total pertandingan dipindai: {total_matches}*",
        f"🎯 *Sinyal value bet ditemukan: {total_signals}*",
        f"💼 Bankroll: Rp{bankroll:,.0f}",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "*Detail per olahraga:*",
    ]

    for sport, count in sport_counts.items():
        label = _SPORT_DISPLAY.get(sport, sport.upper())
        sigs = sport_signals.get(sport, 0)
        no_cons = sport_skipped_no_consensus.get(sport, 0)
        no_val = sport_skipped_no_value.get(sport, 0)

        if count == 0:
            lines.append(f"  {label}: _0 pertandingan_ (off-season / no data)")
        elif sigs > 0:
            lines.append(f"  {label}: *{count} match → {sigs} SINYAL* ✅")
        else:
            reasons = []
            if no_cons:
                reasons.append(f"{no_cons} kurang bookmaker")
            if no_val:
                reasons.append(f"{no_val} edge < {config.MIN_VALUE_EDGE*100:.0f}%")
            reason_str = ", ".join(reasons) if reasons else "diproses"
            lines.append(f"  {label}: {count} match, 0 sinyal ({reason_str})")

    if total_signals == 0:
        lines.append("")
        lines.append("_Tidak ada value bet siklus ini. Bot scan lagi nanti._")

    message = "\n".join(lines)
    url = TELEGRAM_API_BASE.format(token=config.TELEGRAM_BOT_TOKEN, method="sendMessage")
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "🔄 Scan Sekarang", "callback_data": "scan_now"},
                {"text": "📊 Status", "callback_data": "status"},
            ]]
        },
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp.status_code == 200 and resp.json().get("ok", False)
    except requests.exceptions.RequestException:
        return False


def send_status_message() -> bool:
    """
    Kirim pesan status bot ke Telegram saat user tekan tombol 📊 Status.
    Menampilkan: waktu scan terakhir, scan berikutnya, sinyal hari ini,
    match dipindai, bankroll, dan jumlah OddsAPI key aktif.

    Returns:
        True jika berhasil dikirim.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False

    now_wib = datetime.now(_WIB).strftime("%d %b %Y, %H:%M WIB")
    last_scan = _bot_state.get("last_scan_wib") or "—"
    next_h = _bot_state.get("next_scan_hour")
    next_scan_str = f"Jam {next_h:02d}:00 WIB" if next_h is not None else "—"
    signals_today = _bot_state.get("signals_today", 0)
    matches_today = _bot_state.get("matches_today", 0)
    bankroll = _bot_state.get("bankroll", 0.0)
    key_slots = _bot_state.get("key_slots", 0)

    # Hitung berapa sinyal pending (belum dikonfirmasi user)
    with _pending_lock:
        pending = _load_pending_signals()
    pending_count = sum(1 for s in pending.values() if not s.get("reminder_sent") is True or True)
    # Sederhananya: semua yang ada di pending (termasuk yang sudah di-remind)
    pending_count = len(pending)

    lines = [
        "📊 *Status Bot Saat Ini*",
        f"🕐 Waktu sekarang : {now_wib}",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🔁 Scan terakhir   : {last_scan}",
        f"⏰ Scan berikutnya : {next_scan_str}",
        f"📅 Jadwal scan     : {', '.join(f'{h:02d}:00' for h in config.SCHEDULED_HOURS)} WIB",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🎯 Sinyal hari ini : {signals_today} sinyal",
        f"🔍 Match dipindai  : {matches_today} pertandingan",
        f"⏳ Sinyal pending  : {pending_count} belum dikonfirmasi",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"💼 Bankroll        : Rp{bankroll:,.0f}",
        f"🔑 OddsAPI keys    : {key_slots} slot dimuat",
        "",
        f"⏰ _Pengingat otomatis {config.MATCH_REMINDER_HOURS_BEFORE} jam sebelum match mulai._",
    ]
    message = "\n".join(lines)
    url = TELEGRAM_API_BASE.format(token=config.TELEGRAM_BOT_TOKEN, method="sendMessage")
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "🔄 Scan Sekarang", "callback_data": "scan_now"},
                {"text": "📊 Status", "callback_data": "status"},
            ]]
        },
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp.status_code == 200 and resp.json().get("ok", False)
    except requests.exceptions.RequestException as exc:
        logger.error("Telegram status error: %s", exc)
        return False


def test_telegram_connection() -> bool:
    """
    Kirim pesan tes ke Telegram saat bot restart/online.

    Returns:
        True jika bot token & chat_id valid.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.warning("TELEGRAM_BOT_TOKEN atau TELEGRAM_CHAT_ID belum diset.")
        return False

    now_wib = datetime.now(_WIB)
    now_str = now_wib.strftime("%d %b %Y, %H:%M WIB")
    next_hours = [h for h in config.SCHEDULED_HOURS if h > now_wib.hour]
    next_h = next_hours[0] if next_hours else config.SCHEDULED_HOURS[0]

    url = TELEGRAM_API_BASE.format(token=config.TELEGRAM_BOT_TOKEN, method="sendMessage")
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": (
            "🔁 *Sportsbook Bot — Restart / Online*\n\n"
            f"🕐 Waktu: {now_str}\n"
            f"⏰ Scan berikutnya: jam *{next_h:02d}:00 WIB*\n"
            f"🔔 Pengingat otomatis *{config.MATCH_REMINDER_HOURS_BEFORE} jam* sebelum match mulai.\n\n"
            "✅ Bot aktif dan siap mengirim sinyal value bet.\n"
            "Taruhan dilakukan *MANUAL* — bot hanya memberi sinyal.\n\n"
            "Tekan tombol di bawah:"
        ),
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "🔄 Scan Sekarang", "callback_data": "scan_now"},
                {"text": "📊 Status", "callback_data": "status"},
            ]]
        },
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
