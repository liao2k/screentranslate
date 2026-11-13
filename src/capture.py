"""螢幕擷取：呼叫 macOS 內建的 screencapture，不需要額外套件。

座標一律使用「點」(points, 也就是 Qt / macOS 的邏輯座標)，
而 screencapture 產生的圖在 Retina 螢幕上是 2 倍像素，
所以回傳時一併給出 scale，OCR 的像素座標除以 scale 就會回到點座標。
"""
from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image


@dataclass
class Shot:
    image: Image.Image   # 擷取到的畫面 (像素)
    x: int               # 擷取區域左上角 (點)
    y: int
    w: int               # 擷取區域寬高 (點)
    h: int
    scale: float         # 像素 / 點

    def to_screen(self, px: float, py: float) -> tuple[float, float]:
        """把圖片內的像素座標換算成螢幕上的點座標。"""
        return self.x + px / self.scale, self.y + py / self.scale


def grab(x: int, y: int, w: int, h: int) -> Shot:
    """擷取螢幕上指定矩形（點座標）。"""
    if w <= 0 or h <= 0:
        raise ValueError("擷取區域必須大於 0")

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "shot.png"
        cmd = ["screencapture", "-x", "-o", "-t", "png",
               "-R", f"{x},{y},{w},{h}", str(out)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not out.exists():
            raise RuntimeError(
                "screencapture 失敗（通常是沒給「螢幕錄製」權限）：\n"
                + (proc.stderr or "").strip()
            )
        img = Image.open(out).convert("RGB")
        img.load()

    scale = img.width / w if w else 1.0

    # 非 Retina 螢幕上字太小，OCR 會把英文的空白吃掉（Savechangesbefore…）。
    # 統一放大到 2 倍像素密度再辨識，scale 一起帶上去，座標換算不受影響。
    if scale < 2.0:
        factor = 2.0 / scale
        img = img.resize((round(img.width * factor), round(img.height * factor)),
                         Image.Resampling.LANCZOS)
        scale = 2.0

    return Shot(image=img, x=x, y=y, w=w, h=h, scale=scale)
