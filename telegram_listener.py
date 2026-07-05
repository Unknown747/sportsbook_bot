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
from datetime import datetime, timezone
from typing import Optional

import requests

import bot_config as config
from executor import append_bet_record
from telegram_notifier import get_pending_signal, remove_pending_signal

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT_SECONDS = 25


def _api_call(method: str, payload: dict) -> Optional[dict]:
    """Helper untuk memanggil Telegram Bot API dan mengembalikan JSON response."""
    url = TELEGRAM_API_BASE.format(token=config.TELEGRAM_BOT_TOKEN, method=method)
    try:
        resp = requests.post(url, json=payload, timeout=POLL_TIMEOUT_SECONDS + 10)
        return resp.json()
    except requests.exceptions.RequestException as exc:
        logger.error("Telegram API error (%s): %s", method, exc)
        return None


def _answer_callback(callback_query_id: str, text: str) -> None:
    """Jawab callback query supaya tombol Telegram berhenti loading."""
    _api_call("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})


def _edit_message(chat_id, message_id: int, new_text: str) -> None:
    """Edit pesan asli (hapus tombol, tampilkan status baru)."""
    _api_call(
        "editMessageText",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": new_text,
            "parse_mode": "Markdown",
        },
    )


def _handle_placed(signal_id: str, chat_id, message_id: int, original_text: str) -> None:
    """Tandai sinyal sebagai sudah dipasang manual oleh user, catat ke bet_history.json."""
    signal = get_pending_signal(signal_id)
    if signal is None:
        _edit_message(chat_id, message_id, original_text + "\n\n_(Sinyal ini sudah tidak berlaku)_")
        return

    timestamp = datetime.now(timezone.utc).isoformat()
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
        + f"\n\n✅ *Ditandai SUDAH DIPASANG* pada {timestamp[11:16]} UTC."
    )
    _edit_message(chat_id, message_id, new_text)
    logger.info("Sinyal %s dikonfirmasi sudah dipasang oleh user.", signal_id)


def _handle_skip(signal_id: str, chat_id, message_id: int, original_text: str) -> None:
    """Tandai sinyal dilewati (tidak jadi dipasang)."""
    remove_pending_signal(signal_id)
    new_text = original_text + "\n\n⏭️ _Dilewati — tidak dipasang._"
    _edit_message(chat_id, message_id, new_text)
    logger.info("Sinyal %s dilewati oleh user.", signal_id)


def _process_update(update: dict) -> None:
    """Proses satu update dari Telegram (hanya callback_query yang ditangani)."""
    callback = update.get("callback_query")
    if not callback:
        return

    data = callback.get("data", "")
    callback_id = callback.get("id")
    message = callback.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    original_text = message.get("text", "")

    if not data or chat_id is None or message_id is None:
        return

    try:
        action, signal_id = data.split(":", 1)
    except ValueError:
        _answer_callback(callback_id, "Data tombol tidak valid.")
        return

    if action == "placed":
        _handle_placed(signal_id, chat_id, message_id, original_text)
        _answer_callback(callback_id, "✅ Dicatat sebagai sudah dipasang!")
    elif action == "skip":
        _handle_skip(signal_id, chat_id, message_id, original_text)
        _answer_callback(callback_id, "Dilewati.")
    else:
        _answer_callback(callback_id, "Aksi tidak dikenal.")


def _poll_loop(stop_event: threading.Event) -> None:
    """Loop polling getUpdates (long polling) selama bot berjalan."""
    offset = 0
    logger.info("📡 Telegram button listener aktif — menunggu klik tombol...")

    while not stop_event.is_set():
        response = _api_call(
            "getUpdates",
            {"offset": offset, "timeout": POLL_TIMEOUT_SECONDS, "allowed_updates": ["callback_query"]},
        )

        if not response or not response.get("ok"):
            time.sleep(5)
            continue

        for update in response.get("result", []):
            offset = update["update_id"] + 1
            try:
                _process_update(update)
            except Exception as exc:
                logger.error("Error memproses update Telegram: %s", exc, exc_info=True)


def start_listener() -> Optional[threading.Thread]:
    """
    Jalankan Telegram button listener di background thread (daemon).
    Tidak melakukan apapun jika Telegram belum dikonfigurasi.

    Returns:
        Thread object jika berhasil dijalankan, None jika dilewati.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.debug("Telegram belum dikonfigurasi — listener tombol dilewati.")
        return None

    stop_event = threading.Event()
    thread = threading.Thread(target=_poll_loop, args=(stop_event,), daemon=True, name="telegram-listener")
    thread.stop_event = stop_event  # type: ignore[attr-defined]
    thread.start()
    return thread
