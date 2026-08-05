"""GUI 定位模型推論服務（host 直跑，由 backend/engine/vlm_grounding.py 啟動）。

跑在本外掛專屬的 .venv 裡（有 torch + CUDA），與主程式完全隔離。
以檔案交換：讀 _req.json → 推論 → 寫 _resp.json。模型只載入一次。

只回座標，不決定要做什麼動作 —— 動作種類由 YAML 指定。

用法：python server.py <交換目錄> <模型目錄>
"""
import glob
import json
import os
import re
import sys
import time

import torch
from PIL import Image
from transformers import AutoProcessor, Qwen3VLForConditionalGeneration

IO = sys.argv[1] if len(sys.argv) > 1 else "_io"
MODEL_DIR = sys.argv[2] if len(sys.argv) > 2 else "models"
REQ, RESP, READY = (os.path.join(IO, x) for x in ("_req.json", "_resp.json", "_ready"))

# 只列定位會用到的動作。動作種類由 YAML 決定，不讓模型自己選 ——
# 2026-08-03 實測它選動作會錯（該點擊卻回 scroll、多步任務裡 Ctrl+B 開了又關），
# 但給座標 33 個目標全中，所以只用它最強的那一項。
ACTION_SPACE = """click(start_box='<|box_start|>(x1,y1)<|box_end|>')
stop(reason='') # If the item can not be found in the image, give the reason"""

TEMPLATE = """You are a GUI agent. You are given a task and your action history, with screenshots. You need to perform the next action to complete the task.

## Output Format
<think>思考过程</think>
<action_desp>动作描述</action_desp>
<action>具体动作</action>

## Action Space
{space}

## Note
- Use Chinese in `<think>` part.
- 你只需要**定位**目標元素並回傳 click 座標，不要選擇其他動作。

## 最重要的一條
畫面上**不一定有**使用者說的那個元素。
先在 `<think>` 裡逐項確認：這個元素真的出現在圖片裡嗎？它的文字/圖示/顏色**完全符合**描述嗎？
- 只要有任何一項對不上（找不到、只是相似、需要先捲動或展開選單才會出現）
  → 一律回 `stop(reason='說明哪裡對不上')`
- **寧可回 stop，也不要猜一個座標。** 猜錯會讓自動化點到別的東西、造成實際損害。
- 只有你能在圖片中明確指出該元素時，才回 click 座標。

## User Instruction
點擊：{prompt}"""


def _fail(msg):
    json.dump({"ok": False, "reason": msg}, open(RESP, "w", encoding="utf-8"),
              ensure_ascii=False)


def _resolve_model(d):
    """模型目錄可能是權重本身，也可能包一層資料夾。"""
    if glob.glob(os.path.join(d, "*.safetensors")):
        return d
    subs = sorted(glob.glob(os.path.join(d, "*")))
    for s in subs:
        if os.path.isdir(s) and glob.glob(os.path.join(s, "*.safetensors")):
            return s
    return d


MODEL = _resolve_model(MODEL_DIR)
os.makedirs(IO, exist_ok=True)

if not glob.glob(os.path.join(MODEL, "*.safetensors")):
    print("找不到模型權重:", MODEL, flush=True)
    sys.exit(2)

if not torch.cuda.is_available():
    print("CUDA 不可用 —— 這個外掛需要 NVIDIA GPU", flush=True)
    sys.exit(3)

# ── 依顯卡記憶體自動決定精度 ────────────────────────────────────────
# 2026-08-06 於 Windows host 實測（11 個定位目標）：
#   fp16 峰值 9.65GB 誤差 3.0px 5.1s
#   int4 峰值 3.68GB 誤差 3.6px 6.0s
#   int8 在容器實測比 int4 更肥又慢 6 倍，不提供
# 量化幾乎不傷精度，所以記憶體不夠時降精度是划算的。
_total = torch.cuda.get_device_properties(0).total_memory / 1e9
_free = torch.cuda.mem_get_info()[0] / 1e9
_MODE = os.environ.get("ATLASLITE_GROUNDING_PRECISION", "auto").lower()
if _MODE == "auto":
    # 用「目前可用」而非「總量」—— 使用者可能同時在跑別的 GPU 程式
    _MODE = "fp16" if _free >= 11.0 else ("int4" if _free >= 4.5 else "none")

print("GPU {}：總 {:.1f}GB / 可用 {:.1f}GB → 精度 {}".format(
    torch.cuda.get_device_name(0), _total, _free, _MODE), flush=True)
if _MODE == "none":
    print("可用顯卡記憶體不足（int4 至少需要約 4.5GB）", flush=True)
    sys.exit(4)

_kw = {"device_map": "cuda:0"}
if _MODE == "int4":
    from transformers import BitsAndBytesConfig
    _kw["quantization_config"] = BitsAndBytesConfig(
        load_in_4bit=True, bnb_4bit_compute_dtype=torch.float16,
        bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True)
else:
    _kw["dtype"] = torch.float16

print("載入模型…", MODEL, flush=True)
model = Qwen3VLForConditionalGeneration.from_pretrained(MODEL, **_kw)
model.eval()
proc = AutoProcessor.from_pretrained(MODEL)
for f in (REQ, RESP):
    try:
        os.remove(f)
    except OSError:
        pass
open(os.path.join(IO, "_precision"), "w").write(_MODE)
open(READY, "w").write("1")
print("就緒", flush=True)


def _tag(name, text):
    m = re.search(r"<{0}>(.*?)</{0}>".format(name), text, re.DOTALL)
    return m.group(1).strip() if m else ""


while True:
    if not os.path.exists(REQ):
        time.sleep(0.1)
        continue
    try:
        time.sleep(0.1)
        req = json.load(open(REQ, encoding="utf-8"))
        os.remove(REQ)
    except Exception:
        time.sleep(0.2)
        continue

    if req.get("cmd") == "quit":
        print("結束", flush=True)
        break

    try:
        img = Image.open(req["image"]).convert("RGB")
    except Exception as e:
        _fail(f"讀不到截圖：{e}")
        continue

    W, H = img.size
    # 模型輸入解析度是 1280 寬；座標是正規化的，縮放不影響精度
    small = img.resize((1280, max(1, int(H * 1280 / W))), Image.LANCZOS)
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": [
            {"type": "image", "image": small},
            {"type": "text", "text": TEMPLATE.format(
                space=ACTION_SPACE, prompt=req.get("prompt", ""))},
        ]},
    ]
    try:
        ti = proc.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = proc(text=[ti], images=[small], padding=True,
                      return_tensors="pt").to(model.device)
        t0 = time.time()
        with torch.no_grad():
            out = model.generate(**inputs, max_new_tokens=384, do_sample=False)
        el = time.time() - t0
        raw = proc.batch_decode(out[:, inputs.input_ids.shape[1]:],
                                skip_special_tokens=True)[0]
    except Exception as e:
        _fail(f"推論失敗：{e.__class__.__name__}: {e}")
        continue

    action = _tag("action", raw) or raw.strip()
    if action.startswith("stop"):
        rm = re.search(r"reason\s*=\s*'(.*?)'", action)
        _fail("模型回報找不到目標：" + (rm.group(1) if rm else action[:80]))
        continue
    m = re.search(r"\((\d+)\s*,\s*(\d+)\)", action)
    if not m:
        _fail(f"模型未給座標：{action[:80]}")
        continue

    json.dump({"ok": True, "nx": int(m.group(1)), "ny": int(m.group(2)),
               "desp": _tag("action_desp", raw), "elapsed": round(el, 2)},
              open(RESP, "w", encoding="utf-8"), ensure_ascii=False)
    print("  → ({},{}) {:.1f}s".format(m.group(1), m.group(2), el), flush=True)
