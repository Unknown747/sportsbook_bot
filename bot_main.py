"""
Main orchestrator untuk Sportsbook Prediction Bot.

Workflow:
  1. Scan jadwal & odds RIIL dari OddsAPI (tanpa fallback data buatan).
  2. Hitung peluang fair dari konsensus multi-bookmaker riil & deteksi value bet
     terhadap odds Stake.
  3. Kirim sinyal ke Telegram — taruhan dipasang MANUAL oleh user dan
     dikonfirmasi lewat tombol (Stake tidak mendukung eksekusi otomatis).
"""

import json
import logging
import os
import threading
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Tuple

# WIB = UTC+7 — konsisten dengan jam scan terjadwal.
_WIB = timezone(timedelta(hours=7))

# Event yang di-set oleh Telegram listener saat user klik tombol "🔄 Scan Sekarang".
# Main loop menunggu event ini (bukan time.sleep) supaya scan mulai dalam hitungan detik.
_manual_scan_event = threading.Event()

# ── Deduplication: sinyal yang sama tidak dikirim 2x dalam 1 hari ─────────────
_sent_signals_today: set = set()
_sent_signals_date: Optional[date] = None


def _is_signal_already_sent(match_id: str, outcome: str, today: date) -> bool:
    """
    Kembalikan True jika sinyal match_id+outcome sudah dikirim hari ini (WIB).
    Sekaligus mendaftarkan sinyal baru ke set supaya pengecekan berikutnya tahu.
    Set di-reset otomatis saat hari berganti.
    """
    global _sent_signals_today, _sent_signals_date
    if _sent_signals_date != today:
        _sent_signals_today = set()
        _sent_signals_date = today
    key = (match_id, outcome)
    if key in _sent_signals_today:
        return True
    _sent_signals_today.add(key)
    return False

import bot_config as config
from arbitrage_finder import scan_opportunities
from bet_sizer import calculate_idr_stake
from executor import get_today_confirmed_stake_total
from fetcher import StakeFetcher
from predictor import get_market_consensus_prediction
from telegram_listener import start_listener
from telegram_notifier import (
    send_daily_summary,
    send_value_bet_alert,
    test_telegram_connection,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bot_main")

SPORTS_TO_SCAN: List[str] = [
    "soccer",
    "baseball",
    "basketball",
    "american-football",
    "cricket",
    "baseball_kbo",
    "baseball_npb",
]


# ── Data fetching ─────────────────────────────────────────────────────────────

def _sport_category(sport_key: str) -> str:
    """Petakan OddsAPI sport key spesifik ke kategori umum di SPORTS_TO_SCAN."""
    if sport_key.startswith("soccer"):
        return "soccer"
    if sport_key.startswith("basketball"):
        return "basketball"
    if sport_key.startswith("americanfootball"):
        return "american-football"
    if sport_key.startswith("cricket"):
        return "cricket"
    if sport_key.startswith("baseball"):
        return "baseball"
    return sport_key


def _fetch_all_matches(fetcher: StakeFetcher) -> tuple:
    """
    Ambil pertandingan live/upcoming dari semua cabang olahraga.

    TIDAK ADA fallback data buatan — kalau tidak ada data riil dari OddsAPI
    (misal kuota habis atau tidak ada pertandingan aktif), siklus akan
    dilewati sepenuhnya. Bot live tidak boleh mengirim sinyal berdasarkan
    data palsu.

    Returns:
        (all_matches, sport_counts) — sport_counts = {sport_slug: jumlah_match}
    """
    all_matches: list = []
    sport_counts: dict = {}
    for sport in SPORTS_TO_SCAN:
        try:
            matches = fetcher.fetch_live_matches(sport=sport, limit=20)
            sport_counts[sport] = len(matches)
            if matches:
                logger.info("  ✅ %s: %d pertandingan ditemukan.", sport, len(matches))
            else:
                logger.info("  ⬜ %s: 0 pertandingan (tidak ada data / off-season).", sport)
            all_matches.extend(matches)
        except Exception as exc:
            logger.error("Error fetching '%s' matches: %s", sport, exc)
            sport_counts[sport] = 0

    if not all_matches:
        logger.warning(
            "Tidak ada data live/riil dari OddsAPI untuk sport manapun — "
            "siklus dilewati (tidak ada sinyal palsu yang dikirim)."
        )
        return [], sport_counts

    logger.info("Total %d pertandingan live/upcoming dari semua sport.", len(all_matches))
    return all_matches, sport_counts


# ── Scheduler helper ──────────────────────────────────────────────────────────

SCAN_STATE_FILE = "scan_state.json"


def _load_scan_state() -> dict:
    """
    Baca state scan terakhir (tanggal WIB + jam-jam yang sudah dieksekusi hari
    itu) dari disk. Dipakai untuk catch-up scan setelah restart, supaya kalau
    bot mati/restart tepat sesudah jam terjadwal lewat, scan hari itu tidak
    diam-diam terlewat sampai jadwal berikutnya.
    """
    if not os.path.exists(SCAN_STATE_FILE):
        return {"date": None, "done_hours": []}
    try:
        with open(SCAN_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"date": None, "done_hours": []}


def _save_scan_state(state: dict) -> None:
    """Persist state scan ke disk. Kegagalan tulis di-log tapi tidak menghentikan bot."""
    try:
        with open(SCAN_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except OSError as exc:
        logger.error("Gagal menyimpan scan_state.json: %s", exc)


def _get_due_scheduled_hour(state: dict) -> Optional[int]:
    """
    Tentukan apakah ada jam terjadwal hari ini (WIB) yang SUDAH LEWAT tapi
    BELUM dieksekusi (misal karena bot baru restart). Reset daftar
    `done_hours` otomatis kalau tanggal WIB sudah berganti hari.

    Returns:
        Jam (int) yang perlu di-catch-up-scan sekarang, atau None kalau tidak
        ada yang tertunda.
    """
    now_wib = datetime.now(_WIB)
    today_str = now_wib.date().isoformat()

    if state.get("date") != today_str:
        state["date"] = today_str
        state["done_hours"] = []

    done_hours = set(state.get("done_hours", []))
    due_hours = [h for h in config.SCHEDULED_HOURS if h <= now_wib.hour and h not in done_hours]
    if not due_hours:
        return None
    return max(due_hours)


# ── Core betting cycle ────────────────────────────────────────────────────────

def run_betting_cycle(
    fetcher: StakeFetcher,
    current_bankroll: float,
    last_reset_date: date,
) -> Tuple[float, date]:
    """
    Jalankan satu siklus analisis lengkap:
      1. Fetch pertandingan (data odds riil dari OddsAPI — tidak ada fallback palsu).
      2. Hitung peluang fair dari konsensus multi-bookmaker riil & deteksi value bet.
      3. Kirim sinyal Telegram (taruhan dipasang MANUAL oleh user, lalu dikonfirmasi
         lewat tombol — Stake tidak mendukung eksekusi taruhan otomatis via API).

    Batas risiko harian (MAX_DAILY_DRAWDOWN) dihitung ULANG dari bet_history.json
    setiap siklus (total stake CONFIRMED_MANUAL hari ini) — bukan dari counter
    di memori — supaya tetap akurat walau taruhan dikonfirmasi via tombol
    Telegram oleh thread listener yang terpisah dari loop utama ini.

    Returns:
        (updated_bankroll, updated_last_reset_date)
    """
    today = datetime.now(_WIB).date()   # WIB — konsisten dengan jam scan & drawdown tracking
    if today != last_reset_date:
        logger.info("Hari baru — daily loss counter otomatis mulai dari 0 lagi.")
        last_reset_date = today

    daily_loss = get_today_confirmed_stake_total(today)
    logger.info("Total stake terkonfirmasi hari ini: Rp%.0f", daily_loss)

    matches, sport_counts = _fetch_all_matches(fetcher)
    logger.info("Memproses %d pertandingan.", len(matches))

    signals_sent: int = 0
    # Per-sport: jumlah sinyal yang berhasil dikirim
    sport_signals: dict = {s: 0 for s in SPORTS_TO_SCAN}
    # Per-sport: alasan match dilewati (untuk diagnostik di log & Telegram)
    sport_skipped_no_consensus: dict = {s: 0 for s in SPORTS_TO_SCAN}
    sport_skipped_no_value: dict = {s: 0 for s in SPORTS_TO_SCAN}

    for match in matches:
        match_id = match["match_id"]
        label = f"{match.get('home_team', '?')} vs {match.get('away_team', '?')}"
        sport_key: str = match.get("sport", "unknown")
        sport_cat: str = _sport_category(sport_key)

        try:
            fair_probs = get_market_consensus_prediction(match)

            if fair_probs is None:
                logger.info(
                    "[%s] %s | Data odds riil tidak cukup (<2 bookmaker) — dilewati.",
                    match_id, label,
                )
                sport_skipped_no_consensus[sport_cat] = sport_skipped_no_consensus.get(sport_cat, 0) + 1
                continue

            is_3way: bool = match.get("is_3way", False)
            if is_3way:
                logger.info(
                    "[%s] %s | Fair (konsensus pasar): H=%.3f D=%.3f A=%.3f",
                    match_id, label,
                    fair_probs.get("Home", 0.0), fair_probs.get("Draw", 0.0), fair_probs.get("Away", 0.0),
                )
            else:
                logger.info(
                    "[%s] %s | Fair (konsensus pasar): H=%.3f A=%.3f (2-way)",
                    match_id, label, fair_probs.get("Home", 0.0), fair_probs.get("Away", 0.0),
                )

            opportunities = scan_opportunities(
                match["best_odds"],
                fair_probs,
                best_bookmaker=match.get("best_bookmaker"),
            )

            if not opportunities:
                # Log edge terbesar yang tersedia meski di bawah threshold (diagnostik)
                best_edges = []
                for outcome_k, o_odds in match["best_odds"].items():
                    fp = fair_probs.get(outcome_k)
                    if fp and o_odds > 1.0:
                        best_edges.append(fp - (1.0 / o_odds))
                max_edge = max(best_edges) if best_edges else 0.0
                logger.info(
                    "[%s] %s | Tidak ada value bet (edge terbaik=%.2f%% < threshold %.0f%%).",
                    match_id, label, max_edge * 100, config.MIN_VALUE_EDGE * 100,
                )
                sport_skipped_no_value[sport_cat] = sport_skipped_no_value.get(sport_cat, 0) + 1
                continue

            for opp in opportunities:
                outcome: str = opp["outcome"]
                odds: float = opp["best_odds"]
                edge: float = opp["edge"]
                opp_type: str = opp["type"]
                ai_prob: float = opp["ai_prob"]
                platform: str = opp["platform"]

                logger.info(
                    "[%s] %s | %s @ %.2f | edge=%.4f | ref=%s (cek Stake manual)",
                    match_id, opp_type.upper(), outcome, odds, edge, platform,
                )

                stake = calculate_idr_stake(
                    odds=odds,
                    prob=ai_prob,
                    current_bankroll=current_bankroll,
                    current_daily_loss=daily_loss,
                )

                if stake == 0.0:
                    logger.info("[%s] Stake=0 (Kelly terlalu kecil / drawdown limit).", match_id)
                    continue

                logger.info(
                    "[%s] 🎯 SINYAL: %s | %s @ %.2f | Kelly=Rp%.0f",
                    match_id, label, outcome, odds, stake,
                )

                # ── Deduplication: skip jika sinyal ini sudah dikirim hari ini ──
                if _is_signal_already_sent(match_id, outcome, today):
                    logger.info(
                        "[%s] %s %s — sudah dikirim di siklus sebelumnya hari ini, skip.",
                        match_id, outcome, label,
                    )
                    continue

                # ── Kirim Telegram alert (taruhan dipasang MANUAL oleh user) ──
                if float(edge) >= config.TELEGRAM_MIN_EDGE:
                    sent = send_value_bet_alert(
                        home_team=match["home_team"],
                        away_team=match["away_team"],
                        outcome=outcome,
                        odds=odds,
                        edge=float(edge),
                        kelly_stake=stake,
                        ai_prob=ai_prob,
                        sport_key=sport_key,
                        opp_type=opp_type,
                        platform=platform,
                    )
                    if sent:
                        signals_sent += 1
                        sport_signals[sport_cat] = sport_signals.get(sport_cat, 0) + 1

        except Exception as exc:
            logger.error("[%s] Error: %s", match_id, exc, exc_info=True)

    # Kirim ringkasan siklus ke Telegram — breakdown per sport supaya user
    # tahu persis kenapa soccer (atau sport lain) tidak menghasilkan sinyal.
    send_daily_summary(
        total_signals=signals_sent,
        total_matches=len(matches),
        sport_counts=sport_counts,
        sport_signals=sport_signals,
        sport_skipped_no_consensus=sport_skipped_no_consensus,
        sport_skipped_no_value=sport_skipped_no_value,
        bankroll=current_bankroll,
    )

    return current_bankroll, last_reset_date


# ── Config validation ─────────────────────────────────────────────────────────

def _validate_config() -> None:
    """Validasi konfigurasi penting sebelum bot start."""
    if not config.ODDS_API_KEY:
        raise EnvironmentError(
            "ODDS_API_KEY tidak ditemukan. Bot butuh data odds riil untuk berjalan "
            "live — set secret ODDS_API_KEY sebelum menjalankan bot."
        )

    logger.info("Mode sinyal: LIVE (data 100%% dari OddsAPI, tidak ada data buatan).")

    if config.STAKE_API_KEY:
        logger.info("Stake API key: ****%s", config.STAKE_API_KEY[-4:])

    logger.warning(
        "Eksekusi taruhan otomatis ke Stake TIDAK didukung (API Stake tidak "
        "menyediakan market/odds ID sportsbook riil, dan situsnya diblok "
        "Cloudflare untuk scraping). Semua taruhan harus dipasang MANUAL "
        "oleh user setelah menerima sinyal Telegram."
    )

    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        logger.info("Telegram: aktif (chat_id=%s...)", str(config.TELEGRAM_CHAT_ID)[:6])
    else:
        logger.warning(
            "Telegram belum dikonfigurasi — set TELEGRAM_BOT_TOKEN & TELEGRAM_CHAT_ID "
            "di Secrets untuk menerima sinyal di HP."
        )

    if config.MIN_BET_IDR >= config.MAX_BET_IDR:
        raise ValueError("MIN_BET_IDR harus lebih kecil dari MAX_BET_IDR.")
    if not (0 < config.KELLY_MULTIPLIER <= 1):
        raise ValueError("KELLY_MULTIPLIER harus antara 0 dan 1.")
    if not (0 < config.MAX_DAILY_DRAWDOWN <= 1):
        raise ValueError("MAX_DAILY_DRAWDOWN harus antara 0 dan 1.")
    if not config.SCHEDULED_HOURS:
        raise ValueError("SCHEDULED_HOURS tidak boleh kosong — harus ada minimal 1 jam scan.")


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    """
    Entry point — loop dengan jadwal scan terjadwal (default 08:00 / 14:00 / 20:00 WIB).
    Setiap 10 menit cek apakah sudah waktunya scan. Jika ya, jalankan siklus.
    """
    logger.info("=" * 60)
    logger.info("Sportsbook Prediction Bot — START")
    logger.info("Sports dipantau : %s", ", ".join(SPORTS_TO_SCAN))
    logger.info("Jam scan (WIB)  : %s", config.SCHEDULED_HOURS)
    logger.info("Bankroll awal   : Rp%.0f", config.INITIAL_BANKROLL)
    logger.info("=" * 60)

    _validate_config()

    # Kirim notifikasi restart ke Telegram dan mulai listener tombol
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        test_telegram_connection()
        start_listener(scan_event=_manual_scan_event)

    fetcher = StakeFetcher()
    current_bankroll: float = config.INITIAL_BANKROLL
    last_reset_date: date = datetime.now(_WIB).date()
    scan_state = _load_scan_state()

    while True:
        now_wib_hour = datetime.now(_WIB).hour
        due_hour = _get_due_scheduled_hour(scan_state)

        # Cek apakah ada tombol "Scan Sekarang" yang ditekan dari Telegram
        manual_trigger = _manual_scan_event.is_set()
        if manual_trigger:
            _manual_scan_event.clear()

        run_scan = False
        scan_label = ""

        if due_hour is not None:
            if due_hour != now_wib_hour:
                logger.warning(
                    "Catch-up scan: jam %02d:00 WIB terlewat (kemungkinan bot baru "
                    "restart) — menjalankan siklus sekarang.",
                    due_hour,
                )
            logger.info("=== JAM SCAN (WIB %02d:00) — memulai siklus ===", due_hour)
            scan_state.setdefault("done_hours", []).append(due_hour)
            _save_scan_state(scan_state)
            scan_label = f"WIB {due_hour:02d}:00"
            run_scan = True
        elif manual_trigger:
            logger.info("=== SCAN MANUAL — dipicu dari tombol Telegram ===")
            scan_label = "MANUAL"
            run_scan = True
        else:
            next_hours = [h for h in config.SCHEDULED_HOURS if h > now_wib_hour]
            next_h = next_hours[0] if next_hours else config.SCHEDULED_HOURS[0]
            logger.info(
                "Jam sekarang WIB: %02d:xx. Scan berikutnya jam %02d:00.",
                now_wib_hour, next_h,
            )

        if run_scan:
            try:
                current_bankroll, last_reset_date = run_betting_cycle(
                    fetcher=fetcher,
                    current_bankroll=current_bankroll,
                    last_reset_date=last_reset_date,
                )
            except Exception as exc:
                logger.critical("Error kritis di siklus utama: %s", exc, exc_info=True)
            logger.info("Siklus [%s] selesai. Bankroll=Rp%.0f", scan_label, current_bankroll)

        # Tunggu hingga LOOP_INTERVAL_SECONDS atau sampai tombol Scan ditekan —
        # event.wait() lebih responsif dari time.sleep() karena bisa bangun lebih awal.
        _manual_scan_event.wait(timeout=config.LOOP_INTERVAL_SECONDS)
        _manual_scan_event.clear()


if __name__ == "__main__":
    main()
