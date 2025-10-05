"""Manual single-code redeem helpers."""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from time import sleep
from typing import List, Optional, Sequence

from common import _L
import query
from query import Key
from shift import Status
from redeem_logic import (
    RedemptionCandidate,
    RedemptionPlan,
    build_redemption_plan,
    normalize_requested_platforms,
    normalize_shift_code,
)

_STRIP_RE = re.compile(r"[^A-Z0-9]", re.IGNORECASE)


def _is_positive_status(status: Status) -> bool:
    return status in (Status.SUCCESS, Status.REDEEMED)


class ManualRedeemUsageError(Exception):
    """Raised when --redeem arguments cannot be handled in manual mode."""


@dataclass
class ManualContext:
    original_code: str
    normalized_code: str
    scheduled: bool
    platform_filter: Optional[Sequence[str]]
    bypass_fail: bool
    plan: RedemptionPlan
    verbose: bool


@dataclass
class AttemptResult:
    candidate: RedemptionCandidate
    status: Status
    detail: str


def maybe_handle_manual_redeem(args, shift_client, redeem_cb) -> bool:
    try:
        manual_request = _extract_manual_request(args)
    except ManualRedeemUsageError as exc:
        _L.error(str(exc))
        sys.exit(2)

    if not manual_request:
        return False

    normalized_code, platform_filter = manual_request
    verbose = bool(getattr(args, "verbose", False))
    bypass_fail = bool(getattr(args, "bypass_fail", False))

    try:
        _ensure_manual_flags_allowed(args)
    except ManualRedeemUsageError as exc:
        _L.error(str(exc))
        sys.exit(2)

    context = _build_manual_context(
        normalized_code,
        bool(getattr(args, "schedule", None)),
        platform_filter,
        bypass_fail,
        verbose,
    )

    _log_plan_intro(context)
    _handle_skipped_candidates(context)

    results, hit_try_later = _redeem_candidates(context, shift_client, redeem_cb)

    previous_success = any(
        candidate.skip_reason == "redeemed" for candidate in context.plan.skipped
    )
    run_success = any(_is_positive_status(result.status) for result in results)
    any_success = previous_success or run_success

    _summarize_results(context, results, hit_try_later)

    if context.scheduled:
        return True

    if hit_try_later and not any_success:
        _L.warning(
            "Manual redeem ended early due to TRY LATER without any successful redemption."
        )

    sys.exit(0 if any_success else 1)


def _extract_manual_request(args) -> Optional[tuple[str, Optional[Sequence[str]]]]:
    entries = [
        entry.strip()
        for entry in (getattr(args, "redeem", None) or [])
        if entry and entry.strip()
    ]
    if not entries:
        return None

    if len(entries) != 1:
        manual_like = [
            entry for entry in entries if _looks_like_shift_code(entry.split(":", 1)[0])
        ]
        if manual_like:
            raise ManualRedeemUsageError(
                "Manual --redeem expects exactly one SHiFT code argument."
            )
        return None

    entry = entries[0]
    if ":" in entry:
        code_part, platform_part = entry.split(":", 1)
        if not _looks_like_shift_code(code_part):
            return None
        normalized = normalize_shift_code(code_part)
        if not normalized:
            raise ManualRedeemUsageError(
                "Manual --redeem requires a 25-character SHiFT code (5 blocks of 5 letters/numbers)."
            )
        platform_filter = _normalize_manual_platforms(platform_part)
        return normalized, platform_filter

    if not _looks_like_shift_code(entry):
        return None

    normalized = normalize_shift_code(entry)
    if not normalized:
        raise ManualRedeemUsageError(
            "Manual --redeem requires a 25-character SHiFT code (5 blocks of 5 letters/numbers)."
        )

    return normalized, None


def _looks_like_shift_code(raw: str) -> bool:
    stripped = _STRIP_RE.sub("", raw)
    return len(stripped) == 25 and stripped.isalnum()


def _normalize_manual_platforms(raw: str) -> Sequence[str]:
    tokens = [token.strip() for token in raw.split(",") if token.strip()]
    if not tokens:
        raise ManualRedeemUsageError(
            "Manual --redeem with :platform requires at least one platform."
        )

    normalized: List[str] = []
    for token in tokens:
        plat = _normalize_platform_token(token)
        if plat == "universal":
            raise ManualRedeemUsageError(
                "Manual --redeem does not support the 'universal' pseudo-platform."
            )
        normalized.append(plat)

    return normalize_requested_platforms(normalized)


def _normalize_platform_token(token: str) -> str:
    token_lower = token.strip().lower()
    if token_lower in query.known_platforms:
        return token_lower
    inverted = getattr(query.known_platforms, "inv", {})
    if token_lower in inverted:
        return inverted[token_lower]
    raise ManualRedeemUsageError(f"Unknown platform '{token}' for manual --redeem.")


def _build_manual_context(
    code: str,
    scheduled: bool,
    platform_filter: Optional[Sequence[str]],
    bypass_fail: bool,
    verbose: bool,
) -> ManualContext:
    requested = (
        normalize_requested_platforms(platform_filter)
        if platform_filter
        else normalize_requested_platforms(None)
    )
    plan = build_redemption_plan(code, requested, bypass_fail=bypass_fail)
    stored_filter = tuple(platform_filter) if platform_filter else None
    return ManualContext(
        original_code=code,
        normalized_code=code,
        scheduled=scheduled,
        platform_filter=stored_filter,
        bypass_fail=bypass_fail,
        plan=plan,
        verbose=verbose,
    )


def _log_plan_intro(context: ManualContext) -> None:
    plan = context.plan
    platform_note = (
        f" (filtered: {', '.join(plan.requested_platforms)})"
        if context.platform_filter
        else ""
    )
    games_label = ", ".join(plan.games) or "fallback"
    _L.info(
        f"Manual redeem mode: {context.normalized_code} -> games={games_label}; "
        f"probing {len(plan.games)} game(s) across {len(plan.requested_platforms)} platform(s){platform_note}. "
        f"Reward hint: {plan.reward_hint}."
    )
    if context.verbose:
        bypass_label = "on" if context.bypass_fail else "off"
        _L.debug(f"Manual redeem bypass-fail={bypass_label}")
    if plan.skipped:
        _L.info(
            f"Manual redeem: {len(plan.skipped)} pair(s) already satisfied/blocked; "
            f"{len(plan.attempts)} pending attempt(s)."
        )
    else:
        _L.info(f"Manual redeem: {len(plan.attempts)} pending attempt(s).")


def _format_pair(candidate: RedemptionCandidate) -> str:
    return f"{candidate.platform}:{candidate.game}"


def _key_for_candidate(candidate: RedemptionCandidate) -> Key:
    return candidate.key.copy().set(
        platform=candidate.platform,
        game=candidate.game,
        code=candidate.code,
    )


def _failure_label_for_status(status: Status) -> str:
    if status == Status.EXPIRED:
        return "EXPIRED"
    if status == Status.INVALID:
        return "INVALID"
    if status == Status.SLOWDOWN:
        return "RATELIMIT"
    if status == Status.TRYLATER:
        return "TRYLATER"
    if status == Status.REDIRECT:
        return "NETWORK_ERROR"
    if status in (Status.NONE, Status.UNKNOWN):
        return "UNKNOWN_ERROR"
    return getattr(status, "name", "UNKNOWN_ERROR")


def _handle_skipped_candidates(context: ManualContext) -> None:
    for candidate in context.plan.skipped:
        pair = _format_pair(candidate)
        if candidate.skip_reason == "redeemed":
            _L.info(f"Previously recorded success for {pair}; skipping remote call.")
            candidate.previously_redeemed = True
        elif candidate.skip_reason == "failed":
            reason = candidate.previously_failed or "UNKNOWN"
            reward_display = candidate.reward or "Unknown"
            _L.info(
                f"Previously recorded failure ({reason}) ({reward_display}) for {pair}; "
                "skipping remote call."
            )
        elif candidate.skip_reason == "expired":
            _L.info(
                f"Source marks {context.normalized_code} as expired for {pair}; "
                "recording EXPIRED without remote call."
            )
            status_label = candidate.preclassified_status or "EXPIRED"
            candidate.previously_failed = status_label
            if candidate.should_record_preclassification:
                key_obj = _key_for_candidate(candidate)
                query.db.record_failure(
                    key_obj,
                    candidate.platform,
                    status_label,
                    "Preclassified expiry from source metadata",
                )
        else:
            _L.info(
                f"Skipping {pair}; reason={candidate.skip_reason or 'unknown'}."
            )


def _redeem_candidates(
    context: ManualContext,
    shift_client,
    redeem_cb,
) -> tuple[List[AttemptResult], bool]:
    results: List[AttemptResult] = []
    hit_try_later = False

    for candidate in context.plan.attempts:
        attempt_key = _key_for_candidate(candidate)
        _L.info(
            f"Trying to redeem {candidate.reward} ({candidate.code}) "
            f"on {candidate.platform} for {candidate.game}"
        )

        slowdown_retry = False
        while True:
            try:
                redeem_cb(attempt_key)
            except SystemExit:
                raise
            except Exception as exc:
                _L.error(f"Redeem callback raised {exc}; treating as UNKNOWN status.")
                shift_client.last_status = Status.UNKNOWN(str(exc))

            status = getattr(shift_client, "last_status", Status.NONE)
            if status == Status.SLOWDOWN and not slowdown_retry:
                _L.info(
                    "Manual redeem hit SLOWDOWN; sleeping 60s before retrying same code."
                )
                slowdown_retry = True
                sleep(60)
                continue
            if status == Status.SLOWDOWN:
                _L.info(
                    "Manual redeem hit SLOWDOWN twice; treating as TRY LATER and stopping."
                )
                status = Status.TRYLATER
                shift_client.last_status = status
            break

        detail = getattr(status, "msg", str(status))
        result = AttemptResult(candidate=candidate, status=status, detail=detail)
        results.append(result)

        if _is_positive_status(status):
            query.db.set_redeemed(attempt_key)
            candidate.previously_redeemed = True
            candidate.previously_failed = None
            candidate.failure_detail = None
        else:
            failure_label = _failure_label_for_status(status)
            query.db.record_failure(
                attempt_key,
                candidate.platform,
                failure_label,
                detail,
            )
            candidate.previously_failed = failure_label
            candidate.failure_detail = detail

        if status == Status.TRYLATER:
            _L.info("Manual redeem received TRY LATER; stopping further attempts.")
            hit_try_later = True
            break

    return results, hit_try_later


def _summarize_results(
    context: ManualContext,
    results: Sequence[AttemptResult],
    hit_try_later: bool,
) -> None:
    plan = context.plan
    if not results and not plan.skipped:
        _L.warning("Manual redeem skipped all attempts (no candidates).")
        return

    if context.verbose:
        _L.debug("Manual redeem summary:")
        for candidate in plan.skipped:
            status_hint = (
                "success"
                if candidate.skip_reason == "redeemed"
                else candidate.previously_failed or candidate.skip_reason or "unknown"
            )
            extras = ""
            if str(status_hint).upper() == "EXPIRED":
                extras = f" ({candidate.code}) ({candidate.reward or 'Unknown'})"
            _L.debug(
                f"  {_format_pair(candidate)} -> skipped ({status_hint}){extras}"
            )
        for result in results:
            status_name = getattr(result.status, "name", str(result.status))
            _L.debug(f"  {_format_pair(result.candidate)} -> {status_name}")

    def _dedup(seq: Sequence[str]) -> List[str]:
        return list(dict.fromkeys(seq))

    all_candidates: List[RedemptionCandidate] = list(plan.attempts) + list(plan.skipped)
    platforms = _dedup([cand.platform for cand in all_candidates])
    games = _dedup([cand.game for cand in all_candidates])
    total_pairs = len(all_candidates)

    status_counts: dict[str, int] = {}
    status_platforms: dict[str, List[str]] = {}
    status_order: List[str] = []
    origin_counts: dict[str, int] = {}

    def _note_status(label: str, platform: str) -> None:
        if not label:
            label = "UNKNOWN"
        if label not in status_counts:
            status_counts[label] = 0
            status_platforms[label] = []
            status_order.append(label)
        status_counts[label] += 1
        status_platforms[label].append(platform)

    for result in results:
        status = result.status
        if _is_positive_status(status):
            label = getattr(status, "name", str(status)) or "SUCCESS"
        else:
            label = _failure_label_for_status(status)
        _note_status(label, result.candidate.platform)
        origin = result.candidate.origin or "unknown"
        origin_counts[origin] = origin_counts.get(origin, 0) + 1

    for candidate in plan.skipped:
        if candidate.skip_reason == "redeemed":
            label = "REDEEMED"
        elif candidate.skip_reason == "expired":
            label = candidate.preclassified_status or "EXPIRED"
        elif candidate.skip_reason == "failed":
            label = candidate.previously_failed or "FAILED"
        else:
            label = (candidate.skip_reason or "SKIPPED").upper()
        _note_status(label, candidate.platform)
        origin = candidate.origin or "unknown"
        origin_counts[origin] = origin_counts.get(origin, 0) + 1

    summary_line = (
        f"Manual redeem outcome: [{plan.normalized_code}] - "
        f"[Platforms: {', '.join(platforms) if platforms else 'none'}] - "
        f"[Games: {', '.join(games) if games else 'none'}] - "
        f"[Count {total_pairs}]"
    )
    _L.info(summary_line)

    if context.verbose and origin_counts:
        origin_labels = {
            "db": "database",
            "shift": "SHiFT source",
            "fallback": "fallback",
        }
        origin_fragments: List[str] = []
        for key in ("db", "shift", "fallback"):
            if key in origin_counts:
                origin_fragments.append(f"{origin_labels[key]} {origin_counts[key]}")
        for key, value in origin_counts.items():
            if key not in origin_labels:
                origin_fragments.append(f"{key} {value}")
        if origin_fragments:
            _L.debug("Count sources: " + ", ".join(origin_fragments))

    if status_counts:
        status_segments: List[str] = []
        for label in status_order:
            platforms_for_label = _dedup(status_platforms[label])
            fragment = f"{label} x{status_counts[label]}"
            if platforms_for_label:
                fragment += f" ({', '.join(platforms_for_label)})"
            status_segments.append(fragment)
        status_line = "Status breakdown: " + "; ".join(status_segments)
        if hit_try_later:
            status_line += "; TRY-LATER encountered"
        _L.info(status_line)
    elif hit_try_later:
        _L.info("Status breakdown: TRY-LATER encountered")


def _ensure_manual_flags_allowed(args) -> None:
    disallowed = []
    if getattr(args, "golden", False):
        disallowed.append("--golden")
    if getattr(args, "non_golden", False):
        disallowed.append("--non-golden")
    if getattr(args, "other", False):
        disallowed.append("--other")
    if getattr(args, "_limit_was_supplied", False):
        disallowed.append("--limit")
    if getattr(args, "games", None):
        disallowed.append("--games")
    if getattr(args, "platforms", None):
        disallowed.append("--platforms")
    if getattr(args, "schedule", None) is not None:
        disallowed.append("--schedule")

    if disallowed:
        raise ManualRedeemUsageError(
            "Manual --redeem does not support these flags: "
            + ", ".join(sorted(disallowed))
            + ". Remove them or use mapping mode."
        )
