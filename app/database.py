from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal, localcontext
import secrets

import aiosqlite
from app.receipts import BotReply, source_links


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Claim:
    parameter: str
    source_chat_id: int | None
    source_message_id: int | None
    random_id: int
    source_title: str | None = None
    source_username: str | None = None
    source_type: str = 'unknown'


class ProcessedStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None
        self._toggle_lock = asyncio.Lock()
        self._settings_lock = asyncio.Lock()
        self._results_lock = asyncio.Lock()

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
        await self._db.executescript('''
            CREATE TABLE IF NOT EXISTS claim_details (
                parameter TEXT PRIMARY KEY,
                source_title TEXT, source_username TEXT, source_type TEXT,
                chat_url TEXT, message_url TEXT,
                outcome TEXT NOT NULL DEFAULT 'queued',
                request_message_id INTEGER, boundary_id INTEGER, requested_at TEXT,
                response_message_id INTEGER, amount TEXT, asset TEXT, usd TEXT,
                correlation TEXT, updated_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS claim_request_id
                ON claim_details(request_message_id) WHERE request_message_id IS NOT NULL;
            CREATE TABLE IF NOT EXISTS bot_receipts (
                message_id INTEGER PRIMARY KEY, kind TEXT NOT NULL,
                amount TEXT, asset TEXT, usd TEXT, parameter TEXT,
                received_at TEXT NOT NULL, correlation TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS receipt_per_check
                ON bot_receipts(parameter) WHERE kind='received' AND parameter IS NOT NULL;
            CREATE TABLE IF NOT EXISTS notification_outbox (
                event_key TEXT PRIMARY KEY, body TEXT NOT NULL, random_id INTEGER NOT NULL,
                created_at TEXT NOT NULL, sent_at TEXT
            );
            INSERT OR IGNORE INTO claim_details(parameter,outcome,updated_at)
                SELECT parameter,CASE WHEN status='processing' THEN 'queued'
                WHEN status='failed' THEN 'legacy_failed' ELSE 'legacy' END,
                COALESCE(finished_at,claimed_at) FROM processed_starts;
        ''')
        # Preserve deduplication and old transport statuses; never call legacy sends income.
        await self._db.execute("PRAGMA user_version=3")
        await self._db.commit()

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
        if cursor.rowcount == 1:
            chat_url, message_url = source_links(claim.source_chat_id, claim.source_message_id, claim.source_username, claim.source_type)
            await db.execute('''INSERT OR IGNORE INTO claim_details
                (parameter,source_title,source_username,source_type,chat_url,message_url,outcome,updated_at)
                VALUES (?,?,?,?,?,?,'queued',?)''',
                (claim.parameter,claim.source_title,claim.source_username,claim.source_type,chat_url,message_url,_now()))
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

    async def detail(self, parameter: str) -> dict:
        db = self._connection()
        cursor = await db.execute('''SELECT d.*,p.source_chat_id,p.source_message_id,p.claimed_at,
            COALESCE(d.source_title,c.title) AS display_title,
            COALESCE(d.source_username,c.username) AS display_username,
            COALESCE(NULLIF(d.source_type,'unknown'),c.chat_type,'unknown') AS display_type
            FROM claim_details d JOIN processed_starts p USING(parameter)
            LEFT JOIN observed_chats c ON c.chat_id=p.source_chat_id WHERE d.parameter=?''', (parameter,))
        row = await cursor.fetchone()
        result = dict(zip((column[0] for column in cursor.description),row)) if row else {}
        if result:
            result['source_title'] = result['display_title']
            if not result['chat_url']:
                result['chat_url'],result['message_url'] = source_links(result['source_chat_id'],result['source_message_id'],result['display_username'],result['display_type'])
        return result

    async def begin_attempt(self, parameter: str, boundary_id: int) -> None:
        await self._connection().execute('''UPDATE claim_details SET outcome='awaiting',
            boundary_id=?,requested_at=?,updated_at=? WHERE parameter=? AND outcome='queued' ''',
            (boundary_id,_now(),_now(),parameter))
        await self._connection().commit()

    async def register_request(self, parameter: str, message_id: int, requested_at: str | None = None) -> None:
        await self._connection().execute('UPDATE claim_details SET request_message_id=?,requested_at=? WHERE parameter=?', (message_id,requested_at or _now(),parameter))
        await self._connection().commit()

    async def finish_outcome(self, parameter: str, outcome: str, *, uncertain: bool = False) -> dict:
        async with self._results_lock:
            db = self._connection()
            cursor = await db.execute('''UPDATE claim_details SET outcome=?,updated_at=?
                WHERE parameter=? AND outcome IN ('queued','awaiting')''', (outcome,_now(),parameter))
            await db.commit()
            if uncertain and cursor.rowcount:
                await self.set_bool_setting('correlation_safe',False)
            return await self.detail(parameter) if cursor.rowcount else {}

    async def recover_attempts(self) -> int:
        db = self._connection()
        cursor = await db.execute("SELECT COUNT(*) FROM claim_details WHERE outcome='awaiting'")
        count = (await cursor.fetchone())[0]
        if count:
            await db.execute("UPDATE processed_starts SET status='sent',finished_at=? WHERE parameter IN (SELECT parameter FROM claim_details WHERE outcome='awaiting') AND status='processing'", (_now(),))
            await db.execute("UPDATE claim_details SET outcome='unconfirmed',updated_at=? WHERE outcome='awaiting'", (_now(),))
            await db.commit()
            await self.set_bool_setting('correlation_safe',False)
        return count

    async def record_bot_reply(self, message_id: int, reply: BotReply, request_id: int | None,
                               *, explicit: bool = False, received_at: str | None = None) -> dict | None:
        """Immutable receipt IDs and one payment per check prevent replay/edited-message income."""
        async with self._results_lock:
            db = self._connection()
            cursor = await db.execute('SELECT kind FROM bot_receipts WHERE message_id=?', (message_id,))
            existing = await cursor.fetchone()
            if existing and existing[0] in ('received','duplicate'): return None
            parameter, correlation = None, None
            candidate = None
            if request_id:
                cursor = await db.execute('SELECT parameter FROM claim_details WHERE request_message_id=?', (request_id,))
                row = await cursor.fetchone()
                if row: candidate = await self.detail(row[0])
            timestamp = received_at or _now()
            if candidate:
                within_attempt = candidate['requested_at'] and 0 <= (datetime.fromisoformat(timestamp)-datetime.fromisoformat(candidate['requested_at'])).total_seconds() <= 20
                safe = explicit or (within_attempt and await self.get_bool_setting('correlation_safe',True))
                if safe and message_id > (candidate['boundary_id'] or 0) and candidate['outcome'] == 'awaiting' and reply.kind != 'unknown':
                    parameter = candidate['parameter']
                    correlation = 'reply' if explicit else 'sequence'
                elif candidate['outcome'] == 'received' and reply.kind == 'received':
                    reply = BotReply('duplicate')
            await db.execute('''INSERT INTO bot_receipts(message_id,kind,amount,asset,usd,parameter,received_at,correlation)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(message_id) DO UPDATE SET
                kind=excluded.kind,amount=excluded.amount,asset=excluded.asset,usd=excluded.usd,
                parameter=excluded.parameter,correlation=excluded.correlation''',
                (message_id,reply.kind,reply.amount,reply.asset,reply.usd,parameter,timestamp,correlation))
            if parameter:
                await db.execute('''UPDATE claim_details SET outcome=?,response_message_id=?,
                    amount=?,asset=?,usd=?,correlation=?,updated_at=? WHERE parameter=?''',
                    (reply.kind,message_id,reply.amount,reply.asset,reply.usd,correlation,_now(),parameter))
            await db.commit()
            if parameter: return await self.detail(parameter)
            if reply.kind == 'received':
                return {'outcome':'received','amount':reply.amount,'asset':reply.asset,'usd':reply.usd,'parameter':None,'response_message_id':message_id}
            return None

    async def history_page(self, page: int = 0, size: int = 5) -> tuple[list[dict], int]:
        db = self._connection()
        cursor = await db.execute('SELECT COUNT(*) FROM claim_details')
        count = (await cursor.fetchone())[0]
        cursor = await db.execute('''SELECT d.*,p.source_chat_id,p.source_message_id,p.claimed_at,
            COALESCE(d.source_title,c.title) AS display_title,
            COALESCE(d.source_username,c.username) AS display_username,
            COALESCE(NULLIF(d.source_type,'unknown'),c.chat_type,'unknown') AS display_type
            FROM claim_details d JOIN processed_starts p USING(parameter)
            LEFT JOIN observed_chats c ON c.chat_id=p.source_chat_id
            ORDER BY p.claimed_at DESC,p.parameter LIMIT ? OFFSET ?''',(size,max(page,0)*size))
        columns = [column[0] for column in cursor.description]
        rows = [dict(zip(columns,row)) for row in await cursor.fetchall()]
        for row in rows:
            row['source_title'] = row['display_title']
            if not row['chat_url']:
                row['chat_url'],row['message_url'] = source_links(row['source_chat_id'],row['source_message_id'],row['display_username'],row['display_type'])
        return rows,count

    async def income(self, since: str | None = None) -> dict:
        cursor = await self._connection().execute('''SELECT amount,asset,parameter FROM bot_receipts
            WHERE kind='received' AND (? IS NULL OR received_at>=?)''',(since,since))
        totals: dict[str, Decimal] = {}
        unmatched: dict[str, Decimal] = {}
        count, other_count = 0,0
        with localcontext() as context:
            context.prec = 100
            for amount,asset,parameter in await cursor.fetchall():
                target = totals if parameter else unmatched
                target[asset] = target.get(asset,Decimal(0))+Decimal(amount)
                if parameter: count += 1
                else: other_count += 1
        return {'count':count,'totals':{k:format(v,'f') for k,v in sorted(totals.items())},
                'unmatched_count':other_count,'unmatched':{k:format(v,'f') for k,v in sorted(unmatched.items())}}

    async def outcome_counts(self) -> dict[str,int]:
        cursor = await self._connection().execute('SELECT outcome,COUNT(*) FROM claim_details GROUP BY outcome')
        return {row[0]:row[1] for row in await cursor.fetchall()}

    async def queue_notice(self, key: str, body: str) -> None:
        await self._connection().execute('INSERT OR IGNORE INTO notification_outbox(event_key,body,random_id,created_at) VALUES (?,?,?,?)',
                                        (key,body,secrets.randbits(63),_now()))
        await self._connection().commit()

    async def pending_notices(self) -> list[tuple[str,str,int]]:
        cursor = await self._connection().execute('SELECT event_key,body,random_id FROM notification_outbox WHERE sent_at IS NULL ORDER BY created_at,event_key LIMIT 20')
        return await cursor.fetchall()

    async def results_without_notice(self) -> list[dict]:
        cursor = await self._connection().execute('''SELECT d.parameter FROM claim_details d
            WHERE d.outcome NOT IN ('queued','awaiting','legacy','legacy_failed','blocked')
            AND NOT EXISTS (SELECT 1 FROM notification_outbox n WHERE n.event_key='result:'||d.parameter||':'||d.outcome)
            ORDER BY d.updated_at LIMIT 100''')
        rows = [await self.detail(row[0]) for row in await cursor.fetchall()]
        cursor = await self._connection().execute('''SELECT message_id,amount,asset,usd FROM bot_receipts r
            WHERE r.kind='received' AND r.parameter IS NULL
            AND NOT EXISTS (SELECT 1 FROM notification_outbox n WHERE n.event_key='result:'||r.message_id||':received')
            ORDER BY received_at LIMIT 100''')
        rows.extend({'parameter':None,'outcome':'received','response_message_id':row[0],'amount':row[1],'asset':row[2],'usd':row[3]} for row in await cursor.fetchall())
        return rows

    async def notice_sent(self, key: str) -> None:
        await self._connection().execute('UPDATE notification_outbox SET sent_at=? WHERE event_key=?',(_now(),key))
        await self._connection().commit()
