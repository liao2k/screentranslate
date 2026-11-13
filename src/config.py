"""設定載入：讀取專案根目錄的 config.json，缺少的欄位用預設值補齊。"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"

DEFAULTS = {
    "target_lang": "繁體中文",
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_model": "qwen2.5:1.5b-instruct",
    "ollama_timeout": 60,
    "keep_alive": "10m",
    "ocr_engine": "rapidocr",
    "vision_langs": [],
    "vision_auto_language": True,
    "min_confidence": 0.5,
    "ocr_timeout": 20,            # Vision 超過這麼多秒沒回應就放棄，免得背景工作永遠卡住
    "hotkey": "<ctrl>+<cmd>+t",
    "font_family": "PingFang TC",
    "min_font_size": 9,
    "max_font_size": 48,
    "max_workers": 4,
    "block_merge_gap": 0.8,
    "never_translate": [],
    "glossary": {},
    "hover_translate": False,
    "hover_dwell_ms": 600,
    "hover_capture": [420, 90],
    "hover_all_senses": True,
    "hover_max_senses": 0,
    "rewrite_model": "",          # 選單列 AI 校稿／修飾用的模型，空白＝沿用 ollama_model
    "rewrite_timeout": 120,
    "debug_save_dir": "",
}


class Config(dict):
    """dict 再加上屬性存取，方便 cfg.target_lang 這樣用。"""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def load(path: Path | None = None) -> Config:
    cfg = dict(DEFAULTS)
    path = path or CONFIG_PATH
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    return Config(cfg)
