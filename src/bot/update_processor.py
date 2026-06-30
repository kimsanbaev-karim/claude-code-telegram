"""Selective-concurrency update processor for PTB.

Regular updates process sequentially **per topic** -- one at a time within a
single ``(chat_id, thread_id)``, but *different* topics run concurrently.
Priority callbacks (``stop:`` / ``ask:``) bypass the queue and run immediately
so they can interrupt the currently-running handler.
"""

import asyncio
from typing import Any, Awaitable, Dict, Optional, Tuple

from telegram import Update
from telegram.ext._baseupdateprocessor import BaseUpdateProcessor

# (chat_id, thread_id); either may be None for updates without a clear topic.
TopicKey = Tuple[Optional[int], Optional[int]]


class StopAwareUpdateProcessor(BaseUpdateProcessor):
    """Update processor with per-topic sequential processing.

    PTB calls ``process_update(update, coroutine)`` for every incoming update.
    The base class holds a semaphore (max 256) then calls our
    ``do_process_update()``.

    For priority callbacks (``stop:`` / ``ask:``) and text answers to a pending
    question: we just ``await coroutine`` -- runs immediately, no lock.
    For everything else: we acquire the lock **for that update's topic** -- only
    one runs at a time *within a topic*, while distinct topics proceed
    concurrently. This lets the bot answer in several Telegram topics at once
    while still ordering messages inside a single topic.

    A stop callback arrives while a text handler holds its topic lock -> stop
    callback runs concurrently (priority bypass) -> fires the ``asyncio.Event``
    -> the watcher task inside ``execute_command()`` calls ``client.interrupt()``
    -> Claude stops -> ``run_command()`` returns -> handler finishes -> lock
    released.
    """

    _PRIORITY_PREFIXES = ("stop:", "ask:")

    def __init__(self) -> None:
        # High limit so priority callbacks are never blocked by semaphore
        super().__init__(max_concurrent_updates=256)
        # One lock per topic. Different topics -> different locks -> concurrent
        # processing; same topic -> serialized.
        self._topic_locks: Dict[TopicKey, asyncio.Lock] = {}

    def _get_topic_lock(self, key: TopicKey) -> asyncio.Lock:
        """Return the lock for *key*, creating one on first use."""
        lock = self._topic_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._topic_locks[key] = lock
        return lock

    @staticmethod
    def _topic_key(update: object) -> TopicKey:
        """Derive the ``(chat_id, thread_id)`` topic key for an update.

        Falls back to ``(None, None)`` for updates without an associated message
        so they share a single serial lane (preserving prior behaviour)."""
        if not isinstance(update, Update):
            return (None, None)
        msg = update.message
        if msg is None and update.callback_query is not None:
            msg = update.callback_query.message
        if msg is None:
            return (None, None)
        chat = getattr(msg, "chat", None)
        chat_id = getattr(chat, "id", None) if chat is not None else None
        thread_id = getattr(msg, "message_thread_id", None)
        return (chat_id, thread_id)

    @classmethod
    def _is_priority(cls, update: object) -> bool:
        """Priority updates bypass the per-topic lock so they can run WHILE a
        handler holds it: stop/ask button callbacks, and text answers to a pending
        AskUserQuestion (else the answer deadlocks behind the awaiting question run)."""
        if not isinstance(update, Update):
            return False
        cb = update.callback_query
        if cb is not None and cb.data is not None and cb.data.startswith(cls._PRIORITY_PREFIXES):
            return True
        msg = update.message
        # Любое сообщение (текст ИЛИ голос/др.), когда в чате/топике висит вопрос —
        # потенциальный ответ; должно обойти лок, иначе застрянет за ожидающим прогоном.
        # Раньше проверялся только msg.text → голосовой ответ не проходил.
        if msg is not None:
            from .ask_user import has_pending
            if has_pending(msg.chat.id, msg.message_thread_id):
                return True
        return False

    async def do_process_update(self, update: object, coroutine: Awaitable[Any]) -> None:
        """Process an update, applying the per-topic lock for non-priority updates."""
        if self._is_priority(update):
            # Run immediately -- no lock
            await coroutine
        else:
            # One at a time within a topic; distinct topics run concurrently.
            lock = self._get_topic_lock(self._topic_key(update))
            async with lock:
                await coroutine

    async def initialize(self) -> None:
        """Initialize the processor (no-op)."""

    async def shutdown(self) -> None:
        """Shutdown the processor (no-op)."""
