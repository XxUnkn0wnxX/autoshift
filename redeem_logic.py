"""Shared redemption candidate resolution and skip logic."""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from common import _L
import query
from query import ALL_SUPPORTED_GAMES, ALL_SUPPORTED_PLATFORMS, Key
from shift import Status

UTC = _dt.timezone.utc

_CODE_RE = re.compile(r"^[A-Z0-9-]{5}(?:[A-Z0-9-]{5}){4}$", re.IGNORECASE)
_STRIP_RE = re.compile(r"[^A-Z0-9]", re.IGNORECASE)
_ORIGIN_PRIORITY = {"db": 0, "shift": 1, "fallback": 2}
_SUPPORTED_GAME_SET = set(ALL_SUPPORTED_GAMES)
_SUPPORTED_PLATFORM_SET = set(ALL_SUPPORTED_PLATFORMS)


@dataclass
class RedemptionCandidate:
    code: str
    game: str
    platform: str
    reward: str
    origin: str
    key: Key
    source: Optional[str]
    expires_at: Optional[_dt.datetime] = None
    expired_flag: bool = False
    preclassified_status: Optional[str] = None
    skip_reason: Optional[str] = None
    previously_redeemed: bool = False
    previously_redeemed_status: Optional[str] = None
    previously_failed: Optional[str] = None
    failure_detail: Optional[str] = None
    should_record_preclassification: bool = False
    priority: int = field(default=99)


@dataclass
class RedemptionPlan:
    code: str
    normalized_code: str
    requested_platforms: List[str]
    games: List[str]
    candidates: List[RedemptionCandidate]
    attempts: List[RedemptionCandidate]
    skipped: List[RedemptionCandidate]
    db_keys: List[Key]
    source_keys: List[Key]
    reward_hint: str
    db_had_code: bool


def normalize_shift_code(raw: str) -> Optional[str]:
    if not raw:
        return None
    candidate = raw.strip().upper()
    if not _CODE_RE.match(candidate):
        candidate = _STRIP_RE.sub("", candidate)
        if len(candidate) != 25 or not candidate.isalnum():
            return None
    else:
        candidate = _STRIP_RE.sub("", candidate)

    blocks = [candidate[i : i + 5] for i in range(0, 25, 5)]
    return "-".join(blocks)


def normalize_requested_platforms(platforms: Optional[Sequence[str]]) -> List[str]:
    if not platforms:
        return list(ALL_SUPPORTED_PLATFORMS)

    normalized: List[str] = []
    seen: set[str] = set()
    for token in platforms:
        if not token:
            continue
        canonical = _canonical_platform(token)
        if canonical in (None, "manual"):
            continue
        if canonical == "universal":
            for plat in ALL_SUPPORTED_PLATFORMS:
                if plat not in seen:
                    seen.add(plat)
                    normalized.append(plat)
            continue
        if canonical not in _SUPPORTED_PLATFORM_SET:
            _L.debug(f"Ignoring unsupported platform token '{token}' in manual filter")
            continue
        if canonical not in seen:
            seen.add(canonical)
            normalized.append(canonical)

    if not normalized:
        raise ValueError("No supported platforms remain after normalization.")

    ordered = [plat for plat in ALL_SUPPORTED_PLATFORMS if plat in seen]
    for plat in normalized:
        if plat not in ordered:
            ordered.append(plat)
    return ordered


def build_redemption_plan(
    code: str,
    requested_platforms: Sequence[str],
    *,
    bypass_fail: bool = False,
) -> RedemptionPlan:
    normalized_code = normalize_shift_code(code)
    if not normalized_code:
        raise ValueError("Invalid SHiFT code; expected 25 characters after normalization.")

    requested = normalize_requested_platforms(requested_platforms)

    db_keys = query.db.fetch_keys_for_code(normalized_code)
    source_keys = _load_source_matches(normalized_code)

    matched_games = _determine_matched_games(db_keys, source_keys)
    games_to_probe = (
        [game for game in ALL_SUPPORTED_GAMES if game in matched_games]
        if matched_games
        else list(ALL_SUPPORTED_GAMES)
    )

    candidates: Dict[Tuple[str, str], RedemptionCandidate] = {}

    for key in db_keys:
        game = _normalize_game(key.game)
        if not game or game not in matched_games and matched_games:
            continue
        for platform in _expand_platforms(key.platform, requested):
            candidate = _make_candidate(
                normalized_code,
                game,
                platform,
                reward=getattr(key, "reward", None),
                origin="db",
                source=getattr(key, "source", None) or "database",
                expires=getattr(key, "expires", None),
                expired_flag=_normalize_expired_flag(getattr(key, "expired", False)),
            )
            _upsert_candidate(candidates, candidate)

    for key in source_keys:
        game = _normalize_game(key.game)
        if not game or game not in matched_games and matched_games:
            continue
        for platform in _expand_platforms(key.platform, requested):
            candidate = _make_candidate(
                normalized_code,
                game,
                platform,
                reward=getattr(key, "reward", None),
                origin="shift",
                source=getattr(key, "source", None) or "shift_source",
                expires=getattr(key, "expires", None),
                expired_flag=_normalize_expired_flag(getattr(key, "expired", False)),
            )
            _upsert_candidate(candidates, candidate)

    if not candidates:
        for game in games_to_probe:
            for platform in requested:
                candidate = _make_candidate(
                    normalized_code,
                    game,
                    platform,
                    reward="Unknown",
                    origin="fallback",
                    source="fallback",
                    expires=None,
                    expired_flag=False,
                )
                _upsert_candidate(candidates, candidate)

    ordered_candidates = _order_candidates(candidates, games_to_probe)
    attempts, skipped = _apply_skip_logic(ordered_candidates, normalized_code, bypass_fail)

    reward_hint = _determine_reward_hint(ordered_candidates)

    return RedemptionPlan(
        code=code,
        normalized_code=normalized_code,
        requested_platforms=list(requested),
        games=games_to_probe,
        candidates=ordered_candidates,
        attempts=attempts,
        skipped=skipped,
        db_keys=db_keys,
        source_keys=source_keys,
        reward_hint=reward_hint,
        db_had_code=bool(db_keys),
    )


def _determine_reward_hint(candidates: Iterable[RedemptionCandidate]) -> str:
    for candidate in candidates:
        reward = (candidate.reward or "").strip()
        if reward and reward.lower() != "unknown":
            return reward
    return "Unknown"


def _order_candidates(
    candidates: Dict[Tuple[str, str], RedemptionCandidate],
    games_to_probe: Sequence[str],
) -> List[RedemptionCandidate]:
    ordered: List[RedemptionCandidate] = []
    present_games = {game for game, _ in candidates.keys()}
    for game in games_to_probe:
        if game not in present_games:
            continue
        for platform in ALL_SUPPORTED_PLATFORMS:
            candidate = candidates.get((game, platform))
            if candidate:
                ordered.append(candidate)
    return ordered


def _apply_skip_logic(
    candidates: Sequence[RedemptionCandidate],
    normalized_code: str,
    bypass_fail: bool,
) -> Tuple[List[RedemptionCandidate], List[RedemptionCandidate]]:
    redeemed_map, failed_map = query.db.fetch_outcomes_for_code(normalized_code)
    now = _dt.datetime.now(UTC)

    attempts: List[RedemptionCandidate] = []
    skipped: List[RedemptionCandidate] = []

    for candidate in candidates:
        pair = (candidate.game, candidate.platform)

        failure = failed_map.get(pair)
        if failure:
            candidate.previously_failed = failure.get("status")
            candidate.failure_detail = failure.get("detail")

        success = redeemed_map.get(pair)
        if success:
            candidate.previously_redeemed = True
            candidate.previously_redeemed_status = success.get("status")
            candidate.skip_reason = "redeemed"
            skipped.append(candidate)
            continue

        expired_now = candidate.expired_flag or (
            candidate.expires_at is not None and candidate.expires_at <= now
        )
        if expired_now:
            candidate.preclassified_status = "EXPIRED"
            if not bypass_fail:
                candidate.skip_reason = "expired"
                candidate.should_record_preclassification = not failure
                skipped.append(candidate)
                continue
            candidate.should_record_preclassification = False

        if failure and not bypass_fail:
            candidate.skip_reason = "failed"
            skipped.append(candidate)
            continue

        candidate.skip_reason = None
        attempts.append(candidate)

    return attempts, skipped


def _upsert_candidate(
    candidates: Dict[Tuple[str, str], RedemptionCandidate],
    candidate: RedemptionCandidate,
) -> None:
    key = (candidate.game, candidate.platform)
    existing = candidates.get(key)
    if not existing or candidate.priority < existing.priority:
        candidates[key] = candidate
        return
    if candidate.priority > existing.priority:
        return

    if existing.reward.strip().lower() == "unknown" and candidate.reward.strip():
        existing.reward = candidate.reward

    if existing.expires_at is None and candidate.expires_at is not None:
        existing.expires_at = candidate.expires_at
        existing.expired_flag = candidate.expired_flag

    if existing.preclassified_status is None and candidate.preclassified_status is not None:
        existing.preclassified_status = candidate.preclassified_status

    if existing.source in (None, "fallback") and candidate.source:
        existing.source = candidate.source


def _make_candidate(
    code: str,
    game: str,
    platform: str,
    *,
    reward: Optional[str],
    origin: str,
    source: Optional[str],
    expires: Optional[str],
    expired_flag: bool,
) -> RedemptionCandidate:
    reward_value = (reward or "").strip() or "Unknown"
    key_obj = query.db.ensure_key(
        code=code,
        game=game,
        platform=platform,
        reward=reward_value,
        source=source,
    )
    expires_at = _parse_expiry(expires)
    return RedemptionCandidate(
        code=code,
        game=game,
        platform=platform,
        reward=reward_value,
        origin=origin,
        key=key_obj,
        source=source,
        expires_at=expires_at,
        expired_flag=expired_flag,
        priority=_ORIGIN_PRIORITY.get(origin, 99),
    )


def _determine_matched_games(db_keys: Iterable[Key], source_keys: Iterable[Key]) -> set[str]:
    games: set[str] = set()
    for key in db_keys:
        game = _normalize_game(key.game)
        if game:
            games.add(game)
    for key in source_keys:
        game = _normalize_game(key.game)
        if game:
            games.add(game)
    return games


def _normalize_game(game: Optional[str]) -> Optional[str]:
    if not game:
        return None
    game_clean = str(game).strip()
    if not game_clean:
        return None
    short = game_clean
    if game_clean not in _SUPPORTED_GAME_SET:
        if game_clean in query.known_games.inv:
            short = query.known_games.inv[game_clean]
        elif game_clean in query.known_games:
            short = game_clean
        else:
            return None
    return short if short in _SUPPORTED_GAME_SET else None


def _expand_platforms(platform: Optional[str], requested: Sequence[str]) -> List[str]:
    canonical = _canonical_platform(platform)
    if canonical in (None, "manual", "universal"):
        return list(requested)
    return [canonical] if canonical in requested else []


def _canonical_platform(platform: Optional[str]) -> Optional[str]:
    if platform is None:
        return None
    raw = str(platform).strip()
    if not raw:
        return None
    lowered = raw.lower()
    if lowered in ("manual", "universal"):
        return lowered
    if lowered in _SUPPORTED_PLATFORM_SET:
        return lowered
    try:
        return query.get_short_platform_key(raw)
    except Exception:
        return None


def _normalize_expired_flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    return False


def _parse_expiry(value: Optional[str]) -> Optional[_dt.datetime]:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None

    text = text.replace("Z", "+00:00")
    try:
        dt_obj = _dt.datetime.fromisoformat(text)
        if dt_obj.tzinfo is None:
            dt_obj = dt_obj.replace(tzinfo=UTC)
        else:
            dt_obj = dt_obj.astimezone(UTC)
        return dt_obj
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt_obj = _dt.datetime.strptime(text, fmt)
            return dt_obj.replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


@lru_cache(maxsize=1)
def _get_shift_dataset() -> Tuple[Key, ...]:
    keys_iter = query.parse_shift_orcicorn()
    if keys_iter is None:
        return ()
    return tuple(key.copy() for key in keys_iter)


def _load_source_matches(normalized_code: str) -> List[Key]:
    matches: List[Key] = []
    for key in _get_shift_dataset():
        candidate = normalize_shift_code(key.code)
        if candidate and candidate == normalized_code:
            matches.append(key.copy())
    return matches


__all__ = [
    "RedemptionCandidate",
    "RedemptionPlan",
    "build_redemption_plan",
    "normalize_shift_code",
    "normalize_requested_platforms",
    "format_status_detail",
]


def format_status_detail(status: Status, key: Key) -> str:
    """Render a Status message with key metadata, falling back gracefully."""

    detail = getattr(status, "msg", str(status))

    # First try the common {key.reward}/{key.code} placeholders.
    try:
        return detail.format(key=key)
    except Exception:
        pass

    # Some dynamically constructed Status objects may store the rendered string.
    try:
        return str(detail.format())  # type: ignore[arg-type]
    except Exception:
        return str(detail)
