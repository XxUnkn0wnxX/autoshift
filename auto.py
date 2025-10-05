#!/usr/bin/env python3
#############################################################################
#
# Copyright (C) 2018 Fabian Schweinfurth
# Contact: autoshift <at> derfabbi.de
#
# This file is part of autoshift
#
# autoshift is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# autoshift is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with autoshift.  If not, see <http://www.gnu.org/licenses/>.
#
#############################################################################
from __future__ import print_function, annotations

import os  # must run before importing common/query
import sys  # must run before importing common/query

# Early profile bootstrap: set AUTOSHIFT_PROFILE before common.py is imported
if "--profile" in sys.argv:
    i = sys.argv.index("--profile")
    if i + 1 < len(sys.argv):
        os.environ["AUTOSHIFT_PROFILE"] = sys.argv[i + 1]

from collections import deque
from typing import TYPE_CHECKING, Optional

from common import _L, DEBUG, INFO, data_path, DATA_DIR, PROFILE
from m_redeem import maybe_handle_manual_redeem
from redeem_logic import (
    RedemptionCandidate,
    RedemptionPlan,
    build_redemption_plan,
    format_status_detail,
    normalize_requested_platforms,
    normalize_shift_code,
)
from shift import Status

# Static choices so CLI parsing doesn't need to import query/db
STATIC_GAMES = ["bl4", "bl3", "blps", "bl2", "bl1", "ttw", "gdfll"]
STATIC_PLATFORMS = ["epic", "steam", "xboxlive", "psn", "nintendo", "stadia"]

if TYPE_CHECKING:
    from query import Key

client: "ShiftClient" = None  # type: ignore

LICENSE_TEXT = """\
========================================================================
autoshift  Copyright (C) 2019  Fabian Schweinfurth
This program comes with ABSOLUTELY NO WARRANTY; for details see LICENSE.
This is free software, and you are welcome to redistribute it
under certain conditions; see LICENSE for details.
========================================================================
"""


def redeem(key: "Key"):
    """Redeem key and set as redeemed if successful"""
    import query

    _L.info(f"Trying to redeem {key.reward} ({key.code}) on {key.platform}")
    # use query.known_games (query imported above) instead of relying on a global name
    status = client.redeem(key.code, query.known_games[key.game], key.platform)
    _L.debug(f"Status: {status}")

    detail = format_status_detail(status, key)

    # set redeemed status only for positive outcomes
    if status in (Status.SUCCESS, Status.REDEEMED):
        status_label = getattr(status, "name", "SUCCESS")
        query.db.set_redeemed(key, status_label, detail)

    # notify user
    msg = detail
    if status == Status.INVALID:
        msg = f"Cannot redeem on {key.platform}"

    _L.info("  " + msg)

    return status in (Status.SUCCESS, Status.REDEEMED)


def _load_plan(plan_cache: dict[str, RedemptionPlan], code: str, bypass_fail: bool) -> RedemptionPlan:
    normalized = normalize_shift_code(code) or code
    plan = plan_cache.get(normalized)
    if plan is None:
        plan = build_redemption_plan(
            normalized,
            normalize_requested_platforms(None),
            bypass_fail=bypass_fail,
        )
        plan_cache[normalized] = plan
    return plan


def _find_candidate(
    plan: RedemptionPlan, game: str, platform: str
) -> tuple[Optional[RedemptionCandidate], Optional[str]]:
    for candidate in plan.attempts:
        if candidate.game == game and candidate.platform == platform:
            return candidate, "attempt"
    for candidate in plan.skipped:
        if candidate.game == game and candidate.platform == platform:
            return candidate, "skip"
    return None, None


def _format_pair(code: str, candidate: RedemptionCandidate) -> str:
    return f"{code} -> {candidate.platform}:{candidate.game}"


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


def _key_for_candidate(candidate: RedemptionCandidate) -> Key:
    return candidate.key.copy().set(
        platform=candidate.platform,
        game=candidate.game,
        code=candidate.code,
    )


def _log_auto_skip(code: str, candidate: RedemptionCandidate, _bypass_fail: bool) -> None:
    label = _format_pair(code, candidate)
    if candidate.skip_reason == "redeemed":
        _L.debug(f"{label}: previously recorded success; skipping remote call.")
    elif candidate.skip_reason == "failed":
        reason = candidate.previously_failed or "UNKNOWN"
        _L.debug(f"{label}: previously recorded failure ({reason}); skipping remote call.")
    elif candidate.skip_reason == "expired":
        _L.debug(f"{label}: source expired; recording EXPIRED without remote call.")
    else:
        _L.debug(f"{label}: skipping ({candidate.skip_reason or 'unknown'}).")


def parse_redeem_mapping(args):
    """
    Returns a dict mapping game -> list of platforms.
    If --redeem is not used, returns None.
    """
    if hasattr(args, "redeem") and args.redeem:
        mapping = {}
        for entry in args.redeem:
            if ":" not in entry:
                _L.error(
                    f"Invalid --redeem entry: {entry}. Use format game:platform[,platform...]"
                )
                sys.exit(2)
            game, plats = entry.split(":", 1)
            mapping[game] = [p.strip() for p in plats.split(",") if p.strip()]
            if not mapping[game]:
                _L.error(
                    f"Invalid --redeem entry: {entry}. At least one platform is required."
                )
                sys.exit(2)
        return mapping
    return None


def query_keys_with_mapping(redeem_mapping, games, platforms):
    """
    Returns dict of dicts of lists with [game][platform] as keys,
    using the redeem_mapping if provided.
    """
    from itertools import groupby
    import query

    all_keys: dict[str, dict[str, list[Key]]] = {}

    keys = list(query.db.get_keys(None, None))
    query.update_keys()
    new_keys = list(query.db.get_keys(None, None))
    diff = len(new_keys) - len(keys)
    _L.info(f"done. ({diff if diff else 'no'} new Keys)")

    _g = lambda key: key.game
    _p = lambda key: key.platform

    # Ensure all requested games and platforms are present, even if no keys exist yet
    if redeem_mapping:
        for g, plats in redeem_mapping.items():
            all_keys[g] = {p: [] for p in plats}
    else:
        for g in games:
            all_keys[g] = {p: [] for p in platforms}

    for g, g_keys in groupby(sorted(new_keys, key=_g), _g):
        if redeem_mapping:
            if g not in redeem_mapping:
                continue
            plats = redeem_mapping[g]
        else:
            if g not in games:
                continue
            plats = platforms

        for platform, p_keys in groupby(sorted(g_keys, key=_p), _p):
            if platform not in plats and platform != "universal":
                continue

            _ps = [platform]
            if platform == "universal":
                _ps = plats.copy()

            for key in p_keys:
                temp_key = key
                for p in _ps:
                    _L.debug(f"Platform: {p}, {key}")
                    all_keys[g][p].append(temp_key.copy().set(platform=p))

    # Always print info for all requested game/platform pairs
    # DEV NOTE: This summary mirrors the new About-to-redeem breakdown: Golden vs Non-Golden vs Other Codes.
    #          It replaces the old 'golden-only' summary so users can see per-category counts up front.
    for g in all_keys:
        for p in all_keys[g]:
            keys_list = [k for k in all_keys[g][p] if not getattr(k, "redeemed", False)]
            # Split into Golden, non-golden Keys, and Codes to mirror the "About to redeem" summary
            golden_count = sum(1 for k in keys_list if query.r_golden_keys.match((k.reward or "")))
            non_golden_count = sum(
                1
                for k in keys_list
                if ("key" in (k.reward or "").lower()) and not query.r_golden_keys.match((k.reward or ""))
            )
            codes_count = max(0, len(keys_list) - golden_count - non_golden_count)
            _L.info(
                f"You have {golden_count} Golden Keys, {non_golden_count} Non-Golden Keys, {codes_count} Other Codes for {g} to redeem for {p}"
            )

    return all_keys

def dump_db_to_csv(filename):
    import csv
    from query import db

    # Always write into the profile-aware data directory
    os.makedirs(DATA_DIR, exist_ok=True)
    base = os.path.basename(filename)
    out_path = data_path(base)

    db_path = os.path.join(DATA_DIR, "keys.db")
    if not os.path.exists(db_path):
        profile_label = PROFILE or "default"
        _L.error(
            f"No database found for profile '{profile_label}'. Run the main autoshift command first to populate keys."
        )
        sys.exit(2)

    with db:
        conn = db._Database__conn  # Access the underlying sqlite3.Connection
        c = conn.cursor()
        c.execute("SELECT * FROM keys")
        rows = c.fetchall()
        if not rows:
            profile_label = PROFILE or "default"
            _L.error(
                f"Database for profile '{profile_label}' contains no keys. Run autoshift to load SHiFT codes before exporting."
            )
            sys.exit(2)
        headers = [desc[0] for desc in c.description]
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for row in rows:
                try:
                    writer.writerow([row[h] for h in headers])  # sqlite3.Row path
                except Exception:
                    writer.writerow(list(row))  # tuple fallback
        _L.info(f"Dumped {len(rows)} rows to {out_path}")


def setup_argparser():
    import argparse
    import textwrap

    # NOTE: we avoid importing query here so we can parse --profile early.
    # Use the static lists for argparse choices
    games = STATIC_GAMES
    platforms = STATIC_PLATFORMS

    parser = argparse.ArgumentParser(formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument(
        "-u",
        "--user",
        default=None,
        help=(
            "User login you want to use "
            "(optional. You will be prompted to enter your "
            " credentials if you didn't specify them here)"
        ),
    )
    parser.add_argument(
        "-p",
        "--pass",
        help=(
            "Password for your login. "
            "(optional. You will be prompted to enter your "
            " credentials if you didn't specify them here)"
        ),
    )
    parser.add_argument("--golden", action="store_true", help="Only redeem golden keys")
    parser.add_argument(
        "--non-golden",
        dest="non_golden",
        action="store_true",
        help="Only redeem Non-Golden keys",
    )
    # DEV NOTE: We split non-key codes (Unknown/cosmetics/etc.) into their own selectable group via --other.
    # Without --other, codes are excluded when a mode flag is used, matching requested behavior.
    parser.add_argument(
        "--other",
        action="store_true",
        help="Also redeem generic non-key codes (Unknown/cosmetics). Without this, Codes are excluded when using --golden or --non-golden.",
    )
    # DEV NOTE (2025-10-05): Legacy --games/--platforms mode is intentionally disabled while we
    # align bulk logging with the manual pipeline. Leaving the argument definitions commented out
    # prevents argparse from accepting them, but preserves the original code for future reference.
    # parser.add_argument(
    #     "--games",
    #     type=str,
    #     required=False,
    #     choices=games,
    #     nargs="+",
    #     help=("Games you want to query SHiFT keys for"),
    # )
    # parser.add_argument(
    #     "--platforms",
    #     type=str,
    #     required=False,
    #     choices=platforms,
    #     nargs="+",
    #     help=("Platforms you want to query SHiFT keys for"),
    # )
    # DEV NOTE: Document both mapping and manual flows inline so --help stays truthful.
    parser.add_argument(
        "--redeem",
        type=str,
        nargs="+",
        help=textwrap.dedent("""\
            Specify redemption targets.
            Mapping mode: bl3:steam,epic bl2:epic
            Manual mode: SHiFT code or code:platform[,platform...] to filter targets.
            Required for bulk runs; legacy --games/--platforms flags are disabled.
            Manual mode cannot be combined with --schedule.
        """),
    )
    parser.add_argument(
        "--bypass-fail",
        action="store_true",
        help="Ignore failed_keys/expiry short-circuits; attempt pairs again and update outcomes.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=textwrap.dedent(
            """\
                        Max number of golden Keys you want to redeem.
                        (default 200 if not supplied)
                        NOTE: You can only have 255 keys at any given time!"""
        ),
    )  # noqa
    parser.add_argument(
        "--schedule",
        type=float,
        const=2,
        nargs="?",
        help="Keep checking for keys every N hours (min 2). If used without a value, defaults to 2.",
    )
    parser.add_argument("-v", dest="verbose", action="store_true", help="Verbose mode")
    parser.add_argument(
        "--dump-csv",
        type=str,
        help="Dump all key data in the database to the specified CSV file and exit.",
    )
    parser.add_argument(
        "--shift-source",
        type=str,
        help="Override the SHiFT codes source (URL or local path). Can also be set via SHIFT_SOURCE env var.",
    )
    parser.add_argument(
        "--profile",
        type=str,
        help="Use a named profile (affects files stored under data/<profile>). Can also be set via AUTOSHIFT_PROFILE env var.",
    )

    return parser


def main(args):
    global client
    from time import sleep

    # apply profile override (CLI takes precedence over env)
    if getattr(args, "profile", None):
        os.environ["AUTOSHIFT_PROFILE"] = args.profile

    # Now import modules that rely on data paths / migrations
    import query
    from query import db, r_golden_keys, known_games, known_platforms
    from shift import ShiftClient, Status

    # apply shift source override (CLI takes precedence over env)
    shift_src = (
        args.shift_source
        if getattr(args, "shift_source", None)
        else os.getenv("SHIFT_SOURCE")
    )
    if shift_src:
        query.set_shift_source(shift_src)

    if not getattr(args, "redeem", None):
        _L.error(
            "--redeem is now required. Legacy --games/--platforms mode is disabled because --redeem provides the improved logging pipeline."
        )
        sys.exit(2)

    with db:
        if not client:
            # DEV NOTE: Password/credential source detection & debug logging
            #   - Prefer CLI creds only if BOTH --user and --pass are provided.
            #   - Else prefer env (SHIFT_PASS / AUTOSHIFT_PASS_RAW).
            #   - Else rely on cookie jar if present, otherwise interactive prompt.
            # The debug line logs the *auth source* (cli/env/cookies/prompt) so logs are truthful.

            env_pw = os.getenv("SHIFT_PASS") or os.getenv("AUTOSHIFT_PASS_RAW")

            cookie_path = data_path(".cookies.save")
            has_cookies = os.path.exists(cookie_path)
            # DEV NOTE: If cookies exist we may not need a password at all; keep debug wording generic ('auth from').

            chosen_pw = None
            pw_source = "cookies(.cookies.save)" if has_cookies else "prompt"

            # Only treat as CLI if both user and pass were supplied
            if args.user and args.pw:
                chosen_pw = args.pw
                pw_source = "cli"
                # heuristic: if CLI pw contains '!' and env_pw looks longer, prefer env
                if "!" in args.pw and env_pw and len(env_pw) > len(args.pw):
                    chosen_pw = env_pw
                    pw_source = "env(SHIFT_PASS/AUTOSHIFT_PASS_RAW)"
            elif env_pw:
                chosen_pw = env_pw
                pw_source = "env(SHIFT_PASS/AUTOSHIFT_PASS_RAW)"
            # else: no cli pw, no env pw => rely on cookies or prompt

            # More accurate log (we may not be using a password at all if cookies exist)
            # DEV NOTE: This replaces the older, misleading 'Using password from:'
            # because cookies imply no password may be used.
            _L.debug(f"Using auth from: {pw_source}")
            if args.schedule and (not has_cookies) and (not chosen_pw):
                _L.error("Scheduled mode requires saved cookies or a password (env/CLI). Exiting.")
                sys.exit(2)
            client = ShiftClient(args.user, chosen_pw)

        # DEV NOTE: Manual mode hook runs before mapping logic so parse_redeem_mapping never mis-parses SHiFT codes.
        if maybe_handle_manual_redeem(args, client, redeem):
            return

        bypass_fail = bool(getattr(args, "bypass_fail", False))

        redeem_mapping = parse_redeem_mapping(args)
        if not redeem_mapping:
            _L.error(
                "Bulk mode requires --redeem mapping. Legacy --games/--platforms flow is disabled."
            )
            sys.exit(2)

        # New mapping mode
        games = list(redeem_mapping.keys())
        platforms = sorted(set(p for plats in redeem_mapping.values() for p in plats))
        _L.info("Redeem mapping (game: platforms):")
        for game, plats in redeem_mapping.items():
            _L.info(f"  {game}: {', '.join(plats)}")

        # DEV NOTE (2025-10-05): The legacy fallback that relied on --games/--platforms is kept below
        # for posterity but intentionally commented out. Do not delete until we confirm we will never
        # need to revive the old interface.
        # else:
        #     games = args.games or list(known_games.keys())
        #     platforms = args.platforms or list(known_platforms)
        #     _L.warning(
        #         "You are using the legacy --games/--platforms format. "
        #         "Prefer --redeem bl3:steam,epic bl2:epic for more control."
        #     )
        #     _L.info("Redeeming all of these games/platforms combinations:")
        #     _L.info(f"  Games: {', '.join(games)}")
        #     _L.info(f"  Platforms: {', '.join(platforms)}")

        all_keys = query_keys_with_mapping(redeem_mapping, games, platforms)

        plan_cache: dict[str, RedemptionPlan] = {}
        processed_pairs: set[tuple[str, str, str]] = set()

        _L.info("Trying to redeem now.")

        # Track what actually got redeemed across the whole run
        any_keys_redeemed = False
        any_codes_redeemed = False

        last_end_label: Optional[str] = None

        # now redeem
        for game in all_keys.keys():
            for platform in all_keys[game].keys():
                _L.info(f"Redeeming for {game} on {platform}")
                t_keys = list(
                    filter(lambda key: not key.redeemed, all_keys[game][platform])
                )                # Build categories & prioritised queue (global --limit aware; mode respected)
                # DEV NOTE: Classification rules
                #   - Golden Keys: reward matches r_golden_keys
                #   - Non-Golden Keys: reward contains 'key' but not golden (e.g., Diamond keys)
                #   - Codes: everything else (Unknown/cosmetics/etc.)
                # This powers mode filtering and the per-category limit math.
                def _is_key_reward(k):
                    try:
                        rv = (getattr(k, "reward", "") or "").lower()
                        return "key" in rv
                    except Exception:
                        return False

                def _is_golden(k):
                    return bool(r_golden_keys.match((getattr(k, "reward", "") or "")))

                # Category lists (Codes are anything that is not a *key*; Unknowns land here)
                golden_list        = [k for k in t_keys if _is_golden(k)]
                nongolden_key_list = [k for k in t_keys if _is_key_reward(k) and not _is_golden(k)]  # Diamond, etc.
                codes_list         = [k for k in t_keys if not _is_key_reward(k)]                     # "Unknown", cosmetics, etc.

                # ---- per-pair limit and base queue (mode-aware + --other) ----
                # DEV NOTE: Base queue reflects the active mode, preserving redemption priority order:
                lim = args.limit

                # per-category take counters for limit math
                g_take = 0
                ng_take = 0
                c_take = 0

                union_mode = bool(args.golden and args.non_golden)
                # DEV NOTE: Union mode is kept for backwards-compat.
                #          The parser enforces mutual exclusivity now, so this will normally be False.
                other_only = bool(args.other and not args.golden and not args.non_golden)

                if union_mode:
                    # Golden + non-golden; include Codes only if --other
                    base_queue = golden_list + nongolden_key_list + (codes_list if args.other else [])
                    total_keys  = len(golden_list) + len(nongolden_key_list)
                    total_codes = len(codes_list) if args.other else 0

                elif other_only:
                    # Codes only
                    base_queue = codes_list
                    total_keys  = 0
                    total_codes = len(codes_list)

                elif args.golden:
                    # Codes only included if --other is set
                    base_queue = golden_list + (codes_list if args.other else [])
                    total_keys  = len(golden_list)
                    total_codes = len(codes_list) if args.other else 0

                elif args.non_golden:
                    # non-golden Keys; Codes only if --other
                    base_queue = nongolden_key_list + (codes_list if args.other else [])
                    total_keys  = len(nongolden_key_list)
                    total_codes = len(codes_list) if args.other else 0

                else:
                    # Default: Golden → non-golden → Codes
                    base_queue = golden_list + nongolden_key_list + codes_list
                    total_keys  = len(golden_list) + len(nongolden_key_list)
                    total_codes = len(codes_list)

                # ---- Apply per-pair limit ----
                # DEV NOTE: Limit is applied PER (game, platform) pair.
                #          We compute per-category takes (g_take/ng_take/c_take) honoring priority and mode.
                #          lim == 0 => redeem nothing but still print accurate ignore counts.
                if lim == 0:
                    # Explicit zero means redeem nothing; still report ignores
                    redeem_queue = []
                    red_keys  = 0
                    red_codes = 0
                elif lim > 0:
                    if union_mode:
                        # Golden → non-golden → Codes(if --other)
                        g_take  = min(len(golden_list), lim)
                        rem     = lim - g_take
                        ng_take = min(len(nongolden_key_list), max(0, rem))
                        rem2    = rem - ng_take
                        c_take  = min(len(codes_list), max(0, rem2)) if args.other else 0

                        red_keys  = g_take + ng_take
                        red_codes = c_take
                        redeem_queue = golden_list[:g_take] + nongolden_key_list[:ng_take] + (codes_list[:c_take] if args.other else [])

                    elif other_only:
                        # Codes only
                        c_take = min(len(codes_list), lim)
                        red_keys  = 0
                        red_codes = c_take
                        redeem_queue = codes_list[:c_take]

                    elif args.golden:
                        # Golden → Codes(if --other)
                        g_take = min(len(golden_list), lim)
                        rem    = lim - g_take
                        c_take = min(len(codes_list), max(0, rem)) if args.other else 0

                        red_keys  = g_take
                        red_codes = c_take
                        redeem_queue = golden_list[:g_take] + (codes_list[:c_take] if args.other else [])

                    elif args.non_golden:
                        # non-golden → Codes(if --other)
                        k_take = min(len(nongolden_key_list), lim)
                        rem    = lim - k_take
                        c_take = min(len(codes_list), max(0, rem)) if args.other else 0

                        red_keys  = k_take
                        red_codes = c_take
                        redeem_queue = nongolden_key_list[:k_take] + (codes_list[:c_take] if args.other else [])

                    else:
                        # Default: Golden → non-golden → Codes
                        g_take  = min(len(golden_list), lim)
                        rem     = lim - g_take
                        ng_take = min(len(nongolden_key_list), max(0, rem))
                        rem2    = rem - ng_take
                        c_take  = min(len(codes_list), max(0, rem2))

                        red_keys  = g_take + ng_take
                        red_codes = c_take
                        redeem_queue = golden_list[:g_take] + nongolden_key_list[:ng_take] + codes_list[:c_take]
                else:
                    # (lim < 0) shouldn't happen; treat as unlimited
                    redeem_queue = base_queue
                    red_keys  = total_keys
                    red_codes = total_codes

                # Ignored (for header)
                ign_keys  = max(0, total_keys  - red_keys)
                ign_codes = max(0, total_codes - red_codes)

                # Build the "About to redeem …" header label based on flags
                # DEV NOTE: Labeling rules
                #   - --golden / --non-golden => "Key/Keys"
                #   - --other => "Code/Codes"
                #   - No flags => show both lowercase labels when both types are present.
                # For --golden / --non-golden: always use "Key/Keys"
                # For --other: always use "Code/Codes"
                # For no mode flags: if both present show both (lowercase), else show the one present
                if args.golden or args.non_golden:
                    main_count = red_keys
                    main_label = "Key" if red_keys == 1 else "Keys"
                    line = f"About to redeem {main_count} {main_label} for {game} on {platform}"
                elif args.other:
                    main_count = red_codes
                    main_label = "Code" if red_codes == 1 else "Codes"
                    line = f"About to redeem {main_count} {main_label} for {game} on {platform}"
                else:
                    if red_keys and red_codes:
                        rk = "key" if red_keys == 1 else "keys"
                        rc = "code" if red_codes == 1 else "codes"
                        line = f"About to redeem {red_keys} {rk}, {red_codes} {rc} for {game} on {platform}"
                    else:
                        if red_keys:
                            rk = "key" if red_keys == 1 else "keys"
                            line = f"About to redeem {red_keys} {rk} for {game} on {platform}"
                        else:
                            rc = "code" if red_codes == 1 else "codes"
                            line = f"About to redeem {red_codes} {rc} for {game} on {platform}"

                # ---- Rebuilt header ignore summary (simple & mode-specific) ----
                # DEV NOTE: We removed verbose 'why' justifications.
                #          If --limit is explicitly present in argv, we show 'due to limit' ignores.
                #          Without --limit, we show which categories are excluded by the selected mode. (simple & mode-specific) ----
                explicit_limit = getattr(args, "_limit_was_supplied", False)

                g_total  = len(golden_list)
                ng_total = len(nongolden_key_list)
                c_total  = len(codes_list)

                if explicit_limit:
                    # Show ignores only for the active category; default shows all three
                    if args.golden:
                        g_ignored = max(0, g_total - g_take)
                        line += f" (Ignoring {g_ignored} {'Golden Key' if g_ignored==1 else 'Golden Keys'} due to limit)"
                    elif args.non_golden:
                        ng_ignored = max(0, ng_total - ng_take)
                        line += f" (Ignoring {ng_ignored} {'Non-Golden Key' if ng_ignored==1 else 'Non-Golden Keys'} due to limit)"
                    elif args.other:
                        c_ignored = max(0, c_total - c_take)
                        line += f" (Ignoring {c_ignored} {'Code' if c_ignored==1 else 'Codes'} due to limit)"
                    else:
                        # No mode flags → all categories considered
                        g_ignored  = max(0, g_total  - g_take)
                        ng_ignored = max(0, ng_total - ng_take)
                        c_ignored  = max(0, c_total  - c_take)
                        line += (
                            f" (Ignoring {g_ignored} {'Golden Key' if g_ignored==1 else 'Golden Keys'}, "
                            f"{ng_ignored} {'Non-Golden Key' if ng_ignored==1 else 'Non-Golden Keys'}, "
                            f"{c_ignored} {'Code' if c_ignored==1 else 'Codes'} due to limit)"
                        )
                else:
                    # No explicit --limit → report categories excluded by mode
                    if args.golden:
                        line += (
                            f" (Ignoring {ng_total} {'Non-Golden Key' if ng_total==1 else 'Non-Golden Keys'}, "
                            f"{c_total} {'Code' if c_total==1 else 'Codes'})"
                        )
                    elif args.non_golden:
                        line += (
                            f" (Ignoring {g_total} {'Golden Key' if g_total==1 else 'Golden Keys'}, "
                            f"{c_total} {'Code' if c_total==1 else 'Codes'})"
                        )
                    elif args.other:
                        line += (
                            f" (Ignoring {g_total} {'Golden Key' if g_total==1 else 'Golden Keys'}, "
                            f"{ng_total} {'Non-Golden Key' if ng_total==1 else 'Non-Golden Keys'})"
                        )
                    else:
                        line += " (Ignoring 0 Golden Keys, 0 Non-Golden Keys, 0 Codes)"

                _L.info(line)

                # Prepare per-iteration counters for Key/Code progress
                # DEV NOTE: Separate counters produce accurate "Key #i/N" vs "Code #j/M" progress lines.
                queue_keys_len  = sum(1 for k in redeem_queue if _is_key_reward(k))
                queue_codes_len = len(redeem_queue) - queue_keys_len
                k_index = 0
                c_index = 0

                # Track previously redeemed skips for summary output
                ignored_redeemed_g = 0
                ignored_redeemed_ng = 0
                ignored_redeemed_codes = 0

                failed_g = 0
                failed_ng = 0
                failed_codes = 0

                pending_queue = deque(redeem_queue)
                overflow_index = len(redeem_queue)
                attempted_count = 0

                # Iterate and redeem
                while pending_queue:
                    key = pending_queue.popleft()

                    # polite throttling
                    if (attempted_count and not (attempted_count % 15)) or client.last_status == Status.SLOWDOWN:
                        if client.last_status == Status.SLOWDOWN:
                            _L.info("Slowing down a bit..")
                        else:
                            _L.info("Trying to prevent a 'too many requests'-block.")
                        sleep(60)

                    # Skip items that won't be attempted in this mode (belt-and-braces)
                    if union_mode:
                        if _is_key_reward(key) or (args.other and not _is_key_reward(key)):
                            pass
                        else:
                            _L.debug("Skipping Code in union mode (no --other)")
                            continue
                    elif other_only:
                        if _is_key_reward(key):
                            _L.debug("Skipping Key in --other-only mode")
                            continue
                    elif args.golden:
                        if _is_golden(key) or (args.other and not _is_key_reward(key)):
                            pass
                        else:
                            _L.debug("Skipping item in --golden mode")
                            continue
                    elif args.non_golden:
                        if _is_golden(key):
                            _L.debug("Skipping Golden in --non-golden mode")
                            continue
                        if (not _is_key_reward(key)) and (not args.other):
                            _L.debug("Skipping Code (no --other)")
                            continue
                    # else: default mode allows all

                    normalized_code = normalize_shift_code(key.code) or key.code
                    pair_id = (normalized_code, game, platform)
                    plan = _load_plan(plan_cache, normalized_code, bypass_fail)
                    candidate, disposition = _find_candidate(plan, game, platform)
                    if candidate is None:
                        _L.debug(
                            f"No candidate metadata for {normalized_code} on {platform}:{game}; skipping."
                        )
                        continue
                    if disposition == "skip":
                        _log_auto_skip(normalized_code, candidate, bypass_fail)
                        if (
                            candidate.skip_reason == "expired"
                            and candidate.should_record_preclassification
                        ):
                            attempt_key = _key_for_candidate(candidate)
                            query.db.record_failure(
                                attempt_key,
                                candidate.platform,
                                candidate.preclassified_status or "EXPIRED",
                                f"Preclassified expiry from source metadata ({candidate.reward})",
                            )
                            candidate.should_record_preclassification = False

                        if candidate.skip_reason == "redeemed":
                            counted_key = _key_for_candidate(candidate)
                            if _is_key_reward(counted_key):
                                if _is_golden(counted_key):
                                    ignored_redeemed_g += 1
                                else:
                                    ignored_redeemed_ng += 1
                            else:
                                ignored_redeemed_codes += 1

                        processed_pairs.add(pair_id)

                        if lim and lim > 0:
                            while (
                                (attempted_count + len(pending_queue)) < lim
                                and overflow_index < len(base_queue)
                            ):
                                extra_key = base_queue[overflow_index]
                                pending_queue.append(extra_key)
                                if _is_key_reward(extra_key):
                                    queue_keys_len += 1
                                else:
                                    queue_codes_len += 1
                                overflow_index += 1
                        continue
                    if pair_id in processed_pairs:
                        continue
                    attempt_key = _key_for_candidate(candidate)
                    processed_pairs.add(pair_id)

                    # Per-item progress line
                    if _is_key_reward(key):
                        k_index += 1
                        label = f"Key #{k_index}/{queue_keys_len}"
                    else:
                        c_index += 1
                        label = f"Code #{c_index}/{queue_codes_len}"
                    _L.info(f"{label} for {game} on {platform}")

                    attempted_count += 1

                    slowdown_retry = False
                    while True:
                        redeemed = redeem(attempt_key)
                        status = getattr(client, "last_status", Status.NONE)
                        if status == Status.SLOWDOWN and not slowdown_retry:
                            _L.info(
                                "Auto redeem hit SLOWDOWN; sleeping 60s before retrying same code."
                            )
                            slowdown_retry = True
                            sleep(60)
                            continue
                        if status == Status.SLOWDOWN:
                            _L.info(
                                "Auto redeem hit SLOWDOWN twice; treating as TRY LATER and ending run."
                            )
                            status = Status.TRYLATER
                            client.last_status = status
                            redeemed = False
                        break

                    detail = format_status_detail(status, attempt_key)

                    if redeemed:
                        candidate.previously_redeemed = True
                        candidate.previously_failed = None
                        candidate.failure_detail = None
                        # Update global redeemed trackers
                        if _is_key_reward(key):
                            any_keys_redeemed = True
                        else:
                            any_codes_redeemed = True
                        # Report what's left in THIS batch (Keys vs Codes)
                        rem_keys  = max(0, queue_keys_len  - k_index)
                        rem_codes = max(0, queue_codes_len - c_index)
                        if rem_keys and rem_codes:
                            _L.info(
                                f"Redeeming another {rem_keys} {'Key' if rem_keys==1 else 'Keys'}, {rem_codes} {'Code' if rem_codes==1 else 'Codes'}"
                            )
                        elif rem_keys:
                            _L.info(f"Redeeming another {rem_keys} {'Key' if rem_keys==1 else 'Keys'}")
                        elif rem_codes:
                            _L.info(f"Redeeming another {rem_codes} {'Code' if rem_codes==1 else 'Codes'}")
                        else:
                            # Choose end-of-batch label based on mode flags and what this queue contained
                            # DEV NOTE: In mode flags, we pin to keys/codes accordingly; otherwise prefer keys if present.
                            if args.golden or args.non_golden:
                                end_label = "keys"
                            elif args.other and not (args.golden or args.non_golden):
                                end_label = "codes"
                            else:
                                # default: prefer keys if any were in this batch, else codes; if none, both
                                if queue_keys_len > 0:
                                    end_label = "keys"
                                elif queue_codes_len > 0:
                                    end_label = "codes"
                                else:
                                    end_label = "keys & codes"
                            _L.info(f"No more {end_label} left!")
                            last_end_label = end_label
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

                        if _is_key_reward(attempt_key):
                            if _is_golden(attempt_key):
                                failed_g += 1
                            else:
                                failed_ng += 1
                        else:
                            failed_codes += 1
                        # don't spam if we reached the hourly limit
                        if status == Status.TRYLATER:
                            return

                ignored_total = ignored_redeemed_g + ignored_redeemed_ng + ignored_redeemed_codes
                if ignored_total:
                    _L.info(
                        f"\t{ignored_redeemed_g} Golden Keys, {ignored_redeemed_ng} Non-Golden Keys, {ignored_redeemed_codes} Codes IGNORED (already redeemed)."
                    )

                _L.info(
                    f"\t{failed_g} Golden Keys, {failed_ng} Non-Golden Keys, {failed_codes} Codes FAILED."
                )

        # Final end-of-run label: based on flags, or on what was actually redeemed
        # DEV NOTE: We track any_keys_redeemed/any_codes_redeemed to produce a truthful final summary.
        if args.golden or args.non_golden:
            final_label = "keys"
        elif args.other and not (args.golden or args.non_golden):
            final_label = "codes"
        else:
            if any_keys_redeemed:
                final_label = "keys"
            elif any_codes_redeemed:
                final_label = "codes"
            else:
                final_label = "keys & codes"
        if last_end_label != final_label:
            _L.info(f"No more {final_label} left!")


if __name__ == "__main__":

    # only print license text on first use (profile-aware path)
    if not os.path.exists(data_path(".cookies.save")):
        print(LICENSE_TEXT)

    # build argument parser
    parser = setup_argparser()
    args = parser.parse_args()
    # Track whether the user explicitly supplied --limit
    limit_was_supplied = (args.limit is not None)
    if args.limit is None:
        args.limit = 200
    setattr(args, "_limit_was_supplied", limit_was_supplied)

    # DEV NOTE: Enforce mutual exclusivity for mode flags per product spec.
    # If users want all categories, they should omit --golden/--non-golden/--other.
    # Enforce mutual exclusivity for mode flags: choose only one, or none for all
    mode_count = int(bool(getattr(args, 'golden', False))) + int(bool(getattr(args, 'non_golden', False))) + int(bool(getattr(args, 'other', False)))
    if mode_count > 1:
        _L.error("Please choose only one of --golden, --non-golden, or --other. For all types, omit these flags.")
        sys.exit(2)

    args.pw = getattr(args, "pass")

    _L.setLevel(INFO)
    if args.verbose:
        _L.setLevel(DEBUG)
        _L.debug("Debug mode on")

    if getattr(args, "dump_csv", None):
        dump_db_to_csv(args.dump_csv)
        sys.exit(0)

    if args.schedule and args.schedule < 2: # DEV NOTE (Scheduling): Enforce minimum cadence of 2 hours to avoid platform blocks and to match log text.
        _L.warning(
            f"Running this tool every {args.schedule} hours would result in "
            "too many requests.\n"
            "Scheduling changed to run every 2 hours!"
        )
        args.schedule = 2.0

    # always execute at least once
    main(args)

    # scheduling will start after first trigger (so in an hour..)
    if args.schedule:
        hours = int(args.schedule)
        minutes = int((args.schedule - hours) * 60 + 1e-5)
        _L.info(f"Scheduling to run every {hours:02}:{minutes:02} hours")
        from apscheduler.schedulers.blocking import BlockingScheduler

        scheduler = BlockingScheduler()
        total_minutes = hours * 60 + minutes
        # DEV NOTE (Scheduling): Use integer minutes on the interval trigger instead of a float 'hours='.
        #   - Keeps APScheduler happy and supports fractional hours (HH:MM).
        #   - Optional safety margin below nudges away from exact-hour collisions.
        # If you want a safety margin, add it here (e.g., +5).
        # total_minutes += 5
        # Optionally add coalescing/misfire_grace_time if the process may sleep:
        # scheduler.add_job(main, "interval", args=(args,), minutes=total_minutes, coalesce=True, misfire_grace_time=300)

        scheduler.add_job(
            main, "interval", args=(args,), minutes=total_minutes,
            coalesce=True, max_instances=1, misfire_grace_time=300
        )
        print(f"Press Ctrl+{'Break' if os.name == 'nt' else 'C'} to exit")

        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            pass
    _L.info("Goodbye.")
