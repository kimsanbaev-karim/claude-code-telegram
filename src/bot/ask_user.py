"""Human-in-the-loop для AskUserQuestion: вопросы агента → inline-кнопки в Telegram.

Оркестратор строит `ask_user(tool_input)` через `build_ask_user()` и прокидывает его в
Claude-слой (`can_use_tool`). Когда агент зовёт `AskUserQuestion`, `can_use_tool` делает
`await ask_user(...)`: постит кнопки в чат/топик, ждёт выбор пользователя и возвращает
строку-ответ (она инжектится агенту как результат тула через Deny-message).

Слоистость: bot строит замыкание (знает Telegram), claude-слой лишь вызывает его как
непрозрачный callable — без импорта bot-слоя.
"""
import asyncio
import json
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

import structlog
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from .utils.html_format import escape_html

logger = structlog.get_logger()

ANSWER_TIMEOUT = 1800  # сек ждать ответа человека (30 мин). Это человеко-время ИСКЛЮЧЕНО из command-timeout (sdk_integration: timeout watchdog прибавляет его к бюджету), поэтому больше не обязано быть меньше claude_timeout_seconds. Stop-кнопка прерывает в любой момент.

# qid -> Future с выбранной меткой; qid -> список меток опций (для маппинга индекса)
_PENDING: Dict[str, "asyncio.Future[str]"] = {}
_OPTION_LABELS: Dict[str, List[str]] = {}
# (chat_id, message_thread_id) -> qid текущего ожидающего вопроса — чтобы принять ответ текстом
_PENDING_BY_CHAT: Dict[Any, str] = {}


def resolve(qid: str, idx: int) -> Optional[str]:
    """Разрешить ожидающий вопрос выбором пользователя (по индексу опции)."""
    fut = _PENDING.get(qid)
    if fut is None or fut.done():
        return None
    labels = _OPTION_LABELS.get(qid, [])
    label = labels[idx] if 0 <= idx < len(labels) else str(idx)
    fut.set_result(label)
    return label


def resolve_text(chat_id: int, message_thread_id: Optional[int], text: str) -> bool:
    """Принять свободный текст как ответ на ожидающий в этом чате/топике вопрос (как «Other»)."""
    qid = _PENDING_BY_CHAT.get((chat_id, message_thread_id))
    if not qid:
        return False
    fut = _PENDING.get(qid)
    if fut is None or fut.done():
        return False
    fut.set_result(text)
    return True


def has_pending(chat_id: int, message_thread_id: Optional[int]) -> bool:
    """Есть ли в этом чате/топике вопрос, ожидающий ответа (для приоритезации апдейта)."""
    qid = _PENDING_BY_CHAT.get((chat_id, message_thread_id))
    if not qid:
        return False
    fut = _PENDING.get(qid)
    return fut is not None and not fut.done()


def has_any_pending(chat_id: int) -> bool:
    """Есть ли в этом чате (любой топик) живой ожидающий вопрос — для паузы typing-хартбита."""
    for (cid, _thread), qid in list(_PENDING_BY_CHAT.items()):
        if cid == chat_id:
            fut = _PENDING.get(qid)
            if fut is not None and not fut.done():
                return True
    return False


def build_ask_user(bot: Bot, chat_id: int, message_thread_id: Optional[int]) -> Callable[[Dict[str, Any]], Awaitable[str]]:
    """Собрать ask_user-замыкание с привязкой к конкретному чату/топику."""

    async def ask_user(tool_input: Dict[str, Any]) -> str:
        questions = tool_input.get("questions") or []
        answers: List[Dict[str, Any]] = []
        for q in questions:
            qtext = q.get("question", "?")
            options = q.get("options") or []
            qid = uuid.uuid4().hex[:8]
            fut: "asyncio.Future[str]" = asyncio.get_event_loop().create_future()
            _PENDING[qid] = fut
            _OPTION_LABELS[qid] = [str(o.get("label", i)) for i, o in enumerate(options)]
            rows = [[InlineKeyboardButton(str(o.get("label", i)), callback_data=f"ask:{qid}:{i}")] for i, o in enumerate(options)]
            header = escape_html(str(q.get("header") or "Вопрос"))
            text = f"❓ <b>{header}</b>\n{escape_html(qtext)}\n\n<i>(нажми кнопку или просто напиши свой ответ)</i>"
            kwargs: Dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "reply_markup": InlineKeyboardMarkup(rows)}
            if message_thread_id:
                kwargs["message_thread_id"] = message_thread_id
            try:
                await bot.send_message(**kwargs)
            except Exception as e:
                logger.warning("ask_user: send failed", error=str(e), qid=qid)
                _PENDING.pop(qid, None)
                _OPTION_LABELS.pop(qid, None)
                answers.append({"question": qtext, "answer": None})
                continue
            _PENDING_BY_CHAT[(chat_id, message_thread_id)] = qid
            try:
                chosen = await asyncio.wait_for(fut, timeout=ANSWER_TIMEOUT)
            except asyncio.TimeoutError:
                chosen = None
            finally:
                _PENDING.pop(qid, None)
                _OPTION_LABELS.pop(qid, None)
                _PENDING_BY_CHAT.pop((chat_id, message_thread_id), None)
            answers.append({"question": qtext, "answer": chosen})
        return json.dumps(answers, ensure_ascii=False)

    return ask_user
