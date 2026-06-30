"""Тесты команды /status (agentic_status): простаивает vs выполняет задачу."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestrator import ActiveRequest, MessageOrchestrator
from src.config.settings import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(telegram_bot_token="test:token", telegram_bot_username="testbot", approved_directory=tmp_path, agentic_mode=True)


@pytest.fixture
def orchestrator(settings):
    return MessageOrchestrator(settings, {})


def _make_update():
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 42
    update.message = AsyncMock()
    return update


def _ctx():
    ctx = MagicMock()
    ctx.user_data = {}
    ctx.bot_data = {"rate_limiter": None}
    return ctx


class TestStatusCommand:
    async def test_idle_when_no_active_requests(self, orchestrator):
        update, context = _make_update(), _ctx()
        await orchestrator.agentic_status(update, context)
        text = update.message.reply_text.call_args.args[0]
        assert "Простаивает" in text
        assert "Аптайм" in text

    async def test_reports_active_task_with_topic_and_duration(self, orchestrator):
        # Симулируем активный прогон в топике (chat=500, thread=7), начавшийся 5с назад.
        req = ActiveRequest(user_id=42, started_at=time.time() - 5)
        orchestrator._active_requests[(500, 7)] = req

        update, context = _make_update(), _ctx()
        await orchestrator.agentic_status(update, context)
        text = update.message.reply_text.call_args.args[0]

        assert "Выполняет задач: 1" in text
        assert "500:7" in text  # топик активной задачи виден

    async def test_interrupted_request_not_counted_as_active(self, orchestrator):
        req = ActiveRequest(user_id=42)
        req.interrupted = True
        orchestrator._active_requests[(500, 7)] = req

        update, context = _make_update(), _ctx()
        await orchestrator.agentic_status(update, context)
        text = update.message.reply_text.call_args.args[0]
        assert "Простаивает" in text
