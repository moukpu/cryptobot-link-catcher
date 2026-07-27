from __future__ import annotations

import asyncio

from app.database import Claim, ProcessedStore


def test_atomic_claim_allows_only_one_concurrent_winner(tmp_path) -> None:
    async def scenario() -> None:
        store = ProcessedStore(tmp_path / "processed.sqlite3")
        await store.open()
        try:
            claim = Claim("SAME", 100, 200, 12345)
            results = await asyncio.gather(*(store.claim(claim) for _ in range(20)))
            assert sum(results) == 1
            await store.mark_sent("SAME")
            assert await store.claim(claim) is False
        finally:
            await store.close()

    asyncio.run(scenario())


def test_unfinished_claim_survives_database_reopen(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "processed.sqlite3"
        claim = Claim("PERSISTENT", 10, 20, 987654321)
        first = ProcessedStore(path)
        await first.open()
        assert await first.claim(claim)
        await first.close()

        second = ProcessedStore(path)
        await second.open()
        try:
            assert await second.pending_claims() == [claim]
        finally:
            await second.close()

    asyncio.run(scenario())


def test_persistent_runtime_settings_and_statistics(tmp_path) -> None:
    async def scenario() -> None:
        store = ProcessedStore(tmp_path / "processed.sqlite3")
        await store.open()
        try:
            assert await store.get_bool_setting("catcher_enabled", False) is True
            await store.set_bool_setting("catcher_enabled", False)
            assert await store.get_bool_setting("catcher_enabled", True) is False

            claim = Claim("DONE", 1, 2, 67890)
            assert await store.claim(claim)
            await store.mark_sent("DONE")
            stats = await store.statistics()
            assert stats == {"processing": 0, "sent": 1, "failed": 0}
            assert (await store.recent(1))[0][0:2] == ("DONE", "sent")
        finally:
            await store.close()

    asyncio.run(scenario())


def test_observed_and_ignored_chats(tmp_path) -> None:
    async def scenario() -> None:
        store = ProcessedStore(tmp_path / "processed.sqlite3")
        await store.open()
        try:
            await store.remember_chat(
                -100123,
                "Тестовая группа",
                "test_group",
                "group",
            )
            assert await store.is_chat_ignored(-100123) is False
            assert await store.toggle_chat_ignored(-100123) is True
            assert await store.is_chat_ignored(-100123) is True
            assert (await store.ignored_chats()) == [
                (-100123, "Тестовая группа", False)
            ]
            recent = await store.chats_page(0)
            assert recent[0] == (
                -100123,
                "Тестовая группа",
                "test_group",
                "group",
                True,
                False,
            )
            await store.set_chat_ignored(-100123, False)
            assert await store.ignored_chats() == []
        finally:
            await store.close()

    asyncio.run(scenario())


def test_system_exclusions_are_locked_and_visible(tmp_path) -> None:
    async def scenario() -> None:
        store = ProcessedStore(tmp_path / "processed.sqlite3")
        await store.open()
        try:
            await store.remember_chat(100, "Target", "target", "bot")
            await store.remember_chat(200, "Control", "control", "bot")
            await store.replace_system_ignored(
                [(100, "@Target"), (200, "Панель управления")]
            )
            assert await store.is_chat_ignored(100)
            assert await store.toggle_chat_ignored(100) is None
            assert await store.set_chat_ignored(100, False) is False
            rows = await store.chats_page(0)
            by_id = {row[0]: row for row in rows}
            assert by_id[100][-1] is True
            assert by_id[100][-2] is False
            assert (100, "@Target", True) in await store.ignored_chats()
        finally:
            await store.close()

    asyncio.run(scenario())


def test_concurrent_toggle_is_atomic(tmp_path) -> None:
    async def scenario() -> None:
        store = ProcessedStore(tmp_path / "processed.sqlite3")
        await store.open()
        try:
            await store.remember_chat(300, "Chat", None, "user")
            results = await asyncio.gather(
                store.toggle_chat_ignored(300),
                store.toggle_chat_ignored(300),
            )
            assert results == [True, False]
            assert await store.is_chat_ignored(300) is False
        finally:
            await store.close()

    asyncio.run(scenario())
