"""
Fetcher module for Sportsbook Auto Betting Agent.

STATUS API STAKE SPORTSBOOK:
  Stake.com membatasi akses GraphQL sportsbook lewat API key publik.
  API key yang tersedia (x-access-token) hanya memberi akses penuh ke
  casino games (Dice, Limbo, Mines). Query sportEvents / slugEvent /
  sportFixtures mengembalikan 400/data kosong dari server Stake.

  Yang BISA diakses via API key:
    - user { id name balances }          → verifikasi auth
    - sportList                           → daftar cabang olahraga
    - tournamentList                      → daftar turnamen aktif
    - allSportBets                        → riwayat taruhan sports akun
    - createSportsBet mutation            → pasang taruhan (jika ada odds ID)

  Yang TIDAK bisa diakses via API key biasa:
    - Live odds / markets per pertandingan
    - sportEvents / sportFixtures / event detail

SOLUSI YANG DIPAKAI:
  Bot menggunakan data odds dari sample/konfigurasi manual, atau dari
  penyedia data odds pihak ketiga (OddsAPI, SportRadar, dll) yang bisa
  dikonfigurasi di config.py. Fallback ke sample data jika tidak ada
  sumber odds eksternal.

  Untuk pengembangan lebih lanjut, pertimbangkan:
  1. Odds-API (the-odds-api.com) — gratis 500 request/bulan
  2. SportRadar API — data odds profesional
  3. API-Sports (api-sports.io) — gratis 100 request/hari
"""

import logging
import os
from typing import Optional

import requests

import bot_config as config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Queries yang TERBUKTI bekerja di Stake API
# ---------------------------------------------------------------------------

USER_QUERY = """
{
  user {
    id
    name
    balances {
      available {
        amount
        currency
      }
    }
  }
}
"""

TOURNAMENT_LIST_QUERY = """
{
  tournamentList(limit: 30) {
    id
    name
    slug
  }
}
"""

SPORT_LIST_QUERY = """
{
  sportList {
    id
    name
    slug
  }
}
"""

ALL_SPORT_BETS_QUERY = """
query AllSportBets($limit: Int) {
  allSportBets(limit: $limit) {
    id
  }
}
"""

# ---------------------------------------------------------------------------
# Opsional: OddsAPI (the-odds-api.com) — gratis 500 req/bulan
# Set ODDS_API_KEY di environment variable untuk mengaktifkan
# ---------------------------------------------------------------------------

ODDS_API_KEY: str = os.environ.get("ODDS_API_KEY", "")
ODDS_API_BASE: str = "https://api.the-odds-api.com/v4"

SPORT_TO_ODDSAPI: dict = {
    "soccer": "soccer_epl",
    "basketball": "basketball_wnba",
    "baseball": "baseball_mlb",
    "american-football": "americanfootball_nfl_preseason",
    "cricket": "cricket_international_t20",
    "baseball_kbo": "baseball_kbo",
    "baseball_npb": "baseball_npb",
}


class StakeFetcher:
    """
    Fetches sports match data for the betting bot.

    Primary source : OddsAPI (if ODDS_API_KEY is set)
    Fallback       : Sample/manual data (always available)

    Also provides Stake API utility methods (verify auth, get user info).
    """

    def __init__(self) -> None:
        self.stake_session = requests.Session()
        self.stake_session.headers.update(
            {
                "Content-Type": "application/json",
                "x-access-token": config.STAKE_API_KEY,
                "Connection": "keep-alive",
            }
        )
        self.odds_session = requests.Session()

    # ------------------------------------------------------------------
    # Stake API utility methods
    # ------------------------------------------------------------------

    def _stake_post(self, query: str, variables: Optional[dict] = None) -> Optional[dict]:
        """Send a GraphQL request to Stake.com. Returns 'data' field or None."""
        payload: dict = {"query": query}
        if variables:
            payload["variables"] = variables
        try:
            resp = self.stake_session.post(config.STAKE_API_URL, json=payload, timeout=20)
            resp.raise_for_status()
            body = resp.json()
            if "errors" in body:
                for err in body["errors"]:
                    msg = err.get("message", "")
                    if any(k in msg.lower() for k in ("access denied", "session", "unauthorized")):
                        raise PermissionError(msg)
                logger.warning("GraphQL errors: %s", body["errors"])
                return None
            return body.get("data")
        except PermissionError:
            raise
        except requests.exceptions.Timeout:
            logger.error("Stake API timeout.")
        except requests.exceptions.HTTPError as exc:
            logger.error("HTTP error dari Stake API: %s", exc)
        except Exception as exc:
            logger.error("Error Stake API: %s", exc)
        return None

    def verify_connection(self) -> bool:
        """
        Verify API key and connectivity by querying user info.

        Returns:
            True if auth succeeds, False otherwise.
        """
        try:
            data = self._stake_post(USER_QUERY)
            if data and data.get("user"):
                user = data["user"]
                logger.info(
                    "Stake API OK — User: %s (id=%s)",
                    user.get("name", "?"),
                    user.get("id", "?"),
                )
                # Log balances
                for bal in user.get("balances") or []:
                    avail = bal.get("available", {})
                    if float(avail.get("amount", 0)) > 0:
                        logger.info(
                            "  Saldo %s: %s",
                            avail.get("currency", "?").upper(),
                            avail.get("amount"),
                        )
                return True
            logger.warning("Stake API OK tapi data user kosong.")
            return False
        except PermissionError as exc:
            logger.error("API key tidak valid: %s", exc)
            return False

    def get_active_tournaments(self) -> list:
        """
        Fetch list of active tournaments from Stake.com.

        Returns:
            List of tournament dicts with id, name, slug.
        """
        data = self._stake_post(TOURNAMENT_LIST_QUERY)
        if data:
            return data.get("tournamentList") or []
        return []

    # ------------------------------------------------------------------
    # OddsAPI (the-odds-api.com) — primary odds source
    # ------------------------------------------------------------------

    def _fetch_odds_api(self, sport_key: str, limit: int = 20) -> list:
        """
        Fetch live/upcoming match odds from the-odds-api.com.

        Args:
            sport_key: OddsAPI sport key e.g. 'soccer_epl', 'basketball_nba'.
            limit: Max number of events to return.

        Returns:
            List of match dicts in bot format, or [] on failure.
        """
        if not ODDS_API_KEY:
            return []

        try:
            resp = self.odds_session.get(
                f"{ODDS_API_BASE}/sports/{sport_key}/odds",
                params={
                    "apiKey": ODDS_API_KEY,
                    "regions": "eu,us,au",
                    "markets": "h2h",
                    "oddsFormat": "decimal",
                },
                timeout=15,
            )

            remaining = resp.headers.get("x-requests-remaining", "?")
            logger.info("OddsAPI remaining requests: %s", remaining)

            if resp.status_code == 401:
                logger.error("OddsAPI: API key tidak valid.")
                return []
            if resp.status_code == 422:
                logger.warning("OddsAPI: sport '%s' tidak tersedia.", sport_key)
                return []

            resp.raise_for_status()
            events = resp.json()

        except requests.exceptions.RequestException as exc:
            logger.error("OddsAPI request error: %s", exc)
            return []

        matches = []
        for event in events[:limit]:
            home_name = event.get("home_team", "Home")
            away_name = event.get("away_team", "Away")
            event_id = event.get("id", "unknown")

            # Find best odds from available bookmakers
            best_home, best_draw, best_away = 0.0, 0.0, 0.0
            stake_home, stake_draw, stake_away = 0.0, 0.0, 0.0

            for bookie in event.get("bookmakers") or []:
                for market in bookie.get("markets") or []:
                    if market.get("key") != "h2h":
                        continue
                    bookie_odds: dict = {}
                    for outcome in market.get("outcomes") or []:
                        name = outcome.get("name", "")
                        price = float(outcome.get("price", 0))
                        if name == home_name:
                            bookie_odds["Home"] = price
                        elif name == away_name:
                            bookie_odds["Away"] = price
                        else:
                            bookie_odds["Draw"] = price

                    if bookie.get("key") == "stake":
                        stake_home = bookie_odds.get("Home", 0.0)
                        stake_draw = bookie_odds.get("Draw", 0.0)
                        stake_away = bookie_odds.get("Away", 0.0)

                    best_home = max(best_home, bookie_odds.get("Home", 0.0))
                    best_draw = max(best_draw, bookie_odds.get("Draw", 0.0))
                    best_away = max(best_away, bookie_odds.get("Away", 0.0))

            # Use Stake odds if available, else best from any bookmaker
            h = stake_home or best_home
            d = stake_draw or best_draw   # 0.0 for 2-way markets (no draw)
            a = stake_away or best_away

            # Must have at least Home and Away odds
            if not (h > 1.0 and a > 1.0):
                continue

            is_3way = d > 1.0   # True for soccer; False for baseball/basketball

            def _build_odds(home: float, draw: float, away: float) -> dict:
                """Build odds dict — include Draw only if available."""
                o: dict = {"Home": home, "Away": away}
                if draw > 1.0:
                    o["Draw"] = draw
                return o

            odds_data: dict = {}

            # Best available odds across all bookmakers
            best_odds = _build_odds(best_home, best_draw, best_away)
            if best_odds:
                odds_data["best"] = best_odds

            # Stake-specific odds (only if stake bookmaker listed for this sport)
            if stake_home > 1.0 and stake_away > 1.0:
                stake_odds = _build_odds(stake_home, stake_draw, stake_away)
                odds_data["stake"] = stake_odds

            if not odds_data:
                continue

            # active_odds_ids — placeholder; replaced by real Stake IDs when available
            active_ids: dict = {
                "Home": f"{event_id}_home",
                "Away": f"{event_id}_away",
            }
            if is_3way:
                active_ids["Draw"] = f"{event_id}_draw"

            matches.append({
                "match_id": event_id,
                "home_team": home_name,
                "away_team": away_name,
                "home_strength": 50.0,
                "away_strength": 50.0,
                "league_draw_rate": 0.27 if is_3way else 0.0,
                "sentiment_home_bias": 0.0,
                "sport": event.get("sport_key", sport_key),
                "is_3way": is_3way,
                "commence_time": event.get("commence_time", ""),
                "odds_data": odds_data,
                "active_odds_ids": active_ids,
            })

        return matches

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def fetch_live_matches(self, sport: str = "soccer", limit: int = 20) -> list:
        """
        Fetch live/upcoming matches with odds for the bot pipeline.

        Primary source  : OddsAPI (if ODDS_API_KEY env var is set)
        Fallback        : Empty list (main.py will use sample data)

        Args:
            sport: Sport slug e.g. 'soccer', 'basketball', 'tennis'.
            limit: Max events to return.

        Returns:
            List of match dicts, or [] if no data available.
        """
        oddsapi_key = SPORT_TO_ODDSAPI.get(sport)

        if ODDS_API_KEY and oddsapi_key:
            logger.info("Fetching '%s' dari OddsAPI (key=%s)...", sport, oddsapi_key)
            matches = self._fetch_odds_api(oddsapi_key, limit)
            if matches:
                logger.info("OddsAPI: %d pertandingan '%s' ditemukan.", len(matches), sport)
                return matches
            logger.warning("OddsAPI: tidak ada data untuk '%s'.", sport)
        else:
            if not ODDS_API_KEY:
                logger.debug(
                    "ODDS_API_KEY tidak diset — OddsAPI dinonaktifkan. "
                    "Set env var ODDS_API_KEY untuk data odds live."
                )

        return []
