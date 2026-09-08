#!/bin/zsh
# Interactive local launcher. All configuration and authorization rules remain in the CLI.
set -eu
cd -- "${0:A:h:h}"

if [[ ! -t 0 || ! -t 1 ]]; then
  print -u2 -- '请在本机交互终端运行；此入口不接受管道输入。'
  exit 1
fi

whoop_python="$PWD/.venv/bin/python"
if [[ ! -x "$whoop_python" ]]; then
  print -u2 -- '请先在项目目录运行 uv sync --locked。'
  exit 1
fi

function whoop_cli() {
  "$whoop_python" -m whoop_copilot.cli "$@"
}

print -- 'WHOOP Personal Copilot · 本机接入'
print -- '凭据保存到 OS Keychain；Client Secret 在专用隐藏提示中输入。'
print -- '请准备 App 的 Client ID、Client Secret 和控制台实际登记的回调地址。'
print -- ''
read -r 'whoop_client_id?Client ID：'
read -r 'whoop_redirect?回调地址（回车使用 http://127.0.0.1:8765/oauth/callback）：'
whoop_redirect="${whoop_redirect:-http://127.0.0.1:8765/oauth/callback}"

if [[ ! -f runtime/real/copilot.sqlite3 ]]; then
  print -- ''
  print -- '将授权 WHOOP API / 导出数据在本机加密保存，不包含向模型或其他接收方外发。'
  print -- '到期会清理关联记录、分析和受管备份；应用未运行时不自动清理。'
  read -r 'whoop_retention?保留天数（1–3650，回车使用 90）：'
  whoop_retention="${whoop_retention:-90}"
  print -- "本地保留期限：${whoop_retention} 天。"
  read -r 'whoop_consent?同意以上本地存储安排请输入 YES，其他输入取消：'
  if [[ "$whoop_consent" != YES ]]; then
    print -- '已取消，尚未初始化真实数据库或保存凭据。'
    exit 0
  fi
  whoop_cli init-real --retention-days "$whoop_retention" --accept-local-storage
fi

whoop_cli whoop configure --client-id "$whoop_client_id" --redirect-uri "$whoop_redirect"
whoop_cli whoop login
whoop_cli whoop status

print -- ''
print -- '授权步骤已完成。请回到当前任务告知“已授权”，继续小窗口同步验证。'
read -r 'whoop_done?按回车关闭此配置入口。'
