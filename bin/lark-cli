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


def main() -> int:
    real_lark_cli = os.environ.get("FEISHU_BRIDGE_REAL_LARK_CLI", "").strip()
    if not real_lark_cli:
        print("FEISHU_BRIDGE_REAL_LARK_CLI is not set", file=sys.stderr)
        return 127

    args = sys.argv[1:]
    kind = INTERESTING_COMMANDS.get(tuple(args[:2]), "")
    if "--help" in args or "-h" in args:
        kind = ""
    event_log_path = os.environ.get("FEISHU_BRIDGE_LARK_EVENT_LOG", "").strip()

    try:
        completed = subprocess.run([real_lark_cli, *args])
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
