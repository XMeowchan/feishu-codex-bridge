#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib


ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\].*?(?:\x07|\x1b\\))")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1a\x1c-\x1f\x7f]")
TITLE_NOISE_RE = re.compile(r"^(?:0;.*|10;\?)$")
CURSOR_NOISE_RE = re.compile(r"^[0-9;?]+H.*$")
SPINNER_NOISE_RE = re.compile(r"^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]+(?:\s+OwMini)?$")
ASCII_LOG_KEYWORDS = (
    "OpenAI Codex",
    "Tip:",
    "To get started",
    "Do you trust the contents of this directory?",
    "Continue anyway? [y/N]:",
    "MCP startup incomplete",
    "MCP client",
    "failed to start",
    "Ran ",
    "Read ",
    "Explored",
    "lark-cli",
)

DEFAULT_ACK_TEXT = "已收到，正在转给本地 Codex 处理，后续回复会直接回到这条消息下。"
UNSUPPORTED_MESSAGE_TEXT = "当前仅支持文本消息，请直接发送文字内容。"
CODEx_FAILURE_TEXT = "本地 Codex 会话启动失败，请稍后重试。"
WATCHDOG_PROCESSING_TEXT = "仍在处理中，我会继续同步进展，完成后会继续回复你。"
WATCHDOG_HEARTBEAT_TEXT = "还在处理中，当前还没有结束；我会继续同步新的进展。"
SUBSCRIBE_RESTART_DELAY_SECONDS = 5.0
SUBSCRIPTION_READY_STABILIZE_SECONDS = 2.0
QUIET_WINDOW_SECONDS = 4.0
FIRST_OUTPUT_TIMEOUT_SECONDS = 20.0
SESSION_STARTUP_TIMEOUT_SECONDS = 25.0
TASK_MAX_WAIT_SECONDS = 60.0 * 30.0
WATCHDOG_FIRST_REPLY_DELAY_SECONDS = 10.0
WATCHDOG_HEARTBEAT_INTERVAL_SECONDS = 30.0
PROMPT_PASTE_SETTLE_SECONDS = 0.25
PROMPT_SUBMIT_RETRY_DELAY_SECONDS = 1.5
OUTPUT_TAIL_LIMIT = 12000
TMUX_COLUMNS = 120
TMUX_LINES = 40
TMUX_CAPTURE_HISTORY_LINES = 200
TMUX_HISTORY_LIMIT = 50000
TMUX_WHEEL_SCROLL_LINES = 3
DEFAULT_TMUX_SESSION_NAME = "feishu-codex-bridge"
LOG_DEDUP_WINDOW_SECONDS = 15.0
RUNTIME_DIR_NAME = ".runtime"
POSIX_MUX_BINARY = "tmux"
WINDOWS_MUX_BINARY = "psmux"
LARK_CLI_WRAPPER_RELATIVE_PATH = Path("bin") / "lark-cli"
LARK_CLI_WRAPPER_WINDOWS_RELATIVE_PATH = Path("bin") / "lark-cli.cmd"
LARK_CLI_WRAPPER_MODULE_RELATIVE_PATH = Path("bin") / "lark_cli_wrapper.py"
SEEN_MESSAGE_IDS_FILENAME = "seen_message_ids.txt"


def is_windows() -> bool:
    return os.name == "nt"


def mux_binary_name() -> str:
    return WINDOWS_MUX_BINARY if is_windows() else POSIX_MUX_BINARY


def mux_session_label() -> str:
    return f"{mux_binary_name()} 会话"


def mux_attach_command(session_name: str) -> str:
    return f"{mux_binary_name()} attach -t {session_name}"


def lark_cli_wrapper_relative_path() -> Path:
    if is_windows():
        return LARK_CLI_WRAPPER_WINDOWS_RELATIVE_PATH
    return LARK_CLI_WRAPPER_RELATIVE_PATH


def split_command(command: str) -> list[str]:
    if not is_windows():
        return shlex.split(command)

    import ctypes

    argc = ctypes.c_int()
    argv = ctypes.windll.shell32.CommandLineToArgvW(command, ctypes.byref(argc))
    if not argv:
        raise ValueError(f"failed to parse command: {command}")

    try:
        return [argv[index] for index in range(argc.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv)


def build_bootstrap_prompt(skill_path: Path) -> str:
    shared_path = skill_path.parent.parent / "lark-shared" / "SKILL.md"
    return f"""你当前运行在飞书 P2P 桥接模式。

接下来的飞书用户消息会由本地守护进程持续送入当前会话。请严格遵守以下规则：

1. 用户可见回复不要依赖终端输出。
2. 回复用户时优先使用技能文件 `{skill_path}` 对应的能力。
3. 在使用该技能前，先读取 `{shared_path}`，遵守其中的认证、权限和安全规则。
4. 针对每条飞书消息，优先使用：
   `lark-cli im +messages-reply --message-id <当前消息ID> --text "<内容>" --as bot`
5. 每次收到新消息后，不要等全部思考完成才回复；先尽快发送一条简短进度消息，告知你已经开始处理。
6. 如果处理超过几秒、进入新阶段、正在读文件/查信息/执行命令，继续用同一个 message_id 追加进度回复。
7. 允许对同一条用户消息多次发送进度回复与最终回复；最终结果也必须通过飞书发给用户。
8. 除非我明确要求，否则不要只把答案打印到 stdout 就结束。
9. 终端输出仅用于本地日志和调试，不视为用户已经收到。

请记住这套桥接规则，后续收到 envelope 消息后按其中提供的 message_id 直接回复飞书用户。"""


def build_message_envelope(
    *,
    chat_id: str,
    message_id: str,
    sender_open_id: str,
    skill_path: Path,
    content: str,
) -> str:
    return f"""收到一条新的飞书用户消息，请直接在飞书里回复这条消息，不要只输出到终端。

桥接上下文：
- chat_id: {chat_id}
- message_id: {message_id}
- sender_open_id: {sender_open_id}
- skill_path: {skill_path}

执行要求：
1. 按技能要求先读取 lark-shared，再使用 lark-im。
2. 对当前消息优先使用：
   lark-cli im +messages-reply --message-id "{message_id}" --text "<内容>" --as bot
3. 回复过程中持续使用 lark-im 回复给用户，而不是等所有回答说完才回复。
4. 开始处理后尽快先回复一条简短进度消息，例如“已开始处理，正在查看”。
5. 如果任务还没结束，并且进入了新阶段、正在读文件、正在执行命令、或耗时超过几秒，继续追加进度回复。
6. 最终结果完成后，再发送最终回复；不要把全部内容都憋到最后一条。
7. 若你需要补充说明，也请继续通过飞书回复，而不是只写在终端里。

用户消息原文：
{content}"""


def strip_terminal_artifacts(text: str) -> str:
    without_ansi = ANSI_ESCAPE_RE.sub("", text)
    normalized = without_ansi.replace("\r", "\n")
    normalized = CONTROL_RE.sub("", normalized)
    return normalized


def is_logworthy_codex_line(line: str) -> bool:
    normalized = " ".join(line.split())
    if not normalized:
        return False

    if TITLE_NOISE_RE.match(normalized):
        return False

    if CURSOR_NOISE_RE.match(normalized):
        return False

    if SPINNER_NOISE_RE.match(normalized):
        return False

    noise_substrings = (
        "esc to interrupt",
        "tab to queue message",
        "context left",
        "background terminal running",
        "Summarize recent commits",
    )
    if any(fragment in normalized for fragment in noise_substrings):
        return False

    if normalized in {
        "W",
        "Wo",
        "Wor",
        "Work",
        "Worki",
        "Workin",
        "Working",
        "orking",
        "rking",
        "king",
        "ing",
        "ng",
        "g",
        "S",
        "St",
        "Sta",
        "Star",
        "Start",
        "Starti",
        "Startin",
        "Starting",
        "MCP",
        "server",
        "servers",
        "notion",
        "on",
        "ion",
        "tion",
    }:
        return False

    if len(normalized) <= 2 and normalized.isascii():
        return False

    if normalized.isascii():
        if any(keyword in normalized for keyword in ASCII_LOG_KEYWORDS):
            return True
        if len(normalized) < 40:
            return False

    return True


def collapse_for_log(text: str) -> list[str]:
    cleaned = strip_terminal_artifacts(text)
    lines = []
    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if is_logworthy_codex_line(line):
            lines.append(line)
    return lines


def should_log_lark_event_stderr(text: str) -> bool:
    if not text:
        return False

    harmless_sdk_errors = (
        "not found handler",
        "event type: message_read",
        "event type: im.message.message_read_v1",
        "event type: message, not found handler",
    )
    if "[SDK Error] handle message failed" in text and any(fragment in text for fragment in harmless_sdk_errors):
        return False

    return True


class MuxCommandError(RuntimeError):
    pass


def run_mux(
    args: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    mux_name = mux_binary_name()
    completed = subprocess.run(
        [mux_name, *args],
        input=input_text,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and completed.returncode != 0:
        raise MuxCommandError(
            f"{mux_name} {' '.join(args)} failed with code {completed.returncode}: {completed.stderr.strip()}"
        )
    return completed


@dataclass(frozen=True)
class BridgeConfig:
    cwd: Path
    command: str
    allowed_sender_open_id: str
    message_scope: str
    skill_path: Path
    ack_text: str
    session_idle_minutes: int
    log_path: Path
    tmux_session_name: str

    @property
    def session_idle_seconds(self) -> float:
        return float(self.session_idle_minutes) * 60.0

    @property
    def command_argv(self) -> list[str]:
        return split_command(self.command)


@dataclass(frozen=True)
class IncomingMessage:
    message_id: str
    chat_id: str
    sender_id: str
    message_type: str
    content: str


@dataclass(frozen=True)
class ControlCommand:
    kind: str
    message_id: str


@dataclass
class ActiveTaskState:
    token: int
    message_id: str
    chat_id: str
    sender_id: str
    user_reply_seen: bool = False
    finished: bool = False
    watchdog_count: int = 0


class MessageDeduper:
    def __init__(self, max_entries: int = 4096, persistence_path: Path | None = None) -> None:
        self._max_entries = max_entries
        self._entries: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()
        self._persistence_path = persistence_path
        self._load()

    def seen(self, message_id: str) -> bool:
        with self._lock:
            if message_id in self._entries:
                self._entries.move_to_end(message_id)
                return True

            self._entries[message_id] = None
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
            self._persist()
            return False

    def _load(self) -> None:
        if not self._persistence_path or not self._persistence_path.exists():
            return

        try:
            for raw_line in self._persistence_path.read_text(encoding="utf-8").splitlines():
                message_id = raw_line.strip()
                if not message_id:
                    continue
                self._entries[message_id] = None
                while len(self._entries) > self._max_entries:
                    self._entries.popitem(last=False)
        except OSError:
            return

    def _persist(self) -> None:
        if not self._persistence_path:
            return

        try:
            self._persistence_path.parent.mkdir(parents=True, exist_ok=True)
            self._persistence_path.write_text("\n".join(self._entries.keys()) + "\n", encoding="utf-8")
        except OSError:
            return


class LarkMessenger:
    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def reply_text(self, message_id: str, text: str) -> bool:
        if not text.strip():
            return True

        command = [
            "lark-cli",
            "im",
            "+messages-reply",
            "--message-id",
            message_id,
            "--text",
            text,
            "--as",
            "bot",
        ]
        self._logger.info("Replying to Feishu message %s", message_id)
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode == 0:
            return True

        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        self._logger.error(
            "Feishu reply failed for %s (code=%s). stdout=%s stderr=%s",
            message_id,
            completed.returncode,
            stdout,
            stderr,
        )
        return False


class CodexSession:
    def __init__(self, config: BridgeConfig, logger: logging.Logger, messenger: LarkMessenger) -> None:
        self._config = config
        self._logger = logger
        self._messenger = messenger
        self._task_queue: queue.Queue[IncomingMessage | ControlCommand | None] = queue.Queue()
        self._shutdown = threading.Event()
        self._process_lock = threading.RLock()
        self._output_lock = threading.Lock()

        self._reader_thread: threading.Thread | None = None
        self._worker_thread = threading.Thread(target=self._run_worker, name="codex-session-worker", daemon=True)
        self._worker_thread.start()

        self._ui_ready = threading.Event()
        self._bootstrap_sent = False
        self._term_confirmed = False
        self._trust_confirmed = False
        self._output_tail = ""
        self._last_snapshot = ""
        self._last_output_at = 0.0
        self._last_task_finished_at = 0.0
        self._recent_logged_lines: OrderedDict[str, float] = OrderedDict()
        self._busy = threading.Event()
        self._active_task_lock = threading.Lock()
        self._active_task: ActiveTaskState | None = None
        self._task_token_counter = 0
        self._mux_name = mux_binary_name()
        self._runtime_dir = Path(__file__).parent / RUNTIME_DIR_NAME
        self._lark_cli_event_log_path = self._runtime_dir / f"{self._config.tmux_session_name}.lark-cli-events.jsonl"
        self._lark_cli_wrapper_path = Path(__file__).parent / lark_cli_wrapper_relative_path()
        self._lark_cli_wrapper_module_path = Path(__file__).parent / LARK_CLI_WRAPPER_MODULE_RELATIVE_PATH
        self._real_lark_cli_path = shutil.which("lark-cli") or "lark-cli"
        self._lark_event_thread: threading.Thread | None = None

    @property
    def _mux_target(self) -> str:
        return f"{self._config.tmux_session_name}:0.0"

    def _attach_command(self) -> str:
        return mux_attach_command(self._config.tmux_session_name)

    def _session_environment(self) -> dict[str, str]:
        path_entries = [str(self._lark_cli_wrapper_path.parent)]
        if os.environ.get("PATH"):
            path_entries.append(os.environ["PATH"])

        return {
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "COLUMNS": str(TMUX_COLUMNS),
            "LINES": str(TMUX_LINES),
            "FEISHU_BRIDGE_REAL_LARK_CLI": self._real_lark_cli_path,
            "FEISHU_BRIDGE_LARK_EVENT_LOG": str(self._lark_cli_event_log_path),
            "PATH": os.pathsep.join(path_entries),
        }

    def submit(self, message: IncomingMessage) -> None:
        self._task_queue.put(message)

    def submit_reset(self, message_id: str) -> int:
        self._task_queue.put(ControlCommand(kind="reset", message_id=message_id))
        return self.queue_length

    def interrupt(self) -> tuple[bool, str]:
        with self._process_lock:
            if not self._session_exists():
                return False, "当前没有活动的 Codex 会话可中断。"

            if not self.is_busy:
                return False, "当前没有正在执行的任务，不需要中断。"

            self._logger.info("Sending interrupt signal to Codex session")
            self._send_keys("Escape")
            return (
                True,
                (
                    "已向当前 Codex 会话发送中断信号。\n"
                    f"{mux_session_label()}名：{self._config.tmux_session_name}\n"
                    "如果这一轮仍未停止，可以继续发送 /reset 强制重建会话。"
                ),
            )

    @property
    def queue_length(self) -> int:
        return self._task_queue.qsize()

    @property
    def is_busy(self) -> bool:
        return self._busy.is_set()

    def build_status_text(self) -> str:
        session_exists = self._session_exists()
        if self._last_task_finished_at > 0:
            idle_seconds = max(0, int(time.monotonic() - self._last_task_finished_at))
            idle_text = f"{idle_seconds} 秒"
        else:
            idle_text = "尚无完成记录"

        bootstrap_text = "已完成" if (session_exists and self._bootstrap_sent) else "未完成"
        session_text = "在线" if session_exists else "未启动"
        busy_text = "是" if self.is_busy else "否"

        return "\n".join(
            [
                "桥接状态：运行中",
                f"{mux_session_label()}名：{self._config.tmux_session_name}",
                f"{mux_session_label()}：{session_text}",
                f"会话引导：{bootstrap_text}",
                f"处理中：{busy_text}",
                f"排队消息数：{self.queue_length}",
                f"上次任务完成后空闲：{idle_text}",
                f"工作目录：{self._config.cwd}",
                f"attach 命令：{self._attach_command()}",
            ]
        )

    def close(self) -> None:
        self._shutdown.set()
        self._task_queue.put(None)
        self._worker_thread.join(timeout=5.0)
        self._stop_process()

    def _run_worker(self) -> None:
        while not self._shutdown.is_set():
            task = self._task_queue.get()
            if task is None:
                self._task_queue.task_done()
                return

            self._busy.set()
            active_token = 0
            try:
                if isinstance(task, ControlCommand):
                    self._handle_control_command(task)
                else:
                    self._ensure_session()
                    active_token = self._begin_active_task(task.message_id, task.chat_id, task.sender_id)
                    envelope = build_message_envelope(
                        chat_id=task.chat_id,
                        message_id=task.message_id,
                        sender_open_id=task.sender_id,
                        skill_path=self._config.skill_path,
                        content=task.content,
                    )
                    self._submit_prompt(envelope, prompt_name=f"user-message:{task.message_id}")
            except Exception:
                task_label = task.message_id if isinstance(task, (IncomingMessage, ControlCommand)) else "unknown"
                self._logger.exception("Failed to process task %s inside Codex session", task_label)
                self._messenger.reply_text(task_label, CODEx_FAILURE_TEXT)
                self._stop_process()
            finally:
                if active_token:
                    self._finish_active_task(active_token)
                self._busy.clear()
                self._last_task_finished_at = time.monotonic()
                self._task_queue.task_done()

    def _handle_control_command(self, command: ControlCommand) -> None:
        if command.kind != "reset":
            self._logger.warning("Ignoring unknown control command: %s", command.kind)
            return

        self._logger.info("Resetting Codex session by command message %s", command.message_id)
        self._stop_process()
        self._ensure_session()
        self._messenger.reply_text(
            command.message_id,
            (
                "已重置本地 Codex 会话。\n"
                f"新的 {mux_session_label()}已就绪：{self._config.tmux_session_name}\n"
                f"可查看：{self._attach_command()}"
            ),
        )

    def _ensure_session(self) -> None:
        with self._process_lock:
            if self._session_idle_expired():
                self._logger.info("Codex session idle timeout reached, restarting session")
                self._stop_process()

            if self._bootstrap_sent and not self._session_exists():
                self._logger.warning("Codex %s disappeared, recreating it", mux_session_label())
                self._stop_process()

            if not self._session_exists():
                self._start_process()

            if not self._bootstrap_sent:
                bootstrap = build_bootstrap_prompt(self._config.skill_path)
                self._submit_prompt(bootstrap, prompt_name="bootstrap")
                self._bootstrap_sent = True

    def _session_idle_expired(self) -> bool:
        if not self._session_exists() or self._config.session_idle_seconds <= 0:
            return False
        if self._last_task_finished_at <= 0:
            return False
        return (time.monotonic() - self._last_task_finished_at) >= self._config.session_idle_seconds

    def _start_process(self) -> None:
        argv = self._config.command_argv
        if not argv:
            raise RuntimeError("command is empty")

        self._ui_ready.clear()
        self._bootstrap_sent = False
        self._term_confirmed = False
        self._trust_confirmed = False
        self._output_tail = ""
        self._last_snapshot = ""
        self._last_output_at = time.monotonic()
        self._recent_logged_lines.clear()

        if self._session_exists():
            self._stop_process()

        self._prepare_runtime_files()
        self._logger.info("Starting Codex session: %s", self._config.command)
        if is_windows():
            self._start_windows_session(argv)
        else:
            self._start_posix_session()
        self._configure_mux_session()
        self._logger.info("%s session created: %s", self._mux_name, self._config.tmux_session_name)
        self._logger.info("Attach with: %s", self._attach_command())
        self._lark_event_thread = threading.Thread(
            target=self._read_lark_cli_event_loop,
            name="codex-lark-cli-event-reader",
            daemon=True,
        )
        self._lark_event_thread.start()
        self._reader_thread = threading.Thread(target=self._read_output_loop, name="codex-output-reader", daemon=True)
        self._reader_thread.start()

        if not self._ui_ready.wait(timeout=SESSION_STARTUP_TIMEOUT_SECONDS):
            self._logger.warning("Codex startup prompt was not detected within %.1fs", SESSION_STARTUP_TIMEOUT_SECONDS)

    def _start_posix_session(self) -> None:
        shell_command = (
            f"export TERM=xterm-256color COLORTERM=truecolor "
            f"COLUMNS={TMUX_COLUMNS} LINES={TMUX_LINES}; "
            f"export FEISHU_BRIDGE_REAL_LARK_CLI={shlex.quote(self._real_lark_cli_path)}; "
            f"export FEISHU_BRIDGE_LARK_EVENT_LOG={shlex.quote(str(self._lark_cli_event_log_path))}; "
            f"export PATH={shlex.quote(str(self._lark_cli_wrapper_path.parent))}{os.pathsep}$PATH; "
            f"exec {self._config.command}"
        )
        run_mux(
            [
                "new-session",
                "-d",
                "-s",
                self._config.tmux_session_name,
                "-x",
                str(TMUX_COLUMNS),
                "-y",
                str(TMUX_LINES),
                "-c",
                str(self._config.cwd),
                shell_command,
            ]
        )

    def _start_windows_session(self, argv: list[str]) -> None:
        run_mux(
            [
                "new-session",
                "-d",
                "-s",
                self._config.tmux_session_name,
                "-x",
                str(TMUX_COLUMNS),
                "-y",
                str(TMUX_LINES),
                "-c",
                str(self._config.cwd),
            ]
        )
        for key, value in self._session_environment().items():
            run_mux(["set-environment", "-t", self._config.tmux_session_name, key, value])
        run_mux(
            [
                "respawn-pane",
                "-k",
                "-t",
                self._mux_target,
                "-c",
                str(self._config.cwd),
                "--",
                *argv,
            ]
        )

    def _configure_mux_session(self) -> None:
        session_target = self._config.tmux_session_name
        window_target = f"{session_target}:0"
        scroll_lines = str(TMUX_WHEEL_SCROLL_LINES)

        run_mux(["set-option", "-t", session_target, "mouse", "on"], check=False)
        run_mux(["set-window-option", "-t", window_target, "history-limit", str(TMUX_HISTORY_LIMIT)], check=False)
        run_mux(["set-window-option", "-t", window_target, "mode-keys", "vi"], check=False)

        # Prefer mux copy-mode scrolling over passing wheel events through to Codex.
        run_mux(["unbind-key", "-T", "root", "WheelUpPane"], check=False)
        run_mux(["unbind-key", "-T", "root", "WheelDownPane"], check=False)
        run_mux(
            [
                "bind-key",
                "-T",
                "root",
                "WheelUpPane",
                "if-shell",
                "-F",
                "#{pane_in_mode}",
                f"send-keys -X -N {scroll_lines} scroll-up",
                f"copy-mode -e; send-keys -X -N {scroll_lines} scroll-up",
            ],
            check=False,
        )
        run_mux(
            [
                "bind-key",
                "-T",
                "root",
                "WheelDownPane",
                "if-shell",
                "-F",
                "#{pane_in_mode}",
                f"send-keys -X -N {scroll_lines} scroll-down",
                "",
            ],
            check=False,
        )

    def _stop_process(self) -> None:
        self._ui_ready.clear()
        self._bootstrap_sent = False

        if self._session_exists():
            try:
                run_mux(["send-keys", "-t", self._mux_target, "/quit", "Enter"], check=False)
                time.sleep(1.0)
            except MuxCommandError:
                pass

            if self._session_exists():
                run_mux(["kill-session", "-t", self._config.tmux_session_name], check=False)

        reader_thread = self._reader_thread
        if reader_thread and reader_thread.is_alive():
            reader_thread.join(timeout=1.0)
        self._reader_thread = None

        lark_event_thread = self._lark_event_thread
        if lark_event_thread and lark_event_thread.is_alive():
            lark_event_thread.join(timeout=1.0)
        self._lark_event_thread = None

    def _read_output_loop(self) -> None:
        while not self._shutdown.is_set() and self._session_exists():
            snapshot = self._capture_snapshot()
            if snapshot is None:
                return

            now = time.monotonic()
            if snapshot != self._last_snapshot:
                with self._output_lock:
                    self._last_output_at = now
                    self._output_tail = snapshot[-OUTPUT_TAIL_LIMIT:]
                    tail = self._output_tail

                self._log_snapshot(snapshot, now)
                self._last_snapshot = snapshot
                self._handle_session_prompts(tail)

            time.sleep(0.25)

    def _handle_session_prompts(self, tail: str) -> None:
        if not self._term_confirmed and 'Continue anyway? [y/N]:' in tail:
            self._logger.info("Accepting TERM compatibility prompt")
            self._send_keys("y", "Enter")
            self._term_confirmed = True

        if not self._trust_confirmed and "Do you trust the contents of this directory?" in tail:
            self._logger.info("Accepting Codex workspace trust prompt")
            self._send_keys("Enter")
            self._trust_confirmed = True

        if (
            "To get started, describe a task" in tail
            or "Write tests for @filename" in tail
            or "OpenAI Codex" in tail
            or "tab to queue message" in tail
            or "context left" in tail
            or "Starting MCP servers" in tail
        ):
            self._ui_ready.set()

    def _session_exists(self) -> bool:
        return run_mux(["has-session", "-t", self._config.tmux_session_name], check=False).returncode == 0

    def _capture_snapshot(self) -> str | None:
        completed = run_mux(
            [
                "capture-pane",
                "-p",
                "-J",
                "-t",
                self._mux_target,
                "-S",
                f"-{TMUX_CAPTURE_HISTORY_LINES}",
            ],
            check=False,
        )
        if completed.returncode != 0:
            return None
        return completed.stdout

    def _send_keys(self, *keys: str) -> None:
        run_mux(["send-keys", "-t", self._mux_target, *keys])

    def _paste_text(self, text: str) -> None:
        prompt_path = self._runtime_dir / f"prompt-{uuid.uuid4().hex}.txt"
        prompt_path.write_text(text, encoding="utf-8")
        try:
            run_mux(["load-buffer", str(prompt_path)])
            run_mux(["paste-buffer", "-d", "-t", self._mux_target])
        finally:
            try:
                prompt_path.unlink()
            except OSError:
                pass

    def _send_submit_key(self) -> None:
        self._send_keys("C-m")

    def _wait_for_output_change(self, baseline: float, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline and not self._shutdown.is_set():
            with self._output_lock:
                last_output = self._last_output_at
            if last_output > baseline:
                return True
            time.sleep(0.1)
        return False

    def _log_snapshot(self, snapshot: str, now: float) -> None:
        cutoff = now - LOG_DEDUP_WINDOW_SECONDS
        stale_keys = [line for line, seen_at in self._recent_logged_lines.items() if seen_at < cutoff]
        for key in stale_keys:
            self._recent_logged_lines.pop(key, None)

        for line in collapse_for_log(snapshot):
            normalized = " ".join(line.split())
            if normalized in self._recent_logged_lines:
                self._recent_logged_lines[normalized] = now
                continue

            self._recent_logged_lines[normalized] = now
            while len(self._recent_logged_lines) > 512:
                self._recent_logged_lines.popitem(last=False)
            self._logger.info("[codex] %s", line)

    def _submit_prompt(self, prompt: str, *, prompt_name: str) -> None:
        if not self._session_exists():
            raise RuntimeError(f"Codex {self._mux_name} session is not running")

        if not self._ui_ready.is_set():
            self._ui_ready.wait(timeout=SESSION_STARTUP_TIMEOUT_SECONDS)

        self._logger.info("Submitting prompt to Codex (%s)", prompt_name)
        self._paste_text(prompt)
        time.sleep(PROMPT_PASTE_SETTLE_SECONDS)
        with self._output_lock:
            submit_baseline = self._last_output_at
        self._send_submit_key()
        if not self._wait_for_output_change(submit_baseline, PROMPT_SUBMIT_RETRY_DELAY_SECONDS):
            self._logger.warning(
                "No Codex activity detected after submit key for %s, retrying once",
                prompt_name,
            )
            self._send_submit_key()
        self._wait_for_quiet(prompt_name)

    def _wait_for_quiet(self, prompt_name: str) -> None:
        start = time.monotonic()
        saw_new_output = False
        baseline = self._last_output_at

        while not self._shutdown.is_set():
            if not self._session_exists():
                raise RuntimeError(f"Codex {self._mux_name} session disappeared while handling {prompt_name}")

            with self._output_lock:
                last_output = self._last_output_at

            if last_output > baseline:
                saw_new_output = True

            now = time.monotonic()
            if saw_new_output and (now - last_output) >= QUIET_WINDOW_SECONDS:
                return

            if not saw_new_output and (now - start) >= FIRST_OUTPUT_TIMEOUT_SECONDS:
                self._logger.warning(
                    "No Codex output detected within %.1fs for %s; continuing",
                    FIRST_OUTPUT_TIMEOUT_SECONDS,
                    prompt_name,
                )
                return

            if (now - start) >= TASK_MAX_WAIT_SECONDS:
                self._logger.warning("Timed out waiting for Codex quiet after %s", prompt_name)
                return

            time.sleep(0.5)

    def _prepare_runtime_files(self) -> None:
        self._runtime_dir.mkdir(parents=True, exist_ok=True)
        self._lark_cli_event_log_path.write_text("", encoding="utf-8")
        if not self._lark_cli_wrapper_path.exists():
            raise RuntimeError(f"Missing lark-cli wrapper: {self._lark_cli_wrapper_path}")
        if not self._lark_cli_wrapper_module_path.exists():
            raise RuntimeError(f"Missing lark-cli wrapper module: {self._lark_cli_wrapper_module_path}")

    def _begin_active_task(self, message_id: str, chat_id: str, sender_id: str) -> int:
        with self._active_task_lock:
            self._task_token_counter += 1
            token = self._task_token_counter
            self._active_task = ActiveTaskState(
                token=token,
                message_id=message_id,
                chat_id=chat_id,
                sender_id=sender_id,
            )

        threading.Thread(
            target=self._watch_for_user_visible_reply,
            args=(token, message_id),
            name=f"codex-watchdog-{token}",
            daemon=True,
        ).start()
        return token

    def _finish_active_task(self, token: int) -> None:
        with self._active_task_lock:
            state = self._active_task
            if not state or state.token != token:
                return
            state.finished = True
            self._active_task = None

    def _watch_for_user_visible_reply(self, token: int, message_id: str) -> None:
        delay_seconds = WATCHDOG_FIRST_REPLY_DELAY_SECONDS

        while not self._shutdown.is_set():
            time.sleep(delay_seconds)

            with self._active_task_lock:
                state = self._active_task
                if not state or state.token != token or state.finished or state.user_reply_seen:
                    return

                state.watchdog_count += 1
                watchdog_count = state.watchdog_count

            if watchdog_count == 1:
                self._logger.info(
                    "Watchdog triggered for message %s after %.1fs without Codex-visible reply",
                    message_id,
                    WATCHDOG_FIRST_REPLY_DELAY_SECONDS,
                )
                self._messenger.reply_text(message_id, WATCHDOG_PROCESSING_TEXT)
                delay_seconds = WATCHDOG_HEARTBEAT_INTERVAL_SECONDS
                continue

            self._logger.info(
                "Watchdog heartbeat #%s for message %s after %.1fs without Codex-visible reply",
                watchdog_count - 1,
                message_id,
                WATCHDOG_HEARTBEAT_INTERVAL_SECONDS,
            )
            self._messenger.reply_text(message_id, WATCHDOG_HEARTBEAT_TEXT)
            delay_seconds = WATCHDOG_HEARTBEAT_INTERVAL_SECONDS

    def _read_lark_cli_event_loop(self) -> None:
        position = 0
        buffered = ""

        while not self._shutdown.is_set() and self._session_exists():
            try:
                if self._lark_cli_event_log_path.exists():
                    file_size = self._lark_cli_event_log_path.stat().st_size
                    if position > file_size:
                        position = 0

                    with self._lark_cli_event_log_path.open("r", encoding="utf-8") as file:
                        file.seek(position)
                        chunk = file.read()
                        position = file.tell()
                else:
                    chunk = ""
            except OSError as exc:
                self._logger.warning("Failed to read lark-cli event log: %s", exc)
                time.sleep(0.25)
                continue

            if chunk:
                buffered += chunk
                while "\n" in buffered:
                    line, buffered = buffered.split("\n", 1)
                    line = line.strip()
                    if line:
                        self._handle_lark_cli_event_line(line)

            time.sleep(0.25)

    def _handle_lark_cli_event_line(self, line: str) -> None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            self._logger.warning("Ignoring malformed lark-cli event: %s", line)
            return

        kind = str(payload.get("kind") or "")
        returncode = payload.get("returncode")
        if kind not in {"messages-reply", "messages-send"} or returncode != 0:
            return

        target_message_id = str(payload.get("message_id") or "")
        target_chat_id = str(payload.get("chat_id") or "")
        target_user_id = str(payload.get("user_id") or "")
        self._logger.info(
            "Observed Codex %s event%s",
            kind,
            f" for {target_message_id}" if target_message_id else "",
        )
        self._mark_active_task_user_reply_seen(
            kind=kind,
            target_message_id=target_message_id,
            target_chat_id=target_chat_id,
            target_user_id=target_user_id,
        )

    def _mark_active_task_user_reply_seen(
        self,
        *,
        kind: str,
        target_message_id: str,
        target_chat_id: str,
        target_user_id: str,
    ) -> None:
        with self._active_task_lock:
            state = self._active_task
            if not state or state.finished or state.user_reply_seen:
                return

            matched = False
            if kind == "messages-reply":
                matched = bool(target_message_id) and target_message_id == state.message_id
            elif kind == "messages-send":
                matched = (
                    bool(target_chat_id) and target_chat_id == state.chat_id
                ) or (
                    bool(target_user_id) and target_user_id == state.sender_id
                )

            if not matched:
                return

            state.user_reply_seen = True


class BridgeService:
    def __init__(self, config: BridgeConfig, logger: logging.Logger) -> None:
        self._config = config
        self._logger = logger
        self._messenger = LarkMessenger(logger)
        self._session = CodexSession(config, logger, self._messenger)
        self._runtime_dir = Path(__file__).parent / RUNTIME_DIR_NAME
        self._seen_message_ids_path = self._runtime_dir / SEEN_MESSAGE_IDS_FILENAME
        self._deduper = MessageDeduper(persistence_path=self._seen_message_ids_path)
        self._stop_event = threading.Event()
        self._subscription_lock = threading.Lock()
        self._subscription_proc: subprocess.Popen[str] | None = None
        self._state_lock = threading.Lock()
        self._subscription_started_at = 0.0
        self._subscription_ready_at = 0.0
        self._subscription_last_event_at = 0.0

    def close(self) -> None:
        self._stop_event.set()
        with self._subscription_lock:
            proc = self._subscription_proc
            self._subscription_proc = None
        if proc and proc.poll() is None:
            proc.terminate()
        self._session.close()

    def run(self) -> None:
        while not self._stop_event.is_set():
            self._run_subscribe_once()
            if not self._stop_event.is_set():
                self._logger.warning(
                    "Feishu event subscription stopped unexpectedly, restarting in %.1fs",
                    SUBSCRIBE_RESTART_DELAY_SECONDS,
                )
                time.sleep(SUBSCRIBE_RESTART_DELAY_SECONDS)

    def _run_subscribe_once(self) -> None:
        command = [
            "lark-cli",
            "event",
            "+subscribe",
            "--event-types",
            "im.message.receive_v1",
            "--compact",
            "--quiet",
            "--as",
            "bot",
        ]
        self._logger.info("Starting Feishu event subscription")
        with self._state_lock:
            self._subscription_started_at = time.monotonic()
            self._subscription_ready_at = 0.0
            self._subscription_last_event_at = 0.0
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        with self._subscription_lock:
            self._subscription_proc = proc

        stderr_thread = threading.Thread(target=self._log_stderr, args=(proc,), daemon=True)
        stderr_thread.start()
        ready_thread = threading.Thread(target=self._await_subscription_ready, args=(proc,), daemon=True)
        ready_thread.start()

        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                if self._stop_event.is_set():
                    break
                text = line.strip()
                if not text:
                    continue
                self._mark_subscription_event_observed()
                self._handle_event_line(text)
        finally:
            with self._subscription_lock:
                if self._subscription_proc is proc:
                    self._subscription_proc = None
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    proc.kill()

            stderr_thread.join(timeout=1.0)
            ready_thread.join(timeout=1.0)

    def _await_subscription_ready(self, proc: subprocess.Popen[str]) -> None:
        deadline = time.monotonic() + SUBSCRIPTION_READY_STABILIZE_SECONDS
        while time.monotonic() < deadline:
            if self._stop_event.is_set() or proc.poll() is not None:
                return
            time.sleep(0.1)

        if self._stop_event.is_set() or proc.poll() is not None:
            return

        if self._mark_subscription_ready():
            self._logger.info(
                "Feishu event subscription ready (connection stable, you can now send messages from Feishu)"
            )

    def _log_stderr(self, proc: subprocess.Popen[str]) -> None:
        if not proc.stderr:
            return
        for line in proc.stderr:
            text = line.strip()
            if should_log_lark_event_stderr(text):
                self._logger.info("[lark-event] %s", text)

    def _mark_subscription_ready(self) -> bool:
        with self._state_lock:
            if self._subscription_ready_at > 0:
                return False
            self._subscription_ready_at = time.monotonic()
            return True

    def _mark_subscription_event_observed(self) -> None:
        should_log_first_event = False
        with self._state_lock:
            if self._subscription_ready_at <= 0:
                self._subscription_ready_at = time.monotonic()
            if self._subscription_last_event_at <= 0:
                should_log_first_event = True
            self._subscription_last_event_at = time.monotonic()

        if should_log_first_event:
            self._logger.info("Feishu event subscription is receiving events (first event observed)")

    def _subscription_process_alive(self) -> bool:
        with self._subscription_lock:
            proc = self._subscription_proc
        return bool(proc and proc.poll() is None)

    def _format_age_seconds(self, event_at: float) -> str:
        if event_at <= 0:
            return "未知"
        return f"{max(0, int(time.monotonic() - event_at))} 秒前"

    def build_status_text(self) -> str:
        base_lines = self._session.build_status_text().splitlines()
        if base_lines and base_lines[0].startswith("桥接状态："):
            session_lines = base_lines[1:]
            header_line = base_lines[0]
        else:
            session_lines = base_lines
            header_line = "桥接状态：运行中"

        with self._state_lock:
            subscription_started_at = self._subscription_started_at
            subscription_ready_at = self._subscription_ready_at
            subscription_last_event_at = self._subscription_last_event_at

        if self._subscription_process_alive():
            subscription_process_text = "已启动"
        else:
            subscription_process_text = "未启动"

        if subscription_last_event_at > 0:
            event_channel_text = f"已收到事件（最近 {self._format_age_seconds(subscription_last_event_at)}）"
        elif subscription_ready_at > 0 and self._subscription_process_alive():
            event_channel_text = "已就绪，当前可以在飞书发送消息"
        elif subscription_started_at > 0 and self._subscription_process_alive():
            event_channel_text = "连接中，正在建立事件通道"
        else:
            event_channel_text = "未连接"

        lines = [
            header_line,
            f"订阅进程：{subscription_process_text}",
            f"事件通道：{event_channel_text}",
        ]
        lines.extend(session_lines)
        return "\n".join(lines)

    def _handle_event_line(self, line: str) -> None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            self._logger.warning("Ignoring non-JSON Feishu event: %s", line)
            return

        if payload.get("type") != "im.message.receive_v1":
            return

        message = self._parse_incoming_message(payload)
        if not message:
            return
        self._process_incoming_message(message)

    def _parse_incoming_message(self, payload: dict[str, Any]) -> IncomingMessage | None:
        chat_type = str(payload.get("chat_type") or "")
        sender_id = str(payload.get("sender_id") or "")
        message_id = str(payload.get("message_id") or "")
        chat_id = str(payload.get("chat_id") or "")
        message_type = str(payload.get("message_type") or "")
        content = str(payload.get("content") or "")

        if chat_type != self._config.message_scope:
            self._logger.info("Ignoring message %s because chat_type=%s", message_id, chat_type)
            return None

        if sender_id != self._config.allowed_sender_open_id:
            self._logger.info("Ignoring message %s from unauthorized sender %s", message_id, sender_id)
            return None

        if not message_id or not chat_id:
            self._logger.warning("Ignoring incomplete Feishu event: %s", payload)
            return None

        return IncomingMessage(
            message_id=message_id,
            chat_id=chat_id,
            sender_id=sender_id,
            message_type=message_type,
            content=content,
        )

    def _process_incoming_message(self, message: IncomingMessage) -> None:
        if message.message_id and self._deduper.seen(message.message_id):
            self._logger.info("Skipping duplicated message %s", message.message_id)
            return

        if message.message_type not in {"text", "post"} or not message.content.strip():
            self._messenger.reply_text(message.message_id, UNSUPPORTED_MESSAGE_TEXT)
            return

        normalized_content = message.content.strip()
        if normalized_content == "/status":
            self._messenger.reply_text(message.message_id, self.build_status_text())
            return

        if normalized_content == "/reset":
            queue_length = self._session.submit_reset(message.message_id)
            self._messenger.reply_text(
                message.message_id,
                (
                    "已收到重置请求，会按当前顺序执行。\n"
                    f"当前排队消息数：{queue_length}\n"
                    f"{mux_session_label()}名：{self._config.tmux_session_name}"
                ),
            )
            return

        if normalized_content == "/interrupt":
            _, reply_text = self._session.interrupt()
            self._messenger.reply_text(message.message_id, reply_text)
            return

        self._messenger.reply_text(message.message_id, self._config.ack_text)
        self._session.submit(message)


def load_config(path: Path) -> BridgeConfig:
    with path.open("rb") as file:
        raw = tomllib.load(file)
    config_dir = path.parent

    def require_string(key: str) -> str:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"config key `{key}` must be a non-empty string")
        return value.strip()

    def resolve_path(value: str) -> Path:
        resolved = Path(value).expanduser()
        if not resolved.is_absolute():
            resolved = config_dir / resolved
        return resolved.resolve()

    def reject_placeholder_path(key: str, value: Path) -> None:
        normalized = str(value)
        if "/ABSOLUTE/" in normalized or normalized.startswith("/ABSOLUTE"):
            raise ValueError(
                f"config key `{key}` is still using the example placeholder path: {normalized}"
            )

    cwd = resolve_path(require_string("cwd"))
    skill_path = resolve_path(require_string("skill_path"))
    log_path = resolve_path(require_string("log_path"))
    reject_placeholder_path("cwd", cwd)
    reject_placeholder_path("log_path", log_path)
    tmux_session_name = raw.get("tmux_session_name", DEFAULT_TMUX_SESSION_NAME)
    if not isinstance(tmux_session_name, str) or not tmux_session_name.strip():
        raise ValueError("config key `tmux_session_name` must be a non-empty string when provided")

    session_idle_minutes = raw.get("session_idle_minutes")
    if not isinstance(session_idle_minutes, int) or session_idle_minutes <= 0:
        raise ValueError("config key `session_idle_minutes` must be a positive integer")

    message_scope = require_string("message_scope")
    if message_scope != "p2p":
        raise ValueError("v1 only supports `message_scope = \"p2p\"`")

    ack_text = raw.get("ack_text", DEFAULT_ACK_TEXT)
    if not isinstance(ack_text, str) or not ack_text.strip():
        raise ValueError("config key `ack_text` must be a non-empty string when provided")

    return BridgeConfig(
        cwd=cwd,
        command=require_string("command"),
        allowed_sender_open_id=require_string("allowed_sender_open_id"),
        message_scope=message_scope,
        skill_path=skill_path,
        ack_text=ack_text.strip(),
        session_idle_minutes=session_idle_minutes,
        log_path=log_path,
        tmux_session_name=tmux_session_name.strip(),
    )


def check_environment(config: BridgeConfig) -> list[str]:
    problems: list[str] = []

    if not config.cwd.exists():
        problems.append(f"cwd does not exist: {config.cwd}")
    elif not config.cwd.is_dir():
        problems.append(f"cwd is not a directory: {config.cwd}")

    if not config.skill_path.exists():
        problems.append(f"skill_path does not exist: {config.skill_path}")

    command_argv = config.command_argv
    if not command_argv:
        problems.append("command is empty after parsing")
    else:
        executable = command_argv[0]
        if "/" in executable or "\\" in executable:
            if not Path(executable).expanduser().exists():
                problems.append(f"command executable does not exist: {executable}")
        elif shutil.which(executable) is None:
            problems.append(f"command executable not found in PATH: {executable}")

    if shutil.which("lark-cli") is None:
        problems.append("lark-cli is not available in PATH")

    mux_name = mux_binary_name()
    if shutil.which(mux_name) is None:
        if is_windows():
            problems.append("psmux is not available in PATH. Install it with: winget install psmux")
        else:
            problems.append("tmux is not available in PATH")

    wrapper_path = Path(__file__).parent / lark_cli_wrapper_relative_path()
    if not wrapper_path.exists():
        problems.append(f"lark-cli wrapper does not exist: {wrapper_path}")

    wrapper_module_path = Path(__file__).parent / LARK_CLI_WRAPPER_MODULE_RELATIVE_PATH
    if not wrapper_module_path.exists():
        problems.append(f"lark-cli wrapper module does not exist: {wrapper_module_path}")

    return problems


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bridge Feishu P2P messages into a long-lived Codex CLI session.")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).with_name("bridge.toml")),
        help="Path to the runtime TOML config file.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate the config and local environment, then exit.",
    )
    return parser


def setup_logger(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("feishu_codex_bridge")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    config_path = Path(args.config).expanduser()

    try:
        config = load_config(config_path)
    except Exception as exc:
        print(f"Failed to load config: {exc}", file=sys.stderr)
        return 1

    try:
        logger = setup_logger(config.log_path)
    except OSError as exc:
        print(f"Failed to initialize log file `{config.log_path}`: {exc}", file=sys.stderr)
        return 1

    problems = check_environment(config)
    if problems:
        for item in problems:
            logger.error(item)
        return 1

    if args.check:
        logger.info("Environment check passed")
        return 0

    service = BridgeService(config, logger)

    def handle_signal(signum: int, _frame: Any) -> None:
        logger.info("Received signal %s, shutting down", signum)
        service.close()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        service.run()
    finally:
        service.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
