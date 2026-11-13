"""把 OCR 出來的「行」合併成「段落方塊」。

一行一行送去翻譯會讓語意斷掉（尤其英文換行），
所以先把版面上相鄰、對齊、行高相近的行併成一段再翻。

判斷用的是「行首到行首」的間距(pitch)，而不是兩個 OCR 方塊之間的空隙：
方塊只框到墨跡，一行有沒有 g/y 這種下伸部、有沒有大寫，高度就差 20%，
空隙因此非常不穩。而門檻也不寫死倍數，改成先統計這塊版面自己的
行距中位數，再容許 1+tol 倍 —— 不管介面用的是緊排還是鬆排都能適應。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import median

from ocr import TextLine

CJK = re.compile(r"[　-鿿가-힯＀-￯]")

# OCR 常把行首的圖示認成字元：符號（• *）、單一字母（E Skills）、
# 或帶括號的短碼（q) Voice、4) Voice）。這些會被小模型當成內容照抄。
# 只清掉「幾乎不可能是真標籤開頭」的樣式，像 AI Settings、3D View、
# A Brief History 這種要保住，所以兩字母不帶標點的一律不動。
_SYMBOLS = r"\u2022\u00b7\u2219\u25e6\u25aa\u25b8\u25b6\u25cf\u25cb\u25c6\u25a0\u25a1\u2605\u2606"
ICON_PREFIX = re.compile(
    r"^(?:"
    rf"[{_SYMBOLS}*+|/\\\-\u2013\u2014]+\s*"      # 符號
    r"|[A-Za-z0-9]{1,2}[)\].:]\s+"              # q)  4)  E.
    r"|(?![AaIi]\s)[A-Za-z0-9]\s+"              # 單一字母，但 A / I 是真的英文字
    r")+"
)


def strip_icon_noise(text: str) -> str:
    cleaned = ICON_PREFIX.sub("", text).strip()
    return cleaned if len(cleaned) >= 3 else text.strip()


@dataclass
class Block:
    lines: list[TextLine] = field(default_factory=list)

    @property
    def x0(self) -> float:
        return min(l.x0 for l in self.lines)

    @property
    def y0(self) -> float:
        return min(l.y0 for l in self.lines)

    @property
    def x1(self) -> float:
        return max(l.x1 for l in self.lines)

    @property
    def y1(self) -> float:
        return max(l.y1 for l in self.lines)

    @property
    def line_height(self) -> float:
        return median(l.h for l in self.lines)

    @property
    def text(self) -> str:
        out = ""
        for i, line in enumerate(self.lines):
            t = strip_icon_noise(line.text) if i == 0 else line.text.strip()
            if i == 0:
                out = t
            elif out.endswith("-"):          # 英文斷字：直接接起來
                out = out[:-1] + t
            elif CJK.search(out[-1:]) or CJK.search(t[:1]):
                out += t                      # 中日韓不加空白
            else:
                out += " " + t
        return out


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    """兩個區間重疊長度 / 較短者長度。"""
    inter = min(a1, b1) - max(a0, b0)
    shorter = min(a1 - a0, b1 - b0)
    return inter / shorter if shorter > 0 else 0.0


def _text_height(lines: list[TextLine]) -> float:
    """代表性的「文字」高度。

    用 25 百分位而不是中位數：OCR 的偵測框常常把行首的圖示一起框進去
    （* Agent、MA Connected Apps），那些框明顯偏高，是離群值。
    取低百分位才拿得到純文字那幾行的真實高度。
    """
    hs = sorted(l.h for l in lines)
    return hs[max(0, round(0.25 * (len(hs) - 1)))]


def _typical_pitch(lines: list[TextLine], ref_h: float) -> float:
    """這塊版面「正常一行接一行」的距離。抓不到就退回 1.6 倍行高。"""
    pitches = []
    for prev, cur in zip(lines, lines[1:]):
        pitch = cur.y0 - prev.y0
        # 太小＝同一列的並排元素；太大＝空行或不同區塊，兩者都不算數
        if 0.5 * ref_h < pitch <= 3.0 * ref_h:
            pitches.append(pitch)
    return median(pitches) if pitches else 1.6 * ref_h


def group(lines: list[TextLine], min_conf: float = 0.5,
          tol: float = 0.35) -> list[Block]:
    kept = [l for l in lines if l.text.strip() and l.conf >= min_conf]
    if not kept:
        return []
    kept.sort(key=lambda l: (l.y0, l.x0))

    ref_h = _text_height(kept)
    # 兩道門檻都要過：
    #   一、不得超過這塊版面自己的行距常態（適應緊排／鬆排）
    #   二、不得超過 1.7 倍行高。實測：段落接續 1.00~1.56 倍，
    #       垂直選單每一項之間 1.94~2.06 倍，區塊之間 2.9 倍以上，
    #       1.7 剛好切在段落與選單中間。
    #       寧可把段落切成兩段（翻譯只是略差），也不要把整排選單黏成一段（會全毀）。
    max_pitch = min((1.0 + tol) * _typical_pitch(kept, ref_h), 1.7 * ref_h)

    blocks: list[Block] = []
    for line in kept:
        target = None
        for b in reversed(blocks):
            last = b.lines[-1]
            h = max(last.h, line.h, 1.0)

            pitch = line.y0 - last.y0
            if pitch < 0.5 * h:
                continue                                  # 同一列的並排元素，不是下一行
            if pitch > max_pitch:
                continue                                  # 行距超出這塊版面的常態，換段了
            # OCR 框到的高度抖動很大（同一個字體量出 30 和 51 都有），
            # 所以這裡放寬；標題與內文的分離交給上面的行距門檻負責。
            if not 0.45 <= (line.h / last.h if last.h else 1) <= 2.2:
                continue                                  # 字級真的差太多
            aligned = (_overlap(b.x0, b.x1, line.x0, line.x1) > 0.35
                       or abs(line.x0 - b.x0) < 0.8 * h)
            if aligned:
                target = b
                break
        if target is None:
            blocks.append(Block(lines=[line]))
        else:
            target.lines.append(line)

    blocks.sort(key=lambda b: (b.y0, b.x0))
    return blocks
