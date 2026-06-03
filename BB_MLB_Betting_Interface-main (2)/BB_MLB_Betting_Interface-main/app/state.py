"""
Pure state transitions for BarBoards game state.

No I/O. No async. No imports from app modules. This file is testable in
isolation — `replay_state.py` runs every event from a webhook log through
these functions and prints state evolution.

The webhook handler (webhooks.py) is responsible for:
  - Receiving the HTTP request
  - Ordering check (received_at >= last_applied_time)
  - Calling the right transition function from this module
  - Updating GameState.last_applied_time
  - Broadcasting the result

This module does the rest: figuring out new bases, outs, lineup index,
pinch detection, and so on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Optional, Dict, List, Set, Tuple

logger = logging.getLogger("barboards.state")

# Feature flag — flip to False if pinch runner detection misbehaves in practice
PINCH_RUNNER_DETECTION = True


# ── Data classes ──────────────────────────────────────────────────

@dataclass
class Runner:
    """A player on a base. We store id and last_name so we can detect pinch
    runners by id-comparison across snapshots."""
    id: int
    last_name: str

    def to_dict(self) -> dict:
        return {"id": self.id, "last_name": self.last_name}


@dataclass
class Bases:
    first: Optional[Runner] = None
    second: Optional[Runner] = None
    third: Optional[Runner] = None

    def to_dict(self) -> dict:
        return {
            "first": self.first.to_dict() if self.first else None,
            "second": self.second.to_dict() if self.second else None,
            "third": self.third.to_dict() if self.third else None,
        }

    def ids(self) -> Set[int]:
        return {r.id for r in (self.first, self.second, self.third) if r is not None}

    def empty(self) -> bool:
        return self.first is None and self.second is None and self.third is None


@dataclass
class GameState:
    inning: int = 1
    half: str = "top"
    outs: int = 0
    bases: Bases = field(default_factory=Bases)
    away_score: int = 0
    home_score: int = 0
    current_pitcher_id_away: Optional[int] = None
    current_pitcher_id_home: Optional[int] = None
    displayed_pitcher_id: Optional[int] = None
    away_lineup_idx: int = 0
    home_lineup_idx: int = 0
    computing: bool = False
    synced: bool = False
    score_anchored: bool = False    # true once score has a real reference point
    game_over: bool = False
    last_applied_time: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "inning": self.inning,
            "half": self.half,
            "outs": self.outs,
            "bases": self.bases.to_dict(),
            "away_score": self.away_score,
            "home_score": self.home_score,
            "current_pitcher_id_away": self.current_pitcher_id_away,
            "current_pitcher_id_home": self.current_pitcher_id_home,
            "displayed_pitcher_id": self.displayed_pitcher_id,
            "away_lineup_idx": self.away_lineup_idx,
            "home_lineup_idx": self.home_lineup_idx,
            "computing": self.computing,
            "synced": self.synced,
            "score_anchored": self.score_anchored,
            "game_over": self.game_over,
        }


@dataclass
class LineupContext:
    """Mutable lineup data the webhook handler keeps per game.

    Pinch hitters and runners mutate `home_lineup` / `away_lineup` in-place
    by replacing entries at specific batting-order slots.

    `roster` is a fallback id→name map from the broader team rosters
    (bench, bullpen) — used when an ID isn't in the active lineup yet.
    """
    home_team_id: int
    away_team_id: int
    home_lineup: List[dict]  # list of 9 {id, last_name, first_name, batting_order}
    away_lineup: List[dict]
    home_id_to_slot: Dict[int, int]   # player_id -> 0..8 slot index
    away_id_to_slot: Dict[int, int]
    roster: Dict[int, str]            # all known player ids -> last_name (lineup + bench)


# ── Event categorization ──────────────────────────────────────────

PRE_PLAY_EVENTS = {
    "mlb.batter.hit",
    "mlb.batter.home_run",
    "mlb.batter.hit_by_pitch",
}

POST_PLAY_BATTER_EVENTS = {
    "mlb.batter.strikeout",
    "mlb.batter.walk",
    "mlb.batter.groundout",
    "mlb.batter.flyout",
    "mlb.batter.lineout",
    "mlb.batter.popout",
    "mlb.batter.foulout",
    "mlb.batter.sacrifice_fly",
    "mlb.batter.sacrifice",
    "mlb.batter.fielders_choice",
    "mlb.batter.reached_on_error",
}

# Batter is retired (out delta = +1, plus DP/TP detection from text)
BATTER_OUT_EVENTS = {
    "mlb.batter.strikeout",
    "mlb.batter.groundout",
    "mlb.batter.flyout",
    "mlb.batter.lineout",
    "mlb.batter.popout",
    "mlb.batter.foulout",
    "mlb.batter.sacrifice_fly",
    "mlb.batter.sacrifice",
    "mlb.batter.fielders_choice",
}

ALL_BATTER_EVENTS = PRE_PLAY_EVENTS | POST_PLAY_BATTER_EVENTS

SYNC_EVENTS = {
    "mlb.game.started",
    "mlb.game.inning_half_ended",
    "mlb.game.inning_ended",
}


# ── Hits rule table (52 of 60 real-world hit configurations) ─────

# Key: (play_type, has_1B, has_2B, has_3B, num_scored)
# Value: (slot_1B, slot_2B, slot_3B) where each slot is "B" (batter), "1"/"2"/"3"
#        (the prev runner from that base), or None (empty).
_HITS_TABLE: Dict[Tuple[str, bool, bool, bool, int], Tuple] = {
    # Singles
    ("single", False, False, False, 0): ("B", None, None),
    ("single", True,  False, False, 1): ("B", None, None),
    ("single", False, True,  False, 1): ("B", None, None),
    ("single", True,  True,  False, 0): ("B", "1",  "2"),
    ("single", True,  True,  False, 2): ("B", None, None),
    ("single", False, False, True,  0): ("B", None, "3"),
    ("single", False, False, True,  1): ("B", None, None),
    ("single", True,  False, True,  0): ("B", "1",  "3"),
    ("single", True,  False, True,  2): ("B", None, None),
    ("single", False, True,  True,  0): ("B", "2",  "3"),
    ("single", False, True,  True,  2): ("B", None, None),
    ("single", True,  True,  True,  1): ("B", "1",  "2"),
    ("single", True,  True,  True,  3): ("B", None, None),

    # Doubles
    ("double", False, False, False, 0): (None, "B", None),
    ("double", True,  False, False, 0): (None, "B", "1"),
    ("double", True,  False, False, 1): (None, "B", None),
    ("double", False, True,  False, 0): (None, "B", "2"),
    ("double", False, True,  False, 1): (None, "B", None),
    ("double", True,  True,  False, 1): (None, "B", "1"),
    ("double", True,  True,  False, 2): (None, "B", None),
    ("double", False, False, True,  0): (None, "B", "3"),
    ("double", False, False, True,  1): (None, "B", None),
    ("double", True,  False, True,  1): (None, "B", "1"),
    ("double", True,  False, True,  2): (None, "B", None),
    ("double", False, True,  True,  1): (None, "B", "2"),
    ("double", False, True,  True,  2): (None, "B", None),
    ("double", True,  True,  True,  2): (None, "B", "1"),
    ("double", True,  True,  True,  3): (None, "B", None),

    # Triples
    ("triple", False, False, False, 0): (None, None, "B"),
    ("triple", True,  False, False, 0): (None, "1",  "B"),
    ("triple", True,  False, False, 1): (None, None, "B"),
    ("triple", False, True,  False, 1): (None, None, "B"),
    ("triple", True,  True,  False, 1): (None, "1",  "B"),
    ("triple", True,  True,  False, 2): (None, None, "B"),
    ("triple", False, False, True,  1): (None, None, "B"),
    ("triple", True,  False, True,  1): (None, "1",  "B"),
    ("triple", True,  False, True,  2): (None, None, "B"),
    ("triple", False, True,  True,  2): (None, None, "B"),
    ("triple", True,  True,  True,  2): (None, "1",  "B"),
    ("triple", True,  True,  True,  3): (None, None, "B"),
}


def _resolve_hit(
    play_type: str,
    pre: Bases,
    runs_scored: int,
    batter: Runner,
) -> Optional[Bases]:
    """Look up the hit in the rule table. Returns None if ambiguous."""
    pt = (play_type or "").lower().strip()
    if pt not in ("single", "double", "triple"):
        return None

    key = (pt, pre.first is not None, pre.second is not None, pre.third is not None, runs_scored)
    mapping = _HITS_TABLE.get(key)
    if mapping is None:
        return None  # ambiguous

    sources = {"B": batter, "1": pre.first, "2": pre.second, "3": pre.third, None: None}
    return Bases(
        first=sources[mapping[0]],
        second=sources[mapping[1]],
        third=sources[mapping[2]],
    )


def _hbp_advance(pre: Bases, batter: Runner) -> Bases:
    """HBP: batter to 1B; runners advance only if forced."""
    new = Bases(first=pre.first, second=pre.second, third=pre.third)
    if new.first is None:
        new.first = batter
    elif new.second is None:
        new.second, new.first = new.first, batter
    elif new.third is None:
        new.third, new.second, new.first = new.second, new.first, batter
    else:
        # Bases loaded — 3B scores, everyone shifts
        new.third, new.second, new.first = new.second, new.first, batter
    return new


# ── Snapshot helpers ──────────────────────────────────────────────

def _resolve_runner(
    player_id: Optional[int],
    ctx: LineupContext,
) -> Optional[Runner]:
    """Resolve a player ID from a webhook snapshot into a Runner.
    Lookup order: combined lineups, then roster, then fallback to 'P{id}'."""
    if player_id is None:
        return None
    # Try the active lineups first
    for lineup in (ctx.home_lineup, ctx.away_lineup):
        for entry in lineup:
            if entry.get("id") == player_id:
                return Runner(id=player_id, last_name=entry.get("last_name") or f"P{player_id}")
    # Roster fallback (bench, bullpen)
    name = ctx.roster.get(player_id)
    if name:
        return Runner(id=player_id, last_name=name)
    # Last resort
    return Runner(id=player_id, last_name=f"P{player_id}")


def _bases_from_snapshot(snapshot: dict, ctx: LineupContext) -> Bases:
    """Build a Bases from the play.runners object."""
    if not snapshot:
        return Bases()
    return Bases(
        first=_resolve_runner(snapshot.get("on_first"), ctx),
        second=_resolve_runner(snapshot.get("on_second"), ctx),
        third=_resolve_runner(snapshot.get("on_third"), ctx),
    )


# ── Pinch detection ───────────────────────────────────────────────

@dataclass
class PinchEvent:
    """Logged whenever pinch detection fires (or fails)."""
    kind: str           # "detected" | "fallthrough"
    new_id: int
    replaced_id: Optional[int]
    slot: Optional[int]
    team_side: Optional[str]
    reason: str
    prev_ids: Set[int] = field(default_factory=set)
    new_ids: Set[int] = field(default_factory=set)


def detect_pinch_runners(
    prev_bases: Bases,
    new_snapshot: dict,
    batter_id: Optional[int],
    ctx: LineupContext,
) -> List[PinchEvent]:
    """Compare prev bases to a new runners snapshot and detect pinch runners.

    Mutates ctx.home_lineup / away_lineup / home_id_to_slot / away_id_to_slot
    in place when a pinch runner is unambiguously attributable.

    Returns log entries describing what happened (detected or fell through).
    """
    if not PINCH_RUNNER_DETECTION:
        return []

    events: List[PinchEvent] = []
    if not new_snapshot:
        return events

    new_ids = {
        new_snapshot.get(k) for k in ("on_first", "on_second", "on_third")
        if new_snapshot.get(k) is not None
    }
    prev_ids = prev_bases.ids()

    appeared = new_ids - prev_ids
    disappeared = prev_ids - new_ids

    for new_id in appeared:
        if new_id == batter_id:
            continue
        if new_id in ctx.home_id_to_slot or new_id in ctx.away_id_to_slot:
            continue

        # Unknown ID. Pinch runner candidate.
        if len(disappeared) != 1:
            events.append(PinchEvent(
                kind="fallthrough",
                new_id=new_id,
                replaced_id=None,
                slot=None,
                team_side=None,
                reason=f"ambiguous: {len(disappeared)} prev runners disappeared",
                prev_ids=prev_ids,
                new_ids=new_ids,
            ))
            continue

        replaced_id = next(iter(disappeared))

        # Find which team's lineup the replaced player was in
        if replaced_id in ctx.home_id_to_slot:
            slot = ctx.home_id_to_slot[replaced_id]
            lineup = ctx.home_lineup
            id_to_slot = ctx.home_id_to_slot
            team_side = "home"
        elif replaced_id in ctx.away_id_to_slot:
            slot = ctx.away_id_to_slot[replaced_id]
            lineup = ctx.away_lineup
            id_to_slot = ctx.away_id_to_slot
            team_side = "away"
        else:
            events.append(PinchEvent(
                kind="fallthrough",
                new_id=new_id,
                replaced_id=replaced_id,
                slot=None,
                team_side=None,
                reason="replaced player not in either lineup",
                prev_ids=prev_ids,
                new_ids=new_ids,
            ))
            continue

        pinch_name = ctx.roster.get(new_id) or f"P{new_id}"
        # Replace the entry at that slot, preserving slot-level fields
        lineup[slot] = {
            "id": new_id,
            "last_name": pinch_name,
            "first_name": lineup[slot].get("first_name", ""),
            "batting_order": slot + 1,
            "team_id": lineup[slot].get("team_id"),
            "is_pinch": True,
        }
        del id_to_slot[replaced_id]
        id_to_slot[new_id] = slot

        events.append(PinchEvent(
            kind="detected",
            new_id=new_id,
            replaced_id=replaced_id,
            slot=slot,
            team_side=team_side,
            reason="single replacement, attributed",
            prev_ids=prev_ids,
            new_ids=new_ids,
        ))

    return events


# ── Lineup index advancement ──────────────────────────────────────

def advance_lineup_idx(
    state: GameState,
    batter_id: int,
    batter_last_name: str,
    batter_first_name: str,
    batter_team_id: int,
    inning_half: str,
    ctx: LineupContext,
) -> None:
    """Advance the appropriate team's lineup index. Replaces the lineup slot
    with the batter if they're a pinch hitter.

    Mutates state.{home,away}_lineup_idx and ctx.{home,away}_lineup in place.
    """
    team_side = "away" if inning_half == "top" else "home"
    if team_side == "away":
        lineup = ctx.away_lineup
        id_to_slot = ctx.away_id_to_slot
        cur_idx = state.away_lineup_idx
    else:
        lineup = ctx.home_lineup
        id_to_slot = ctx.home_id_to_slot
        cur_idx = state.home_lineup_idx

    if batter_id in id_to_slot:
        # Resync index based on actual lineup position — handles drift
        slot = id_to_slot[batter_id]
        new_idx = (slot + 1) % 9
    else:
        # Pinch hitter — they take the current expected slot
        slot = cur_idx
        lineup[slot] = {
            "id": batter_id,
            "last_name": batter_last_name,
            "first_name": batter_first_name,
            "batting_order": slot + 1,
            "team_id": batter_team_id,
            "is_pinch": True,
        }
        id_to_slot[batter_id] = slot
        new_idx = (slot + 1) % 9

    if team_side == "away":
        state.away_lineup_idx = new_idx
    else:
        state.home_lineup_idx = new_idx


# ── Score handling (max-merge) ────────────────────────────────────

def merge_score(state: GameState, payload_home: Optional[int], payload_away: Optional[int]) -> bool:
    """Returns True if score changed."""
    changed = False
    if payload_home is not None and payload_home > state.home_score:
        state.home_score = payload_home
        changed = True
    if payload_away is not None and payload_away > state.away_score:
        state.away_score = payload_away
        changed = True
    return changed


def _inning_matches(state: GameState, play: dict) -> bool:
    """Return True if this play event's inning/half matches current state.
    Used to drop stale events that BDL replays from earlier in the game.
    Only meaningful post-sync — caller must check state.synced first."""
    play_inning = play.get("inning")
    play_half = play.get("inning_half")
    if play_inning is None or play_half is None:
        # Missing fields — can't validate, accept conservatively
        return True
    return play_inning == state.inning and play_half == state.half


# ── Boundary handling ─────────────────────────────────────────────

def apply_boundary(
    state: GameState,
    event_type: str,
    payload: dict,
) -> None:
    """Apply a sync (boundary) event in place. Idempotent: if state already
    matches the target, no-op."""
    inning = payload.get("inning")
    inning_half = payload.get("inning_half")

    if event_type == "mlb.game.started":
        target_inning, target_half = 1, "top"
    elif event_type == "mlb.game.inning_half_ended":
        if inning_half == "top":
            target_inning, target_half = (inning or state.inning), "bottom"
        elif inning_half == "bottom":
            target_inning, target_half = ((inning or state.inning) + 1), "top"
        else:
            # Missing/unexpected — do nothing
            return
    elif event_type == "mlb.game.inning_ended":
        target_inning, target_half = ((inning or state.inning) + 1), "top"
    else:
        return

    # Idempotent guard: if we're already at or past the target, no-op
    cur_pos = (state.inning, 0 if state.half == "top" else 1)
    target_pos = (target_inning, 0 if target_half == "top" else 1)
    if cur_pos >= target_pos:
        # Already advanced. Don't reset bases/outs/computing again.
        return

    state.inning = target_inning
    state.half = target_half
    state.outs = 0
    state.bases = Bases()
    state.computing = False

    # Swap displayed pitcher to whichever team is now pitching.
    # If half is "top", the away team is batting → home team pitches.
    state.displayed_pitcher_id = (
        state.current_pitcher_id_home if state.half == "top"
        else state.current_pitcher_id_away
    )


# ── Main per-event entry points ───────────────────────────────────

@dataclass
class TransitionResult:
    """Result of applying a single webhook event to state."""
    state_changed: bool
    score_changed: bool
    pitcher_changed: bool
    pinch_events: List[PinchEvent] = field(default_factory=list)
    note: str = ""


def apply_event(
    state: GameState,
    event_type: str,
    payload: dict,
    ctx: LineupContext,
) -> TransitionResult:
    """Entry point: apply one webhook event to GameState in place.

    Caller is responsible for the ordering check (received_at >= last_applied_time)
    and for updating last_applied_time afterward.
    """

    # ── Game ended ────────────────────────────────
    if event_type == "mlb.game.ended":
        state.game_over = True
        return TransitionResult(state_changed=True, score_changed=False, pitcher_changed=False,
                                note="game_ended")

    # ── Sync events ───────────────────────────────
    if event_type in SYNC_EVENTS:
        prev_pos = (state.inning, 0 if state.half == "top" else 1)
        apply_boundary(state, event_type, payload)
        new_pos = (state.inning, 0 if state.half == "top" else 1)
        was_synced = state.synced
        if not state.synced:
            state.synced = True
        return TransitionResult(
            state_changed=(new_pos != prev_pos) or not was_synced,
            score_changed=False,
            pitcher_changed=False,
            note=f"boundary {event_type}",
        )

    # ── team.scored: score-only ───────────────────
    if event_type == "mlb.team.scored":
        play = payload.get("play") or {}
        # Stale-by-inning check: if this scored event is from a different
        # inning than current, ignore. Max-merge would protect the score
        # but other consumers may want stale events flagged.
        if state.synced and not _inning_matches(state, play):
            return TransitionResult(
                state_changed=False, score_changed=False, pitcher_changed=False,
                note=f"team.scored stale-by-inning (event={play.get('inning')}{play.get('inning_half')}, state={state.inning}{state.half})",
            )
        score_changed = merge_score(state, play.get("home_score"), play.get("away_score"))
        return TransitionResult(state_changed=score_changed, score_changed=score_changed,
                                pitcher_changed=False, note="team.scored (score only)")

    # ── Batter events: only valid post-sync ───────
    if event_type not in ALL_BATTER_EVENTS:
        return TransitionResult(state_changed=False, score_changed=False, pitcher_changed=False,
                                note=f"unknown event {event_type}")

    if not state.synced:
        # Pre-sync batter events still trigger odds/props refresh upstream,
        # but we don't apply state changes.
        return TransitionResult(state_changed=False, score_changed=False, pitcher_changed=False,
                                note="pre-sync batter event ignored for state")

    play = payload.get("play") or {}

    # Stale-by-inning check: a batter event whose inning/half doesn't match
    # our current state is a stale replay (BDL re-fires old events for hours
    # after the fact). Dropping these is critical — applying a stale event
    # would corrupt bases, outs, and pitcher.
    if not _inning_matches(state, play):
        return TransitionResult(
            state_changed=False, score_changed=False, pitcher_changed=False,
            note=f"batter event stale-by-inning (event={play.get('inning')}{play.get('inning_half')}, state={state.inning}{state.half})",
        )

    batter = payload.get("batter") or {}
    pitcher = payload.get("pitcher") or {}
    snapshot = play.get("runners") or {}

    batter_id = batter.get("id")
    batter_last = batter.get("last_name") or ""
    batter_first = batter.get("first_name") or ""
    batter_team_id = batter.get("team_id")

    # Score: max-merge from the payload. Score semantics confirmed from log
    # analysis: home_score/away_score are POST-PLAY on all event types
    # (including hit/HR/HBP). The runners snapshot is pre-play on hit/HR/HBP
    # but the score is already updated.
    #
    # For runs_this_play (used by the hits rule table):
    #   - score_value is authoritative when present (HR, team.scored)
    #   - score_value is null on hits (Singles/Doubles/Triples) — fall back
    #     to score-delta against pre-merge cached totals, but ONLY when
    #     score was already anchored before this event arrived (otherwise
    #     prev_total may be 0-0 from a cold start with no reliable reference)
    #   - Cap at 4 (max possible runs on a single play: bases loaded + batter
    #     scoring) as a defensive guard against bad payloads
    #
    # score_anchored becomes True via:
    #   1. seeding.anchor_score() succeeding at first sync (preferred)
    #   2. ANY accepted event carrying score fields (after that point our
    #      cached score is definitionally fresh)
    was_anchored = state.score_anchored
    prev_total = state.home_score + state.away_score
    score_changed = merge_score(state, play.get("home_score"), play.get("away_score"))
    new_total = state.home_score + state.away_score

    # Mark score as anchored once we've applied a real event with score data
    if play.get("home_score") is not None or play.get("away_score") is not None:
        state.score_anchored = True

    sv = play.get("score_value")
    if sv is not None:
        runs_this_play = sv
    elif was_anchored:
        runs_this_play = min(4, max(0, new_total - prev_total))
    else:
        runs_this_play = 0

    # Pitcher: update for the team that's currently pitching
    pitcher_changed = False
    pitcher_id = pitcher.get("id")
    inning_half = play.get("inning_half") or "top"
    if pitcher_id:
        pitching_side = "home" if inning_half == "top" else "away"
        if pitching_side == "home":
            if state.current_pitcher_id_home != pitcher_id:
                pitcher_changed = state.current_pitcher_id_home is not None
                state.current_pitcher_id_home = pitcher_id
        else:
            if state.current_pitcher_id_away != pitcher_id:
                pitcher_changed = state.current_pitcher_id_away is not None
                state.current_pitcher_id_away = pitcher_id
        state.displayed_pitcher_id = pitcher_id

    # Pinch runner detection (compare prev bases vs incoming snapshot)
    pinch_events = detect_pinch_runners(state.bases, snapshot, batter_id, ctx)

    # Now apply the actual state transition based on event type
    batter_runner = Runner(id=batter_id, last_name=batter_last) if batter_id else None

    if event_type == "mlb.batter.home_run":
        state.bases = Bases()
        state.computing = False
    elif event_type == "mlb.batter.hit_by_pitch":
        if batter_runner:
            # HBP snapshot is pre-play; advance from the snapshot, not from cached state.
            pre = _bases_from_snapshot(snapshot, ctx)
            state.bases = _hbp_advance(pre, batter_runner)
        state.computing = False
    elif event_type == "mlb.batter.hit":
        play_type = (play.get("type") or "").lower().strip()
        if play_type == "home run":
            state.bases = Bases()
            state.computing = False
        else:
            pre = _bases_from_snapshot(snapshot, ctx)
            resolved = _resolve_hit(play_type or "single", pre, runs_this_play, batter_runner)
            if resolved is not None:
                state.bases = resolved
                state.computing = False
            else:
                # Ambiguous: place batter on 1B, leave pre-runners; flag for UI
                state.bases = Bases(first=batter_runner, second=pre.second, third=pre.third)
                state.computing = True
    else:  # POST_PLAY_BATTER_EVENTS
        # Trust the snapshot
        state.bases = _bases_from_snapshot(snapshot, ctx)
        state.computing = False  # post-play snapshot is authoritative; clear any lingering chip
        if event_type in BATTER_OUT_EVENTS:
            state.outs += 1
            text = (play.get("text") or "").lower()
            type_str = (play.get("type") or "").lower()
            if "double play" in text or "double play" in type_str:
                state.outs += 1
            if "triple play" in text or "triple play" in type_str:
                state.outs += 2

        # Defensive rollover (boundary webhook should also fire and is authoritative)
        if state.outs >= 3:
            state.outs = 0
            state.bases = Bases()

    # Lineup index advancement
    if batter_id and batter_team_id:
        advance_lineup_idx(
            state, batter_id, batter_last, batter_first, batter_team_id, inning_half, ctx,
        )

    return TransitionResult(
        state_changed=True,
        score_changed=score_changed,
        pitcher_changed=pitcher_changed,
        pinch_events=pinch_events,
        note=f"applied {event_type}",
    )
