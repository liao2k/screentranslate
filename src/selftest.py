"""不碰螢幕的自我測試：用一張合成圖跑完 OCR → 分段 → 翻譯 → 取色。

    python src/selftest.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image, ImageDraw, ImageFont

import config
import group as grouping
import ocr as ocr_mod
from translate import Translator

SAMPLE = [
    "Save changes before closing?",
    "Your document has unsaved edits that will",
    "be lost if you continue.",
    "",
    "Network connection timed out.",
]


def make_image() -> Image.Image:
    img = Image.new("RGB", (620, 260), (248, 248, 250))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 22)
    except OSError:
        font = ImageFont.load_default()
    y = 28
    for line in SAMPLE:
        draw.text((30, y), line, fill=(28, 28, 32), font=font)
        y += 40
    return img


def main() -> int:
    cfg = config.load()
    img = make_image()
    out_dir = Path(cfg.get("debug_save_dir") or "/tmp")
    img.save(out_dir / "selftest-input.png")
    print(f"測試圖：{out_dir / 'selftest-input.png'}")

    print(f"\n[OCR] 引擎 = {cfg['ocr_engine']}")
    t0 = time.time()
    engine = ocr_mod.build(cfg)
    print(f"  載入 {time.time() - t0:.1f}s")
    t0 = time.time()
    lines = engine.run(img)
    print(f"  辨識 {time.time() - t0:.1f}s，共 {len(lines)} 行")
    for l in lines:
        print(f"    ({l.x0:4.0f},{l.y0:4.0f})-({l.x1:4.0f},{l.y1:4.0f}) "
              f"{l.conf:.2f}  {l.text}")

    blocks = grouping.group(lines, cfg["min_confidence"], cfg["block_merge_gap"])
    print(f"\n[分段] {len(lines)} 行 → {len(blocks)} 段")
    for b in blocks:
        print(f"    {b.text}")

    print(f"\n[翻譯] {cfg['ollama_model']} → {cfg['target_lang']}")
    tr = Translator(cfg)
    ok, msg = tr.health()
    if not ok:
        print(f"  ✗ {msg}")
        return 1
    t0 = time.time()
    outs = tr.translate_many([b.text for b in blocks])
    print(f"  {time.time() - t0:.1f}s（{len(blocks)} 段平行）")
    for b, o in zip(blocks, outs):
        print(f"    {b.text}\n      → {o}")

    from overlay import sample_colors
    print("\n[取色]")
    for b in blocks:
        bg, fg = sample_colors(img, (b.x0, b.y0, b.x1, b.y1))
        print(f"    底色 {bg}  字色 {fg}   ← {b.text[:30]}")

    print("\n✅ 全部通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
