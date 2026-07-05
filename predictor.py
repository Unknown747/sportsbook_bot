"""
Predictor module for Sportsbook Auto Betting Agent.

Menghitung probabilitas "fair" (nilai sebenarnya) murni dari data odds pasar
real yang sudah diambil dari OddsAPI — TIDAK ADA angka acak/simulasi di sini.

Metode: Wisdom-of-the-crowd devigging.
  1. Ambil odds dari semua bookmaker riil yang tersedia untuk pertandingan itu.
  2. Ubah setiap odds jadi peluang implisit (1/odds).
  3. Rata-ratakan peluang implisit lintas bookmaker per outcome (konsensus pasar).
  4. Hilangkan overround/vig (normalisasi supaya total = 1.0) -> peluang "fair".

Kalau pasar yakin outcome A punya peluang lebih tinggi daripada yang
ditawarkan Stake, itulah value bet -- edge dihitung di `arbitrage_finder.py`
dari selisih peluang fair ini dengan peluang implisit odds Stake.

Match tanpa odds bookmaker yang cukup (misal cuma 1 bookmaker) TIDAK bisa
diberi prediksi yang valid -- caller harus skip match tersebut, bukan
menebak-nebak.
"""

from typing import Dict, Optional

MIN_BOOKMAKERS_REQUIRED: int = 2


def _implied_prob(odds: float) -> float:
    """Konversi odds desimal ke peluang implisit."""
    if odds is None or odds <= 1.0:
        return 0.0
    return 1.0 / odds


def get_market_consensus_prediction(match_data: dict) -> Optional[Dict[str, float]]:
    """
    Hitung peluang "fair" dari konsensus multi-bookmaker (real market data).

    Args:
        match_data: Harus mengandung `odds_data_all` -- dict per-bookmaker
            odds (bukan cuma "best"/"stake"), contoh:
            {
                "stake": {"Home": 1.85, "Away": 2.05},
                "pinnacle": {"Home": 1.90, "Away": 2.00},
                "bet365": {"Home": 1.88, "Away": 2.02},
            }

    Returns:
        Dict peluang fair per outcome (jumlah = 1.0), atau None jika data
        odds tidak cukup (kurang dari MIN_BOOKMAKERS_REQUIRED bookmaker) --
        artinya match ini HARUS dilewati, bukan diprediksi dengan tebakan.
    """
    odds_by_bookmaker: dict = match_data.get("odds_data_all") or {}

    if len(odds_by_bookmaker) < MIN_BOOKMAKERS_REQUIRED:
        return None

    outcomes = set()
    for book_odds in odds_by_bookmaker.values():
        outcomes.update(book_odds.keys())

    if not outcomes:
        return None

    sums: Dict[str, float] = {o: 0.0 for o in outcomes}
    counts: Dict[str, int] = {o: 0 for o in outcomes}

    for book_odds in odds_by_bookmaker.values():
        for outcome, odds in book_odds.items():
            implied = _implied_prob(odds)
            if implied > 0.0:
                sums[outcome] += implied
                counts[outcome] += 1

    if any(counts[o] == 0 for o in outcomes):
        return None

    avg_implied = {o: sums[o] / counts[o] for o in outcomes}
    total = sum(avg_implied.values())

    if total <= 0.0:
        return None

    return {o: v / total for o, v in avg_implied.items()}
