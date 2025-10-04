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


# DEV NOTE: Public entry point -------------------------------------------------
def maybe_handle_manual_redeem(args, shift_client, redeem_cb) -> bool:
    """Detect and run manual single-code redemption flow.

    Returns True when manual mode handled the run (even if it ultimately exits).
    Returns False when manual mode is not applicable so auto.py can continue.
    """

    try:
        normalized_code = _extract_manual_code(args)
    except ManualRedeemUsageError as exc:
        _L.error(str(exc))
        sys.exit(2)

    if not normalized_code:
        return False

    # DEV NOTE: Manual mode owns --redeem once we get here; log ignored flags pre-emptively so users are aware.
    _log_ignored_flags(args)

    # DEV NOTE: Build execution context (code metadata, game/platform lists, schedule flag) in a dedicated helper.
    context = _build_manual_context(normalized_code, bool(getattr(args, "schedule", None)))

    _L.info(
        f"Manual redeem mode: {context.code} -> game={context.resolved_game or 'manual'}; "
        f"probing {len(context.games_to_probe)} game(s) across {len(context.platforms_to_probe)} platform(s)."
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
def _extract_manual_code(args) -> Optional[str]:
    """Return normalized SHiFT code string if manual mode applies, otherwise None."""

    entries = [entry.strip() for entry in (getattr(args, "redeem", None) or []) if entry and entry.strip()]
    if not entries:
        return None

    colon_entries = [entry for entry in entries if ":" in entry]
    bare_entries = [entry for entry in entries if ":" not in entry]

    # DEV NOTE: Catch mixed formats up-front so users are nudged toward one clear mode.
    if bare_entries and colon_entries:
        raise ManualRedeemUsageError(
            "Cannot mix manual SHiFT codes with game:platform entries in --redeem."
        )

    # DEV NOTE: A single colon entry may still be a manual attempt (e.g. CODE:psn). Detect and error explicitly.
    if len(entries) == 1 and colon_entries:
        candidate, _ = entries[0].split(":", 1)
        if _looks_like_shift_code(candidate):
            raise ManualRedeemUsageError(
                "Manual --redeem takes a raw SHiFT code only; drop any :platform suffixes."
            )
        return None

    if colon_entries:
        # DEV NOTE: All entries look like mapping mode; manual mode not applicable.
        return None

    if len(bare_entries) != 1:
        raise ManualRedeemUsageError("Manual --redeem expects exactly one SHiFT code argument.")

    normalized = _normalize_shift_code(bare_entries[0])
    if not normalized:
        raise ManualRedeemUsageError(
            "Manual --redeem requires a 25-character SHiFT code (5 blocks of 5 letters/numbers)."
        )

    return normalized


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


# DEV NOTE: Context construction ------------------------------------------------
def _build_manual_context(code: str, scheduled: bool) -> ManualContext:
    reward, resolved_game = _resolve_code_metadata(code)
    games_to_probe = _determine_games_to_probe(resolved_game)
    platforms_to_probe = [plat for plat in query.known_platforms.keys() if plat != "universal"]

    return ManualContext(
        code=code,
        resolved_game=resolved_game,
        reward=reward,
        games_to_probe=games_to_probe,
        platforms_to_probe=platforms_to_probe,
        scheduled=scheduled,
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

    success_count = sum(1 for result in results if result.status == Status.SUCCESS)
    redeemed_count = sum(1 for result in results if result.status == Status.REDEEMED)

    if success_count or redeemed_count:
        parts = []
        if success_count:
            parts.append(f"{success_count} success{'es' if success_count != 1 else ''}")
        if redeemed_count:
            parts.append(f"{redeemed_count} already redeemed")
        outcome_label = ", ".join(parts)
    else:
        outcome_label = "no successes"

    if hit_try_later:
        outcome_label += ", TRY-LATER encountered"

    _L.info(f"Manual redeem outcome: {outcome_label}.")

