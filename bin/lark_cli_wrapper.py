#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


INTERESTING_COMMANDS = {
    ("im", "+messages-reply"): "messages-reply",
    ("im", "+messages-send"): "messages-send",
}


def get_flag_value(args: list[str], flag: str) -> str | None:
    prefix = f"{flag}="
    for index, arg in enumerate(args):
        if arg == flag and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith(prefix):
            return arg[len(prefix) :]
    return None


def append_event(event_path: str, payload: dict[str, object]) -> None:
    if not event_path:
        return

    path = Path(event_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False)
        file.write("\n")
        file.flush()


def has_flag(args: list[str], flag: str) -> bool:
    prefix = f"{flag}="
    return any(arg == flag or arg.startswith(prefix) for arg in args)


def normalize_legacy_im_args(args: list[str]) -> list[str]:
    if len(args) < 3:
        return args

    head = tuple(args[:2])
    rest = args[2:]

    if head == ("im", "+messages-reply") and not has_flag(rest, "--message-id"):
        if len(rest) >= 1:
            message_id = rest[0]
            text = rest[1] if len(rest) >= 2 else ""
            identity = rest[2] if len(rest) >= 3 else ""
            normalized = ["im", "+messages-reply", "--message-id", message_id]
            if text:
                normalized.extend(["--text", text])
            if identity:
                normalized.extend(["--as", identity])
            return normalized

    if head == ("im", "+messages-send") and not (
        has_flag(rest, "--chat-id") or has_flag(rest, "--user-id")
    ):
        if len(rest) >= 1:
            target = rest[0]
            text = rest[1] if len(rest) >= 2 else ""
            identity = rest[2] if len(rest) >= 3 else ""
            normalized = ["im", "+messages-send"]
            if target.startswith("ou_"):
                normalized.extend(["--user-id", target])
            else:
                normalized.extend(["--chat-id", target])
            if text:
                normalized.extend(["--text", text])
            if identity:
                normalized.extend(["--as", identity])
            return normalized

    return args


def main() -> int:
    real_lark_cli_command_json = os.environ.get("FEISHU_BRIDGE_REAL_LARK_CLI_JSON", "").strip()
    if real_lark_cli_command_json:
        try:
            real_lark_cli_command = json.loads(real_lark_cli_command_json)
        except json.JSONDecodeError as exc:
            print(f"FEISHU_BRIDGE_REAL_LARK_CLI_JSON is invalid: {exc}", file=sys.stderr)
            return 127
        if not isinstance(real_lark_cli_command, list) or not all(
            isinstance(item, str) and item for item in real_lark_cli_command
        ):
            print("FEISHU_BRIDGE_REAL_LARK_CLI_JSON must be a JSON array of strings", file=sys.stderr)
            return 127
    else:
        real_lark_cli = os.environ.get("FEISHU_BRIDGE_REAL_LARK_CLI", "").strip()
        if not real_lark_cli:
            print("FEISHU_BRIDGE_REAL_LARK_CLI_JSON is not set", file=sys.stderr)
            return 127
        real_lark_cli_command = [real_lark_cli]

    args = normalize_legacy_im_args(sys.argv[1:])
    kind = INTERESTING_COMMANDS.get(tuple(args[:2]), "")
    if "--help" in args or "-h" in args:
        kind = ""
    event_log_path = os.environ.get("FEISHU_BRIDGE_LARK_EVENT_LOG", "").strip()

    try:
        completed = subprocess.run([*real_lark_cli_command, *args])
    except OSError as exc:
        print(f"failed to execute real lark-cli: {exc}", file=sys.stderr)
        return 127

    if kind:
        append_event(
            event_log_path,
            {
                "ts": time.time(),
                "kind": kind,
                "returncode": completed.returncode,
                "message_id": get_flag_value(args, "--message-id"),
                "chat_id": get_flag_value(args, "--chat-id"),
                "user_id": get_flag_value(args, "--user-id"),
            },
        )

    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
