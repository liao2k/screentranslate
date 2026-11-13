#!/usr/bin/env bash
# 打包成可雙擊的 螢幕翻譯.app（選單列工具，不佔 Dock）
set -euo pipefail
cd "$(dirname "$0")"
PROJECT="$(pwd)"
APP="$PROJECT/螢幕翻譯.app"
DEST="${1:-$HOME/Applications}"

echo "▶ 產生圖示…"
QT_QPA_PLATFORM=offscreen .venv/bin/python - <<'PY'
import sys
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication
from pathlib import Path
app = QApplication(sys.argv)
out = Path("build/AppIcon.iconset"); out.mkdir(parents=True, exist_ok=True)
for size in (16, 32, 64, 128, 256, 512, 1024):
    pm = QPixmap(size, size); pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm); p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(40, 110, 220))
    m = size * 0.06
    p.drawRoundedRect(int(m), int(m), int(size-2*m), int(size-2*m), size*0.22, size*0.22)
    f = QFont("PingFang TC", int(size * 0.56)); f.setBold(True)
    p.setFont(f); p.setPen(QColor(255, 255, 255))
    p.drawText(pm.rect(), int(Qt.AlignmentFlag.AlignCenter), "譯")
    p.end()
    for name in (f"icon_{size}x{size}.png", f"icon_{size//2}x{size//2}@2x.png"):
        n = int(name.split("_")[1].split("x")[0])
        if (n == size and "@2x" not in name) or (n * 2 == size and "@2x" in name):
            pm.save(str(out / name))
PY
iconutil -c icns build/AppIcon.iconset -o build/AppIcon.icns

echo "▶ 組裝 app bundle…"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp build/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>螢幕翻譯</string>
  <key>CFBundleDisplayName</key><string>螢幕翻譯</string>
  <key>CFBundleIdentifier</key><string>local.screen-translate</string>
  <key>CFBundleExecutable</key><string>screen-translate</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/screen-translate" <<LAUNCH
#!/usr/bin/env bash
PROJECT="$PROJECT"
cd "\$PROJECT" || exit 1

# 已經在跑就不要再開一個
if pgrep -f "\$PROJECT/src/main.py" >/dev/null 2>&1; then
  exit 0
fi

# Ollama 沒起來就先起來。開機登入當下系統很忙，固定 sleep 3 秒不夠，
# 改成輪詢等它真的回應（最多 30 秒）。
if ! curl -sf http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
  for O in /opt/homebrew/bin/ollama /usr/local/bin/ollama "\$(command -v ollama)"; do
    if [ -x "\$O" ]; then
      "\$O" serve >/tmp/ollama-serve.log 2>&1 &
      for _ in \$(seq 1 60); do
        curl -sf http://127.0.0.1:11434/api/version >/dev/null 2>&1 && break
        sleep 0.5
      done
      break
    fi
  done
fi

# 這裡千萬不要用 exec。用了的話 python 會直接取代掉這支腳本，
# 而 python 執行檔在 bundle 外面，macOS 會認定這個 app 的身分對不起來，
# 結果就是程序活著、選單列卻生不出圖示。
# 讓腳本留著當 bundle 的主程序，python 當子程序跑，狀態列才正常。
"\$PROJECT/.venv/bin/python" "\$PROJECT/src/main.py" \\
    >>/tmp/screen-translate.log 2>&1 &
wait \$!
LAUNCH
chmod +x "$APP/Contents/MacOS/screen-translate"

# 清掉隔離屬性，免得被當成未知來源
xattr -cr "$APP" 2>/dev/null || true
touch "$APP"

mkdir -p "$DEST"
rm -rf "$DEST/螢幕翻譯.app"
cp -R "$APP" "$DEST/"
echo
echo "✅ 完成：$DEST/螢幕翻譯.app"
echo "   用 Spotlight（⌘空白）打「螢幕翻譯」就能開，或到那個資料夾雙擊。"
