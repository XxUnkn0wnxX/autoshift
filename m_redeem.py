"""Manual single-code redeem helpers."""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from time import sleep
from typing import Iterable, List, Optional, Sequence

from common import _L
import query
from query import Key
from shift import Status

# DEV NOTE: We intentionally keep the regex lean so we can reuse it for both validation and
# normalization. Hyphens, whitespace, and other separators are stripped before the final length check.
_CODE_RE = re.compile(r"^[A-Z0-9-]{5}(?:[A-Z0-9-]{5}){4}$", re.IGNORECASE)
_STRIP_RE = re.compile(r"[^A-Z0-9]", re.IGNORECASE)


# DEV NOTE: Treat both SUCCESS and REDEEMED as "positive" outcomes so summary/exit semantics match user expectations.
def _is_positive_status(status: Status) -> bool:
    return status in (Status.SUCCESS, Status.REDEEMED)


class ManualRedeemUsageError(Exception):
    """Raised when --redeem arguments cannot be handled in manual mode."""


@dataclass
class ManualContext:
    code: str
    resolved_game: Optional[str]
    reward: str
    games_to_probe: Sequence[str]
    platforms_to_probe: Sequence[str]
    scheduled: bool
    platform_filter: Optional[Sequence[str]]


# DEV NOTE: Public entry point -------------------------------------------------
def maybe_handle_manual_redeem(args, shift_client, redeem_cb) -> bool:
    """Detect and run manual single-code redemption flow.

    Returns True when manual mode handled the run (even if it ultimately exits).
    Returns False when manual mode is not applicable so auto.py can continue.
    """

    try:
        manual_request = _extract_manual_request(args)
    except ManualRedeemUsageError as exc:
        _L.error(str(exc))
        sys.exit(2)

    if not manual_request:
        return False

    normalized_code, platform_filter = manual_request

    # DEV NOTE: Manual mode owns --redeem once we get here; log ignored flags pre-emptively so users are aware.
    _log_ignored_flags(args)

    # DEV NOTE: Build execution context (code metadata, game/platform lists, schedule flag) in a dedicated helper.
    context = _build_manual_context(
        normalized_code, bool(getattr(args, "schedule", None)), platform_filter
    )

    platform_note = (
        f" (filtered: {', '.join(context.platforms_to_probe)})"
        if context.platform_filter
        else ""
    )
    _L.info(
        f"Manual redeem mode: {context.code} -> game={context.resolved_game or 'manual'}; "
        f"probing {len(context.games_to_probe)} game(s) across {len(context.platforms_to_probe)} platform(s){platform_note}."
    )

    # DEV NOTE: Kick off the redeem loop and capture per-attempt statuses for summary + exit decisions.
    results = _redeem_across_targets(context, shift_client, redeem_cb)

    any_success = any(_is_positive_status(result.status) for result in results)
    hit_try_later = any(result.status == Status.TRYLATER for result in results)

    _summarize_results(context, results, any_success, hit_try_later)

    if context.scheduled:
        # DEV NOTE: Scheduler callers must retain control; we therefore never sys.exit() on scheduled runs.
        return True

    if hit_try_later and not any_success:
        _L.warning("Manual redeem ended early due to TRY LATER without any successful redemption.")

    sys.exit(0 if any_success else 1)


# DEV NOTE: Detection & normalization helpers ----------------------------------
def _extract_manual_request(args) -> Optional[tuple[str, Optional[Sequence[str]]]]:
    """Return (normalized_code, platform_filter) for manual mode, otherwise None."""

    entries = [entry.strip() for entry in (getattr(args, "redeem", None) or []) if entry and entry.strip()]
    if not entries:
        return None

    # Manual mode only triggers when exactly one entry looks like a SHiFT code.
    if len(entries) != 1:
        manual_like = [entry for entry in entries if _looks_like_shift_code(entry.split(":", 1)[0])]
        if manual_like:
            raise ManualRedeemUsageError("Manual --redeem expects exactly one SHiFT code argument.")
        return None

    entry = entries[0]
    if ":" in entry:
        code_part, platform_part = entry.split(":", 1)
        if not _looks_like_shift_code(code_part):
            # Probably mapping mode (game:platform); let the caller continue normally.
            return None
        normalized = _normalize_shift_code(code_part)
        if not normalized:
            raise ManualRedeemUsageError(
                "Manual --redeem requires a 25-character SHiFT code (5 blocks of 5 letters/numbers)."
            )
        platform_filter = _normalize_manual_platforms(platform_part)
        return normalized, platform_filter

    if not _looks_like_shift_code(entry):
        return None

    normalized = _normalize_shift_code(entry)
    if not normalized:
        raise ManualRedeemUsageError(
            "Manual --redeem requires a 25-character SHiFT code (5 blocks of 5 letters/numbers)."
        )

    return normalized, None


def _looks_like_shift_code(raw: str) -> bool:
    stripped = _STRIP_RE.sub("", raw)
    return len(stripped) == 25 and stripped.isalnum()


def _normalize_shift_code(raw: str) -> Optional[str]:
    candidate = raw.strip().upper()
    if not _CODE_RE.match(candidate):
        candidate = _STRIP_RE.sub("", candidate)
        if len(candidate) != 25 or not candidate.isalnum():
            return None
    else:
        candidate = _STRIP_RE.sub("", candidate)

    blocks = [candidate[i : i + 5] for i in range(0, 25, 5)]
    return "-".join(blocks)


def _normalize_manual_platforms(raw: str) -> Sequence[str]:
    tokens = [token.strip() for token in raw.split(",") if token.strip()]
    if not tokens:
        raise ManualRedeemUsageError("Manual --redeem with :platform requires at least one platform.")

    normalized: list[str] = []
    for token in tokens:
        plat = _normalize_platform_token(token)
        if plat == "universal":
            raise ManualRedeemUsageError("Manual --redeem does not support the 'universal' pseudo-platform.")
        if plat not in normalized:
            normalized.append(plat)
    return normalized


def _normalize_platform_token(token: str) -> str:
    token_lower = token.strip().lower()
    if token_lower in query.known_platforms:
        return token_lower
    inverted = getattr(query.known_platforms, "inv", {})
    if token_lower in inverted:
        return inverted[token_lower]
    raise ManualRedeemUsageError(f"Unknown platform '{token}' for manual --redeem.")


# DEV NOTE: Context construction ------------------------------------------------
def _build_manual_context(
    code: str,
    scheduled: bool,
    platform_filter: Optional[Sequence[str]],
) -> ManualContext:
    reward, resolved_game = _resolve_code_metadata(code)
    games_to_probe = _determine_games_to_probe(resolved_game)

    if platform_filter:
        platforms_to_probe = [plat for plat in platform_filter if plat != "universal"]
        if not platforms_to_probe:
            raise ManualRedeemUsageError("Manual --redeem platform list cannot be empty after normalization.")
    else:
        platforms_to_probe = [plat for plat in query.known_platforms.keys() if plat != "universal"]

    return ManualContext(
        code=code,
        resolved_game=resolved_game,
        reward=reward,
        games_to_probe=games_to_probe,
        platforms_to_probe=platforms_to_probe,
        scheduled=scheduled,
        platform_filter=platform_filter,
    )


def _resolve_code_metadata(code: str) -> tuple[str, Optional[str]]:
    """Return (reward, game) by checking DB first, then SHIFT_SOURCE JSON."""

    key_from_db = _fetch_db_key(code)
    if key_from_db:
        # DEV NOTE: Trust DB first; reward/game already normalized when inserted.
        return key_from_db.reward or "Unknown", key_from_db.game

    key_from_json = _fetch_json_key(code)
    if key_from_json:
        return key_from_json.reward or "Unknown", key_from_json.game

    return "Unknown", None


def _fetch_db_key(code: str) -> Optional[Key]:
    # DEV NOTE: Prefer platform='universal' rows so future per-platform attempts share a single key id.
    rows = list(
        query.db.execute(
            """
            SELECT * FROM keys
            WHERE code = ?
            ORDER BY CASE WHEN platform='universal' THEN 0 ELSE 1 END, id DESC
            """,
            (code,),
        ).fetchall()
    )

    if not rows:
        return None

    row = rows[0]
    return Key(**{col: row[col] for col in row.keys()})


def _fetch_json_key(code: str) -> Optional[Key]:
    # DEV NOTE: parse_shift_orcicorn() may trigger a network read; log failures but keep manual flow resilient.
    try:
        keys: Iterable[Key] = query.parse_shift_orcicorn() or []
    except Exception as exc:
        _L.warning(f"Manual redeem could not read SHiFT source ({exc}); using fallback metadata.")
        return None

    for key in keys:
        if (_normalize_shift_code(key.code) or "").upper() == code.upper():
            return key

    return None


def _determine_games_to_probe(resolved_game: Optional[str]) -> Sequence[str]:
    # DEV NOTE: If we can map to a known short game key, probe that game only. Otherwise brute-force all.
    if resolved_game and resolved_game in query.known_games:
        return [resolved_game]

    return list(query.known_games.keys())


# DEV NOTE: Redeem loop --------------------------------------------------------
@dataclass
class AttemptResult:
    game: str
    platform: str
    status: Status


def _redeem_across_targets(context: ManualContext, shift_client, redeem_cb) -> List[AttemptResult]:
    results: List[AttemptResult] = []
    for game_index, game in enumerate(context.games_to_probe, start=1):
        base_key = _ensure_base_key(context.code, game, context.reward)
        _L.info(
            f"Manual redeem: Game {game_index}/{len(context.games_to_probe)} -> {game}"
        )

        for plat_index, platform in enumerate(context.platforms_to_probe, start=1):
            # DEV NOTE: Copy ensures we keep the DB id while overriding platform per attempt.
            attempt_key = base_key.copy().set(platform=platform, game=game)
            _L.info(
                f"Code {plat_index}/{len(context.platforms_to_probe)} for {game} on {platform}"
            )

            try:
                redeem_cb(attempt_key)
            except SystemExit:
                raise
            except Exception as exc:
                _L.error(f"Redeem callback raised {exc}; treating as UNKNOWN status.")
                shift_client.last_status = Status.UNKNOWN(str(exc))

            status = getattr(shift_client, "last_status", Status.NONE)
            results.append(AttemptResult(game=game, platform=platform, status=status))

            if status == Status.SUCCESS:
                continue
            if status == Status.SLOWDOWN:
                _L.info("Manual redeem hit SLOWDOWN; sleeping 60s before continuing.")
                sleep(60)
            if status == Status.TRYLATER:
                _L.info("Manual redeem received TRY LATER; stopping further attempts.")
                return results

    return results


def _ensure_base_key(code: str, game: str, reward: str) -> Key:
    # DEV NOTE: We want a stable key id per (code, game). Prefer a universal platform row or create one.
    existing = query.db.execute(
        """
        SELECT * FROM keys
        WHERE code = ? AND game = ?
        ORDER BY CASE WHEN platform='universal' THEN 0 ELSE 1 END, id DESC
        LIMIT 1
        """,
        (code, game),
    ).fetchone()

    if existing:
        row = existing
        if row["platform"] != "universal":
            universal = query.db.execute(
                """
                SELECT * FROM keys
                WHERE code = ? AND game = ? AND platform = 'universal'
                ORDER BY id DESC
                LIMIT 1
                """,
                (code, game),
            ).fetchone()
            if universal:
                row = universal
        return Key(**{col: row[col] for col in row.keys()})

    reward_value = reward or "Unknown"
    query.db.execute(
        "INSERT INTO keys(reward, code, platform, game) VALUES (?, ?, 'universal', ?)",
        (reward_value, code, game),
    )
    query.db.commit()

    created = query.db.execute(
        """
        SELECT * FROM keys
        WHERE code = ? AND game = ? AND platform = 'universal'
        ORDER BY id DESC
        LIMIT 1
        """,
        (code, game),
    ).fetchone()
    if not created:
        raise RuntimeError("Failed to ensure base key for manual redeem.")

    return Key(**{col: created[col] for col in created.keys()})


# DEV NOTE: Logging helpers ----------------------------------------------------
def _log_ignored_flags(args) -> None:
    ignored = []
    for attr in ("games", "platforms", "golden", "non_golden", "other", "limit"):
        value = getattr(args, attr, None)
        if value:
            flag = f"--{attr.replace('_', '-')}"
            ignored.append(flag)

    if ignored:
        _L.info(
            "Manual redeem ignores these flags for this run: " + ", ".join(sorted(set(ignored)))
        )


def _summarize_results(
    context: ManualContext,
    results: Sequence[AttemptResult],
    any_success: bool,
    hit_try_later: bool,
) -> None:
    if not results:
        _L.warning("Manual redeem skipped all attempts (likely due to TRY LATER).")
        return

    _L.info("Manual redeem summary:")
    for attempt in results:
        status_name = attempt.status.name if isinstance(attempt.status, Status) else str(attempt.status)
        _L.info(f"  {attempt.game} on {attempt.platform}: {status_name}")

    success_targets = [result.platform for result in results if result.status == Status.SUCCESS]
    redeemed_targets = [result.platform for result in results if result.status == Status.REDEEMED]

    labels = []
    if success_targets:
        labels.append(f"{', '.join(success_targets)} success{'es' if len(success_targets) != 1 else ''}")
    if redeemed_targets:
        labels.append(f"{', '.join(redeemed_targets)} already redeemed")

    if labels:
        outcome_label = ", ".join(labels)
    else:
        outcome_label = "no successes"

    if hit_try_later:
        outcome_label += ", TRY-LATER encountered"

    _L.info(f"Manual redeem outcome: {outcome_label}.")

