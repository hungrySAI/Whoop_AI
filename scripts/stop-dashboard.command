#!/bin/zsh
# Only a matching, locally authenticated dashboard receives a graceful stop request.
set -eu
cd -- "${0:A:h:h}"
whoop_python="$PWD/.venv/bin/python"
if [[ ! -x "$whoop_python" ]]; then
  print -u2 -- '请先在项目目录运行 uv sync --locked。'
  exit 1
fi
"$whoop_python" -m whoop_copilot.cli --environment real dashboard-stop
print -- '看板已关闭，可以关闭此终端窗口。'
