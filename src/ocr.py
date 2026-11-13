"""文字辨識：兩種後端可選。

rapidocr —— 開源 PP-OCRv4 模型（ONNXRuntime，離線、跨平台，中英最佳）
vision   —— macOS 內建 Vision framework（不需下載模型，日韓文較強）

兩者都回傳同樣的 TextLine 清單，座標為圖片像素。
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
import re
from statistics import median
import threading

import numpy as np
from PIL import Image

CJK_CHAR = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")

# 游標取詞的切詞單位：英文一個單字，中日韓連續一段算一個（沒有空白可切）
WORD_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9'\u2019\-]*"
                        r"|[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]+")


class OCRTimeout(RuntimeError):
    """OCR 等太久沒回應。"""


# 給使用者看的：卡住之後同一個程序裡通常就一直卡，只有重開能救
STALL_HINT = "文字辨識沒有回應\n請從選單列「譯」→「離開」，再重新開啟螢幕翻譯"


def utf16_range(text: str, start: int, end: int) -> tuple[int, int]:
    """Python 的字元索引換成 NSRange 要的 UTF-16 索引。

    一般文字兩者一樣，但行內有 emoji（介面上很常見）時會整個位移，
    框就會取到隔壁的字。
    """
    loc = len(text[:start].encode("utf-16-le")) // 2
    length = len(text[start:end].encode("utf-16-le")) // 2
    return loc, length


@dataclass
class TextLine:
    text: str
    conf: float
    x0: float
    y0: float
    x1: float
    y1: float
    extra: dict = field(default_factory=dict)

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class Word:
    """單字層級的辨識結果，游標取詞用。"""
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    line: str = ""      # 所在的整行，翻譯時當上下文

    def contains(self, x: float, y: float, pad: float = 2.0) -> bool:
        return (self.x0 - pad <= x <= self.x1 + pad
                and self.y0 - pad <= y <= self.y1 + pad)


class RapidOCREngine:
    name = "rapidocr"
    wants_autoscale = True      # 小字會吞英文空白，需要放大重跑

    def __init__(self, **_):
        from rapidocr_onnxruntime import RapidOCR  # 延遲匯入，啟動較快
        self._engine = RapidOCR()

    def run(self, image: Image.Image) -> list[TextLine]:
        arr = np.asarray(image)
        result, _ = self._engine(arr)
        lines: list[TextLine] = []
        for box, text, score in result or []:
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
            lines.append(TextLine(text=text.strip(), conf=float(score),
                                  x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys)))
        return lines


class VisionEngine:
    name = "vision"
    # 原生字級就讀得準；放大反而讓偵測框右偏、削掉每行開頭幾個字元
    wants_autoscale = False

    def __init__(self, langs: list[str] | None = None, auto_language: bool = True,
                 timeout: float = 20.0, **_):
        import Vision  # noqa: F401  確認 pyobjc 有裝
        # 注意：setRecognitionLanguages_ 吃的是「優先順序」不是「集合」。
        # 給 ["en-US","ja-JP"] 去讀日文畫面會回傳 0 行——它認定是英文就找不到東西。
        # 所以預設走自動偵測，langs 留空；真的要鎖語言才填，且第一個就是主語言。
        self.langs = list(langs or [])
        self.auto_language = auto_language
        self.timeout = float(timeout)

    def _perform(self, handler, request):
        """performRequests 加上逾時。

        常駐好幾天後（通常是睡眠喚醒之後），Vision 底層的文字辨識偶爾會卡在
        semaphore 上永遠不回來。呼叫端是框選／取詞的背景執行緒，它一直不結束，
        App 就以為「上一次還在跑」而什麼都不做——兩個功能看起來同時失靈。
        所以放到另一條執行緒去跑，等不到就取消並丟 OCRTimeout，
        卡住的那條留著自生自滅，至少呼叫端能結束、使用者看得到原因。
        """
        import objc

        box: dict = {}
        done = threading.Event()

        def work():
            try:
                with objc.autorelease_pool():
                    box["result"] = handler.performRequests_error_([request], None)
            except Exception as exc:          # noqa: BLE001
                box["error"] = exc
            finally:
                done.set()

        threading.Thread(target=work, name="vision-ocr", daemon=True).start()
        if not done.wait(self.timeout):
            request.cancel()
            raise OCRTimeout(f"文字辨識 {self.timeout:g} 秒沒有回應（macOS Vision 卡住了）")
        if "error" in box:
            raise box["error"]
        return box["result"]

    def _recognize(self, image: Image.Image):
        """跑一次 Vision，回傳 (observations, 寬, 高)。run / run_words 共用。"""
        import Vision
        from Foundation import NSData

        buf = io.BytesIO()
        image.save(buf, format="PNG")
        data = NSData.dataWithBytes_length_(buf.getvalue(), len(buf.getvalue()))

        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(0)          # 0 = accurate
        request.setUsesLanguageCorrection_(True)
        if self.auto_language and hasattr(request, "setAutomaticallyDetectsLanguage_"):
            request.setAutomaticallyDetectsLanguage_(True)
        if self.langs:
            request.setRecognitionLanguages_(self.langs)

        handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
        ok, err = self._perform(handler, request)
        if not ok:
            raise RuntimeError(f"Vision OCR 失敗：{err}")
        return (request.results() or []), image.width, image.height

    @staticmethod
    def _to_pixels(bb, W: int, H: int) -> tuple[float, float, float, float]:
        """Vision 的正規化座標（原點在左下）換成圖片像素（原點在左上）。"""
        x0 = bb.origin.x * W
        x1 = (bb.origin.x + bb.size.width) * W
        y1 = (1 - bb.origin.y) * H
        y0 = (1 - bb.origin.y - bb.size.height) * H
        return x0, y0, x1, y1

    def run(self, image: Image.Image) -> list[TextLine]:
        results, W, H = self._recognize(image)
        lines: list[TextLine] = []
        for obs in results:
            cand = obs.topCandidates_(1)
            if not cand:
                continue
            text = cand[0].string().strip()
            if not text:
                continue
            x0, y0, x1, y1 = self._to_pixels(obs.boundingBox(), W, H)
            lines.append(TextLine(text=text, conf=float(cand[0].confidence()),
                                  x0=x0, y0=y0, x1=x1, y1=y1))
        lines.sort(key=lambda l: (l.y0, l.x0))
        return lines

    def run_words(self, image: Image.Image) -> list[Word]:
        """逐詞切出座標。

        中英混排的一行（「…同一份表會當成提示塞進 prompt，所以」）如果整行當成
        一個單位，游標明明停在 prompt 上、查到的卻是一整行。所以逐個 token 取框：
        英文照單字切，中日韓沒有空白可切，連續一段算一個。
        """
        from Foundation import NSRange

        results, W, H = self._recognize(image)
        words: list[Word] = []
        for obs in results:
            cand = obs.topCandidates_(1)
            if not cand:
                continue
            cand = cand[0]
            line = cand.string()
            if not line.strip():
                continue

            before = len(words)
            for m in WORD_TOKEN.finditer(line):
                loc, length = utf16_range(line, m.start(), m.end())
                box, _ = cand.boundingBoxForRange_error_(NSRange(loc, length), None)
                if box is None:
                    continue
                x0, y0, x1, y1 = self._to_pixels(box.boundingBox(), W, H)
                words.append(Word(m.group(), x0, y0, x1, y1, line))
            if len(words) == before:         # 一個框都取不到就退回整行，總比查不到好
                x0, y0, x1, y1 = self._to_pixels(obs.boundingBox(), W, H)
                words.append(Word(line.strip(), x0, y0, x1, y1, line))
        return words


# RapidOCR 偵測階段會把最長邊壓到 2000px，放大超過這個值等於白做
MAX_SIDE = 2000


def run_with_autoscale(engine, image: Image.Image, target_h: float = 60.0,
                       max_factor: float = 3.0) -> list[TextLine]:
    """字太小就放大重跑一次。

    介面文字通常 13–15pt，Retina 截下來墨跡也才 20 出頭像素，
    辨識器把它拉到 48px 高才送進模型，糊掉之後英文空白就會不見
    （EmptyTrash、CheckforUpdates）。先跑一次量出實際行高，
    不夠高就等比放大重跑，座標再換算回原圖像素，下游完全無感。
    """
    lines = engine.run(image)
    if not lines or not getattr(engine, "wants_autoscale", True):
        return lines

    med = median(l.h for l in lines)
    if med >= target_h:
        return lines

    factor = min(max_factor, target_h / med,
                 MAX_SIDE / max(image.width, image.height))
    if factor < 1.25:
        return lines

    big = image.resize((round(image.width * factor), round(image.height * factor)),
                       Image.Resampling.LANCZOS)
    rescanned = engine.run(big)
    if len(rescanned) < len(lines):
        return lines                     # 放大後反而辨識到更少，保留原結果

    for l in rescanned:
        l.x0 /= factor
        l.y0 /= factor
        l.x1 /= factor
        l.y1 /= factor
    return rescanned


def build(cfg) -> "RapidOCREngine | VisionEngine":
    engine = (cfg.get("ocr_engine") or "rapidocr").lower()
    if engine == "vision":
        return VisionEngine(langs=cfg.get("vision_langs"),
                            auto_language=cfg.get("vision_auto_language", True),
                            timeout=cfg.get("ocr_timeout", 20))
    return RapidOCREngine()
