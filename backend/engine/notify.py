"""Telegram 通知與截圖。

從 Atlas 的 pipeline/runner.py 抽出來獨立成模組 —— runner 只管流程控制，
「怎麼把訊息推到手機上」是另一件事。

沒設 bot token / chat id 時所有函式都靜默跳過（只 log），不拋例外：
Telegram 是選用功能，沒設定不該讓工作流掛掉。
"""
import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from settings import get_telegram_credentials

log = logging.getLogger("atlas_lite")


# ── 按鈕鍵盤 ─────────────────────────────────────────────────────────

def decision_keyboard(run_id: str, has_prev: bool = True) -> InlineKeyboardMarkup:
    """步驟失敗時的決策鍵盤。

    截圖按鈕不分節點類型都給 —— 失敗時使用者可能人不在電腦前，
    腳本失敗跟桌面自動化失敗一樣都可能需要先看現場再決策。
    has_prev=False（current_step=0）時隱藏「重做上一步」。
    """
    rows = [[
        InlineKeyboardButton("🔄 重試", callback_data=f"pipe_retry:{run_id}"),
        InlineKeyboardButton("⏩ 跳過此步", callback_data=f"pipe_skip:{run_id}"),
    ]]
    if has_prev:
        rows.append([InlineKeyboardButton("↩ 重做上一步", callback_data=f"pipe_redo_prev:{run_id}")])
    rows.append([
        InlineKeyboardButton("📸 截圖", callback_data=f"pipe_screenshot:{run_id}"),
        InlineKeyboardButton("📋 查看 Log", callback_data=f"pipe_log:{run_id}"),
    ])
    rows.append([InlineKeyboardButton("🛑 中止", callback_data=f"pipe_abort:{run_id}")])
    return InlineKeyboardMarkup(rows)


def missing_dep_keyboard(run_id: str, packages: list[str]) -> InlineKeyboardMarkup:
    """缺套件時的決策鍵盤。每個套件一行「允許安裝」。

    ⚠ 安裝一定要經過這一關 —— 不問就裝等於讓工作流在使用者的機器上任意
    安裝 PyPI 套件。callback_data 有 64 byte 上限，所以最多列 5 個。
    """
    rows = [[InlineKeyboardButton(f"✅ 允許安裝 {pkg}",
                                  callback_data=f"pipe_install_dep:{run_id}:{pkg}")]
            for pkg in packages[:5]]
    if len(packages) > 1:
        rows.append([InlineKeyboardButton(f"✅ 全部安裝（{len(packages)} 個）",
                                          callback_data=f"pipe_install_all:{run_id}")])
    rows.append([
        InlineKeyboardButton("🛑 中止", callback_data=f"pipe_abort:{run_id}"),
        InlineKeyboardButton("📋 查看 Log", callback_data=f"pipe_log:{run_id}"),
    ])
    return InlineKeyboardMarkup(rows)


def confirm_keyboard(run_id: str, screenshot: bool = False) -> InlineKeyboardMarkup:
    """人工確認節點的按鈕。

    「📎 上一步輸出」永遠在 —— 跟 send_prev_output 的自動推送是兩回事：
    自動推是抵達節點當下推一份，按鈕是使用者隨時要重抓用的。
    """
    rows = [
        [InlineKeyboardButton("✅ 繼續執行", callback_data=f"pipe_continue:{run_id}"),
         InlineKeyboardButton("🛑 中止", callback_data=f"pipe_abort:{run_id}")],
        [InlineKeyboardButton("📎 上一步輸出", callback_data=f"pipe_prev_output:{run_id}"),
         InlineKeyboardButton("📂 任一步輸出", callback_data=f"pipe_select_step:{run_id}")],
        [InlineKeyboardButton("📋 查看 Log", callback_data=f"pipe_log:{run_id}")],
    ]
    if screenshot:
        rows[2].append(InlineKeyboardButton("📸 截圖", callback_data=f"pipe_screenshot:{run_id}"))
    return InlineKeyboardMarkup(rows)


# ── 送訊息 ───────────────────────────────────────────────────────────

def _credentials(chat_id: int) -> tuple[str, int]:
    """回 (token, chat_id)。任一為假值代表不該送。"""
    token, default_chat = get_telegram_credentials()
    if not chat_id:
        try:
            chat_id = int(default_chat) if default_chat else 0
        except ValueError:
            log.warning(f"[Telegram] chat_id 格式不正確：{default_chat!r}")
            chat_id = 0
    return token, chat_id


async def send(chat_id: int, text: str, reply_markup=None):
    """發一則訊息。錯誤只記錄，不拋出。"""
    token, chat_id = _credentials(chat_id)
    if not token or not chat_id:
        log.debug("[Telegram] 未設定 bot token / chat id，跳過通知")
        return
    try:
        # 用 async with 而不是手動 bot.close() —— 後者實際是呼叫 TG API 的 `close`
        # method，TG 文件警告它之後 10 分鐘必回 429。async with 走 shutdown()，
        # 只關 httpx 連線、不打 API。
        async with Bot(token=token) as bot:
            await bot.send_message(chat_id=chat_id, text=text,
                                   parse_mode="HTML", reply_markup=reply_markup)
    except Exception as e:
        log.error(f"[Telegram] 發送失敗：{e}")


async def send_long(chat_id: int, text: str, reply_markup=None):
    """發長訊息：超過 TG 單則上限就在換行處分段，不截斷。只有最後一段帶按鈕。

    呼叫端負責 HTML escape（因為用 parse_mode=HTML）。
    """
    CHUNK = 3500  # TG 上限 4096，留 HTML tag / 前綴餘裕
    if len(text) <= CHUNK:
        await send(chat_id, text, reply_markup)
        return
    parts, rest = [], text
    while rest:
        if len(rest) <= CHUNK:
            parts.append(rest)
            break
        cut = rest.rfind("\n", 0, CHUNK)
        if cut < CHUNK // 2:   # 找不到合適換行就硬切
            cut = CHUNK
        parts.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    for i, p in enumerate(parts):
        prefix = "" if i == 0 else f"<i>(續 {i + 1}/{len(parts)})</i>\n"
        await send(chat_id, prefix + p, reply_markup if i == len(parts) - 1 else None)


# 送圖一律壓縮（不看原檔大小），讓每張的傳輸量一致 —— Atlas 踩過的坑：
# 大的壓了變小、小的沒壓還是大 → 上傳時間不對稱造成誤判 timeout 與重複訊息。
# TG 顯示時本來就壓到 ~1280，先壓到 1920 肉眼看不出差。
_PHOTO_MAX_DIM = 1920
_PHOTO_JPEG_Q = 85
_PHOTO_TIMEOUT_S = 120


def _compress(src_path: str) -> str:
    """轉 JPEG + 縮邊。Pillow 缺席 / 讀圖失敗 → 回原路徑。"""
    try:
        src = Path(src_path)
        if not src.exists():
            return src_path
        try:
            from PIL import Image
        except ImportError:
            return src_path
        im = Image.open(src)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        w, h = im.size
        if max(w, h) > _PHOTO_MAX_DIM:
            scale = _PHOTO_MAX_DIM / max(w, h)
            im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        out = src.with_name(src.stem + "_compressed.jpg")
        im.save(out, "JPEG", quality=_PHOTO_JPEG_Q, optimize=True)
        return str(out)
    except Exception as e:
        log.warning(f"[Telegram] 壓縮失敗（照原圖送）：{e}")
        return src_path


async def send_photos(chat_id: int, paths: list[str], caption_prefix: str = ""):
    """批次送截圖。每張分開 try/except，一張壞不會拖垮整批。送完刪檔。

    重複訊息的成因與對策：timeout / network error 時 Python 以為失敗，但 TG
    其實已收到，我們又送一次 document → 使用者收到兩份。所以只在「確定 TG
    拒收這個檔」（BadRequest）時才 fallback 成 document；其他錯誤一律視為
    「很可能已送達」不重送。
    """
    if not paths:
        return
    token, chat_id = _credentials(chat_id)
    if not token or not chat_id:
        return

    from telegram.error import BadRequest

    async def _send_one(bot, path: str, cap: str, i: int, total: int) -> bool:
        try:
            with open(path, "rb") as fh:
                await asyncio.wait_for(
                    bot.send_photo(chat_id=chat_id, photo=fh, caption=cap or None),
                    timeout=_PHOTO_TIMEOUT_S)
            return True
        except BadRequest as e:
            log.warning(f"[Telegram] 圖 {i}/{total} 被 TG 拒收，改送 document：{e}")
        except asyncio.TimeoutError:
            log.warning(f"[Telegram] 圖 {i}/{total} 超過 {_PHOTO_TIMEOUT_S}s 沒回 ack，"
                        f"TG 可能已收到（不重送）")
            return True
        except Exception as e:
            log.warning(f"[Telegram] 圖 {i}/{total} 送出例外（{type(e).__name__}: {e}），"
                        f"TG 可能已收到，不重送")
            return True
        try:
            with open(path, "rb") as fh:
                await asyncio.wait_for(
                    bot.send_document(chat_id=chat_id, document=fh, caption=cap or None),
                    timeout=_PHOTO_TIMEOUT_S)
            return True
        except Exception as e:
            log.error(f"[Telegram] 圖 {i}/{total} 徹底送不出去：{type(e).__name__}: {e}")
            return False

    try:
        async with Bot(token=token) as bot:
            total = len(paths)
            for i, p in enumerate(paths, start=1):
                cap = caption_prefix + (f"（螢幕 {i}/{total}）" if total > 1 else "")
                send_path = _compress(p)
                try:
                    if os.path.getsize(send_path) <= 0:
                        log.error(f"[Telegram] 圖 {i}/{total} 是 0 bytes → 跳過")
                        continue
                except OSError as e:
                    log.error(f"[Telegram] 圖 {i}/{total} 讀不到（{e}）→ 跳過")
                    continue
                if await _send_one(bot, send_path, cap, i, total):
                    for f in {p, send_path}:
                        try:
                            os.unlink(f)
                        except OSError:
                            pass
    except Exception as e:
        log.error(f"[Telegram] 批次送圖異常：{e}")


async def send_file(chat_id: int, path: str, caption: str = "") -> tuple[bool, str]:
    """把一個檔案當 document 送出。回 (成功?, 說明)。

    資料夾會先打包成 zip。超過 TG 的 50MB 上限直接放棄（不硬送、不切割）。
    """
    token, chat_id = _credentials(chat_id)
    if not token or not chat_id:
        return False, "未設定 Telegram"
    p = Path(path)
    if not p.exists():
        return False, f"檔案不存在：{path}"

    tmp_zip = None
    if p.is_dir():
        import shutil
        tmp_zip = Path(str(p) + ".zip")
        try:
            shutil.make_archive(str(p), "zip", str(p))
        except Exception as e:
            return False, f"打包資料夾失敗：{e}"
        p = tmp_zip

    try:
        size_mb = p.stat().st_size / 1024 / 1024
        if size_mb > 50:
            return False, f"檔案 {size_mb:.0f}MB 超過 Telegram 50MB 上限"
        async with Bot(token=token) as bot:
            with open(p, "rb") as fh:
                await asyncio.wait_for(
                    bot.send_document(chat_id=chat_id, document=fh,
                                      filename=p.name, caption=caption or None),
                    timeout=300)
        return True, f"{p.name}（{size_mb:.1f}MB）"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        if tmp_zip is not None:
            try:
                tmp_zip.unlink()
            except OSError:
                pass


# ── 截圖 ─────────────────────────────────────────────────────────────

def take_screenshots(workflow_name: str, step_name: str) -> list[str]:
    """逐螢幕截圖（1 螢幕 → 1 張、2 螢幕 → 2 張）。回檔案路徑清單。

    mss 的 monitors[0] 是「所有螢幕拼成的虛擬桌面」、monitors[1..N] 是每台實體
    螢幕。逐螢幕抓在 Telegram 上更好看（不會因為多螢幕被壓成一張超寬的）。
    """
    import time
    from config import WORKFLOW_DIR

    results: list[str] = []
    try:
        import mss
        from mss.tools import to_png
    except ImportError:
        log.warning(f"[{step_name}] 沒裝 mss，無法截圖")
        return results

    ss_dir = WORKFLOW_DIR / workflow_name
    ss_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    try:
        with mss.mss() as sct:
            monitors = sct.monitors[1:] or sct.monitors  # 單螢幕系統 [1:] 可能是空的
            for idx, mon in enumerate(monitors, start=1):
                tag = f"mon{idx}" if len(monitors) > 1 else "full"
                ss_path = ss_dir / f"screenshot_{step_name}_{ts}_{tag}.png"
                try:
                    img = sct.grab(mon)
                    to_png(img.rgb, img.size, output=str(ss_path))
                except Exception as e:
                    log.warning(f"[{step_name}] 螢幕 {idx} 截圖失敗（略過）：{e}")
                    continue
                # to_png 偶爾會沉默失敗，確認檔案真的產生且非 0 bytes
                if not ss_path.exists() or ss_path.stat().st_size <= 0:
                    log.warning(f"[{step_name}] 螢幕 {idx} 截圖沒產生或是空檔")
                    try:
                        ss_path.unlink()
                    except OSError:
                        pass
                    continue
                results.append(str(ss_path))
    except Exception as e:
        log.warning(f"[{step_name}] 截圖失敗：{e}")
    if results:
        log.info(f"[{step_name}] 📸 截圖 {len(results)} 張")
    return results


# ── 流程事件通知 ─────────────────────────────────────────────────────

async def notify_failure(run, val, step_name: str, total_steps: int):
    """步驟失敗 → 推播 + 決策按鈕。"""
    step_num = run.current_step + 1

    if run.awaiting_type == "missing_dependency":
        import html
        import json
        try:
            meta = json.loads(run.awaiting_suggestion or "{}")
        except json.JSONDecodeError:
            meta = {}
        pkgs = meta.get("packages") or []
        stderr_tail = (meta.get("stderr_tail") or "")[-200:]
        text = (
            f"📦 <b>需要安裝套件</b>\n\n"
            f"📋 {run.pipeline_name}\n"
            f"📍 步驟 {step_num}/{total_steps}：<b>{step_name}</b>\n\n"
            f"這一步用到的套件還沒安裝：\n"
        )
        for p in pkgs:
            text += f"  • <code>{p}</code>\n"
        if stderr_tail:
            text += f"\n<i>stderr：{html.escape(stderr_tail)}</i>\n"
        text += "\n按下方按鈕授權安裝，或中止後自己處理。"
        await send(run.telegram_chat_id, text, missing_dep_keyboard(run.run_id, pkgs))
        return

    text = (
        f"⚠️ <b>需要決策</b>\n\n"
        f"📋 {run.pipeline_name}\n"
        f"📍 步驟 {step_num}/{total_steps}：<b>{step_name}</b>\n\n"
        f"🔴 {val.reason}\n"
    )
    if val.suggestion:
        text += f"💡 建議：{val.suggestion}\n"
    text += "\n請選擇處理方式："
    # 失敗原因可能很長 → 分段送、不截斷（按鈕放最後一段）
    await send_long(run.telegram_chat_id, text,
                    decision_keyboard(run.run_id, has_prev=run.current_step > 0))


async def notify_final(run, config):
    """流程結束 → 推播結果摘要。"""
    total = len(config.steps)
    ok_count = sum(1 for r in run.step_results if r.validation_status == "ok")
    emoji, title = {
        "completed": ("✅", "工作流完成"),
        "aborted": ("🛑", "工作流已中止"),
    }.get(run.status, ("❌", "工作流失敗"))

    duration = ""
    if run.ended_at and run.started_at:
        try:
            secs = int((datetime.fromisoformat(run.ended_at)
                        - datetime.fromisoformat(run.started_at)).total_seconds())
            duration = f"⏱ 耗時：{secs // 60}m {secs % 60}s\n"
        except ValueError:
            pass

    # 按 step_index 查、不按 list 位置 —— condition 跳轉會讓位置 ≠ 索引，
    # 用位置會把跳過的步顯示成別步的結果。
    by_idx = {sr.step_index: sr for sr in run.step_results}
    lines = []
    for i, step in enumerate(config.steps):
        r = by_idx.get(i)
        if r is None:
            lines.append(f"  ⬜ {step.name}（未執行）")
        else:
            icon = {"ok": "✅", "warning": "⚠️", "failed": "❌"}.get(r.validation_status, "❓")
            lines.append(f"  {icon} {step.name}")

    await send(run.telegram_chat_id, (
        f"{emoji} <b>{title}</b>\n\n"
        f"📋 {run.pipeline_name}\n"
        f"🔢 {ok_count}/{total} 步驟成功\n"
        f"{duration}"
        f"\n<b>步驟概覽：</b>\n" + "\n".join(lines) +
        f"\n\n📁 <code>{run.log_path}</code>"
    ))


def find_prev_output(run, config, before_index: Optional[int] = None) -> Optional[str]:
    """找「上一個非人工確認節點」實際產出的檔案。找不到回 None。

    優先用 StepResult.actual_output_path（dir-snapshot 算出來的實際落地位置），
    沒有才退回 step.output.path 的宣告值 —— 宣告值可能指到不存在的地方。
    """
    from engine.checks import resolve_output_path

    idx = (run.current_step if before_index is None else before_index) - 1
    while idx >= 0 and config.steps[idx].human_confirm:
        idx -= 1
    if idx < 0:
        return None

    sr = next((r for r in run.step_results if r.step_index == idx), None)
    if sr and sr.actual_output_path and Path(sr.actual_output_path).exists():
        return sr.actual_output_path

    step = config.steps[idx]
    if step.output and step.output.path:
        p = resolve_output_path(step.output.path, config.name)
        if p.exists():
            return str(p)
    return None
