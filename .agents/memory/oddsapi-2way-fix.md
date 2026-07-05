---
name: OddsAPI 2-way market parsing fix
description: OddsAPI requires no bookmaker filter; must handle sports without Draw outcome
---

## Rule
1. Do NOT use `bookmakers: stake` filter — Stake.com is not listed for most sports (baseball, basketball, etc.)
2. For 2-way markets (baseball, basketball, NFL), `best_draw = 0.0` — condition `best_home and best_draw and best_away` silently drops all events
3. Fix: use `if not (h > 1.0 and a > 1.0): continue` and build odds_data with Draw only if `d > 1.0`
4. Propagate `is_3way` flag to main loop so probability normalization uses correct outcome set

**Why:** The original parsing code was written for soccer (3-way). Baseball/basketball are 2-way markets with no draw, so the draw check caused silent data loss.

**How to apply:** Always check `is_3way` flag in match dict. Normalize AI probs over `["Home","Away"]` for 2-way, `["Home","Draw","Away"]` for 3-way.
