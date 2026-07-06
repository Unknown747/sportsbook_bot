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

# ODDS_API_KEY dibaca dari config (yang sudah load .env via python-dotenv).
# Jangan duplikat os.environ.get di sini — selalu pakai config.ODDS_API_KEY.

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

# ---------------------------------------------------------------------------
# Opsional: OddsAPI (the-odds-api.com) — gratis 500 req/bulan
# Set ODDS_API_KEY di environment variable untuk mengaktifkan
# ---------------------------------------------------------------------------

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

        if not config.ODDS_API_KEY:
            return []

        try:
            resp = self.odds_session.get(
                f"{ODDS_API_BASE}/sports",
                params={"apiKey": config.ODDS_API_KEY},
                timeout=15,
            )
            resp.raise_for_status()
            try:
                sports = resp.json()
            except ValueError as exc:
                logger.error("OddsAPI: respons daftar liga bukan JSON valid: %s", exc)
                return self._active_sports_cache or []
            self._active_sports_cache = sports
            self._active_sports_fetched_at = now
            return sports
        except requests.exceptions.RequestException as exc:
            logger.error("OddsAPI: gagal ambil daftar liga aktif: %s", exc)
            return self._active_sports_cache or []

    # Hanya sport ini yang di-fetch dari semua liga aktif sekaligus.
    # Sport lain (baseball, basketball, dll) cukup 1 liga preferensi —
    # mereka tidak punya banyak liga bersamaan seperti sepak bola.
    MULTI_LEAGUE_SPORTS: set = {"soccer"}

    # Maksimal liga soccer yang di-fetch per siklus (hemat kuota OddsAPI).
    MAX_SOCCER_LEAGUES: int = 8

    # Kata kunci di sport key yang menandakan futures/outrights market
    # (bukan pertandingan nyata) — dilewati supaya tidak buang request.
    _FUTURES_KEYWORDS = ("winner", "championship", "season_wins", "outright", "futures")

    def _is_futures_key(self, key: str) -> bool:
        """True jika key adalah futures/outrights market, bukan jadwal pertandingan."""
        return any(kw in key for kw in self._FUTURES_KEYWORDS)

    def _resolve_sport_keys(self, sport: str) -> list:
        """
        Kembalikan OddsAPI sport key yang aktif untuk kategori `sport`.

        - Soccer (MULTI_LEAGUE_SPORTS): kembalikan semua liga aktif dalam grup
          Soccer (EPL, La Liga, Bundesliga, UCL, dll bisa aktif bersamaan),
          dibatasi MAX_SOCCER_LEAGUES untuk hemat kuota.
        - Sport lain: kembalikan 1 key preferensi saja — baseball/basketball/
          dll biasanya hanya punya 1 liga utama aktif per siklus.
        - Futures market (winner, championship, dll) selalu difilter keluar.
        """
        if sport in SPORT_FIXED_KEY:
            return [SPORT_FIXED_KEY[sport]]

        group = SPORT_TO_GROUP.get(sport)
        preferred = SPORT_TO_ODDSAPI.get(sport)

        if group is None:
            return [preferred] if preferred else []

        active_sports = self._get_active_odds_api_sports()
        if not active_sports:
            return [preferred] if preferred else []

        # Filter: harus aktif, bukan futures market
        candidates = [
            s["key"] for s in active_sports
            if s.get("group") == group
            and s.get("active")
            and s.get("key")
            and not self._is_futures_key(s["key"])
        ]

        if not candidates:
            logger.info(
                "Tidak ada liga aktif untuk '%s' saat ini — kategori dilewati.",
                sport,
            )
            return []

        # Soccer: ambil semua (hingga batas), liga preferensi di depan
        if sport in self.MULTI_LEAGUE_SPORTS:
            if preferred and preferred in candidates:
                candidates = [preferred] + [k for k in candidates if k != preferred]
            return candidates[: self.MAX_SOCCER_LEAGUES]

        # Sport lain: hanya 1 liga — preferensi jika aktif, fallback ke kandidat pertama
        if preferred and preferred in candidates:
            return [preferred]
        return [candidates[0]]

    def _resolve_sport_key(self, sport: str) -> Optional[str]:
        """Backward-compat: kembalikan satu key saja (key pertama dari list)."""
        keys = self._resolve_sport_keys(sport)
        return keys[0] if keys else None

    def _fetch_odds_api(self, sport_key: str, limit: int = 20) -> list:
        """
        Fetch live/upcoming match odds from the-odds-api.com.

        Args:
            sport_key: OddsAPI sport key e.g. 'soccer_epl', 'basketball_nba'.
            limit: Max number of events to return.

        Returns:
            List of match dicts in bot format, or [] on failure.
        """
        if not config.ODDS_API_KEY:
            return []

        for attempt in range(2):  # 1 retry untuk transient network error
            try:
                resp = self.odds_session.get(
                    f"{ODDS_API_BASE}/sports/{sport_key}/odds",
                    params={
                        "apiKey": config.ODDS_API_KEY,
                        "regions": "eu,us,au,uk",
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
                if resp.status_code == 429:
                    logger.warning("OddsAPI: rate limit tercapai (HTTP 429) — coba lagi nanti.")
                    return []

                resp.raise_for_status()
                try:
                    events = resp.json()
                except ValueError as exc:
                    logger.error("OddsAPI: respons bukan JSON valid: %s", exc)
                    return []
                break  # sukses — keluar dari loop retry

            except requests.exceptions.RequestException as exc:
                if attempt == 0:
                    logger.warning("OddsAPI request error (retry 1): %s", exc)
                    time.sleep(2)
                else:
                    logger.error("OddsAPI request error (final): %s", exc)
                    return []
        else:
            return []

        matches = []
        for event in events[:limit]:
            home_name = event.get("home_team", "Home")
            away_name = event.get("away_team", "Away")
            event_id = event.get("id", "unknown")

            # Kumpulkan odds RIIL per-bookmaker (tidak ada data buatan/placeholder).
            odds_by_bookmaker: dict = {}

            for bookie in event.get("bookmakers") or []:
                bookie_key = bookie.get("key", "unknown")
                for market in bookie.get("markets") or []:
                    if market.get("key") != "h2h":
                        continue
                    bookie_outcomes: dict = {}
                    for outcome in market.get("outcomes") or []:
                        name = outcome.get("name", "")
                        try:
                            price = float(outcome.get("price") or 0)
                        except (TypeError, ValueError):
                            continue
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

            # Butuh minimal 2 bookmaker untuk hitung konsensus pasar yang valid.
            if len(odds_by_bookmaker) < 2:
                continue

            # Cari odds terbaik per outcome dari semua bookmaker yang tersedia.
            # Stake tidak selalu ada di OddsAPI — bot membandingkan konsensus pasar
            # vs odds terbaik yang tersedia, user kemudian cek manual di Stake.
            all_outcomes = set()
            for book_odds in odds_by_bookmaker.values():
                all_outcomes.update(book_odds.keys())

            best_odds: dict = {}
            best_bookmaker: dict = {}  # outcome → nama bookmaker dengan odds terbaik
            for outcome in all_outcomes:
                best_price = 0.0
                best_bookie = ""
                for bk, bk_odds in odds_by_bookmaker.items():
                    price = bk_odds.get(outcome, 0.0)
                    if price > best_price:
                        best_price = price
                        best_bookie = bk
                if best_price > 1.0:
                    best_odds[outcome] = best_price
                    best_bookmaker[outcome] = best_bookie

            if not best_odds.get("Home") or not best_odds.get("Away"):
                continue

            is_3way = "Draw" in best_odds

            matches.append({
                "match_id": event_id,
                "home_team": home_name,
                "away_team": away_name,
                "sport": event.get("sport_key", sport_key),
                "is_3way": is_3way,
                "commence_time": event.get("commence_time", ""),
                "odds_data_all": odds_by_bookmaker,
                # Odds terbaik dari pasar (bukan Stake khusus — Stake tidak selalu
                # ada di OddsAPI). User wajib cek manual di Stake sebelum taruhan.
                "best_odds": best_odds,
                "best_bookmaker": best_bookmaker,
            })

        return matches

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def fetch_live_matches(self, sport: str = "soccer", limit: int = 20) -> list:
        """
        Fetch live/upcoming matches with odds for the bot pipeline.

        Untuk sport grup (Soccer, Basketball, dll): secara otomatis mengambil
        dari SEMUA liga aktif dalam grup itu (bukan hanya satu liga preferensi),
        lalu menggabungkan & deduplikasi hasilnya berdasarkan match_id.

        Source          : OddsAPI (requires ODDS_API_KEY env var)
        No data         : Returns [] — caller MUST skip, no fabricated fallback

        Args:
            sport: Sport slug e.g. 'soccer', 'basketball', 'baseball'.
            limit: Max events per league.

        Returns:
            List of match dicts (semua liga digabung), atau [] jika tidak ada data.
        """
        if not config.ODDS_API_KEY:
            logger.debug(
                "ODDS_API_KEY tidak diset — OddsAPI dinonaktifkan. "
                "Set env var ODDS_API_KEY untuk data odds live."
            )
            return []

        sport_keys = self._resolve_sport_keys(sport)
        if not sport_keys:
            return []

        all_matches: dict = {}  # match_id → match dict (dedup otomatis)
        for key in sport_keys:
            logger.info("Fetching '%s' dari OddsAPI (key=%s)...", sport, key)
            matches = self._fetch_odds_api(key, limit)
            for m in matches:
                all_matches[m["match_id"]] = m

        result = list(all_matches.values())
        if result:
            logger.info(
                "OddsAPI: %d pertandingan '%s' ditemukan dari %d liga.",
                len(result), sport, len(sport_keys),
            )
        else:
            logger.warning("OddsAPI: tidak ada data untuk '%s'.", sport)

        return result
