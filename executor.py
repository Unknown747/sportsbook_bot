"""
Executor module for Sportsbook Auto Betting Agent.
Handles bet placement via Stake.com GraphQL API and maintains bet history log.
"""

import json
import logging
import os
import threading
from datetime import date, datetime, timedelta, timezone

# WIB = UTC+7 — dipakai secara konsisten untuk reset harian & drawdown tracking.
_WIB = timezone(timedelta(hours=7))
from typing import Optional

import requests

import bot_config as config

logger = logging.getLogger(__name__)

BET_HISTORY_FILE: str = "bet_history.json"

# bet_history.json ditulis dari 2 thread berbeda (loop utama & Telegram
# listener) — lock ini mencegah race condition read-modify-write yang bisa
# menghilangkan record taruhan.
_history_lock = threading.Lock()

CREATE_SPORTS_BET_MUTATION = """
mutation CreateSportsBet($input: SportsBetInput!) {
  createSportsBet(input: $input) {
    id
    status
    amount
    currency
    createdAt
  }
}
"""


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


def append_bet_record(record: dict) -> None:
    """Append a single bet record to bet_history.json (used by executor & Telegram listener)."""
    with _history_lock:
        history = load_history()
        history.append(record)
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


class StakeExecutor:
    """Handles Stake.com bet placement and history tracking."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "x-access-token": config.STAKE_API_KEY,
                "Connection": "keep-alive",
            }
        )
        ensure_history_file()

    def _log_bet(self, record: dict) -> None:
        """Append a bet record to bet_history.json."""
        append_bet_record(record)

    def place_stake_bet(
        self,
        active_odds_id: str,
        target_stake: float,
        match_info: Optional[dict] = None,
    ) -> dict:
        """
        Place a sports bet on Stake.com via GraphQL mutation.

        In SIMULATION_MODE the bet is logged without hitting the API.

        Args:
            active_odds_id: The Stake odds/market ID to bet on.
            target_stake: Amount in IDR to wager.
            match_info: Optional metadata about the match for history logging.

        Returns:
            Response dict with keys: success (bool), data (dict), error (str|None).
        """
        timestamp = datetime.now(_WIB).isoformat()

        base_record = {
            "timestamp": timestamp,
            "active_odds_id": active_odds_id,
            "stake_idr": target_stake,
            "currency": config.STAKE_CURRENCY,
            "simulation": config.SIMULATION_MODE,
            "match_info": match_info or {},
        }

        if config.SIMULATION_MODE:
            simulated_result = {
                "id": f"sim_{active_odds_id}_{timestamp}",
                "status": "SIMULATED",
                "amount": target_stake,
                "currency": config.STAKE_CURRENCY,
                "createdAt": timestamp,
            }
            record = {**base_record, "status": "SIMULATED", "response": simulated_result}
            self._log_bet(record)
            logger.info("[SIMULATION] Bet logged: %s @ %.2f IDR", active_odds_id, target_stake)
            return {"success": True, "data": simulated_result, "error": None}

        payload = {
            "query": CREATE_SPORTS_BET_MUTATION,
            "variables": {
                "input": {
                    "activeOddsId": active_odds_id,
                    "amount": target_stake,
                    "currency": config.STAKE_CURRENCY,
                }
            },
        }

        try:
            response = self.session.post(
                config.STAKE_API_URL, json=payload, timeout=15
            )
            response.raise_for_status()
            data = response.json()

            if "errors" in data:
                error_msg = str(data["errors"])
                record = {**base_record, "status": "API_ERROR", "error": error_msg}
                self._log_bet(record)
                logger.error("GraphQL error placing bet: %s", error_msg)
                return {"success": False, "data": {}, "error": error_msg}

            bet_data = data.get("data", {}).get("createSportsBet", {})
            record = {**base_record, "status": bet_data.get("status", "UNKNOWN"), "response": bet_data}
            self._log_bet(record)
            logger.info("Bet placed: %s status=%s", active_odds_id, bet_data.get("status"))
            return {"success": True, "data": bet_data, "error": None}

        except requests.exceptions.RequestException as exc:
            error_msg = str(exc)
            record = {**base_record, "status": "REQUEST_ERROR", "error": error_msg}
            self._log_bet(record)
            logger.error("Request error placing bet: %s", error_msg)
            return {"success": False, "data": {}, "error": error_msg}
