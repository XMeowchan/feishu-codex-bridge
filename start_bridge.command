#!/bin/bash
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python)"
else
  echo "未找到 python3 或 python，请先安装 Python。"
  echo
  read -n 1 -s -r -p "按任意键关闭窗口..."
  echo
  exit 1
fi

"$PYTHON_BIN" "$SCRIPT_DIR/start_bridge.py"
RC=$?

echo
if [ "$RC" -ne 0 ]; then
  echo "桥接服务已退出，退出码：$RC"
else
  echo "桥接服务已退出。"
fi
echo
read -n 1 -s -r -p "按任意键关闭窗口..."
echo
exit "$RC"
