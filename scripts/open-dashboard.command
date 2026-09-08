#!/bin/zsh
# Local entry point; the CLI owns encryption, authorization and loopback binding.
set -eu
cd -- "${0:A:h:h}"
whoop_python="$PWD/.venv/bin/python"
if [[ ! -x "$whoop_python" ]]; then
  print -u2 -- '请先在项目目录运行 uv sync --locked。'
  exit 1
fi
"$whoop_python" -m whoop_copilot.cli --environment real dashboard-open
print -- '看板已就绪，可以关闭此终端窗口。关闭服务请双击 stop-dashboard.command。'
