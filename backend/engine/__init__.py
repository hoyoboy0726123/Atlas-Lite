"""Atlas-Lite 執行引擎。

⚠ 這裡**刻意保持空的**，不要加 `from .runner import run_pipeline` 這類轉出。

Atlas 的 pipeline/__init__.py 就是這樣做的，結果是任何一句
`from pipeline.ocr import find_text_on_screen` 都會連帶 import runner →
telegram → langchain → sqlalchemy，把整包 LLM 相依拖進來。錄製工具、
單元測試、只想用 OCR 的小腳本全都被迫等那幾秒的 import。

要用什麼就直接指名：`from engine.runner import run_pipeline`。
"""
