"""
Telegram Listener module untuk Sportsbook Prediction Bot.

Berjalan sebagai background thread yang polling Telegram getUpdates untuk
menangani klik tombol inline ("✅ Sudah Pasang" / "⏭️ Lewati") — TIDAK
memerlukan user mengetik command apapun.

Saat tombol "Sudah Pasang" ditekan:
  1. Sinyal dicatat ke bet_history.json dengan status CONFIRMED_MANUAL.
  2. Pesan asli di Telegram diedit untuk menampilkan status terbaru.
  3. Sinyal dihapus dari daftar pending.
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

# WIB = UTC+7 — timestamp konfirmasi taruhan harus konsisten dengan tanggal WIB.
_WIB = timezone(timedelta(hours=7))
from typing import Optional

import requests

import bot_config as config
from executor import append_bet_record
from telegram_notifier import get_pending_signal, remove_pending_signal

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT_SECONDS = 25

# Backoff saat getUpdates gagal terus-menerus (mis. internet mati lama) —
# supaya tidak spam retry tiap 5 detik tanpa batas selama-lamanya.
_BACKOFF_START_SECONDS = 5
_BACKOFF_MAX_SECONDS = 60


def _api_call(method: str, payload: dict) -> Optional[dict]:
    """Helper untuk memanggil Telegram Bot API dan mengembalikan JSON response."""
    url = TELEGRAM_API_BASE.format(token=config.TELEGRAM_BOT_TOKEN, method=method)
    try:
        resp = requests.post(url, json=payload, timeout=POLL_TIMEOUT_SECONDS + 10)
        try:
            return resp.json()
        except ValueError as exc:
            # Body bukan JSON valid (misal HTML error page dari Telegram CDN)
            logger.error("Telegram API (%s): response bukan JSON — %s", method, exc)
            return None
    except requests.exceptions.RequestException as exc:
        logger.error("Telegram API error (%s): %s", method, exc)
        return None


def _answer_callback(callback_query_id: str, text: str) -> None:
    """Jawab callback query supaya tombol Telegram berhenti loading."""
    _api_call("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})


def _edit_message(chat_id, message_id: int, new_text: str) -> None:
    """Edit pesan asli (hapus tombol inline, tampilkan status baru)."""
    _api_call(
        "editMessageText",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": new_text,
            "parse_mode": "Markdown",
            "reply_markup": {"inline_keyboard": []},  # hapus tombol eksplisit
        },
    )


def _handle_placed(signal_id: str, chat_id, message_id: int, original_text: str) -> None:
    """Tandai sinyal sebagai sudah dipasang manual oleh user, catat ke bet_history.json."""
    signal = get_pending_signal(signal_id)
    if signal is None:
        _edit_message(chat_id, message_id, original_text + "\n\n_(Sinyal ini sudah tidak berlaku)_")
        return

    timestamp = datetime.now(_WIB).isoformat()
    record = {
        "timestamp": timestamp,
        "status": "CONFIRMED_MANUAL",
        "simulation": False,
        "currency": config.STAKE_CURRENCY,
        "stake_idr": signal.get("kelly_stake"),
        "match_info": {
            "home_team": signal.get("home_team"),
            "away_team": signal.get("away_team"),
            "outcome": signal.get("outcome"),
            "odds": signal.get("odds"),
            "edge": signal.get("edge"),
            "type": signal.get("opp_type"),
            "sport": signal.get("sport_key"),
        },
        "note": "Dikonfirmasi manual oleh user via tombol Telegram.",
    }
    append_bet_record(record)
    remove_pending_signal(signal_id)

    new_text = (
        original_text
        + f"\n\n✅ *Ditandai SUDAH DIPASANG* pada {timestamp[11:16]} WIB."
    )
    _edit_message(chat_id, message_id, new_text)
    logger.info("Sinyal %s dikonfirmasi sudah dipasang oleh user.", signal_id)


def _handle_skip(signal_id: str, chat_id, message_id: int, original_text: str) -> None:
    """Tandai sinyal dilewati (tidak jadi dipasang)."""
    remove_pending_signal(signal_id)
    new_text = original_text + "\n\n⏭️ _Dilewati — tidak dipasang._"
    _edit_message(chat_id, message_id, new_text)
    logger.info("Sinyal %s dilewati oleh user.", signal_id)


def _process_update(update: dict, scan_event: Optional[threading.Event] = None) -> None:
    """Proses satu update dari Telegram (callback_query ditangani)."""
    callback = update.get("callback_query")
    if not callback:
        return

    data = callback.get("data", "")
    callback_id = callback.get("id")
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    original_text = message.get("text", "")

    if not data or chat_id is None or message_id is None:
        return

    # ── Tombol "🔄 Scan Sekarang" — tidak pakai format "action:signal_id" ──
    if data == "scan_now":
        if scan_event is not None:
            scan_event.set()
            _answer_callback(callback_id, "⏳ Scan dimulai… hasil dikirim beberapa saat lagi.")
        else:
            _answer_callback(callback_id, "⚠️ Scan event tidak tersedia.")
        return

    try:
        action, signal_id = data.split(":", 1)
    except ValueError:
        _answer_callback(callback_id, "Data tombol tidak valid.")
        return

    # Jawab callback SEBELUM menjalankan handler supaya tombol Telegram
    # berhenti loading segera (Telegram timeout callback setelah 10 detik).
    if action == "placed":
        _answer_callback(callback_id, "⏳ Memproses…")
        _handle_placed(signal_id, chat_id, message_id, original_text)
    elif action == "skip":
        _answer_callback(callback_id, "⏳ Memproses…")
        _handle_skip(signal_id, chat_id, message_id, original_text)
    else:
        _answer_callback(callback_id, "Aksi tidak dikenal.")


def _poll_loop(stop_event: threading.Event, scan_event: Optional[threading.Event] = None) -> None:
    """Loop polling getUpdates (long polling) selama bot berjalan."""
    offset = 0
    backoff = _BACKOFF_START_SECONDS
    logger.info("📡 Telegram button listener aktif — menunggu klik tombol...")

    while not stop_event.is_set():
        response = _api_call(
            "getUpdates",
            {"offset": offset, "timeout": POLL_TIMEOUT_SECONDS, "allowed_updates": ["callback_query"]},
        )

        if not response or not response.get("ok"):
            # Backoff eksponensial (cap di _BACKOFF_MAX_SECONDS) supaya tidak
            # spam retry tanpa henti saat internet/Telegram down lama.
            time.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)
            continue

        backoff = _BACKOFF_START_SECONDS  # reset setelah berhasil

        for update in response.get("result", []):
            offset = update["update_id"] + 1
            try:
                _process_update(update, scan_event=scan_event)
            except Exception as exc:
                logger.error("Error memproses update Telegram: %s", exc, exc_info=True)


def start_listener(scan_event: Optional[threading.Event] = None) -> Optional[threading.Thread]:
    """
    Jalankan Telegram button listener di background thread (daemon).

    Args:
        scan_event: threading.Event yang di-set saat user klik "🔄 Scan Sekarang".
                    Main loop mendeteksi event ini dan menjalankan scan segera.

    Returns:
        Thread object jika berhasil dijalankan, None jika Telegram belum dikonfigurasi.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.debug("Telegram belum dikonfigurasi — listener tombol dilewati.")
        return None

    stop_event = threading.Event()
    thread = threading.Thread(
        target=_poll_loop,
        args=(stop_event, scan_event),
        daemon=True,
        name="telegram-listener",
    )
    thread.stop_event = stop_event  # type: ignore[attr-defined]
    thread.start()
    return thread
