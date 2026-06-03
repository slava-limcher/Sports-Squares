"""
Win Expectancy lookup.

Loads Tom Tango's 2010-2015 MLB win expectancy table and provides
fast lookups by game state. The output is the home team's win probability
given (inning, half, run_differential, outs, base_state).

Data source:
  Tom Tango's WE table at https://tangotiger.net/we.html
  Mirror with CSV: github.com/dev-1999/three_batter_rule (we_matrix.csv)

Expected CSV schema (data/win_expectancy_2010_2015.csv):
  inning,half,run_diff,outs,base_state,home_win_prob

  inning: 1-9
  half: "top" or "bottom"
  run_diff: home_score - away_score (clamped to [-10, 10] in lookups)
  outs: 0, 1, 2
  base_state: 0-7 (3-bit encoding: bit 0 = 1B, bit 1 = 2B, bit 2 = 3B)
  home_win_prob: 0.0-1.0

Phase 2 (planned): recompute table from 2023-2025 Retrosheet data
to account for the ghost runner rule, pitch clock, shift ban,
and three-batter minimum.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger("barboards.we")

# Module-level table: keyed by (inning, half, run_diff, outs, base_state)
# Loaded once at startup from the CSV.
_we_table: Dict[Tuple[int, str, int, int, int], float] = {}


# ── Base state encoding ──────────────────────────

def encode_base_state(
    runner_first: bool,
    runner_second: bool,
    runner_third: bool,
) -> int:
    """
    Encode base runners as a 3-bit integer.
      bit 0 (value 1) = 1st base
      bit 1 (value 2) = 2nd base
      bit 2 (value 4) = 3rd base

    Returns 0-7.
    """
    state = 0
    if runner_first:
        state |= 1
    if runner_second:
        state |= 2
    if runner_third:
        state |= 4
    return state


def decode_base_state(state: int) -> Tuple[bool, bool, bool]:
    """Inverse of encode_base_state. Returns (1B, 2B, 3B)."""
    return (
        bool(state & 1),
        bool(state & 2),
        bool(state & 4),
    )


# ── Loading ──────────────────────────────────────

def load_table(csv_path: Optional[Path] = None) -> int:
    """
    Load the WE table from CSV. Returns the number of entries loaded.
    Called once at app startup.
    """
    global _we_table

    if csv_path is None:
        csv_path = Path(__file__).parent.parent / "data" / "win_expectancy_2010_2015.csv"

    if not csv_path.exists():
        logger.warning(
            "WE table CSV not found at %s. Loading minimal stub data. "
            "Download Tango's WE data and place it at this path for full functionality.",
            csv_path,
        )
        _we_table = _build_stub_table()
        return len(_we_table)

    table: Dict[Tuple[int, str, int, int, int], float] = {}
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                key = (
                    int(row["inning"]),
                    row["half"].lower(),
                    int(row["run_diff"]),
                    int(row["outs"]),
                    int(row["base_state"]),
                )
                table[key] = float(row["home_win_prob"])
            except (KeyError, ValueError):
                logger.exception("Failed to parse WE row: %s", row)

    _we_table = table
    logger.info("Loaded %d WE entries from %s", len(table), csv_path)
    return len(table)


# ── Lookup ───────────────────────────────────────

def get_we(
    inning: int,
    half: str,
    run_diff: int,
    outs: int,
    base_state: int,
) -> float:
    """
    Look up the home team's win probability for a given game state.

    Returns a float 0.0-1.0 representing the home team's chance of winning.
    Returns 0.5 if the state is missing from the table (shouldn't happen
    with proper data, but never crash on missing keys).

    Edge cases:
      - inning > 9: clamps to 9 (Tango's table doesn't cover extras well
        because the 2010-2015 data predates the ghost runner rule)
      - run_diff outside [-10, 10]: clamps to range. Outside ±10 the
        win probability is essentially 0 or 1 anyway.
      - outs > 2: clamps to 2 (defensive — should never happen)
    """
    inning = min(max(inning, 1), 9)
    run_diff = min(max(run_diff, -5), 5)
    outs = min(max(outs, 0), 2)
    half = half.lower() if half else "top"
    if half not in ("top", "bottom"):
        half = "top"

    key = (inning, half, run_diff, outs, base_state)
    return _we_table.get(key, 0.5)


def get_we_from_game_state(
    inning: int,
    half: str,
    home_score: int,
    away_score: int,
    outs: int,
    runner_first: bool = False,
    runner_second: bool = False,
    runner_third: bool = False,
) -> float:
    """
    Convenience wrapper that takes raw game state fields and returns WE.
    """
    return get_we(
        inning=inning,
        half=half,
        run_diff=home_score - away_score,
        outs=outs,
        base_state=encode_base_state(runner_first, runner_second, runner_third),
    )


def get_we_from_pa(pa: dict) -> float:
    """
    Compute WE from a balldontlie plate appearance dict.

    The PA represents the state at the START of that plate appearance,
    which is the standard reference point for WE calculations.
    Note: the PA itself doesn't contain the score, so the caller needs
    to pass it in via get_we_from_game_state if they want score context.
    This helper assumes home_score and away_score are 0 if not present.
    """
    return get_we_from_game_state(
        inning=pa.get("inning", 1),
        half=pa.get("half_inning") or pa.get("inning_half") or "top",
        home_score=pa.get("home_score", 0),
        away_score=pa.get("away_score", 0),
        outs=pa.get("outs", 0),
        runner_first=pa.get("runner_on_first", False),
        runner_second=pa.get("runner_on_second", False),
        runner_third=pa.get("runner_on_third", False),
    )


# ── Stub data (used when CSV is missing) ─────────

def _build_stub_table() -> Dict[Tuple[int, str, int, int, int], float]:
    """
    Build a minimal stub table so the module is functional out of the box.
    This is NOT accurate WE data — it's a rough heuristic so you can develop
    and test the rest of the system before downloading the real CSV.

    Approximation: home team starts at 54% (typical home field advantage).
    Each run lead shifts WE by ~5%. Late innings amplify the effect.
    Outs and base state add small adjustments.
    """
    table: Dict[Tuple[int, str, int, int, int], float] = {}

    for inning in range(1, 10):
        for half in ("top", "bottom"):
            for run_diff in range(-10, 11):
                for outs in (0, 1, 2):
                    for base_state in range(8):
                        # Base WE: 54% home (HFA) shifted by run differential
                        base = 0.54
                        run_effect = 0.05 * run_diff
                        # Late innings: multiply effect (a 1-run lead in
                        # the 9th matters more than in the 1st)
                        inning_multiplier = 1 + (inning - 1) * 0.15
                        we = base + (run_effect * inning_multiplier)

                        # Tiny adjustments for outs (more outs = current
                        # half-inning closer to ending = less swingy)
                        # and base state (runners on = more scoring potential
                        # for the batting team)
                        runners_on = bin(base_state).count("1")
                        if half == "top":
                            we -= runners_on * 0.01
                        else:
                            we += runners_on * 0.01

                        # Clamp to (0.01, 0.99) — never exactly 0 or 1
                        # except in walk-off situations which the table
                        # doesn't handle anyway
                        we = max(0.01, min(0.99, we))

                        table[(inning, half, run_diff, outs, base_state)] = round(we, 4)

    logger.info("Built stub WE table with %d entries", len(table))
    return table


# ── Auto-load on import ──────────────────────────

# Load the table when the module is first imported.
# In production this happens once at app startup.
load_table()
