from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Claim:
    parameter: str
    source_chat_id: int | None
    source_message_id: int | None
    random_id: int


class ProcessedStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None
        self._toggle_lock = asyncio.Lock()
        self._settings_lock = asyncio.Lock()

    async def open(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=FULL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        integrity_cursor = await self._db.execute("PRAGMA quick_check")
        integrity = await integrity_cursor.fetchone()
        if integrity is None or integrity[0] != "ok":
            raise RuntimeError("Проверка целостности SQLite не пройдена")
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_starts (
                parameter TEXT PRIMARY KEY,
                status TEXT NOT NULL CHECK(status IN ('processing', 'sent', 'failed')),
                source_chat_id INTEGER,
                source_message_id INTEGER,
                claimed_at TEXT NOT NULL,
                finished_at TEXT,
                error TEXT,
                random_id INTEGER
            )
            """
        )
        processed_columns_cursor = await self._db.execute(
            "PRAGMA table_info(processed_starts)"
        )
        processed_columns = {
            str(row[1]) for row in await processed_columns_cursor.fetchall()
        }
        if "random_id" not in processed_columns:
            await self._db.execute(
                "ALTER TABLE processed_starts ADD COLUMN random_id INTEGER"
            )
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS observed_chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                username TEXT,
                chat_type TEXT NOT NULL DEFAULT 'unknown',
                last_seen_at TEXT NOT NULL
            )
            """
        )
        columns_cursor = await self._db.execute("PRAGMA table_info(observed_chats)")
        columns = {str(row[1]) for row in await columns_cursor.fetchall()}
        if "chat_type" not in columns:
            await self._db.execute(
                """
                ALTER TABLE observed_chats
                ADD COLUMN chat_type TEXT NOT NULL DEFAULT 'unknown'
                """
            )
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS ignored_chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                added_at TEXT NOT NULL
            )
            """
        )
        await self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS system_ignored_chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL
            )
            """
        )
        await self._db.executemany(
            """
            INSERT OR IGNORE INTO runtime_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            """,
            (
                ("catcher_enabled", "1", _now()),
                ("notifications_enabled", "1", _now()),
                ("worker_connected", "0", _now()),
            ),
        )
        await self._db.execute(
            """
            UPDATE processed_starts
            SET status = 'failed',
                finished_at = ?,
                error = 'Legacy operation without idempotency key'
            WHERE status = 'processing' AND random_id IS NULL
            """,
            (_now(),),
        )
        await self._db.commit()
        await self._db.execute("PRAGMA user_version=2")

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    def _connection(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("База данных не открыта")
        return self._db

    async def claim(self, claim: Claim) -> bool:
        db = self._connection()
        cursor = await db.execute(
            """
            INSERT OR IGNORE INTO processed_starts (
                parameter, status, source_chat_id, source_message_id,
                claimed_at, random_id
            ) VALUES (?, 'processing', ?, ?, ?, ?)
            """,
            (
                claim.parameter,
                claim.source_chat_id,
                claim.source_message_id,
                _now(),
                claim.random_id,
            ),
        )
        await db.commit()
        return cursor.rowcount == 1

    async def pending_claims(self) -> list[Claim]:
        db = self._connection()
        cursor = await db.execute(
            """
            SELECT parameter, source_chat_id, source_message_id, random_id
            FROM processed_starts
            WHERE status = 'processing'
            ORDER BY claimed_at
            """
        )
        claims: list[Claim] = []
        for parameter, chat_id, message_id, random_id in await cursor.fetchall():
            if random_id is None:
                continue
            claims.append(
                Claim(
                    parameter=str(parameter),
                    source_chat_id=chat_id,
                    source_message_id=message_id,
                    random_id=int(random_id),
                )
            )
        return claims

    async def mark_sent(self, parameter: str) -> None:
        db = self._connection()
        await db.execute(
            """
            UPDATE processed_starts
            SET status = 'sent', finished_at = ?, error = NULL
            WHERE parameter = ? AND status = 'processing'
            """,
            (_now(), parameter),
        )
        await db.commit()

    async def mark_failed(self, parameter: str, error: str) -> None:
        db = self._connection()
        await db.execute(
            """
            UPDATE processed_starts
            SET status = 'failed', finished_at = ?, error = ?
            WHERE parameter = ? AND status = 'processing'
            """,
            (_now(), error[:1000], parameter),
        )
        await db.commit()

    async def get_bool_setting(self, key: str, default: bool) -> bool:
        db = self._connection()
        cursor = await db.execute(
            "SELECT value FROM runtime_settings WHERE key = ?",
            (key,),
        )
        row = await cursor.fetchone()
        if row is None:
            return default
        return str(row[0]).lower() in {"1", "true", "yes", "on"}

    async def set_bool_setting(self, key: str, value: bool) -> None:
        db = self._connection()
        await db.execute(
            """
            INSERT INTO runtime_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, "1" if value else "0", _now()),
        )
        await db.commit()

    async def toggle_bool_setting(self, key: str, default: bool) -> bool:
        async with self._settings_lock:
            current = await self.get_bool_setting(key, default)
            await self.set_bool_setting(key, not current)
            return not current

    async def statistics(self) -> dict[str, int]:
        db = self._connection()
        cursor = await db.execute(
            """
            SELECT status, COUNT(*)
            FROM processed_starts
            GROUP BY status
            """
        )
        result = {"processing": 0, "sent": 0, "failed": 0}
        for status, count in await cursor.fetchall():
            result[str(status)] = int(count)
        return result

    async def recent(self, limit: int = 10) -> list[tuple[str, str, str]]:
        db = self._connection()
        cursor = await db.execute(
            """
            SELECT parameter, status, COALESCE(finished_at, claimed_at)
            FROM processed_starts
            ORDER BY claimed_at DESC
            LIMIT ?
            """,
            (max(1, min(limit, 30)),),
        )
        return [
            (str(parameter), str(status), str(timestamp))
            for parameter, status, timestamp in await cursor.fetchall()
        ]

    async def remember_chat(
        self,
        chat_id: int,
        title: str,
        username: str | None,
        chat_type: str = "unknown",
    ) -> None:
        db = self._connection()
        await db.execute(
            """
            INSERT INTO observed_chats (
                chat_id, title, username, chat_type, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                username = excluded.username,
                chat_type = excluded.chat_type,
                last_seen_at = excluded.last_seen_at
            """,
            (chat_id, title[:200], username, chat_type, _now()),
        )
        await db.commit()

    async def sync_dialogs(
        self,
        dialogs: list[tuple[int, str, str | None, str, str]],
    ) -> None:
        if not dialogs:
            return
        db = self._connection()
        await db.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS current_dialog_ids (
                chat_id INTEGER PRIMARY KEY
            )
            """
        )
        await db.execute("DELETE FROM current_dialog_ids")
        await db.executemany(
            "INSERT INTO current_dialog_ids (chat_id) VALUES (?)",
            [(chat_id,) for chat_id, _, _, _, _ in dialogs],
        )
        await db.executemany(
            """
            INSERT INTO observed_chats (
                chat_id, title, username, chat_type, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                username = excluded.username,
                chat_type = excluded.chat_type,
                last_seen_at = CASE
                    WHEN excluded.last_seen_at > observed_chats.last_seen_at
                    THEN excluded.last_seen_at
                    ELSE observed_chats.last_seen_at
                END
            """,
            [
                (chat_id, title[:200], username, chat_type, last_seen_at)
                for chat_id, title, username, chat_type, last_seen_at in dialogs
            ],
        )
        await db.execute(
            """
            DELETE FROM observed_chats
            WHERE NOT EXISTS (
                SELECT 1
                FROM current_dialog_ids
                WHERE current_dialog_ids.chat_id = observed_chats.chat_id
            )
            """
        )
        await db.commit()

    async def is_chat_ignored(self, chat_id: int) -> bool:
        db = self._connection()
        cursor = await db.execute(
            """
            SELECT 1 FROM ignored_chats WHERE chat_id = ?
            UNION ALL
            SELECT 1 FROM system_ignored_chats WHERE chat_id = ?
            LIMIT 1
            """,
            (chat_id, chat_id),
        )
        return await cursor.fetchone() is not None

    async def is_system_ignored(self, chat_id: int) -> bool:
        db = self._connection()
        cursor = await db.execute(
            "SELECT 1 FROM system_ignored_chats WHERE chat_id = ?",
            (chat_id,),
        )
        return await cursor.fetchone() is not None

    async def replace_system_ignored(
        self,
        chats: list[tuple[int, str]],
    ) -> None:
        db = self._connection()
        await db.execute("DELETE FROM system_ignored_chats")
        await db.executemany(
            """
            INSERT INTO system_ignored_chats (chat_id, title)
            VALUES (?, ?)
            """,
            [(chat_id, title[:200]) for chat_id, title in chats],
        )
        await db.execute(
            """
            DELETE FROM ignored_chats
            WHERE chat_id IN (SELECT chat_id FROM system_ignored_chats)
            """
        )
        await db.commit()

    async def set_chat_ignored(
        self,
        chat_id: int,
        ignored: bool,
        title: str | None = None,
    ) -> bool:
        async with self._toggle_lock:
            db = self._connection()
            if await self.is_system_ignored(chat_id):
                return False
            if ignored:
                if title is None:
                    cursor = await db.execute(
                        "SELECT title FROM observed_chats WHERE chat_id = ?",
                        (chat_id,),
                    )
                    row = await cursor.fetchone()
                    title = str(row[0]) if row else str(chat_id)
                await db.execute(
                    """
                    INSERT INTO ignored_chats (chat_id, title, added_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        title = excluded.title,
                        added_at = excluded.added_at
                    """,
                    (chat_id, title[:200], _now()),
                )
            else:
                await db.execute(
                    "DELETE FROM ignored_chats WHERE chat_id = ?",
                    (chat_id,),
                )
            await db.commit()
            return True

    async def toggle_chat_ignored(self, chat_id: int) -> bool | None:
        async with self._toggle_lock:
            if await self.is_system_ignored(chat_id):
                return None
            db = self._connection()
            cursor = await db.execute(
                "SELECT 1 FROM ignored_chats WHERE chat_id = ?",
                (chat_id,),
            )
            ignored = await cursor.fetchone() is not None
            if ignored:
                await db.execute(
                    "DELETE FROM ignored_chats WHERE chat_id = ?",
                    (chat_id,),
                )
            else:
                cursor = await db.execute(
                    "SELECT title FROM observed_chats WHERE chat_id = ?",
                    (chat_id,),
                )
                row = await cursor.fetchone()
                title = str(row[0]) if row else str(chat_id)
                await db.execute(
                    """
                    INSERT INTO ignored_chats (chat_id, title, added_at)
                    VALUES (?, ?, ?)
                    """,
                    (chat_id, title[:200], _now()),
                )
            await db.commit()
            return not ignored

    async def ignored_chats(self) -> list[tuple[int, str, bool]]:
        db = self._connection()
        cursor = await db.execute(
            """
            SELECT chat_id, title, 1 AS locked, '' AS sort_key
            FROM system_ignored_chats
            UNION ALL
            SELECT chat_id, title, 0 AS locked, added_at AS sort_key
            FROM ignored_chats
            ORDER BY locked DESC, sort_key DESC
            """
        )
        return [
            (int(chat_id), str(title), bool(locked))
            for chat_id, title, locked, _ in await cursor.fetchall()
        ]

    @staticmethod
    def _dialog_filter(view: str) -> tuple[str, tuple[str, ...]]:
        if view in {"user", "bot", "group", "channel"}:
            return "WHERE observed.chat_type = ?", (view,)
        if view == "ignored":
            return (
                """
                WHERE ignored.chat_id IS NOT NULL
                   OR system_ignored.chat_id IS NOT NULL
                """,
                (),
            )
        return "", ()

    async def chat_count(self, view: str = "all") -> int:
        db = self._connection()
        where, params = self._dialog_filter(view)
        cursor = await db.execute(
            f"""
            SELECT COUNT(*)
            FROM observed_chats AS observed
            LEFT JOIN ignored_chats AS ignored USING (chat_id)
            LEFT JOIN system_ignored_chats AS system_ignored USING (chat_id)
            {where}
            """,
            params,
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def chats_page(
        self,
        page: int,
        page_size: int = 8,
        view: str = "all",
    ) -> list[tuple[int, str, str | None, str, bool, bool]]:
        db = self._connection()
        safe_page = max(0, page)
        safe_page_size = max(1, min(page_size, 20))
        where, filter_params = self._dialog_filter(view)
        cursor = await db.execute(
            f"""
            SELECT
                observed.chat_id,
                observed.title,
                observed.username,
                observed.chat_type,
                CASE WHEN ignored.chat_id IS NULL THEN 0 ELSE 1 END,
                CASE WHEN system_ignored.chat_id IS NULL THEN 0 ELSE 1 END
            FROM observed_chats AS observed
            LEFT JOIN ignored_chats AS ignored USING (chat_id)
            LEFT JOIN system_ignored_chats AS system_ignored USING (chat_id)
            {where}
            ORDER BY observed.last_seen_at DESC
            LIMIT ?
            OFFSET ?
            """,
            (*filter_params, safe_page_size, safe_page * safe_page_size),
        )
        return [
            (
                int(chat_id),
                str(title),
                username,
                str(chat_type),
                bool(ignored),
                bool(locked),
            )
            for chat_id, title, username, chat_type, ignored, locked
            in await cursor.fetchall()
        ]
