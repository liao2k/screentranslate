# Screen Translate

**English** · [繁體中文](README.md)

Drag a box around any part of the screen → local OCR reads the text → a small local model translates it → **the translation is pasted back exactly where the original text was**.
Everything runs offline. Nothing on your screen is ever sent to a cloud service.

A macOS menu-bar tool. The default target language is Traditional Chinese (Taiwan), and the menus are in Traditional Chinese, but the target language can be changed in `config.json`.

```
Screenshot (screencapture)
  → OCR (macOS Vision with automatic language detection; open-source RapidOCR optional)
  → Paragraphs (lines that wrap are merged into paragraphs before translating, so meaning isn't cut)
  → Filter (numbers, times, single letters and text already in the target language are kept as-is)
  → Translate (glossary hits are used directly / the rest goes to Ollama + qwen2.5:1.5b, 4 in parallel)
  → Guard (if it didn't come back in Chinese, ask once more, otherwise keep the original;
           Simplified → Traditional, mainland wording → Taiwanese wording)
  → Overlay (transparent PyQt6 window, background colour sampled from the original, auto font sizing)
```

## Installation

```bash
git clone https://github.com/liao2k/screentranslate.git
cd screentranslate
```

If you copy the project to another Mac later, **don't copy `.venv`, `build` or `螢幕翻譯.app`**: they contain absolute paths from the old machine and will break. Regenerate them on the new Mac. `src/`, `config.json`, `requirements.txt` and the `.sh` scripts are all you need.

### 1. Prerequisites

```bash
sudo xcodebuild -license accept     # skip if Xcode was never installed; required if Homebrew is blocked
brew install python@3.12 ollama
```

No Homebrew yet? Install it first: <https://brew.sh>

### 2. Set up the environment

```bash
./setup.sh
```

This creates `.venv`, installs the Python packages, starts Ollama and downloads the translation model (about 1 GB; a few minutes depending on your connection).

```bash
./run.sh --check      # all three checks must pass: screenshot / OCR / Ollama
```

### 3. Make a double-clickable app (optional)

```bash
./make_app.sh
```

This creates `~/Applications/螢幕翻譯.app`, which you can then open from Spotlight (⌘Space).
The script writes the project path into the launcher, so **run it again if you move the project folder**.

### 4. Launch at login (optional)

```bash
osascript -e 'tell application "System Events" to make login item at end \
  with properties {path:"'"$HOME"'/Applications/螢幕翻譯.app", hidden:false}'
```

To undo it, remove the item in System Settings → General → Login Items.

### 5. Grant permissions

macOS ties permissions to *which app is running*, so **running from Terminal and running the `.app` need separate permission**.

| Permission | What happens without it | Where |
|---|---|---|
| Screen Recording | Screenshots come back blank and no text is recognised | System Settings › Privacy & Security › Screen Recording |
| Accessibility | The global `⌃⌘T` shortcut doesn't work (the menu-bar icon still does); AI proofreading/polishing can't read the selected text | System Settings › Privacy & Security › Accessibility |

If the app isn't in the list yet, translate a region once and macOS will ask.
**After ticking the box, quit the app completely and reopen it**, or the change won't take effect.

```bash
open "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
```

## Usage

```bash
./run.sh --check     # check the environment first (screenshot, OCR, Ollama)
./run.sh             # stay running: a "譯" icon appears in the menu bar
./run.sh --once      # translate once and quit
./run.sh --lang English --model qwen2.5:3b-instruct   # override settings for this run
```

While it's running there are three ways to use it.

### 1. Translate a region

- `⌃⌘T` (or menu-bar icon → "翻譯選取區域" / Translate selected region) → drag a box around what you want translated
- The translation is drawn directly over the original text
- **Hold the mouse button** to peek at the original, **Esc / double-click** to close
- Menu bar → "重新翻譯上次區域" (Re-translate last region) runs the same area again, e.g. when a subtitle changes

### 2. Hover to look up a word

Tick menu bar → "游標取詞翻譯" (Hover translate). Rest the pointer on any word for about 0.6 s and a bubble appears right below it; move away and it disappears. The bubble has three parts:

```
close                                  ← original word
關閉                                   ← meaning in this sentence (the model answers using the context)
────────────────────────────────
vt. ①關閉；蓋上；合上 ②關（商店等）…    ← every possible meaning (system dictionary, grouped by part of speech)
vi. ①關閉；蓋上；合上 ②(商店等) 關門…
a.  ①近的，接近的 ②(關係) 密切的…
```

"Every possible meaning" comes from the English–Chinese dictionary built into macOS (譯典通英漢雙向字典), not from the model: asking a 1.5B model to list every sense makes it invent things (in testing it answered "mouse" for `windows`). The dictionary is offline and takes about 2 ms per lookup. Inflected forms are resolved automatically (`Instances` → instance; `saw` lists the tool first, then the past tense of see), and homographs are all listed (`close` as both "shut" and "near").
When there are too many senses to fit on screen (`set`, `run`), the end is collapsed into "…" so the bubble never covers the original text. If it's too long for you, limit senses per part of speech with `hover_max_senses`, or turn off `hover_all_senses` to go back to a small two-line bubble.

That dictionary only goes from English to Traditional Chinese, so when `target_lang` isn't Traditional Chinese, or when you hover over Japanese or Korean, the bubble shows only the top two lines. `./run.sh --check` tells you whether the dictionary is installed; if not, open the Dictionary app › Settings and tick 譯典通英漢雙向字典 to download it.

Hover lookup and region translation are mutually exclusive: turning on hover lookup clears the previous region translation, and starting a region selection or re-translating the last region turns hover lookup off. You can turn it back on from the menu bar at any time.

Only a 420×90-point area around the pointer is captured, so it never runs OCR on the whole screen and uses very little CPU.
The whole line is sent along as context, so the "close" in `close this window` isn't translated as "close the door".

If neither the model nor the dictionary has an answer, no bubble appears. Small models tend to return plural tech nouns such as `Instances` untranslated in English; rather than show a fake "translation" that is still English, that line is hidden (dictionary senses are still listed if the dictionary has the word). For words that are often wrong, add them to `glossary`.

Requires `ocr_engine` set to `vision` (word-level coordinates are needed and RapidOCR doesn't provide them).
To have it on at startup, set `hover_translate` to `true` in `config.json`.

### 3. AI proofreading / polishing of your own text

Select text in any app → click the "譯" menu-bar icon →

| Option | What it does |
|---|---|
| AI 校稿選取的文字 (Proofread selection) | Fixes only typos, wrong Zhuyin/IME character choices (會義→會議) and punctuation, keeping your wording |
| AI 修飾選取的文字 (Polish selection) | Fixes typos and makes sentences smooth and clear, keeping the meaning and roughly the same length |

About a second later a window pops up with the original on top and the result below; you can edit the result first.
**Replace** (⌘↩) swaps out the selected text, **Copy only** puts it on the clipboard without touching the original (use this for web pages and other places that can't be edited), **Cancel** (Esc) does nothing.

- It works by pressing ⌘C for you to grab the selection and ⌘V to paste it back, so it needs the Accessibility permission. Whatever was on your clipboard is restored afterwards (except with "Copy only").
- It uses `rewrite_model` (3B by default): proofreading means checking every character, and 1.5B misses more. The output goes through the same Simplified→Traditional and Taiwanese-wording guard.
- 3B occasionally over-corrects. For example, when polishing it guessed 「近況」 for 「近度」 instead of 「進度」. That's why there's a confirmation window: take a look before replacing.

## Settings: `config.json`

| Key | Description |
|---|---|
| `target_lang` | Target language, written as the language name, e.g. `繁體中文`, `English`, `日本語` |
| `ollama_model` | Translation model. Default `qwen2.5:1.5b-instruct` (fast); for better accuracy use `qwen2.5:3b-instruct` or `gemma3:4b` |
| `ocr_engine` | `vision` (default; built into macOS, free, offline, most accurate and fastest for English/Japanese/Korean) or `rapidocr` (open-source ONNX models, needs a download, strong on Chinese) |
| `vision_auto_language` | Let Vision detect the language automatically. Default `true`; usually no need to change |
| `vision_langs` | Lock recognition languages; empty by default. **This is a priority order, not a set**: with `["en-US","ja-JP"]` a Japanese screen recognises nothing. The first entry must be the screen's main language |
| `min_confidence` | Recognition results below this confidence are dropped. Default 0.5 |
| `ocr_timeout` | Maximum seconds to wait for Vision text recognition. Default 20. macOS occasionally lets it hang; on timeout you'll be asked to restart the app |
| `never_translate` | Strings that are never sent to the model and kept as-is (product names, proper nouns) |
| `glossary` | Glossary, e.g. `{"instance": "實例"}`. A hit is used directly without asking the model (zero latency, never wrong); for region translation it's also added to the prompt as a hint. Keys are case-insensitive and singular is enough (`instance` also covers `Instances`). **The glossary is written for `target_lang`**, so it's switched off automatically when you change language with `--lang` |
| `block_merge_gap` | Tolerance for paragraph grouping. The app measures the median line spacing of the region and starts a new paragraph when a gap exceeds median × (1 + this value). Increase it for layouts with irregular spacing |
| `font_family` | Font used for the translation |
| `max_workers` | How many paragraphs are translated at the same time |
| `hotkey` | pynput format, default `<ctrl>+<cmd>+t` (⌃⌘T). Modifiers go in angle brackets: `<ctrl>` ⌃, `<cmd>` ⌘, `<shift>` ⇧, `<alt>` ⌥; letters are lower case. **⌥ Option is used on macOS to type special characters (⌥T → †), so it's not recommended in a hotkey** |
| `hover_translate` | Turn on hover lookup at startup. Default `false` (it can be toggled from the menu bar any time) |
| `hover_dwell_ms` | How long the pointer must rest before a lookup. Default 600 ms |
| `hover_capture` | Capture size for hover lookup, in points. Default `[420, 90]`. Larger catches longer words but OCR gets a little slower |
| `hover_all_senses` | Whether the hover bubble lists every dictionary sense. Default `true`; set `false` to show only the model's answer |
| `hover_max_senses` | Maximum senses per part of speech. Default `0` (all). With `3`, the rest collapses into "…" |
| `rewrite_model` | Model for AI proofreading/polishing; empty means use `ollama_model`. Default `qwen2.5:3b-instruct` |
| `rewrite_timeout` | Maximum seconds to wait for proofreading/polishing. Default 120 |
| `debug_save_dir` | Set a path to save every screenshot for debugging |

## Changing the model

```bash
ollama pull qwen2.5:3b-instruct     # more accurate
ollama pull gemma3:1b               # smaller and faster
ollama list                         # see what's installed
```

Then change `ollama_model` in `config.json`; no code changes needed.

## Project structure

| File | Responsibility |
|---|---|
| `src/main.py` | Entry point: menu-bar icon, hotkey, pipeline orchestration, `--check` |
| `src/capture.py` | Calls `screencapture` and handles Retina point/pixel conversion |
| `src/ocr.py` | Two OCR back ends with a common `TextLine` output; re-runs small text at a larger scale (RapidOCR only) |
| `src/group.py` | Merges lines into paragraph blocks |
| `src/translate.py` | Ollama client: parallel translation + cache, glossary, output guard (retry / Simplified→Traditional / Taiwanese wording) |
| `src/overlay.py` | Selection UI, result overlay, hover bubble, status hints, colour sampling, AI proofreading confirmation window |
| `src/statusbar.py` | Menu-bar icon (native NSStatusItem; Qt's QSystemTrayIcon crashes on click under macOS 27) |
| `src/hover.py` | Pointer-rest detection, local capture, word hit-testing, lookup |
| `src/dictionary.py` | Looks words up in the built-in macOS English–Chinese dictionary and lists every sense by part of speech (for hover lookup) |
| `src/rewrite.py` | AI proofreading/polishing: sends text to Ollama, cleans the output, Traditional Chinese guard |
| `src/selection.py` | Simulates ⌘C / ⌘V to grab another app's selection and paste the result back, restoring the clipboard |
| `src/selftest.py` | End-to-end test that never touches the screen (synthetic image → OCR → paragraphs → translation → colour sampling) |

## Known limitations

- When the translation is much longer than the original (e.g. an English abbreviation → Chinese), the box grows downward a little; in extreme cases it may cover the next line.
- The overlay is a separate window, so it doesn't move when the content underneath scrolls. Press the shortcut again after scrolling.
- Spaces and full-screen apps are supported: the selection window uses `canJoinAllSpaces`, so it appears on whichever desktop you're on instead of pulling you back to the app's original one. The app is a menu-bar (accessory) tool, so it has no Dock icon.
- Word units for hover lookup: one word for English; for Chinese, Japanese and Korean, the whole run between two punctuation marks (there are no spaces to split on, and real word segmentation would need an extra tokenizer). Mixed Chinese/English lines are handled separately, so hovering over `prompt` looks up "prompt", not the whole line.
- Hovering over a word that's already in the target language shows no bubble (there's nothing to look up); the same goes for numbers and single letters.
- "Every possible meaning" uses an undocumented DictionaryServices interface (the public one only returns the first entry). If a future macOS removes it, the bubble falls back to showing only the model's translation instead of breaking. The dictionary itself is fairly old: newer words such as `email` and `macOS` aren't included, and phrases (`close down`) aren't listed.
- RapidOCR's default models are weaker on Japanese kana and Korean, and at normal UI sizes (13–15 pt) they drop the spaces between English words (`EmptyTrash`). The app re-runs at a larger scale to compensate, but it's still not as good as Vision, which is why `vision` is the default.
- Small models occasionally produce mainland Chinese wording or Simplified characters. The prompt includes a Taiwanese wording list, and the output is converted Simplified→Traditional with the ICU built into macOS, with words like 「默認／軟件／網絡」 replaced outright. 「文件」「程序」「數據」「支持」 also have legitimate uses in Taiwan, so replacing them blindly would be wrong; those rely on the prompt alone. For better accuracy, use the 3B model.
- Obscure technical words that aren't in the glossary can be mistranslated by both 1.5B and 3B (in testing both translated `Library` as "a library building"). If a result isn't in Chinese the app asks once more, and keeps the original if both attempts fail. The real fix is to add that word to `glossary`.

## License

Released under the [MIT License](LICENSE). You're free to use, modify and distribute it; please keep the copyright and license notice.
