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

import os, sys  # must run before importing common/query

# Early profile bootstrap: set AUTOSHIFT_PROFILE before common.py is imported
if "--profile" in sys.argv:
    i = sys.argv.index("--profile")
    if i + 1 < len(sys.argv):
        os.environ["AUTOSHIFT_PROFILE"] = sys.argv[i + 1]

from common import _L, DEBUG, DIRNAME, INFO, data_path, DATA_DIR
from typing import Match, cast, TYPE_CHECKING

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
    import query
    from shift import Status

    """Redeem key and set as redeemed if successfull"""

    _L.info(f"Trying to redeem {key.reward} ({key.code}) on {key.platform}")
    # use query.known_games (query imported above) instead of relying on a global name
    status = client.redeem(key.code, query.known_games[key.game], key.platform)
    _L.debug(f"Status: {status}")

    # set redeemed status
    if status in (Status.SUCCESS, Status.REDEEMED, Status.EXPIRED, Status.INVALID):
        query.db.set_redeemed(key)

    # notify user
    try:
        # this may fail if there are other `{<something>}` in the string..
        _L.info("  " + status.msg.format(**locals()))
    except:
        _L.info("  " + status.msg)

    return status == Status.SUCCESS


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
                continue
            game, plats = entry.split(":", 1)
            mapping[game] = [p.strip() for p in plats.split(",") if p.strip()]
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
    import os
    import sqlite3
    from query import db, Key

    # Always write into the profile-aware data directory
    os.makedirs(DATA_DIR, exist_ok=True)
    base = os.path.basename(filename)
    out_path = data_path(base)

    with db:
        conn = db._Database__conn  # Access the underlying sqlite3.Connection
        c = conn.cursor()
        c.execute("SELECT * FROM keys")
        rows = c.fetchall()
        if not rows:
            _L.info("No data to dump.")
            return
        headers = [desc[0] for desc in c.description]
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for row in rows:
                writer.writerow([row[h] for h in headers])
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
        #          Without --other, codes are excluded when a mode flag is used, matching requested behavior.
    parser.add_argument(
        "--other",
        action="store_true",
        help="Also redeem generic non-key codes (Unknown/cosmetics). Without this, Codes are excluded when using --golden or --non-golden.",
    )
    # Provide static choices so argparse can validate without importing query/db
    parser.add_argument(
        "--games",
        type=str,
        required=False,
        choices=games,
        nargs="+",
        help=("Games you want to query SHiFT keys for"),
    )
    parser.add_argument(
        "--platforms",
        type=str,
        required=False,
        choices=platforms,
        nargs="+",
        help=("Platforms you want to query SHiFT keys for"),
    )
    parser.add_argument(
        "--redeem",
        type=str,
        nargs="+",
        help="Specify which platforms to redeem which games for. Format: bl3:steam,epic bl2:epic",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help=textwrap.dedent(
            """\
                        Max number of golden Keys you want to redeem.
                        (default 200)
                        NOTE: You can only have 255 keys at any given time!"""
        ),
    )  # noqa
    parser.add_argument(
        "--schedule",
        type=float,
        const=2,
        nargs="?",
        help="Keep checking for keys and redeeming every hour",
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
    from query import db, r_golden_keys, known_games, known_platforms, Key
    from shift import ShiftClient, Status

    # apply shift source override (CLI takes precedence over env)
    shift_src = (
        args.shift_source
        if getattr(args, "shift_source", None)
        else os.getenv("SHIFT_SOURCE")
    )
    if shift_src:
        query.set_shift_source(shift_src)

    redeem_mapping = parse_redeem_mapping(args)
    if redeem_mapping:
        # New mapping mode
        games = list(redeem_mapping.keys())
        platforms = sorted(set(p for plats in redeem_mapping.values() for p in plats))
        _L.info("Redeem mapping (game: platforms):")
        for game, plats in redeem_mapping.items():
            _L.info(f"  {game}: {', '.join(plats)}")
    else:
        # Legacy mode
        games = args.games
        platforms = args.platforms
        _L.warning(
            "You are using the legacy --games/--platforms format. "
            "In the future, use --redeem bl3:steam,epic bl2:epic for more control."
        )
        _L.info("Redeeming all of these games/platforms combinations:")
        _L.info(f"  Games: {', '.join(games) if games else '(none)'}")
        _L.info(f"  Platforms: {', '.join(platforms) if platforms else '(none)'}")

    with db:
        if not client:
            # Decide which password to use. CLI may have been affected by shell history
            # expansion (e.g. '!' truncation). Prefer environment SHIFT_PASS (or
            # AUTOSHIFT_PASS_RAW) if it appears more complete.
            env_pw = os.getenv("SHIFT_PASS") or os.getenv("AUTOSHIFT_PASS_RAW")
            chosen_pw = args.pw
            pw_source = "cli"
            if args.pw:
                # heuristic: if CLI pw contains '!' and env_pw looks longer, prefer env
                if "!" in args.pw and env_pw and len(env_pw) > len(args.pw):
                    chosen_pw = env_pw
                    pw_source = "env(SHIFT_PASS/AUTOSHIFT_PASS_RAW)"
            else:
                # no CLI pw, use env if present
                if env_pw:
                    chosen_pw = env_pw
                    pw_source = "env(SHIFT_PASS/AUTOSHIFT_PASS_RAW)"

            _L.debug(f"Using password from: {pw_source}")
            client = ShiftClient(args.user, chosen_pw)

        all_keys = query_keys_with_mapping(redeem_mapping, games, platforms)

        _L.info("Trying to redeem now.")

        # Track what actually got redeemed across the whole run
        any_keys_redeemed = False
        any_codes_redeemed = False

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
                explicit_limit = ("--limit" in sys.argv)

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

                # Iterate and redeem
                for idx, key in enumerate(redeem_queue):
                    # polite throttling
                    if (idx and not (idx % 15)) or client.last_status == Status.SLOWDOWN:
                        if client.last_status == Status.SLOWDOWN:
                            _L.info("Slowing down a bit..")
                        else:
                            _L.info("Trying to prevent a 'too many requests'-block.")
                        sleep(60)

                    # Decide golden/non-golden for this item
                    m = r_golden_keys.match(key.reward or "")

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

                    # Per-item progress line
                    if _is_key_reward(key):
                        k_index += 1
                        label = f"Key #{k_index}/{queue_keys_len}"
                    else:
                        c_index += 1
                        label = f"Code #{c_index}/{queue_codes_len}"
                    _L.info(f"{label} for {game} on {platform}")

                    # Attempt redeem
                    redeemed = redeem(key)

                    if redeemed:
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
                    else:
                        # don't spam if we reached the hourly limit
                        if client.last_status == Status.TRYLATER:
                            return

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
        _L.info(f"No more {final_label} left!")


if __name__ == "__main__":
    import os

    # only print license text on first use (profile-aware path)
    if not os.path.exists(data_path(".cookies.save")):
        print(LICENSE_TEXT)

    # build argument parser
    parser = setup_argparser()
    args = parser.parse_args()

    # DEV NOTE: Enforce mutual exclusivity for mode flags per product spec.
    #          If users want all categories, they should omit --golden/--non-golden/--other.
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

    if args.schedule and args.schedule < 2:
        _L.warn(
            f"Running this tool every {args.schedule} hours would result in "
            "too many requests.\n"
            "Scheduling changed to run every 2 hours!"
        )

    # always execute at least once
    main(args)

    # scheduling will start after first trigger (so in an hour..)
    if args.schedule:
        hours = int(args.schedule)
        minutes = int((args.schedule - hours) * 60 + 1e-5)
        _L.info(f"Scheduling to run every {hours:02}:{minutes:02} hours")
        from apscheduler.schedulers.blocking import BlockingScheduler

        scheduler = BlockingScheduler()
        # fire every 1h5m (to prevent being blocked by the shift platform.)
        #  (5min safe margin because it somtimes fires a few seconds too early)
        scheduler.add_job(main, "interval", args=(args,), hours=args.schedule)
        print(f"Press Ctrl+{'Break' if os.name == 'nt' else 'C'} to exit")

        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            pass
    _L.info("Goodbye.")
