"""
Executor module for Sportsbook Auto Betting Agent.
Maintains the bet history log (bet_history.json) — dipakai untuk mencatat
konfirmasi taruhan manual dari user (via tombol Telegram) dan menghitung
eksposur harian.

Catatan: eksekusi taruhan otomatis ke Stake.com TIDAK didukung (API Stake
tidak menyediakan market/odds ID sportsbook riil) — lihat catatan di
fetcher.py. Kelas StakeExecutor yang lama sudah dihapus dari modul ini
karena tidak pernah dipanggil dari mana pun.
"""

import json
import logging
import os
import threading
from datetime import date, datetime, timedelta, timezone

# WIB = UTC+7 — dipakai secara konsisten untuk reset harian & eksposur tracking.
_WIB = timezone(timedelta(hours=7))

import bot_config as config

logger = logging.getLogger(__name__)

BET_HISTORY_FILE: str = "bet_history.json"

# bet_history.json ditulis dari 2 thread berbeda (loop utama & Telegram
# listener) — lock ini mencegah race condition read-modify-write yang bisa
# menghilangkan record taruhan.
_history_lock = threading.Lock()


def ensure_history_file() -> None:
    """Initialize bet history file if it does not exist."""
    if not os.path.exists(BET_HISTORY_FILE):
        with open(BET_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)


def load_history() -> list:
    """Load existing bet history from file."""
    ensure_history_file()
    try:
        with open(BET_HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def save_history(history: list) -> None:
    """Persist bet history to file."""
    with open(BET_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


def _prune_old_records(history: list) -> list:
    """
    Buang record yang lebih tua dari BET_HISTORY_RETENTION_DAYS supaya
    bet_history.json tidak tumbuh tanpa batas. Record tanpa timestamp valid
    tetap dipertahankan (lebih aman daripada menghapus data yang tidak jelas).
    """
    cutoff = datetime.now(_WIB) - timedelta(days=config.BET_HISTORY_RETENTION_DAYS)
    kept: list = []
    for record in history:
        timestamp = record.get("timestamp", "")
        try:
            dt = datetime.fromisoformat(timestamp)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            kept.append(record)
            continue
        if dt.astimezone(_WIB) >= cutoff:
            kept.append(record)
    return kept


def append_bet_record(record: dict) -> None:
    """Append a single bet record to bet_history.json (used by executor & Telegram listener)."""
    with _history_lock:
        history = load_history()
        history.append(record)
        history = _prune_old_records(history)
        save_history(history)


def get_today_confirmed_stake_total(reference_date: date) -> float:
    """
    Hitung total stake (IDR) dari semua taruhan CONFIRMED_MANUAL yang
    dicatat pada `reference_date`. Dipakai sebagai proxy risiko harian
    (MAX_DAILY_DRAWDOWN) karena bot tidak punya data hasil/settlement
    taruhan (hanya tahu taruhan sudah dipasang, bukan menang/kalah) —
    jadi cap ini membatasi TOTAL EKSPOSUR harian, bukan kerugian riil.

    Args:
        reference_date: Tanggal (date object) yang ingin dihitung totalnya.

    Returns:
        Total stake_idr (IDR) dari record CONFIRMED_MANUAL hari itu.
    """
    with _history_lock:
        history = load_history()

    total = 0.0
    for record in history:
        if record.get("status") != "CONFIRMED_MANUAL":
            continue
        timestamp = record.get("timestamp", "")
        try:
            # Konversi ke WIB supaya reset harian konsisten dengan jam scan (WIB).
            dt = datetime.fromisoformat(timestamp)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            record_date = dt.astimezone(_WIB).date()
        except (ValueError, TypeError):
            continue
        if record_date == reference_date:
            try:
                total += float(record.get("stake_idr") or 0.0)
            except (TypeError, ValueError):
                continue
    return total
