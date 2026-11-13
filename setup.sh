#!/usr/bin/env bash
# 一鍵安裝：建立虛擬環境、裝 Python 套件、拉翻譯模型
set -euo pipefail
cd "$(dirname "$0")"

PY="${PY:-$(command -v python3.12 || command -v python3)}"
echo "▶ 使用 Python：$PY ($("$PY" -V))"

if [ ! -d .venv ]; then
  "$PY" -m venv .venv
fi
source .venv/bin/activate
python -m pip install -qU pip
echo "▶ 安裝 Python 套件…"
python -m pip install -q -r requirements.txt

if command -v ollama >/dev/null 2>&1; then
  if ! curl -sf http://127.0.0.1:11434/api/version >/dev/null; then
    echo "▶ 啟動 Ollama 服務…"
    (ollama serve >/tmp/ollama-serve.log 2>&1 &)
    sleep 3
  fi
  MODEL=$(python -c "import json;print(json.load(open('config.json'))['ollama_model'])")
  echo "▶ 下載翻譯模型 $MODEL…"
  ollama pull "$MODEL"
else
  echo "⚠️  找不到 ollama，請先執行：brew install ollama"
fi

echo
echo "✅ 完成。執行方式："
echo "   ./run.sh --check   # 檢查環境"
echo "   ./run.sh           # 常駐使用"
