import os
import re
import sqlite3
from typing import List, Union
from common import DIRNAME, _L

try:
    from common import DATA_DIR, data_path
except Exception:
    DATA_DIR = os.path.join(DIRNAME, "data")

    def data_path(*parts):
        os.makedirs(DATA_DIR, exist_ok=True)
        return os.path.join(DATA_DIR, *parts)


DB_PATH = data_path("keys.db")
CODE_PATTERN = re.compile(r"^[A-Za-z0-9]{5}(?:-[A-Za-z0-9]{5}){4}$")


def migrate_shift_codes():
    """Remove codes from the database that do not match the required format."""
    if not os.path.exists(DB_PATH):
        return
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        # Check if the 'code' column exists before querying it
        c.execute("PRAGMA table_info(keys)")
        columns = [row[1] for row in c.fetchall()]
        if "code" not in columns:
            # DEV NOTE: Older schemas rename `key`->`code` via versioned migrations.
            # Avoid user-facing noise when the upgrade hasn't run yet.
            _L.debug("Skipping code format migration: 'code' column does not exist yet.")
            return
        c.execute("SELECT id, code FROM keys")
        rows = c.fetchall()
        to_remove = []
        for row in rows:
            code_raw = str(row[1])
            code_stripped = code_raw.strip()
            if not CODE_PATTERN.match(code_stripped):
                to_remove.append((row[0], code_raw))
        if to_remove:
            c.executemany("DELETE FROM keys WHERE id = ?", [(i,) for i, _ in to_remove])
            conn.commit()
            removed_codes = [code for _, code in to_remove]
            _L.info(
                f"Migration: Removed {len(removed_codes)} invalid shift codes from database."
            )
            for code in removed_codes:
                _L.info(f"Removed code: {code!r}")
    finally:
        conn.close()


# --- Versioned schema migrations ---

migrationFunctions = {}


def register(version: int):
    from functools import wraps

    def wrap(func):
        @wraps(func)
        def wrapper(conn, *args, **kwargs):  # Accept extra arguments for compatibility
            try:
                if func(conn):
                    conn.cursor().execute("PRAGMA user_version = {}".format(version))
                    conn.commit()
                    return True
            except sqlite3.OperationalError:
                _L.error(
                    "There was an error while migrating database to version {}."
                    "Please contact the developer @ github.com/fabbi".format(version)
                )
                return False

        migrationFunctions[version] = wrapper
        return wrapper

    return wrap


@register(1)
def update_1(conn: sqlite3.Connection):
    c = conn.cursor()

    steps: List[Union[str, tuple]] = [
        """
        ALTER TABLE keys
        RENAME COLUMN description TO reward;
        """,
        """
        ALTER TABLE keys
        RENAME COLUMN key TO code;
        """,
        """
        CREATE VIEW tmp AS
        SELECT * FROM keys
        WHERE platform="pc";
        -- AND redeemed=0;

        INSERT INTO keys(reward, code, platform, game, redeemed)
        SELECT reward, code, "epic", game, redeemed
        FROM tmp;

        UPDATE keys
        SET platform="steam"
        WHERE id in (select id from tmp);

        DROP VIEW tmp;
        """,
        """
        UPDATE keys
        SET game="bl1"
        WHERE game="bl";
        """,
        """
        CREATE TABLE IF NOT EXISTS seen_platforms
        (key TEXT primary key, name TEXT)
        """,
        """
        CREATE TABLE IF NOT EXISTS seen_games
        (key TEXT primary key, name TEXT)
        """,
    ]

    from query import known_games, known_platforms

    for game in known_games.items():
        steps.append(("""INSERT INTO seen_games(key, name) VALUES(?, ?)""", game))
    for platform in known_platforms.items():
        steps.append(
            ("""INSERT INTO seen_platforms(key, name) VALUES(?, ?)""", platform)
        )

    for i, step in enumerate(steps):
        _L.debug("executing step {} of update_1".format(i))
        try:
            if isinstance(step, tuple):
                c.execute(*step)
            else:
                c.executescript(step)
        except sqlite3.OperationalError as e:
            _L.error(f"'{e}' in\n{step}")
            return False
    return True


@register(2)
def migrate_redeemed_per_platform(conn):
    """
    Ensure that each (code, game, platform) has its own redeemed status.
    If there are any rows with the same code/game but different platforms,
    ensure each has its own redeemed field.
    """
    c = conn.cursor()
    # Check for duplicate codes across platforms
    c.execute(
        """
        SELECT code, game, COUNT(DISTINCT platform) as platform_count
        FROM keys
        GROUP BY code, game
        HAVING platform_count > 1
    """
    )
    duplicates = c.fetchall()
    # If there are duplicates, ensure each has its own redeemed field (already true in schema)
    # This migration is a no-op unless you want to do data cleanup.
    # Optionally, you could log or clean up here.
    return True


@register(3)
def migrate_redeemed_table(conn):
    """
    Move redeemed status to a new redeemed_keys table and remove the redeemed column from keys.
    Do NOT migrate redeemed=1 rows, as we cannot know for which platforms they were redeemed.
    """
    c = conn.cursor()
    # 1. Create new table if not exists
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS redeemed_keys (
            key_id INTEGER,
            platform TEXT,
            PRIMARY KEY (key_id, platform),
            FOREIGN KEY (key_id) REFERENCES keys(id)
        )
    """
    )
    # 2. Remove redeemed column from keys (SQLite doesn't support DROP COLUMN directly)
    c.execute("PRAGMA table_info(keys)")
    columns = [col[1] for col in c.fetchall() if col[1] != "redeemed"]
    columns_str = ", ".join(columns)
    c.execute(f"ALTER TABLE keys RENAME TO keys_old")
    c.execute(
        f"""
        CREATE TABLE keys (
            id INTEGER primary key,
            reward TEXT,
            code TEXT,
            platform TEXT,
            game TEXT
        )
    """
    )
    c.execute(f"INSERT INTO keys ({columns_str}) SELECT {columns_str} FROM keys_old")
    c.execute("DROP TABLE keys_old")
    conn.commit()
    return True


@register(4)
def add_failed_keys_table(conn: sqlite3.Connection):
    """Add failed_keys outcome table and extend keys metadata."""

    c = conn.cursor()

    # Ensure failed_keys exists to persist negative outcomes per (key, platform)
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS failed_keys (
            key_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT,
            attempted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (key_id, platform),
            FOREIGN KEY (key_id) REFERENCES keys(id)
        )
        """
    )

    # Add provenance column to keys if missing
    c.execute("PRAGMA table_info(keys)")
    existing_columns = {row[1] for row in c.fetchall()}
    if "source" not in existing_columns:
        c.execute("ALTER TABLE keys ADD COLUMN source TEXT")

    conn.commit()
    return True


@register(5)
def extend_redeemed_keys_table(conn: sqlite3.Connection):
    """Add outcome metadata columns to redeemed_keys for parity with failed_keys."""

    c = conn.cursor()
    c.execute("PRAGMA table_info(redeemed_keys)")
    columns_info = c.fetchall()
    column_names = {row[1] for row in columns_info}

    if not column_names:
        # Table missing; create a fresh one.
        c.execute(
            """
            CREATE TABLE redeemed_keys (
                key_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                attempted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (key_id, platform),
                FOREIGN KEY (key_id) REFERENCES keys(id)
            )
            """
        )
        c.execute("DROP TABLE IF EXISTS redeemed_keys_old_v5")
        conn.commit()
        return True

    # Detect legacy FK pointing to keys_old or missing columns
    c.execute("PRAGMA foreign_key_list(redeemed_keys)")
    fk_targets = {row[2] for row in c.fetchall()}
    needs_rebuild = ("status" not in column_names) or ("detail" not in column_names)
    needs_rebuild = needs_rebuild or ("attempted_at" not in column_names)
    needs_rebuild = needs_rebuild or (fk_targets and "keys" not in fk_targets)

    if needs_rebuild:
        # Rebuild table to ensure clean schema and preserve existing data when available.
        c.execute("ALTER TABLE redeemed_keys RENAME TO redeemed_keys_old_v5")
        c.execute(
            """
            CREATE TABLE redeemed_keys (
                key_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                attempted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (key_id, platform),
                FOREIGN KEY (key_id) REFERENCES keys(id)
            )
            """
        )

        existing_cols = column_names
        select_columns = ["key_id", "platform"]
        status_expr = "COALESCE(status, 'SUCCESS')" if "status" in existing_cols else "'SUCCESS'"
        select_columns.append(f"{status_expr} AS status")
        detail_expr = "detail" if "detail" in existing_cols else "NULL"
        select_columns.append(f"{detail_expr} AS detail")
        attempted_expr = (
            "COALESCE(attempted_at, CURRENT_TIMESTAMP)"
            if "attempted_at" in existing_cols
            else "CURRENT_TIMESTAMP"
        )
        select_columns.append(f"{attempted_expr} AS attempted_at")

        select_sql = ", ".join(select_columns)
        c.execute(
            f"INSERT OR IGNORE INTO redeemed_keys (key_id, platform, status, detail, attempted_at)\n"
            f"SELECT {select_sql} FROM redeemed_keys_old_v5"
        )
        c.execute("DROP TABLE redeemed_keys_old_v5")
        conn.commit()
        return True

    # Columns exist but may be NULL; backfill defaults and ensure attempted_at present
    if "status" in column_names:
        c.execute("UPDATE redeemed_keys SET status = COALESCE(status, 'SUCCESS')")
    if "attempted_at" in column_names:
        c.execute(
            "UPDATE redeemed_keys SET attempted_at = COALESCE(attempted_at, CURRENT_TIMESTAMP)"
        )

    conn.commit()
    return True


@register(6)
def backfill_seen_platform_names(conn: sqlite3.Connection):
    """Ensure seen_platforms entries have a non-empty display name."""

    c = conn.cursor()
    c.execute(
        """
        UPDATE seen_platforms
        SET name = key
        WHERE name IS NULL OR TRIM(name) = ''
        """
    )
    conn.commit()
    return True
