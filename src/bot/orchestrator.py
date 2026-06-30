"""Message orchestrator — single entry point for all Telegram updates.

Routes messages based on agentic vs classic mode. In agentic mode, provides
a minimal conversational interface (3 commands, no inline keyboards). In
classic mode, delegates to existing full-featured handlers.
"""

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import structlog
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..claude.sdk_integration import StreamUpdate
from ..config.settings import Settings
from ..projects import PrivateTopicsUnavailableError
from .features.skill_discovery import (
    DiscoveredSkill,
    discover_skills,
    rewrite_skill_command,
)
from .utils.draft_streamer import DraftStreamer, generate_draft_id
from .utils.file_extractor import (
    REJECTION_SURFACE_TO_USER,
    FileAttachment,
    validate_file_path,
)
from .utils.html_format import escape_html
from .utils.image_extractor import (
    ImageAttachment,
    extract_image_paths_from_text,
    should_send_as_photo,
    validate_image_path,
)
from .utils.media_group_buffer import BufferedMediaGroup, MediaGroupBuffer, MediaGroupKey
from .utils.message_buffer import BufferedResult, BufferKey, MessageBuffer
from .utils.quote_prompt import build_user_prompt

logger = structlog.get_logger()

_MEDIA_TYPE_MAP = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

# Patterns that look like secrets/credentials in CLI arguments
_SECRET_PATTERNS: List[re.Pattern[str]] = [
    # API keys / tokens (sk-ant-..., sk-..., ghp_..., gho_..., github_pat_..., xoxb-...)
    re.compile(
        r"(sk-ant-api\d*-[A-Za-z0-9_-]{10})[A-Za-z0-9_-]*"
        r"|(sk-[A-Za-z0-9_-]{20})[A-Za-z0-9_-]*"
        r"|(ghp_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(gho_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(github_pat_[A-Za-z0-9_]{5})[A-Za-z0-9_]*"
        r"|(xoxb-[A-Za-z0-9]{5})[A-Za-z0-9-]*"
    ),
    # AWS access keys
    re.compile(r"(AKIA[0-9A-Z]{4})[0-9A-Z]{12}"),
    # Generic long hex/base64 tokens after common flags/env patterns
    re.compile(
        r"((?:--token|--secret|--password|--api-key|--apikey|--auth)"
        r"[= ]+)['\"]?[A-Za-z0-9+/_.:-]{8,}['\"]?"
    ),
    # Inline env assignments like KEY=value
    re.compile(
        r"((?:TOKEN|SECRET|PASSWORD|API_KEY|APIKEY|AUTH_TOKEN|PRIVATE_KEY"
        r"|ACCESS_KEY|CLIENT_SECRET|WEBHOOK_SECRET)"
        r"=)['\"]?[^\s'\"]{8,}['\"]?"
    ),
    # Bearer / Basic auth headers
    re.compile(r"(Bearer )[A-Za-z0-9+/_.:-]{8,}" r"|(Basic )[A-Za-z0-9+/=]{8,}"),
    # Connection strings with credentials  user:pass@host
    re.compile(r"://([^:]+:)[^@]{4,}(@)"),
]


def _redact_secrets(text: str) -> str:
    """Replace likely secrets/credentials with redacted placeholders."""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(
            lambda m: next((g + "***" for g in m.groups() if g is not None), "***"),
            result,
        )
    return result


# Tool name -> friendly emoji mapping for verbose output
_TOOL_ICONS: Dict[str, str] = {
    "Read": "\U0001f4d6",
    "Write": "\u270f\ufe0f",
    "Edit": "\u270f\ufe0f",
    "MultiEdit": "\u270f\ufe0f",
    "Bash": "\U0001f4bb",
    "Glob": "\U0001f50d",
    "Grep": "\U0001f50d",
    "LS": "\U0001f4c2",
    "Task": "\U0001f9e0",
    "TaskOutput": "\U0001f9e0",
    "WebFetch": "\U0001f310",
    "WebSearch": "\U0001f310",
    "NotebookRead": "\U0001f4d3",
    "NotebookEdit": "\U0001f4d3",
    "TodoRead": "\u2611\ufe0f",
    "TodoWrite": "\u2611\ufe0f",
}


def _tool_icon(name: str) -> str:
    """Return emoji for a tool, with a default wrench."""
    return _TOOL_ICONS.get(name, "\U0001f527")


@dataclass
class ActiveRequest:
    """Tracks an in-flight Claude request so it can be interrupted."""

    user_id: int
    interrupt_event: asyncio.Event = field(default_factory=asyncio.Event)
    interrupted: bool = False
    progress_msg: Any = None  # telegram Message object
    started_at: float = field(default_factory=time.time)  # для /status: длительность задачи


class MessageOrchestrator:
    """Routes messages based on mode. Single entry point for all Telegram updates."""

    def __init__(self, settings: Settings, deps: Dict[str, Any]):
        self.settings = settings
        self.deps = deps
        self._started_at: float = time.time()  # аптайм процесса бота для /status
        # Active in-flight requests are tracked per TOPIC (chat_id, thread_id), not
        # per user: one user has many topics that run concurrently, and Stop must
        # target the right topic's run.
        self._active_requests: Dict[Tuple[Optional[int], Optional[int]], ActiveRequest] = {}
        self._known_commands: frozenset[str] = frozenset()
        self._skills: Dict[str, DiscoveredSkill] = discover_skills(settings.approved_directory)
        # One lock per topic so different Telegram topics process concurrently
        # while messages inside a single topic stay ordered.
        self._topic_locks: Dict[Tuple[Optional[int], Optional[int]], asyncio.Lock] = {}
        self._message_buffer = MessageBuffer(
            chunk_timeout=settings.chunk_buffer_timeout,
            chunk_threshold=settings.chunk_buffer_threshold,
            on_flush=self._on_buffer_flush,
        )
        self._media_group_buffer = MediaGroupBuffer(flush_timeout=settings.media_group_buffer_timeout, on_flush=self._on_media_group_flush)

    def _refresh_skills(self) -> None:
        """Re-scan skill directories. Called from /new so newly-added skills
        appear without restarting the bot."""
        self._skills = discover_skills(self.settings.approved_directory)

    def rewrite_skill_command(self, text: str) -> str:
        """Undo dash->underscore normalization for discovered skill commands."""
        return rewrite_skill_command(text, self._skills)

    def _inject_deps(self, handler: Callable) -> Callable:  # type: ignore[type-arg]
        """Wrap handler to inject dependencies into context.bot_data."""

        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            for key, value in self.deps.items():
                context.bot_data[key] = value
            context.bot_data["settings"] = self.settings
            context.user_data.pop("_thread_context", None)

            is_sync_bypass = handler.__name__ == "sync_threads"
            is_start_bypass = handler.__name__ in {"start_command", "agentic_start"}
            message_thread_id = self._extract_message_thread_id(update)
            should_enforce = self.settings.enable_project_threads

            if should_enforce:
                if self.settings.project_threads_mode == "private":
                    should_enforce = not is_sync_bypass and not (
                        is_start_bypass and message_thread_id is None
                    )
                else:
                    should_enforce = not is_sync_bypass

            if should_enforce:
                allowed = await self._apply_thread_routing_context(update, context)
                if not allowed:
                    return

            try:
                await handler(update, context)
            finally:
                if should_enforce:
                    self._persist_thread_state(update, context)

        return wrapped

    async def _apply_thread_routing_context(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> bool:
        """Enforce strict project-thread routing and load thread-local state."""
        manager = context.bot_data.get("project_threads_manager")
        if manager is None:
            await self._reject_for_thread_mode(
                update,
                "❌ <b>Project Thread Mode Misconfigured</b>\n\n"
                "Thread manager is not initialized.",
            )
            return False

        chat = update.effective_chat
        message = update.effective_message
        if not chat or not message:
            return False

        if self.settings.project_threads_mode == "group":
            if chat.id != self.settings.project_threads_chat_id:
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False
        else:
            if getattr(chat, "type", "") != "private":
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False

        message_thread_id = self._extract_message_thread_id(update)
        if not message_thread_id:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        project = await manager.resolve_project(chat.id, message_thread_id)
        if not project:
            # Fallback 1: karim.kanban task-topic → route to the task's working_dir
            # (writing in a task topic = commanding that task's agent, two-way).
            if self._apply_task_thread_context(chat.id, message_thread_id, context):
                return True
            # Fallback 2: forum General topic → base working dir for general commands
            # (not tied to a project/task; agent may propose kanban tasks per CLAUDE.md).
            if self._apply_general_context(chat.id, message_thread_id, context):
                return True
            await self._reject_for_thread_mode(update, manager.guidance_message(mode=self.settings.project_threads_mode))
            return False

        state_key = f"{chat.id}:{message_thread_id}"
        thread_states = context.user_data.setdefault("thread_state", {})
        state = thread_states.get(state_key, {})

        project_root = project.absolute_path
        current_dir_raw = state.get("current_directory")
        current_dir = (
            Path(current_dir_raw).resolve() if current_dir_raw else project_root
        )
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        context.user_data["current_directory"] = current_dir
        context.user_data["claude_session_id"] = state.get("claude_session_id")
        context.user_data["_thread_context"] = {
            "chat_id": chat.id,
            "message_thread_id": message_thread_id,
            "state_key": state_key,
            "project_slug": project.slug,
            "project_root": str(project_root),
            "project_name": project.name,
        }
        return True

    def _apply_task_thread_context(self, chat_id: int, message_thread_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Route a karim.kanban task-topic to the task's working_dir (two-way).

        Reads data/task_threads.json (written by karim.kanban's topic.py). When the
        thread maps to a task, points thread-local state at the task's working_dir so
        a message in the task topic runs Claude in that folder.
        """
        import json

        log = structlog.get_logger()
        try:
            raw = Path("data/task_threads.json").read_text(encoding="utf-8")
            mapping = json.loads(raw) if raw.strip() else {}
        except (OSError, ValueError):
            return False
        info = mapping.get(str(message_thread_id))
        working_raw = info.get("working_dir") if info else None
        if not working_raw:
            return False
        working = Path(working_raw).resolve()
        if not working.is_dir():
            log.warning("task-topic working_dir missing", working_dir=str(working), task_id=(info or {}).get("task_id"))
            return False
        task_id = info.get("task_id", "?")
        state_key = f"{chat_id}:{message_thread_id}"
        thread_states = context.user_data.setdefault("thread_state", {})
        state = thread_states.get(state_key, {})
        current_dir_raw = state.get("current_directory")
        current_dir = Path(current_dir_raw).resolve() if current_dir_raw else working
        if not self._is_within(current_dir, working) or not current_dir.is_dir():
            current_dir = working
        context.user_data["current_directory"] = current_dir
        context.user_data["claude_session_id"] = state.get("claude_session_id")
        context.user_data["_thread_context"] = {
            "chat_id": chat_id,
            "message_thread_id": message_thread_id,
            "state_key": state_key,
            "project_slug": f"task:{task_id}",
            "project_root": str(working),
            "project_name": f"kanban:{task_id}",
            "task_id": task_id,
        }
        log.info("Routed task-topic", task_id=task_id, message_thread_id=message_thread_id, working_dir=str(working))
        return True

    def _apply_general_context(self, chat_id: int, message_thread_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Allow the forum General topic (thread 1) as a base-dir working context.

        General is for broad commands not tied to a project/task. Routes to
        APPROVED_DIRECTORY so the agent works at the projects root and can, per the
        global CLAUDE.md rules, propose creating kanban tasks when appropriate.
        """
        if message_thread_id != 1:
            return False
        base = Path(self.settings.approved_directory).resolve()
        if not base.is_dir():
            return False
        state_key = f"{chat_id}:{message_thread_id}"
        thread_states = context.user_data.setdefault("thread_state", {})
        state = thread_states.get(state_key, {})
        current_dir_raw = state.get("current_directory")
        current_dir = Path(current_dir_raw).resolve() if current_dir_raw else base
        if not self._is_within(current_dir, base) or not current_dir.is_dir():
            current_dir = base
        context.user_data["current_directory"] = current_dir
        context.user_data["claude_session_id"] = state.get("claude_session_id")
        context.user_data["_thread_context"] = {
            "chat_id": chat_id,
            "message_thread_id": message_thread_id,
            "state_key": state_key,
            "project_slug": "general",
            "project_root": str(base),
            "project_name": "General",
        }
        return True

    def _persist_thread_state(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Persist compatibility keys back into per-thread state.

        Concurrency-safe: only persists when the shared ``_thread_context`` still
        belongs to THIS update's topic. If a concurrently-processed topic
        overwrote the shared user_data keys mid-run, the agentic path already
        wrote this topic's ``thread_state`` slot directly, so we skip rather than
        write another topic's values into the wrong slot."""
        thread_context = context.user_data.get("_thread_context")
        if not thread_context:
            return

        # Guard against cross-topic clobber of the shared keys.
        if thread_context.get("state_key") != self._topic_state_key(update):
            return

        state_key = thread_context["state_key"]
        project_root = Path(thread_context["project_root"])
        current_dir = context.user_data.get("current_directory", project_root)
        if not isinstance(current_dir, Path):
            current_dir = Path(str(current_dir))
        current_dir = current_dir.resolve()
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        thread_states = context.user_data.setdefault("thread_state", {})
        rec = dict(thread_states.get(state_key, {}))
        rec["current_directory"] = str(current_dir)
        rec["claude_session_id"] = context.user_data.get("claude_session_id")
        rec["project_slug"] = thread_context["project_slug"]
        rec["project_root"] = str(project_root)
        if thread_context.get("project_name") is not None:
            rec["project_name"] = thread_context["project_name"]
        if "task_id" in thread_context:
            rec["task_id"] = thread_context["task_id"]
        thread_states[state_key] = rec

    def _topic_state_key(self, update: Update) -> str:
        """``"{chat_id}:{thread_id}"`` key for an update's topic.

        Derived purely from the update (stable per call) so it is correct even
        when the shared ``_thread_context`` was clobbered by a concurrent topic.
        Matches the key routing writes in ``_apply_*_context``."""
        chat = getattr(update, "effective_chat", None)
        chat_id = getattr(chat, "id", None) if chat is not None else None
        thread_id = self._extract_message_thread_id(update)
        return f"{chat_id}:{thread_id}"

    def _capture_thread_snapshot(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Copy routing's shared-key context into this topic's ``thread_state`` slot.

        MUST run synchronously (before any await) so a concurrently-processed
        topic cannot have overwritten the shared user_data keys yet. The agentic
        path then reads/writes only the per-topic slot, immune to cross-topic
        clobber of the shared keys."""
        state_key = self._topic_state_key(update)
        tc = context.user_data.get("_thread_context") or {}
        thread_states = context.user_data.setdefault("thread_state", {})
        rec = dict(thread_states.get(state_key, {}))
        cur = context.user_data.get("current_directory", self.settings.approved_directory)
        rec["current_directory"] = str(cur)
        rec["claude_session_id"] = context.user_data.get("claude_session_id")
        rec["project_root"] = tc.get("project_root", rec.get("project_root", str(self.settings.approved_directory)))
        if tc.get("project_name") is not None:
            rec["project_name"] = tc.get("project_name")
        if tc.get("project_slug") is not None:
            rec["project_slug"] = tc.get("project_slug")
        if "task_id" in tc:
            rec["task_id"] = tc["task_id"]
        thread_states[state_key] = rec

    def _thread_state_for(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> Tuple[str, Dict[str, Any]]:
        """Return ``(state_key, record)`` for this update's topic.

        ``record`` is the live dict inside ``thread_state`` — mutate it in place
        to persist session/cwd for THIS topic only. Synthesizes a record from the
        shared keys when none exists (e.g. unit tests bypassing routing)."""
        state_key = self._topic_state_key(update)
        thread_states = context.user_data.setdefault("thread_state", {})
        rec = thread_states.get(state_key)
        if rec is None:
            tc = context.user_data.get("_thread_context") or {}
            cur = context.user_data.get("current_directory", self.settings.approved_directory)
            rec = {
                "current_directory": str(cur),
                "claude_session_id": context.user_data.get("claude_session_id"),
                "project_root": tc.get("project_root", str(self.settings.approved_directory)),
            }
            thread_states[state_key] = rec
        return state_key, rec

    def _resolve_cwd_change(self, content: str, current_dir: Path) -> Path:
        """Return the cwd implied by cd-like mentions in Claude's response, or
        *current_dir* unchanged. Pure (mutates no shared state) so it is safe to
        run for concurrent per-topic runs — unlike the legacy
        ``_update_working_directory_from_claude_response`` which writes the shared
        ``context.user_data`` key."""
        patterns = [
            r"(?:^|\n).*?cd\s+([^\s\n]+)",
            r"(?:^|\n).*?Changed directory to:?\s*([^\s\n]+)",
            r"(?:^|\n).*?Current directory:?\s*([^\s\n]+)",
            r"(?:^|\n).*?Working directory:?\s*([^\s\n]+)",
        ]
        text = content.lower()
        approved = self.settings.approved_directory
        for pattern in patterns:
            for match in re.findall(pattern, text, re.MULTILINE | re.IGNORECASE):
                try:
                    raw = match.strip().strip("\"'`")
                    if raw.startswith(("./", "../")) or not raw.startswith("/"):
                        candidate = (current_dir / raw).resolve()
                    else:
                        candidate = Path(raw).resolve()
                    if candidate.is_relative_to(approved) and candidate.exists():
                        return candidate
                except (ValueError, OSError):
                    continue
        return current_dir

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        """Return True if path is within root."""
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _extract_message_thread_id(update: Update) -> Optional[int]:
        """Extract topic/thread id from update message for forum/direct topics."""
        message = update.effective_message
        if not message:
            return None
        message_thread_id = getattr(message, "message_thread_id", None)
        if isinstance(message_thread_id, int) and message_thread_id > 0:
            return message_thread_id
        dm_topic = getattr(message, "direct_messages_topic", None)
        topic_id = getattr(dm_topic, "topic_id", None) if dm_topic else None
        if isinstance(topic_id, int) and topic_id > 0:
            return topic_id
        # Telegram omits message_thread_id for the General topic in forum
        # supergroups; its canonical thread ID is 1.
        chat = update.effective_chat
        if chat and getattr(chat, "is_forum", False):
            return 1
        return None

    async def _reject_for_thread_mode(self, update: Update, message: str) -> None:
        """Send a guidance response when strict thread routing rejects an update."""
        query = update.callback_query
        if query:
            try:
                await query.answer()
            except Exception:
                pass
            if query.message:
                await query.message.reply_text(message, parse_mode="HTML")
            return

        if update.effective_message:
            await update.effective_message.reply_text(message, parse_mode="HTML")

    def register_handlers(self, app: Application) -> None:
        """Register handlers based on mode."""
        if self.settings.agentic_mode:
            self._register_agentic_handlers(app)
        else:
            self._register_classic_handlers(app)

    def _register_agentic_handlers(self, app: Application) -> None:
        """Register agentic handlers: commands + text/file/photo."""
        from .handlers import callback, command

        # Commands
        handlers = [
            ("start", self.agentic_start),
            ("new", self.agentic_new),
            ("status", self.agentic_status),
            ("verbose", self.agentic_verbose),
            ("repo", self.agentic_repo),
            ("model", command.model_command),
            ("restart", command.restart_command),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))
        if self.settings.enable_scheduler:
            from .handlers import schedule

            handlers.append(("schedule", schedule.schedule_command))

        # Derive known commands dynamically — avoids drift when new commands are added
        self._known_commands: frozenset[str] = frozenset(cmd for cmd, _ in handlers)

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        # Text messages -> Claude
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(self.agentic_text),
            ),
            group=10,
        )

        # Unknown slash commands -> Claude (passthrough in agentic mode).
        # Registered commands are handled by CommandHandlers in group 0
        # (higher priority). This catches any /command not matched there
        # and forwards it to Claude, while skipping known commands to
        # avoid double-firing.
        app.add_handler(
            MessageHandler(
                filters.COMMAND,
                self._inject_deps(self._handle_unknown_command),
            ),
            group=10,
        )

        # File uploads -> Claude
        app.add_handler(
            MessageHandler(
                filters.Document.ALL, self._inject_deps(self.agentic_document)
            ),
            group=10,
        )

        # Photo uploads -> Claude
        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(self.agentic_photo)),
            group=10,
        )

        # Voice messages -> transcribe -> Claude
        app.add_handler(
            MessageHandler(filters.VOICE, self._inject_deps(self.agentic_voice)),
            group=10,
        )

        # Stop button callback (must be before cd: handler)
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_stop_callback),
                pattern=r"^stop:",
            )
        )

        # Model/effort selection callbacks
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(command.model_callback),
                pattern=r"^(model|effort):",
            )
        )

        # Only cd: callbacks (for project selection), scoped by pattern
        app.add_handler(CallbackQueryHandler(self._inject_deps(self._agentic_callback), pattern=r"^cd:"))

        # AskUserQuestion answers (ask:<qid>:<idx>) -> resolve pending question.
        # handle_callback_query acks the query and routes ask: to handle_ask_callback.
        # Required because agentic mode does NOT register the classic general
        # CallbackQueryHandler, so ask: would otherwise match no handler.
        app.add_handler(CallbackQueryHandler(self._inject_deps(callback.handle_callback_query), pattern=r"^ask:"))

        logger.info("Agentic handlers registered")

    def _register_classic_handlers(self, app: Application) -> None:
        """Register full classic handler set (moved from core.py)."""
        from .handlers import callback, command, message

        handlers = [
            ("start", command.start_command),
            ("help", command.help_command),
            ("new", command.new_session),
            ("continue", command.continue_session),
            ("end", command.end_session),
            ("ls", command.list_files),
            ("cd", command.change_directory),
            ("pwd", command.print_working_directory),
            ("projects", command.show_projects),
            ("status", command.session_status),
            ("export", command.export_session),
            ("actions", command.quick_actions),
            ("git", command.git_command),
            ("model", command.model_command),
            ("restart", command.restart_command),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))
        if self.settings.enable_scheduler:
            from .handlers import schedule

            handlers.append(("schedule", schedule.schedule_command))

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(message.handle_text_message),
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(
                filters.Document.ALL, self._inject_deps(message.handle_document)
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(message.handle_photo)),
            group=10,
        )
        app.add_handler(
            MessageHandler(filters.VOICE, self._inject_deps(message.handle_voice)),
            group=10,
        )
        app.add_handler(
            CallbackQueryHandler(self._inject_deps(callback.handle_callback_query))
        )

        logger.info("Classic handlers registered (13 commands + full handler set)")

    async def get_bot_commands(self) -> list:  # type: ignore[type-arg]
        """Return bot commands appropriate for current mode."""
        if self.settings.agentic_mode:
            commands = [
                BotCommand("start", "Start the bot"),
                BotCommand("new", "Start a fresh session"),
                BotCommand("status", "Show session status"),
                BotCommand("verbose", "Set output verbosity (0/1/2/3)"),
                BotCommand("repo", "List repos / switch workspace"),
                BotCommand("model", "Switch Claude model and effort"),
                BotCommand("restart", "Restart the bot"),
                # Vault commands (forwarded to Claude via _handle_unknown_command)
                BotCommand("today", "Morning briefing and daily plan"),
                BotCommand("closeday", "End of day review and reflection"),
                BotCommand("capture", "Quick capture a thought"),
                BotCommand("schedule", "Plan your week"),
                BotCommand("context", "Load context for a topic"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "Sync project topics"))
            for skill_name, skill in sorted(self._skills.items()):
                desc = skill.description[:50]
                if skill.argument_hint:
                    desc = f"{desc} ({skill.argument_hint})"
                commands.append(BotCommand(skill_name, desc[:256]))
            # Telegram rejects setMyCommands with more than 100 commands.
            if len(commands) > 100:
                logger.warning("Bot commands exceed Telegram 100 limit; keeping first 100", total=len(commands))
                commands = commands[:100]
            return commands
        else:
            commands = [
                BotCommand("start", "Start bot and show help"),
                BotCommand("help", "Show available commands"),
                BotCommand("new", "Clear context and start fresh session"),
                BotCommand("continue", "Explicitly continue last session"),
                BotCommand("end", "End current session and clear context"),
                BotCommand("ls", "List files in current directory"),
                BotCommand("cd", "Change directory (resumes project session)"),
                BotCommand("pwd", "Show current directory"),
                BotCommand("projects", "Show all projects"),
                BotCommand("status", "Show session status"),
                BotCommand("export", "Export current session"),
                BotCommand("actions", "Show quick actions"),
                BotCommand("git", "Git repository commands"),
                BotCommand("model", "Switch Claude model and effort"),
                BotCommand("restart", "Restart the bot"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "Sync project topics"))
            if self.settings.enable_scheduler:
                commands.append(BotCommand("schedule", "Manage scheduled jobs"))
            return commands

    # --- Agentic handlers ---

    async def agentic_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Brief welcome, no buttons."""
        user = update.effective_user
        sync_line = ""
        if (
            self.settings.enable_project_threads
            and self.settings.project_threads_mode == "private"
        ):
            if (
                not update.effective_chat
                or getattr(update.effective_chat, "type", "") != "private"
            ):
                await update.message.reply_text(
                    "🚫 <b>Private Topics Mode</b>\n\n"
                    "Use this bot in a private chat and run <code>/start</code> there.",
                    parse_mode="HTML",
                )
                return
            manager = context.bot_data.get("project_threads_manager")
            if manager:
                try:
                    result = await manager.sync_topics(
                        context.bot,
                        chat_id=update.effective_chat.id,
                    )
                    sync_line = (
                        "\n\n🧵 Topics synced"
                        f" (created {result.created}, reused {result.reused})."
                    )
                except PrivateTopicsUnavailableError:
                    await update.message.reply_text(
                        manager.private_topics_unavailable_message(),
                        parse_mode="HTML",
                    )
                    return
                except Exception:
                    sync_line = "\n\n🧵 Topic sync failed. Run /sync_threads to retry."
        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        dir_display = f"<code>{current_dir}/</code>"

        safe_name = escape_html(user.first_name)
        await update.message.reply_text(
            f"Hi {safe_name}! I'm your AI coding assistant.\n"
            f"Just tell me what you need — I can read, write, and run code.\n\n"
            f"Working in: {dir_display}\n"
            f"Commands: /new (reset) · /status"
            f"{sync_line}",
            parse_mode="HTML",
        )

    async def agentic_new(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Reset session, one-line confirmation."""
        context.user_data["claude_session_id"] = None
        context.user_data["session_started"] = True
        context.user_data["force_new_session"] = True

        # Re-scan skills so newly-added ones appear without bot restart.
        self._refresh_skills()

        await update.message.reply_text("Session reset. What's next?")

    async def agentic_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Статус бота: выполняет задачу / простаивает, аптайм, активные задачи, сессия.

        Отчёт ГЛОБАЛЬНЫЙ (по всем топикам) — чтобы можно было мониторить, занят
        бот или простаивает, и сколько идут активные прогоны."""
        now = time.time()

        # Активные прогоны (по всем топикам), исключая уже прерванные.
        active = [(k, r) for k, r in self._active_requests.items() if not r.interrupted]

        # Аптайм процесса бота.
        up = int(now - self._started_at)
        uptime_str = f"{up // 3600}ч {(up % 3600) // 60}м" if up >= 3600 else f"{(up % 3600) // 60}м {up % 60}с"

        if active:
            head = f"🟢 Выполняет задач: {len(active)}"
        else:
            head = "🟡 Простаивает (готов к работе)"

        lines = ["🤖 Статус бота", head, f"⏱ Аптайм: {uptime_str}"]
        for (chat_id, thread_id), r in active:
            dur = int(now - r.started_at)
            lines.append(f"  • топик {chat_id}:{thread_id} — {dur}с в работе")

        # Сессия/папка/стоимость текущего топика.
        current_dir = context.user_data.get("current_directory", self.settings.approved_directory)
        session_status = "active" if context.user_data.get("claude_session_id") else "none"
        cost_str = ""
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            try:
                user_status = rate_limiter.get_user_status(update.effective_user.id)
                current_cost = user_status.get("cost_usage", {}).get("current", 0.0)
                cost_str = f" · Cost: ${current_cost:.2f}"
            except Exception:
                pass
        lines.append(f"📂 {current_dir} · Session: {session_status}{cost_str}")

        await update.message.reply_text("\n".join(lines))

    def _get_verbose_level(self, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Return effective verbose level: per-user override or global default."""
        user_override = context.user_data.get("verbose_level")
        if user_override is not None:
            return int(user_override)
        return self.settings.verbose_level

    async def _finalize_progress_msg(
        self, progress_msg: Any, verbose_level: int
    ) -> None:
        """Delete the progress message, or keep it (sans Stop button) as a log.

        At verbose_level 3 the progress text is preserved as a read-only
        record of Claude's tool calls and reasoning (the Stop button is
        stripped). At levels 0-2 the message is deleted (the original
        behavior).
        """
        if verbose_level >= 3:
            try:
                await progress_msg.edit_reply_markup(reply_markup=None)
            except Exception:
                logger.debug("Failed to clear progress reply markup, ignoring")
            return
        try:
            await progress_msg.delete()
        except Exception:
            logger.debug("Failed to delete progress message, ignoring")

    async def agentic_verbose(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Set output verbosity: /verbose [0|1|2|3]."""
        args = update.message.text.split()[1:] if update.message.text else []
        labels = {
            0: "quiet",
            1: "normal",
            2: "detailed",
            3: "detailed+keep-log",
        }
        if not args:
            current = self._get_verbose_level(context)
            await update.message.reply_text(
                f"Verbosity: <b>{current}</b> ({labels.get(current, '?')})\n\n"
                "Usage: <code>/verbose 0|1|2|3</code>\n"
                "  0 = quiet (final response only)\n"
                "  1 = normal (tools + reasoning)\n"
                "  2 = detailed (tools with inputs + reasoning)\n"
                "  3 = detailed, keep progress log visible",
                parse_mode="HTML",
            )
            return

        try:
            level = int(args[0])
            if level not in (0, 1, 2, 3):
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Please use: /verbose 0, /verbose 1, /verbose 2, or /verbose 3"
            )
            return

        context.user_data["verbose_level"] = level
        await update.message.reply_text(
            f"Verbosity set to <b>{level}</b> ({labels[level]})",
            parse_mode="HTML",
        )

    def _format_verbose_progress(
        self,
        activity_log: List[Dict[str, Any]],
        verbose_level: int,
        start_time: float,
    ) -> str:
        """Build the progress message text based on activity so far."""
        if not activity_log:
            return "Working..."

        elapsed = time.time() - start_time
        lines: List[str] = [f"Working... ({elapsed:.0f}s)\n"]

        for entry in activity_log[-15:]:  # Show last 15 entries max
            kind = entry.get("kind", "tool")
            if kind == "text":
                # Claude's intermediate reasoning/commentary
                snippet = entry.get("detail", "")
                if verbose_level >= 2:
                    lines.append(f"\U0001f4ac {snippet}")
                else:
                    # Level 1: one short line
                    lines.append(f"\U0001f4ac {snippet[:80]}")
            else:
                # Tool call
                icon = _tool_icon(entry["name"])
                if verbose_level >= 2 and entry.get("detail"):
                    lines.append(f"{icon} {entry['name']}: {entry['detail']}")
                else:
                    lines.append(f"{icon} {entry['name']}")

        if len(activity_log) > 15:
            lines.insert(1, f"... ({len(activity_log) - 15} earlier entries)\n")

        return "\n".join(lines)

    @staticmethod
    def _summarize_tool_input(tool_name: str, tool_input: Dict[str, Any]) -> str:
        """Return a short summary of tool input for verbose level 2."""
        if not tool_input:
            return ""
        if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
            path = tool_input.get("file_path") or tool_input.get("path", "")
            if path:
                # Show just the filename, not the full path
                return path.rsplit("/", 1)[-1]
        if tool_name in ("Glob", "Grep"):
            pattern = tool_input.get("pattern", "")
            if pattern:
                return pattern[:60]
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            if cmd:
                return _redact_secrets(cmd[:100])[:80]
        if tool_name in ("WebFetch", "WebSearch"):
            return (tool_input.get("url", "") or tool_input.get("query", ""))[:60]
        if tool_name == "Task":
            desc = tool_input.get("description", "")
            if desc:
                return desc[:60]
        # Generic: show first key's value
        for v in tool_input.values():
            if isinstance(v, str) and v:
                return v[:60]
        return ""

    @staticmethod
    def _start_typing_heartbeat(
        chat: Any,
        interval: float = 2.0,
    ) -> "asyncio.Task[None]":
        """Start a background typing indicator task.

        Sends typing every *interval* seconds, independently of
        stream events. Cancel the returned task in a ``finally``
        block.
        """

        async def _heartbeat() -> None:
            from .ask_user import has_any_pending
            try:
                while True:
                    await asyncio.sleep(interval)
                    # Пока висит вопрос (AskUserQuestion) — НЕ показываем "печатает":
                    # вопрос с кнопками сам сигнал пользователю, иначе долгое ожидание
                    # ответа выглядит как зависание бота.
                    if has_any_pending(chat.id):
                        continue
                    try:
                        await chat.send_action("typing")
                    except Exception:
                        pass
            except asyncio.CancelledError:
                pass

        return asyncio.create_task(_heartbeat())

    def _make_stream_callback(
        self,
        verbose_level: int,
        progress_msg: Any,
        tool_log: List[Dict[str, Any]],
        start_time: float,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        mcp_images: Optional[List[ImageAttachment]] = None,
        mcp_files: Optional[List[FileAttachment]] = None,
        mcp_rejected_files: Optional[List[str]] = None,
        approved_directory: Optional[Path] = None,
        draft_streamer: Optional[DraftStreamer] = None,
        interrupt_event: Optional[asyncio.Event] = None,
    ) -> Optional[Callable[[StreamUpdate], Any]]:
        """Create a stream callback for verbose progress updates.

        When *mcp_images* is provided, the callback also intercepts
        ``send_image_to_user`` tool calls and collects validated
        :class:`ImageAttachment` objects for later Telegram delivery.

        When *mcp_files* is provided, the callback intercepts
        ``send_file_to_user`` tool calls and collects validated
        :class:`FileAttachment` objects the same way.

        When *draft_streamer* is provided, tool activity and assistant
        text are streamed to the user in real time via
        ``sendMessageDraft``.

        Returns None when verbose_level is 0 **and** no MCP image/file
        collection or draft streaming is requested.
        Typing indicators are handled by a separate heartbeat task.
        """
        need_mcp_intercept = (
            mcp_images is not None or mcp_files is not None
        ) and approved_directory is not None

        if verbose_level == 0 and not need_mcp_intercept and draft_streamer is None:
            return None

        last_edit_time = [0.0]  # mutable container for closure

        async def _on_stream(update_obj: StreamUpdate) -> None:
            # Stop all streaming activity after interrupt
            if interrupt_event is not None and interrupt_event.is_set():
                return

            # Intercept send_image_to_user / send_file_to_user MCP tool calls.
            # The SDK namespaces MCP tools as "mcp__<server>__<tool>",
            # so match both the bare name and the namespaced variant.
            if update_obj.tool_calls and need_mcp_intercept:
                for tc in update_obj.tool_calls:
                    tc_name = tc.get("name", "")
                    tc_input = tc.get("input", {})
                    file_path = tc_input.get("file_path", "")
                    caption = tc_input.get("caption", "")
                    if mcp_images is not None and (
                        tc_name == "send_image_to_user"
                        or tc_name.endswith("__send_image_to_user")
                    ):
                        img = validate_image_path(
                            file_path, approved_directory, caption
                        )
                        if img:
                            mcp_images.append(img)
                    elif mcp_files is not None and (
                        tc_name == "send_file_to_user"
                        or tc_name.endswith("__send_file_to_user")
                    ):
                        attachment, reason = validate_file_path(
                            file_path, approved_directory, caption
                        )
                        if attachment:
                            mcp_files.append(attachment)
                        elif (
                            mcp_rejected_files is not None
                            and file_path
                            and reason in REJECTION_SURFACE_TO_USER
                        ):
                            # Only surface bot-side rejections (outside
                            # approved dir, secrets blocklist) — the MCP tool
                            # already returned an error for size/empty/etc.,
                            # so Claude will describe those accurately on its
                            # own; our summary would just duplicate/mislead.
                            mcp_rejected_files.append(file_path)

            # Capture tool calls
            if update_obj.tool_calls:
                for tc in update_obj.tool_calls:
                    name = tc.get("name", "unknown")
                    detail = self._summarize_tool_input(name, tc.get("input", {}))
                    if verbose_level >= 1:
                        tool_log.append(
                            {"kind": "tool", "name": name, "detail": detail}
                        )
                    if draft_streamer:
                        icon = _tool_icon(name)
                        line = (
                            f"{icon} {name}: {detail}" if detail else f"{icon} {name}"
                        )
                        await draft_streamer.append_tool(line)

            # Capture assistant text (reasoning / commentary)
            if update_obj.type == "assistant" and update_obj.content:
                text = update_obj.content.strip()
                if text:
                    first_line = text.split("\n", 1)[0].strip()
                    if first_line:
                        if verbose_level >= 1:
                            tool_log.append(
                                {"kind": "text", "detail": first_line[:120]}
                            )
                        if draft_streamer:
                            await draft_streamer.append_tool(
                                f"\U0001f4ac {first_line[:120]}"
                            )

            # Stream text to user via draft (prefer token deltas;
            # skip full assistant messages to avoid double-appending)
            if draft_streamer and update_obj.content:
                if update_obj.type == "stream_delta":
                    await draft_streamer.append_text(update_obj.content)

            # Throttle progress message edits to avoid Telegram rate limits
            if not draft_streamer and verbose_level >= 1:
                now = time.time()
                if (now - last_edit_time[0]) >= 2.0 and tool_log:
                    last_edit_time[0] = now
                    new_text = self._format_verbose_progress(
                        tool_log, verbose_level, start_time
                    )
                    try:
                        await progress_msg.edit_text(
                            new_text, reply_markup=reply_markup
                        )
                    except Exception:
                        pass

        return _on_stream

    async def _send_images(
        self,
        update: Update,
        images: List[ImageAttachment],
        reply_to_message_id: Optional[int] = None,
        caption: Optional[str] = None,
        caption_parse_mode: Optional[str] = None,
    ) -> bool:
        """Send extracted images as a media group (album) or documents.

        If *caption* is provided and fits (≤1024 chars), it is attached to the
        photo / first album item so text + images appear as one message.

        Returns True if the caption was successfully embedded in the photo message.
        """
        photos: List[ImageAttachment] = []
        documents: List[ImageAttachment] = []
        for img in images:
            if should_send_as_photo(img.path):
                photos.append(img)
            else:
                documents.append(img)

        # Telegram caption limit
        use_caption = bool(
            caption and len(caption) <= 1024 and photos and not documents
        )
        caption_sent = False

        # Send raster photos as a single album (Telegram groups 2-10 items)
        if photos:
            try:
                if len(photos) == 1:
                    with open(photos[0].path, "rb") as f:
                        await update.message.reply_photo(
                            photo=f,
                            reply_to_message_id=reply_to_message_id,
                            caption=caption if use_caption else None,
                            parse_mode=caption_parse_mode if use_caption else None,
                        )
                    caption_sent = use_caption
                else:
                    media = []
                    file_handles = []
                    for idx, img in enumerate(photos[:10]):
                        fh = open(img.path, "rb")  # noqa: SIM115
                        file_handles.append(fh)
                        media.append(
                            InputMediaPhoto(
                                media=fh,
                                caption=caption if use_caption and idx == 0 else None,
                                parse_mode=(
                                    caption_parse_mode
                                    if use_caption and idx == 0
                                    else None
                                ),
                            )
                        )
                    try:
                        await update.message.chat.send_media_group(
                            media=media,
                            reply_to_message_id=reply_to_message_id,
                        )
                        caption_sent = use_caption
                    finally:
                        for fh in file_handles:
                            fh.close()
            except Exception as e:
                logger.warning("Failed to send photo album", error=str(e))

        # Send SVGs / large files as documents (one by one — can't mix in album)
        for img in documents:
            try:
                with open(img.path, "rb") as f:
                    await update.message.reply_document(
                        document=f,
                        filename=img.path.name,
                        reply_to_message_id=reply_to_message_id,
                    )
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(
                    "Failed to send document image",
                    path=str(img.path),
                    error=str(e),
                )

        return caption_sent

    async def _send_documents(
        self,
        update: Update,
        files: List[FileAttachment],
        rejected: Optional[List[str]] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> None:
        """Send files collected from ``send_file_to_user`` as Telegram documents.

        Each file is sent independently with its own caption; Telegram does not
        support grouping documents into an album. On per-file failure we log a
        warning and continue with the rest.

        *rejected* carries paths that were refused by bot-side security
        validation (``outside_approved`` or ``blocked_secret``). These are
        listed in the same summary message so the user isn't misled by
        Claude's "file sent" reply. Tool-side rejections
        (``too_large``/``empty``/``not_a_file``) are not surfaced here —
        Claude already sees the error from the MCP tool and describes it
        accurately on its own.

        Each file is re-opened with ``O_NOFOLLOW`` and its inode/device is
        compared against what was captured at validation time, to prevent a
        TOCTOU where the path is swapped for a symlink (e.g. to a secret)
        between the stream callback and this call.
        """
        if not files and not rejected:
            return

        # ``O_NOFOLLOW`` (refuse symlinks) is POSIX-only and ``O_BINARY``
        # (no CRLF translation) is Windows-only; guard both with getattr so
        # the TOCTOU-safe open works cross-platform. On Windows O_NOFOLLOW is
        # absent (-> 0) and O_BINARY keeps binary files byte-exact.
        open_flags = (
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        )

        failed: List[str] = []
        for attachment in files:
            try:
                fd = os.open(str(attachment.path), open_flags)
            except OSError as e:
                logger.warning(
                    "TOCTOU-safe open failed for MCP document",
                    path=str(attachment.path),
                    error=str(e),
                )
                failed.append(attachment.path.name)
                continue

            try:
                file_stat = os.fstat(fd)
                if (
                    file_stat.st_ino != attachment.inode
                    or file_stat.st_dev != attachment.device
                ):
                    logger.warning(
                        "File identity changed since validation — refusing send",
                        path=str(attachment.path),
                    )
                    os.close(fd)
                    failed.append(attachment.path.name)
                    continue

                with os.fdopen(fd, "rb") as f:
                    await update.message.reply_document(
                        document=f,
                        filename=attachment.path.name,
                        caption=attachment.caption or None,
                        reply_to_message_id=reply_to_message_id,
                    )
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(
                    "Failed to send MCP document",
                    path=str(attachment.path),
                    error=str(e),
                )
                failed.append(attachment.path.name)
                try:
                    os.close(fd)
                except OSError:
                    pass

        lines: List[str] = []
        if failed:
            lines.append(f"⚠️ Failed to send: {', '.join(failed)}")
        if rejected:
            rejected_names = ", ".join(Path(p).name or p for p in rejected)
            lines.append(
                "🚫 Rejected by security policy "
                "(outside APPROVED_DIRECTORY or blocked secret file): "
                f"{rejected_names}"
            )
        if lines:
            try:
                await update.message.reply_text(
                    "\n".join(lines),
                    reply_to_message_id=reply_to_message_id,
                )
            except Exception as e:
                logger.debug("Failed to send document error summary", error=str(e))

    def _get_topic_lock(self, chat_id: Optional[int], thread_id: Optional[int]) -> asyncio.Lock:
        """Return a per-topic ``(chat_id, thread_id)`` lock, creating one if needed.

        Per topic (not per user) so different Telegram topics run concurrently
        while messages within one topic stay ordered."""
        key = (chat_id, thread_id)
        lock = self._topic_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._topic_locks[key] = lock
        return lock

    async def agentic_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Entry point for text messages in agentic mode.

        Detects Telegram-split chunks (messages near the 4096-char limit)
        and buffers them before processing.  Short messages bypass the
        buffer and are processed immediately.
        """
        user_id = update.effective_user.id
        # Include reply/quote context so Claude sees the fragment the user is
        # responding to, not just their new text.
        message_text = build_user_prompt(update.message)

        # Telegram only allows [a-z0-9_] in command names, so dashed skills
        # (e.g. /git-activity) are exposed as /git_activity. Restore the
        # original dashed form before forwarding to Claude's skill dispatcher.
        message_text = self.rewrite_skill_command(message_text)

        # Если в этом чате/топике висит AskUserQuestion — этот текст = кастомный ответ на него
        from .ask_user import resolve_text

        if message_text and resolve_text(update.message.chat.id, update.message.message_thread_id, message_text):
            return

        logger.info(
            "Agentic text message",
            user_id=user_id,
            message_length=len(message_text),
        )

        # Snapshot this topic's routed context (cwd/session/project) into its
        # per-topic thread_state slot NOW — synchronously, before any await.
        # Routing populated the *shared* user_data keys; a concurrently-processed
        # topic will overwrite them at the next await, so the agentic path reads
        # from the per-(chat,thread) slot instead.
        self._capture_thread_snapshot(update, context)

        # Rate limit check (runs on every chunk — cheap)
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            allowed, limit_message = await rate_limiter.check_rate_limit(user_id, 0.001)
            if not allowed:
                await update.message.reply_text(f"\u23f1\ufe0f {limit_message}")
                return

        # --- Chunk buffering -----------------------------------------------
        chat_id = update.message.chat.id
        thread_id = self._extract_message_thread_id(update)
        buf_key: BufferKey = (user_id, chat_id, thread_id)

        should_buffer = self._message_buffer.has_buffer(
            buf_key
        ) or self._message_buffer.is_likely_chunk(
            message_text, self.settings.chunk_buffer_threshold
        )

        if should_buffer:
            result = await self._message_buffer.add_chunk(
                buf_key, message_text, update, context
            )
            if result is None:
                # Chunk buffered, timer pending.  Return quickly so
                # the sequential lock is released for the next chunk.
                return
            # Buffer flushed (short tail chunk or single non-chunked message).
            message_text = result.combined_text
            update = result.first_update
            context = result.last_context
            if result.chunk_count > 1:
                logger.info(
                    "Chunk buffer flushed inline",
                    user_id=user_id,
                    chunk_count=result.chunk_count,
                    combined_length=len(message_text),
                )

        # --- Process (may also be called from _on_buffer_flush) ------------
        lock = self._get_topic_lock(chat_id, thread_id)
        async with lock:
            await self._process_agentic_text(update, context, message_text)

    async def _on_buffer_flush(self, key: BufferKey, result: BufferedResult) -> None:
        """Called by MessageBuffer timer when all chunks have been collected.

        Runs as an independent ``asyncio.Task`` — outside the sequential
        lock but guarded by a per-user lock.
        """
        logger.info(
            "Buffer flush via timer",
            user_id=key[0],
            chunk_count=result.chunk_count,
            combined_length=len(result.combined_text),
        )
        # key == (user_id, chat_id, thread_id) -> lock on the topic.
        lock = self._get_topic_lock(key[1], key[2])
        async with lock:
            await self._process_agentic_text(result.first_update, result.last_context, result.combined_text)

    async def _process_agentic_text(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        message_text: str,
    ) -> None:
        """Run *message_text* through Claude and deliver the response.

        Extracted from ``agentic_text`` so it can be called both from the
        inline handler path and from the timer-fired buffer flush.
        """
        user_id = update.effective_user.id
        chat = update.message.chat
        await chat.send_action("typing")

        verbose_level = self._get_verbose_level(context)

        # Per-topic state: read cwd/session from THIS topic's slot, not the shared
        # user_data keys (which a concurrently-processed topic may have overwritten).
        state_key, tstate = self._thread_state_for(update, context)
        topic_key = (chat.id, self._extract_message_thread_id(update))

        # Create Stop button and interrupt event
        interrupt_event = asyncio.Event()
        stop_kb = InlineKeyboardMarkup([[InlineKeyboardButton("Stop", callback_data=f"stop:{user_id}")]])
        progress_msg = await update.message.reply_text("Working...", reply_markup=stop_kb)

        # Register active request for stop callback (keyed per topic)
        active_request = ActiveRequest(user_id=user_id, interrupt_event=interrupt_event, progress_msg=progress_msg)
        self._active_requests[topic_key] = active_request

        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            self._active_requests.pop(topic_key, None)
            await progress_msg.edit_text("Claude integration not available. Check configuration.", reply_markup=None)
            return

        _cwd_raw = tstate.get("current_directory") or self.settings.approved_directory
        current_dir = Path(_cwd_raw)
        session_id = tstate.get("claude_session_id")

        # Check if /new was used — skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        force_new = bool(context.user_data.get("force_new_session"))

        # Restart-resilient resume: if no in-memory session (e.g. a process restart
        # wiped thread_state before PicklePersistence flushed it), recover the most
        # recent session for THIS directory from the persistent SQLite store — the
        # same source cd/repo use. Skipped when /new forced a fresh session.
        if session_id is None and not force_new:
            try:
                _resumable = await claude_integration._find_resumable_session(user_id, current_dir)
                if _resumable:
                    session_id = _resumable.session_id
                    logger.info("Recovered session from persistent store", user_id=user_id, session_id=session_id)
            except Exception as _e:
                logger.debug("resumable-session fallback failed", error=str(_e))

        # --- Verbose progress tracking via stream callback ---
        tool_log: List[Dict[str, Any]] = []
        start_time = time.time()
        mcp_images: List[ImageAttachment] = []
        mcp_files: List[FileAttachment] = []
        mcp_rejected_files: List[str] = []

        # Stream drafts (private chats only)
        draft_streamer: Optional[DraftStreamer] = None
        if self.settings.enable_stream_drafts and chat.type == "private":
            draft_streamer = DraftStreamer(
                bot=context.bot,
                chat_id=chat.id,
                draft_id=generate_draft_id(),
                message_thread_id=update.message.message_thread_id,
                throttle_interval=self.settings.stream_draft_interval,
            )

        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            start_time,
            reply_markup=stop_kb,
            mcp_images=mcp_images,
            mcp_files=mcp_files,
            mcp_rejected_files=mcp_rejected_files,
            approved_directory=self.settings.approved_directory,
            draft_streamer=draft_streamer,
            interrupt_event=interrupt_event,
        )

        # Independent typing heartbeat — stays alive even with no stream events
        heartbeat = self._start_typing_heartbeat(chat)

        # Human-in-the-loop: AskUserQuestion → inline-кнопки в этот чат/топик
        from .ask_user import build_ask_user

        ask_user_fn = build_ask_user(context.bot, chat.id, update.message.message_thread_id)

        success = True
        try:
            claude_response = await claude_integration.run_command(
                prompt=message_text,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                interrupt_event=interrupt_event,
                model_override=context.user_data.get("model_override"),
                effort_override=context.user_data.get("effort_override"),
                ask_user=ask_user_fn,
            )

            # New session created successfully — clear the one-shot flag
            if force_new:
                context.user_data["force_new_session"] = False

            # Persist session + cwd into THIS topic's slot (concurrency-safe);
            # mirror into the shared keys for same-topic continuity / persist.
            new_cwd = self._resolve_cwd_change(claude_response.content, current_dir)
            tstate["claude_session_id"] = claude_response.session_id
            tstate["current_directory"] = str(new_cwd)
            context.user_data["claude_session_id"] = claude_response.session_id
            context.user_data["current_directory"] = new_cwd

            # Store interaction
            storage = context.bot_data.get("storage")
            if storage:
                try:
                    await storage.save_claude_interaction(
                        user_id=user_id,
                        session_id=claude_response.session_id,
                        prompt=message_text,
                        response=claude_response,
                        ip_address=None,
                    )
                except Exception as e:
                    logger.warning("Failed to log interaction", error=str(e))

            # Format response (no reply_markup — strip keyboards)
            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)

            response_content = claude_response.content
            if claude_response.interrupted:
                response_content = (
                    response_content or ""
                ) + "\n\n_(Interrupted by user)_"

            formatted_messages = formatter.format_claude_response(response_content)

        except Exception as e:
            success = False
            logger.error("Claude integration failed", error=str(e), user_id=user_id)
            from .handlers.message import _format_error_message
            from .utils.formatting import FormattedMessage

            formatted_messages = [
                FormattedMessage(_format_error_message(e), parse_mode="HTML")
            ]
        finally:
            heartbeat.cancel()
            self._active_requests.pop(topic_key, None)
            if draft_streamer:
                try:
                    await draft_streamer.flush()
                except Exception:
                    logger.debug("Draft flush failed in finally block", user_id=user_id)

        await self._finalize_progress_msg(progress_msg, verbose_level)

        # Use MCP-collected images and image paths mentioned in the agent text.
        images: List[ImageAttachment] = list(mcp_images)
        if success:
            extracted_images = extract_image_paths_from_text(
                claude_response.content,
                self.settings.approved_directory,
                current_dir,
            )
            known_paths = {img.path for img in images}
            images.extend(
                img for img in extracted_images if img.path not in known_paths
            )

        # Try to combine text + images in one message when possible
        caption_sent = False
        if images and len(formatted_messages) == 1:
            msg = formatted_messages[0]
            if msg.text and len(msg.text) <= 1024:
                try:
                    caption_sent = await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                        caption=msg.text,
                        caption_parse_mode=msg.parse_mode,
                    )
                except Exception as img_err:
                    logger.warning("Image+caption send failed", error=str(img_err))

        # Send text messages (skip if caption was already embedded in photos)
        if not caption_sent:
            for i, message in enumerate(formatted_messages):
                if not message.text or not message.text.strip():
                    continue
                try:
                    await update.message.reply_text(
                        message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=None,  # No keyboards in agentic mode
                        reply_to_message_id=(
                            update.message.message_id if i == 0 else None
                        ),
                    )
                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)
                except Exception as send_err:
                    logger.warning(
                        "Failed to send HTML response, retrying as plain text",
                        error=str(send_err),
                        message_index=i,
                    )
                    try:
                        await update.message.reply_text(
                            message.text,
                            reply_markup=None,
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )
                    except Exception as plain_err:
                        await update.message.reply_text(
                            f"Failed to deliver response "
                            f"(Telegram error: {str(plain_err)[:150]}). "
                            f"Please try again.",
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )

            # Send images separately if caption wasn't used
            if images:
                try:
                    await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                    )
                except Exception as img_err:
                    logger.warning("Image send failed", error=str(img_err))

        # Send MCP-collected files (from send_file_to_user tool calls) and
        # notify the user about any paths rejected by bot-side validation.
        if mcp_files or mcp_rejected_files:
            try:
                await self._send_documents(
                    update,
                    mcp_files,
                    rejected=mcp_rejected_files,
                    reply_to_message_id=update.message.message_id,
                )
            except Exception as file_err:
                logger.warning("Document send failed", error=str(file_err))

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="text_message",
                args=[message_text[:100]],
                success=success,
            )

    async def agentic_document(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process file upload -> Claude, minimal chrome."""
        user_id = update.effective_user.id
        # Capture this topic's routed context before any await (concurrency-safe).
        self._capture_thread_snapshot(update, context)
        document = update.message.document

        logger.info(
            "Agentic document upload",
            user_id=user_id,
            filename=document.file_name,
        )

        # Security validation
        security_validator = context.bot_data.get("security_validator")
        if security_validator:
            valid, error = security_validator.validate_filename(document.file_name)
            if not valid:
                await update.message.reply_text(f"File rejected: {error}")
                return

        # Size check
        max_size = 10 * 1024 * 1024
        if document.file_size > max_size:
            await update.message.reply_text(
                f"File too large ({document.file_size / 1024 / 1024:.1f}MB). Max: 10MB."
            )
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Working...")

        # current_dir is needed by the document branch of file_handler to persist
        # binary uploads (PDF etc.) into <current_dir>/.uploads/ where Claude
        # can reach them via its Read tool.
        # Per-topic state (concurrency-safe): read cwd/session from this topic's slot.
        _doc_state_key, doc_tstate = self._thread_state_for(update, context)
        current_dir = Path(doc_tstate.get("current_directory") or self.settings.approved_directory)

        # Try enhanced file handler, fall back to basic
        features = context.bot_data.get("features")
        file_handler = features.get_file_handler() if features else None
        prompt: Optional[str] = None

        if file_handler:
            try:
                processed_file = await file_handler.handle_document_upload(
                    document,
                    user_id,
                    update.message.caption or "Please review this file:",
                    current_dir=current_dir,
                )
                prompt = processed_file.prompt
            except Exception:
                file_handler = None

        if not file_handler:
            file = await document.get_file()
            file_bytes = await file.download_as_bytearray()
            try:
                content = file_bytes.decode("utf-8")
                if len(content) > 50000:
                    content = content[:50000] + "\n... (truncated)"
                caption = update.message.caption or "Please review this file:"
                prompt = (
                    f"{caption}\n\n**File:** `{document.file_name}`\n\n"
                    f"```\n{content}\n```"
                )
            except UnicodeDecodeError:
                await progress_msg.edit_text(
                    "Unsupported file format. Must be text-based (UTF-8)."
                )
                return

        # Process with Claude
        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await progress_msg.edit_text(
                "Claude integration not available. Check configuration."
            )
            return
        session_id = doc_tstate.get("claude_session_id")

        # Check if /new was used — skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        force_new = bool(context.user_data.get("force_new_session"))

        verbose_level = self._get_verbose_level(context)
        tool_log: List[Dict[str, Any]] = []
        mcp_images_doc: List[ImageAttachment] = []
        mcp_files_doc: List[FileAttachment] = []
        mcp_rejected_files_doc: List[str] = []
        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            time.time(),
            mcp_images=mcp_images_doc,
            mcp_files=mcp_files_doc,
            mcp_rejected_files=mcp_rejected_files_doc,
            approved_directory=self.settings.approved_directory,
        )

        heartbeat = self._start_typing_heartbeat(chat)
        try:
            claude_response = await claude_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                model_override=context.user_data.get("model_override"),
                effort_override=context.user_data.get("effort_override"),
            )

            if force_new:
                context.user_data["force_new_session"] = False

            # Persist session + cwd into THIS topic's slot (concurrency-safe).
            new_cwd = self._resolve_cwd_change(claude_response.content, current_dir)
            doc_tstate["claude_session_id"] = claude_response.session_id
            doc_tstate["current_directory"] = str(new_cwd)
            context.user_data["claude_session_id"] = claude_response.session_id
            context.user_data["current_directory"] = new_cwd

            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

            await self._finalize_progress_msg(progress_msg, verbose_level)

            # Use MCP-collected images (from send_image_to_user tool calls)
            images: List[ImageAttachment] = mcp_images_doc

            caption_sent = False
            if images and len(formatted_messages) == 1:
                msg = formatted_messages[0]
                if msg.text and len(msg.text) <= 1024:
                    try:
                        caption_sent = await self._send_images(
                            update,
                            images,
                            reply_to_message_id=update.message.message_id,
                            caption=msg.text,
                            caption_parse_mode=msg.parse_mode,
                        )
                    except Exception as img_err:
                        logger.warning("Image+caption send failed", error=str(img_err))

            if not caption_sent:
                for i, message in enumerate(formatted_messages):
                    await update.message.reply_text(
                        message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=None,
                        reply_to_message_id=(
                            update.message.message_id if i == 0 else None
                        ),
                    )
                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)

                if images:
                    try:
                        await self._send_images(
                            update,
                            images,
                            reply_to_message_id=update.message.message_id,
                        )
                    except Exception as img_err:
                        logger.warning("Image send failed", error=str(img_err))

            if mcp_files_doc or mcp_rejected_files_doc:
                try:
                    await self._send_documents(
                        update,
                        mcp_files_doc,
                        rejected=mcp_rejected_files_doc,
                        reply_to_message_id=update.message.message_id,
                    )
                except Exception as file_err:
                    logger.warning("Document send failed", error=str(file_err))

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error("Claude file processing failed", error=str(e), user_id=user_id)
        finally:
            heartbeat.cancel()

    async def agentic_photo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process photo -> Claude, minimal chrome.

        Photos that belong to a Telegram album (identified by a shared
        ``media_group_id``) are buffered until all album items have
        arrived, then sent to Claude as a single request.  Standalone
        photos are processed immediately.
        """
        user_id = update.effective_user.id
        message = update.message
        if message is None:
            return
        # Capture this topic's routed context before any await (concurrency-safe).
        self._capture_thread_snapshot(update, context)

        features = context.bot_data.get("features")
        image_handler = features.get_image_handler() if features else None

        if not image_handler:
            await message.reply_text("Photo processing is not available.")
            return

        media_group_id = getattr(message, "media_group_id", None)
        if media_group_id is not None:
            # Album — buffer and wait for siblings.
            chat_id = message.chat.id
            thread_id = self._extract_message_thread_id(update)
            key: MediaGroupKey = (user_id, chat_id, thread_id, media_group_id)
            await self._media_group_buffer.add_photo(
                key, message.photo[-1], message.caption, update, context
            )
            return

        # Standalone photo — process right away.
        await self._process_photo_batch(
            update=update,
            context=context,
            photos=[message.photo[-1]],
            caption=message.caption,
        )

    async def _on_media_group_flush(
        self, key: MediaGroupKey, result: BufferedMediaGroup
    ) -> None:
        """Called by MediaGroupBuffer after the debounce window closes."""
        logger.info(
            "Media group flush",
            user_id=key[0],
            photo_count=result.photo_count,
            has_caption=bool(result.caption),
        )
        await self._process_photo_batch(
            update=result.first_update,
            context=result.last_context,
            photos=result.photos,
            caption=result.caption,
        )

    async def _process_photo_batch(
        self,
        *,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        photos: List[Any],
        caption: Optional[str],
    ) -> None:
        """Send *photos* (one or many) to Claude in a single request."""
        user_id = update.effective_user.id
        features = context.bot_data.get("features")
        image_handler = features.get_image_handler() if features else None

        if not image_handler or not photos:
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Working...")

        try:
            # First photo carries the caption so its prompt template is
            # built with the user's context.  Remaining photos only
            # contribute image data.
            first_processed = await image_handler.process_image(photos[0], caption)
            fmt = first_processed.metadata.get("format", "png")
            images: List[Dict[str, str]] = [
                {
                    "data": first_processed.base64_data,
                    "media_type": _MEDIA_TYPE_MAP.get(fmt, "image/png"),
                }
            ]
            for photo in photos[1:]:
                extra = await image_handler.process_image(photo, None)
                extra_fmt = extra.metadata.get("format", "png")
                images.append(
                    {
                        "data": extra.base64_data,
                        "media_type": _MEDIA_TYPE_MAP.get(extra_fmt, "image/png"),
                    }
                )

            prompt = first_processed.prompt
            if len(images) > 1:
                prompt = (
                    f"{prompt}\n\n"
                    f"(Attached {len(images)} images in this message — "
                    f"please consider them together.)"
                )

            await self._handle_agentic_media_message(
                update=update,
                context=context,
                prompt=prompt,
                progress_msg=progress_msg,
                user_id=user_id,
                chat=chat,
                images=images,
            )

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error(
                "Claude photo processing failed",
                error=str(e),
                user_id=user_id,
                photo_count=len(photos),
            )

    async def agentic_voice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Transcribe voice message -> Claude, minimal chrome."""
        user_id = update.effective_user.id
        # Capture this topic's routed context before any await (concurrency-safe).
        self._capture_thread_snapshot(update, context)

        features = context.bot_data.get("features")
        voice_handler = features.get_voice_handler() if features else None

        if not voice_handler:
            await update.message.reply_text(self._voice_unavailable_message())
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Transcribing...")

        try:
            voice = update.message.voice
            processed_voice = await voice_handler.process_voice_message(voice, update.message.caption)

            # Голосовой ответ на висящий AskUserQuestion: расшифровка → resolve_text
            # (как в текстовом пути agentic_text). Вопрос закрыт — новый прогон не запускаем.
            from .ask_user import resolve_text
            _voice_text = (processed_voice.prompt or "").strip()
            if _voice_text and resolve_text(update.message.chat.id, update.message.message_thread_id, _voice_text):
                await progress_msg.edit_text("✅ Голосовой ответ принят.")
                return

            await progress_msg.edit_text("Working...")
            await self._handle_agentic_media_message(
                update=update,
                context=context,
                prompt=processed_voice.prompt,
                progress_msg=progress_msg,
                user_id=user_id,
                chat=chat,
            )

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error(
                "Claude voice processing failed", error=str(e), user_id=user_id
            )

    async def _handle_agentic_media_message(
        self,
        *,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        prompt: str,
        progress_msg: Any,
        user_id: int,
        chat: Any,
        images: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        """Run a media-derived prompt through Claude and send responses."""
        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await progress_msg.edit_text(
                "Claude integration not available. Check configuration."
            )
            return

        # Per-topic state (concurrency-safe): read cwd/session from this topic's slot.
        _media_state_key, media_tstate = self._thread_state_for(update, context)
        current_dir = Path(media_tstate.get("current_directory") or self.settings.approved_directory)
        session_id = media_tstate.get("claude_session_id")
        force_new = bool(context.user_data.get("force_new_session"))

        verbose_level = self._get_verbose_level(context)
        tool_log: List[Dict[str, Any]] = []
        mcp_images_media: List[ImageAttachment] = []
        mcp_files_media: List[FileAttachment] = []
        mcp_rejected_files_media: List[str] = []
        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            time.time(),
            mcp_images=mcp_images_media,
            mcp_files=mcp_files_media,
            mcp_rejected_files=mcp_rejected_files_media,
            approved_directory=self.settings.approved_directory,
        )

        heartbeat = self._start_typing_heartbeat(chat)
        try:
            claude_response = await claude_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                images=images,
                model_override=context.user_data.get("model_override"),
                effort_override=context.user_data.get("effort_override"),
            )
        finally:
            heartbeat.cancel()

        if force_new:
            context.user_data["force_new_session"] = False

        # Persist session + cwd into THIS topic's slot (concurrency-safe).
        new_cwd = self._resolve_cwd_change(claude_response.content, current_dir)
        media_tstate["claude_session_id"] = claude_response.session_id
        media_tstate["current_directory"] = str(new_cwd)
        context.user_data["claude_session_id"] = claude_response.session_id
        context.user_data["current_directory"] = new_cwd

        from .utils.formatting import ResponseFormatter

        formatter = ResponseFormatter(self.settings)
        formatted_messages = formatter.format_claude_response(claude_response.content)

        await self._finalize_progress_msg(progress_msg, verbose_level)

        # Use MCP-collected images (from send_image_to_user tool calls).
        images: List[ImageAttachment] = mcp_images_media

        caption_sent = False
        if images and len(formatted_messages) == 1:
            msg = formatted_messages[0]
            if msg.text and len(msg.text) <= 1024:
                try:
                    caption_sent = await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                        caption=msg.text,
                        caption_parse_mode=msg.parse_mode,
                    )
                except Exception as img_err:
                    logger.warning("Image+caption send failed", error=str(img_err))

        if not caption_sent:
            for i, message in enumerate(formatted_messages):
                if not message.text or not message.text.strip():
                    continue
                await update.message.reply_text(
                    message.text,
                    parse_mode=message.parse_mode,
                    reply_markup=None,
                    reply_to_message_id=(update.message.message_id if i == 0 else None),
                )
                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)

            if images:
                try:
                    await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                    )
                except Exception as img_err:
                    logger.warning("Image send failed", error=str(img_err))

        if mcp_files_media or mcp_rejected_files_media:
            try:
                await self._send_documents(
                    update,
                    mcp_files_media,
                    rejected=mcp_rejected_files_media,
                    reply_to_message_id=update.message.message_id,
                )
            except Exception as file_err:
                logger.warning("Document send failed", error=str(file_err))

    async def _handle_unknown_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Forward unknown slash commands to Claude in agentic mode.

        Known commands are handled by their own CommandHandlers (group 0);
        this handler fires for *every* COMMAND message in group 10 but
        returns immediately when the command is registered, preventing
        double execution.
        """
        msg = update.effective_message
        if not msg or not msg.text:
            return
        cmd = msg.text.split()[0].lstrip("/").split("@")[0].lower()
        if cmd in self._known_commands:
            return  # let the registered CommandHandler take care of it
        # Forward unrecognised /commands to Claude as natural language
        await self.agentic_text(update, context)

    def _voice_unavailable_message(self) -> str:
        """Return provider-aware guidance when voice feature is unavailable."""
        if self.settings.voice_provider == "local":
            return (
                "Voice processing is not available. "
                "Ensure whisper.cpp is installed and the model file exists. "
                "Check WHISPER_CPP_BINARY_PATH and WHISPER_CPP_MODEL_PATH settings."
            )
        return (
            "Voice processing is not available. "
            f"Set {self.settings.voice_provider_api_key_env} "
            f"for {self.settings.voice_provider_display_name} and install "
            'voice extras with: pip install "claude-code-telegram[voice]"'
        )

    async def agentic_repo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List repos in workspace or switch to one.

        /repo          — list subdirectories with git indicators
        /repo <name>   — switch to that directory, resume session if available
        """
        args = update.message.text.split()[1:] if update.message.text else []
        base = self.settings.approved_directory
        current_dir = context.user_data.get("current_directory", base)

        if args:
            # Switch to named repo
            target_name = args[0]
            target_path = base / target_name
            if not target_path.is_dir():
                await update.message.reply_text(
                    f"Directory not found: <code>{escape_html(target_name)}</code>",
                    parse_mode="HTML",
                )
                return

            context.user_data["current_directory"] = target_path

            # Try to find a resumable session
            claude_integration = context.bot_data.get("claude_integration")
            session_id = None
            if claude_integration:
                existing = await claude_integration._find_resumable_session(
                    update.effective_user.id, target_path
                )
                if existing:
                    session_id = existing.session_id
            context.user_data["claude_session_id"] = session_id

            is_git = (target_path / ".git").is_dir()
            git_badge = " (git)" if is_git else ""
            session_badge = " · session resumed" if session_id else ""

            await update.message.reply_text(
                f"Switched to <code>{escape_html(target_name)}/</code>"
                f"{git_badge}{session_badge}",
                parse_mode="HTML",
            )
            return

        # No args — list repos
        try:
            entries = sorted(
                [
                    d
                    for d in base.iterdir()
                    if d.is_dir() and not d.name.startswith(".")
                ],
                key=lambda d: d.name,
            )
        except OSError as e:
            await update.message.reply_text(f"Error reading workspace: {e}")
            return

        if not entries:
            await update.message.reply_text(
                f"No repos in <code>{escape_html(str(base))}</code>.\n"
                'Clone one by telling me, e.g. <i>"clone org/repo"</i>.',
                parse_mode="HTML",
            )
            return

        lines: List[str] = []
        keyboard_rows: List[list] = []  # type: ignore[type-arg]
        current_name = current_dir.name if current_dir != base else None

        for d in entries:
            is_git = (d / ".git").is_dir()
            icon = "\U0001f4e6" if is_git else "\U0001f4c1"
            marker = " \u25c0" if d.name == current_name else ""
            lines.append(f"{icon} <code>{escape_html(d.name)}/</code>{marker}")

        # Build inline keyboard (2 per row)
        for i in range(0, len(entries), 2):
            row = []
            for j in range(2):
                if i + j < len(entries):
                    name = entries[i + j].name
                    row.append(InlineKeyboardButton(name, callback_data=f"cd:{name}"))
            keyboard_rows.append(row)

        reply_markup = InlineKeyboardMarkup(keyboard_rows)

        await update.message.reply_text(
            "<b>Repos</b>\n\n" + "\n".join(lines),
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    async def _handle_stop_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle stop: callbacks — interrupt a running Claude request.

        The target run is the one in the TOPIC where the Stop button lives, so a
        Stop in one topic never interrupts a concurrent run in another."""
        query = update.callback_query
        target_user_id = int(query.data.split(":", 1)[1])

        # Only the requesting user can stop their own request
        if query.from_user.id != target_user_id:
            await query.answer("Only the requesting user can stop this.", show_alert=True)
            return

        # Topic is where the Stop button lives (same keying as _process_agentic_text).
        chat = update.effective_chat
        chat_id = getattr(chat, "id", None) if chat is not None else None
        thread_id = self._extract_message_thread_id(update)
        topic_key = (chat_id, thread_id)
        buf_key_topic = (target_user_id, chat_id, thread_id)

        # Cancel any pending chunk / media-group buffer for THIS topic only.
        if buf_key_topic in set(self._message_buffer.pending_keys):
            self._message_buffer.cancel(buf_key_topic)
        if buf_key_topic in set(self._media_group_buffer.pending_keys):
            self._media_group_buffer.cancel(buf_key_topic)

        active = self._active_requests.get(topic_key)
        if not active:
            await query.answer("Already completed.", show_alert=False)
            return
        if active.interrupted:
            await query.answer("Already stopping...", show_alert=False)
            return

        active.interrupt_event.set()
        active.interrupted = True
        await query.answer("Stopping...", show_alert=False)

        try:
            await active.progress_msg.edit_text("Stopping...", reply_markup=None)
        except Exception:
            pass

    async def _agentic_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle cd: callbacks — switch directory and resume session if available."""
        query = update.callback_query
        await query.answer()

        data = query.data
        _, project_name = data.split(":", 1)

        base = self.settings.approved_directory
        new_path = base / project_name

        if not new_path.is_dir():
            await query.edit_message_text(
                f"Directory not found: <code>{escape_html(project_name)}</code>",
                parse_mode="HTML",
            )
            return

        context.user_data["current_directory"] = new_path

        # Look for a resumable session instead of always clearing
        claude_integration = context.bot_data.get("claude_integration")
        session_id = None
        if claude_integration:
            existing = await claude_integration._find_resumable_session(
                query.from_user.id, new_path
            )
            if existing:
                session_id = existing.session_id
        context.user_data["claude_session_id"] = session_id

        is_git = (new_path / ".git").is_dir()
        git_badge = " (git)" if is_git else ""
        session_badge = " · session resumed" if session_id else ""

        await query.edit_message_text(
            f"Switched to <code>{escape_html(project_name)}/</code>"
            f"{git_badge}{session_badge}",
            parse_mode="HTML",
        )

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=query.from_user.id,
                command="cd",
                args=[project_name],
                success=True,
            )
