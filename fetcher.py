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
  Bot menggunakan data odds RIIL dari penyedia data pihak ketiga
  (the-odds-api.com). TIDAK ADA data buatan/sample — kalau ODDS_API_KEY
  tidak diset atau tidak ada data odds yang cukup, match/siklus dilewati.

  Untuk pengembangan lebih lanjut, pertimbangkan:
  1. Odds-API (the-odds-api.com) — gratis 500 request/bulan
  2. SportRadar API — data odds profesional
  3. API-Sports (api-sports.io) — gratis 100 request/hari
"""

import logging
import os
import time
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

# Kategori di atas dipetakan lewat GROUP OddsAPI supaya bot bisa memilih liga
# yang SEDANG AKTIF secara dinamis (banyak liga musiman — NFL preseason,
# WNBA, dll — sehingga key statis di atas hanya dipakai sbg preferensi/
# fallback, bukan satu-satunya pilihan). Lihat `_resolve_sport_key`.
SPORT_TO_GROUP: dict = {
    "soccer": "Soccer",
    "basketball": "Basketball",
    "baseball": "Baseball",
    "american-football": "American Football",
    "cricket": "Cricket",
}

# Sport dengan key OddsAPI yang sudah spesifik (bukan musiman/grup umum) —
# tidak perlu resolusi dinamis.
SPORT_FIXED_KEY: dict = {
    "baseball_kbo": "baseball_kbo",
    "baseball_npb": "baseball_npb",
}

_ACTIVE_SPORTS_TTL_SECONDS = 3600


class StakeFetcher:
    """
    Fetches REAL sports match data for the betting bot.

    Source: OddsAPI (requires ODDS_API_KEY). No fabricated/sample data —
    if the key is missing or no real data is available, callers receive
    an empty list and must skip the cycle.

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
        self._active_sports_cache: Optional[list] = None
        self._active_sports_fetched_at: float = 0.0

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

    def _get_active_odds_api_sports(self) -> list:
        """
        Ambil & cache daftar sport/liga yang SEDANG AKTIF (in-season) dari
        OddsAPI. Di-cache selama _ACTIVE_SPORTS_TTL_SECONDS supaya tidak
        boros kuota — daftar ini jarang berubah dalam hitungan jam.
        """
        now = time.time()
        if (
            self._active_sports_cache is not None
            and (now - self._active_sports_fetched_at) < _ACTIVE_SPORTS_TTL_SECONDS
        ):
            return self._active_sports_cache

        if not ODDS_API_KEY:
            return []

        try:
            resp = self.odds_session.get(
                f"{ODDS_API_BASE}/sports",
                params={"apiKey": ODDS_API_KEY},
                timeout=15,
            )
            resp.raise_for_status()
            sports = resp.json()
            self._active_sports_cache = sports
            self._active_sports_fetched_at = now
            return sports
        except requests.exceptions.RequestException as exc:
            logger.error("OddsAPI: gagal ambil daftar liga aktif: %s", exc)
            return self._active_sports_cache or []

    def _resolve_sport_key(self, sport: str) -> Optional[str]:
        """
        Tentukan OddsAPI sport key yang benar-benar AKTIF untuk kategori
        `sport` saat ini. Banyak liga musiman (NFL, WNBA, dll) — key statis
        di SPORT_TO_ODDSAPI hanya dipakai sebagai preferensi jika liga itu
        masih aktif; kalau tidak, bot memilih liga aktif lain di grup yang
        sama, atau melewati kategori itu sepenuhnya jika tidak ada liga
        aktif (bukan menebak/memakai key basi yang pasti kosong).
        """
        if sport in SPORT_FIXED_KEY:
            return SPORT_FIXED_KEY[sport]

        group = SPORT_TO_GROUP.get(sport)
        if group is None:
            return SPORT_TO_ODDSAPI.get(sport)

        active_sports = self._get_active_odds_api_sports()
        if not active_sports:
            # Gagal ambil daftar aktif (mis. error jaringan) — fallback ke
            # key statis, lebih baik dari pada tidak mencoba sama sekali.
            return SPORT_TO_ODDSAPI.get(sport)

        candidates = [
            s["key"] for s in active_sports
            if s.get("group") == group and s.get("active") and s.get("key")
        ]
        if not candidates:
            logger.info(
                "Tidak ada liga aktif untuk kategori '%s' saat ini (kemungkinan "
                "di luar musim) — kategori dilewati, bukan pakai key basi.",
                sport,
            )
            return None

        preferred = SPORT_TO_ODDSAPI.get(sport)
        return preferred if preferred in candidates else candidates[0]

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

            # Kumpulkan odds RIIL per-bookmaker (tidak ada data buatan/placeholder).
            odds_by_bookmaker: dict = {}
            stake_odds: dict = {}

            for bookie in event.get("bookmakers") or []:
                bookie_key = bookie.get("key", "unknown")
                for market in bookie.get("markets") or []:
                    if market.get("key") != "h2h":
                        continue
                    bookie_outcomes: dict = {}
                    for outcome in market.get("outcomes") or []:
                        name = outcome.get("name", "")
                        price = float(outcome.get("price", 0))
                        if price <= 1.0:
                            continue
                        if name == home_name:
                            bookie_outcomes["Home"] = price
                        elif name == away_name:
                            bookie_outcomes["Away"] = price
                        else:
                            bookie_outcomes["Draw"] = price

                    if bookie_outcomes.get("Home") and bookie_outcomes.get("Away"):
                        odds_by_bookmaker[bookie_key] = bookie_outcomes
                        if bookie_key == "stake":
                            stake_odds = bookie_outcomes

            # Wajib ada odds Stake yang riil — kalau tidak, match tidak bisa
            # dieksekusi user (dia hanya punya akun Stake), jadi dilewati.
            if not stake_odds:
                continue

            # Wajib ada minimal beberapa bookmaker lain untuk hitung konsensus
            # pasar riil. Kalau tidak cukup, predictor akan melewati match ini
            # sendiri — tapi kita filter di sini juga supaya hemat proses.
            if len(odds_by_bookmaker) < 2:
                continue

            is_3way = "Draw" in stake_odds

            matches.append({
                "match_id": event_id,
                "home_team": home_name,
                "away_team": away_name,
                "sport": event.get("sport_key", sport_key),
                "is_3way": is_3way,
                "commence_time": event.get("commence_time", ""),
                "odds_data_all": odds_by_bookmaker,
                "stake_odds": stake_odds,
            })

        return matches

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def fetch_live_matches(self, sport: str = "soccer", limit: int = 20) -> list:
        """
        Fetch live/upcoming matches with odds for the bot pipeline.

        Source          : OddsAPI (requires ODDS_API_KEY env var)
        No data         : Returns [] — caller MUST skip, no fabricated fallback

        Args:
            sport: Sport slug e.g. 'soccer', 'basketball', 'tennis'.
            limit: Max events to return.

        Returns:
            List of match dicts, or [] if no data available.
        """
        if not ODDS_API_KEY:
            logger.debug(
                "ODDS_API_KEY tidak diset — OddsAPI dinonaktifkan. "
                "Set env var ODDS_API_KEY untuk data odds live."
            )
            return []

        oddsapi_key = self._resolve_sport_key(sport)

        if oddsapi_key:
            logger.info("Fetching '%s' dari OddsAPI (key=%s)...", sport, oddsapi_key)
            matches = self._fetch_odds_api(oddsapi_key, limit)
            if matches:
                logger.info("OddsAPI: %d pertandingan '%s' ditemukan.", len(matches), sport)
                return matches
            logger.warning("OddsAPI: tidak ada data untuk '%s'.", sport)

        return []
