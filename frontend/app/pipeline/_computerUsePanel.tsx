'use client'
import { useEffect, useRef, useState } from 'react'
import Link from 'next/link'
import { X, Circle, Square as StopIcon, Play, Trash2, ChevronUp, ChevronDown, Pencil, Plus, MousePointer2 } from 'lucide-react'
import { toast } from 'sonner'
import type { ComputerUseData, ComputerUseNode, ComputerUseAction } from './_helpers'

import {
  startComputerUseRecording,
  stopComputerUseRecording,
  getComputerUseRecordingStatus,
  loadComputerUseRecording,
  deleteComputerUseAssets,
  armComputerUseRecordingHotkey,
  disarmComputerUseRecordingHotkey,
  verifyGroundingDesc as verifyGroundingDescApi,
  analyzeAnchors,
  getGroundingStatus,
  getVlmSettings,
  listAssetFiles,
} from '@/lib/api'
import type { GroundingStatus, VlmSettings } from '@/lib/api'
import AnchorEditorModal from './_anchorEditorModal'
import UiaInspectorPanel from './_uiaInspectorPanel'
import { assetImageUrl } from '@/lib/api'

const NODE_COLOR = '#9333ea'

interface Props {
  node: ComputerUseNode
  pipelineName: string       // 用於推導預設 assets_dir
  onUpdate: (data: Partial<ComputerUseData>) => void
  onClose: () => void
  onDelete: () => void
  workflowId?: string
}

export default function ComputerUsePanel({ node, pipelineName, onUpdate, onClose, onDelete, workflowId }: Props) {
  const data = node.data
  const inputCls = 'w-full border border-gray-200 rounded-lg px-2.5 py-1.5 text-sm outline-none focus:border-purple-400 focus:ring-1 focus:ring-purple-400/20 bg-white'

  // 錄製狀態
  const [recording, setRecording] = useState(false)
  const [statusText, setStatusText] = useState('')
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  // F7 待命模式:arm 後最小化瀏覽器、把焦點留在目標 app、按 F7 啟動錄製
  const [armed, setArmed] = useState(false)

  // CV 比對設定摺疊（預設收折，避免佔太多空間）
  const [cvOpen, setCvOpen] = useState(false)
  // OCR 比對設定摺疊（預設收折）
  const [ocrOpen, setOcrOpen] = useState(false)
  // VLM 把關 Phase 1 摺疊（預設收折、進階功能）
  // 4 種 VLM 功能決策樹摺疊（預設收折、給混淆的人查）
  // 進階選項顯示開關 — 預設關、用 localStorage 記住使用者偏好
  // 關閉時:藏「Pixel/UIA 模式切換」按鈕(強制 Pixel 模式、錄製會自動抓 UIA + CV 三層 fallback)
  // 開啟時:顯示模式切換、使用者可手動切到 UIA Inspector 進階功能
  const [showAdvanced, setShowAdvanced] = useState<boolean>(() => {
    if (typeof window === 'undefined') return false
    return window.localStorage.getItem('computer_use_show_advanced') === '1'
  })
  useEffect(() => {
    if (typeof window !== 'undefined') {
      window.localStorage.setItem('computer_use_show_advanced', showAdvanced ? '1' : '0')
    }
  }, [showAdvanced])

  // 預設錄製輸出目錄
  const defaultAssetsDir = data.assetsDir ||
    `workflows/${pipelineName || 'pipeline'}/${data.name}_assets`

  // 錄製過程輪詢狀態
  useEffect(() => {
    if (!recording && !armed) {
      if (pollRef.current) clearInterval(pollRef.current)
      pollRef.current = null
      return
    }
    const poll = async () => {
      try {
        const s = await getComputerUseRecordingStatus()
        if (s.recording) {
          // 不論 arm or 直接按按鈕、recording true 都要進錄製狀態
          if (!recording) setRecording(true)
          if (armed) setArmed(false)
          setStatusText(`錄製中… ${s.action_count ?? 0} 個動作`)
        } else if (recording) {
          // 錄製已被 F9 或後端自行停止
          setRecording(false)
          setStatusText('')
          await handleLoadRecording()
        }
        // armed but not recording: 還在等 F7、保持 armed 狀態
      } catch {/* ignore transient errors */}
    }
    pollRef.current = setInterval(poll, 1000)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [recording, armed])

  // panel 關閉時、清掉 arm(避免熱鍵繼續綁著)
  useEffect(() => {
    return () => {
      if (armed) {
        disarmComputerUseRecordingHotkey().catch(() => {})
      }
    }
  }, [armed])

  // 攔掉 F7 / F9 的瀏覽器預設行為:
  //   F7 = Chrome/Edge「鍵盤瀏覽 Caret Browsing」確認框
  //   F9 = 部分瀏覽器的閱讀模式 / reader view
  // 這兩個都是我們的錄製熱鍵(F7 待命開錄、F9 結束),後端 OS 層級全域熱鍵在收、跟瀏覽器無關,
  // 所以這裡 preventDefault 不影響錄製、只是不讓瀏覽器搶這兩個鍵。panel 開著就生效。
  useEffect(() => {
    const blockFnKeys = (e: KeyboardEvent) => {
      if (e.key === 'F7' || e.key === 'F9' || e.keyCode === 118 || e.keyCode === 120) {
        e.preventDefault()
        e.stopPropagation()
      }
    }
    window.addEventListener('keydown', blockFnKeys, true)
    return () => window.removeEventListener('keydown', blockFnKeys, true)
  }, [])

  const handleArmHotkey = async () => {
    if (armed || recording) return
    try {
      const sessionId = `${data.name}-${Date.now()}`
      await armComputerUseRecordingHotkey(sessionId, defaultAssetsDir)
      onUpdate({ assetsDir: defaultAssetsDir })
      setArmed(true)
      toast.success('🔫 F7 已待命。最小化瀏覽器、把焦點放到目標 app、按 F7 開始錄製')
    } catch (e) {
      toast.error((e as Error).message)
    }
  }

  const handleDisarmHotkey = async () => {
    try {
      await disarmComputerUseRecordingHotkey()
    } finally {
      setArmed(false)
    }
  }

  const handleStart = async () => {
    if (recording) return
    try {
      const sessionId = `${data.name}-${Date.now()}`
      await startComputerUseRecording(sessionId, defaultAssetsDir)
      onUpdate({ assetsDir: defaultAssetsDir })
      setRecording(true)
      setStatusText('錄製中…（按 F9 或這個按鈕結束）')
      toast.success('🔴 開始錄製。請操作螢幕，F9 停止。')
    } catch (e) {
      toast.error((e as Error).message)
    }
  }

  const handleStop = async () => {
    try {
      await stopComputerUseRecording()
      setRecording(false)
      setStatusText('')
      await handleLoadRecording()
    } catch (e) {
      toast.error((e as Error).message)
    }
  }

  // 錨點獨特性：index → { rivals, nearest_rival_px }
  // 「這張錨點在錄製當下的畫面上還對得到幾個地方」。>0 代表回放時 CV 有機會
  // 挑錯那一個 —— 而且假匹配分數可以到 0.95 以上，調門檻擋不住。
  // 只提醒、不自動改行為（試過自動鎖搜尋範圍，實測不可行）。
  const [anchorRisk, setAnchorRisk] = useState<Record<number, {
    rivals: number; nearest: number; scanned: number
    phases?: { box: number; near: number; fullscreen: number }
    flat?: boolean; variance?: number
    targetScore?: number; rivalScore?: number
  }>>({})

  // silent = 自動重算（設定一改就跑），不跳 toast；只有錄完 / 手動按按鈕才跳
  const runAnchorCheck = async (acts: ComputerUseAction[], dir: string, silent = false) => {
    if (!acts?.length || !dir) return
    try {
      const res = await analyzeAnchors(dir, acts as unknown as Record<string, unknown>[], {
        cv_search_radius: data.cvSearchRadius ?? 400,
        cv_threshold: data.cvThreshold ?? 0.5,
        cv_search_only_near: data.cvSearchOnlyNear === true,
      })
      const map: typeof anchorRisk = {}
      for (const r of res.results) {
        // flat（純色錨點）比有替身嚴重 —— CV 和幻覺守門對它都無效，一定要報
        if (r.checked && (r.rivals > 0 || r.flat)) {
          map[r.index] = {
            rivals: r.rivals, nearest: r.nearest_rival_px,
            scanned: r.scanned ?? r.rivals, phases: r.phases,
            flat: r.flat, variance: r.variance,
            targetScore: r.target_score, rivalScore: r.best_rival_score,
          }
        }
      }
      setAnchorRisk(map)
      if (silent) return
      const nFlat = Object.values(map).filter(m => m.flat).length
      const nRival = Object.keys(map).length - nFlat
      if (nFlat > 0) {
        toast.error(`${nFlat} 個錨點幾乎沒有特徵（純色），CV 會亂命中 —— 請重圈（見動作列表的 ⚠）`,
          { duration: 10000 })
      }
      if (nRival > 0) {
        toast.warning(`${nRival} 個錨點有分數逼近的替身，回放時可能被搶走（見動作列表的 ⚠）`,
          { duration: 7000 })
      }
      if (nFlat === 0 && nRival === 0) {
        toast.success('錨點檢查通過：搜尋範圍內沒有分數搶得走真目標的地方')
      }
    } catch (e) {
      console.warn('anchor check:', e)   // 分析失敗不影響錄製結果
    }
  }

  // ── 設定一改就重算警告 ────────────────────────────────────────────
  // 警告是「依目前設定判斷風險」，設定變了卻不重算就會說謊：
  // 縮小橘框把風險解掉了紅字還掛著；半徑從 400 調到 1500 引入新風險卻一片安靜
  // —— 後者更危險。連警告自己那個「勾只搜附近」的連結按下去都不會重算。
  // 只看真正影響判定的欄位，debounce 500ms 避免拖框 / 打字時狂打後端。
  const riskSignature = JSON.stringify({
    dir: data.assetsDir || defaultAssetsDir,
    radius: data.cvSearchRadius ?? 400,
    threshold: data.cvThreshold ?? 0.5,
    onlyNear: data.cvSearchOnlyNear === true,
    acts: (data.actions || []).map(a => [
      a.type, a.image, a.x, a.y,
      (a as { search_region?: number[] }).search_region,
      (a as { cv_strict_region?: boolean }).cv_strict_region,
      (a as { confidence?: number }).confidence,
    ]),
  })
  useEffect(() => {
    const acts = data.actions || []
    if (!acts.some(a => a.type === 'click_image' && a.image)) {
      setAnchorRisk({})
      return
    }
    const t = setTimeout(() => {
      runAnchorCheck(acts, data.assetsDir || defaultAssetsDir, true)
    }, 500)
    return () => clearTimeout(t)
    // riskSignature 已涵蓋所有會影響結果的輸入；放 data.actions 會因為
    // 每次 onUpdate 產生新陣列而無謂重跑
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [riskSignature])

  const handleLoadRecording = async () => {
    try {
      const res = await loadComputerUseRecording(defaultAssetsDir)
      onUpdate({ actions: res.actions || [], assetsDir: defaultAssetsDir })
      toast.success(`已載入 ${res.actions?.length ?? 0} 個動作`)
      runAnchorCheck(res.actions || [], defaultAssetsDir)
    } catch (e) {
      // 錄製尚未停好或目錄不存在是正常狀況
      console.warn('Load recording:', e)
    }
  }

  // 動作操作
  const moveAction = (i: number, dir: -1 | 1) => {
    const next = [...(data.actions || [])]
    const j = i + dir
    if (j < 0 || j >= next.length) return
    ;[next[i], next[j]] = [next[j], next[i]]
    onUpdate({ actions: next })
  }
  const deleteAction = (i: number) => {
    const next = [...(data.actions || [])]
    next.splice(i, 1)
    onUpdate({ actions: next })
  }
  const [editingAnchor, setEditingAnchor] = useState<number | null>(null)
  // 多形態錨點：assets_dir 內的所有錨點圖（懶載入，展開選圖時才抓）
  const [assetFiles, setAssetFiles] = useState<string[] | null>(null)
  const [variantOpenIdx, setVariantOpenIdx] = useState<number | null>(null)
  const openVariantPicker = async (idx: number) => {
    if (variantOpenIdx === idx) { setVariantOpenIdx(null); return }
    setVariantOpenIdx(idx)
    if (assetFiles !== null) return
    try {
      const res = await listAssetFiles(data.assetsDir || defaultAssetsDir)
      setAssetFiles(res.files.map(f => f.name))
    } catch {
      setAssetFiles([])
    }
  }
  const toggleVariant = (idx: number, name: string) => {
    const cur = (data.actions?.[idx]?.image_variants || []) as string[]
    const next = cur.includes(name) ? cur.filter(v => v !== name) : [...cur, name]
    applyAnchorPatch(idx, { image_variants: next })
  }
  // 描述驗證進行中的動作索引（null = 沒在跑）
  const [describingIdx, setDescribingIdx] = useState<number | null>(null)
  // 每個動作的描述驗證結果：'OK:...' 通過 / 'SKIP:...' 沒驗到 / 其他 = 沒過的原因
  const [descVerify, setDescVerify] = useState<Record<number, string | null>>({})
  // 這台機器能不能用「直接定位」（需要外掛 + NVIDIA GPU + 已下載模型）
  const [gStatus, setGStatus] = useState<GroundingStatus | null>(null)
  // 有沒有設定視覺模型（給「描述→OCR」用）。沒設 → 那個選項反灰。
  const [vlmCfg, setVlmCfg] = useState<VlmSettings | null>(null)
  useEffect(() => {
    let alive = true
    getGroundingStatus().then(s => { if (alive) setGStatus(s) }).catch(() => {})
    getVlmSettings().then(s => { if (alive) setVlmCfg(s) }).catch(() => {})
    return () => { alive = false }
  }, [])

  // 把使用者寫的描述餵給地端定位模型，看它會不會點回錄製時的那個位置。
  // Atlas 是「產生描述 + 自動驗證」；Atlas-Lite 沒有雲端視覺可以產生描述，
  // 但驗證這一步一定要留 —— 描述寫錯讀起來一樣通順，只有系統驗得出來。
  const verifyGroundingDesc = async (i: number, a: ComputerUseAction) => {
    const dir = data.assetsDir || defaultAssetsDir
    if (!dir) { toast.error('找不到 assets 目錄'); return }
    setDescribingIdx(i)
    try {
      const j = await verifyGroundingDescApi(dir, a as unknown as Record<string, unknown>, a.vlm_prompt || '')
      // 三種狀態要分開講：驗過且通過 / 驗過但沒過 / 根本沒驗到。
      // 把「沒驗到」講成「通過」會給假的安全感。
      if (j.verified && j.verify_px != null) {
        toast.success(`驗證通過，誤差 ${j.verify_px}px`)
        setDescVerify(v => ({ ...v, [i]: `OK:誤差 ${j.verify_px}px` }))
      } else if (j.verified) {
        toast.info(j.verify_msg, { duration: 6000 })
        setDescVerify(v => ({ ...v, [i]: `SKIP:${j.verify_msg}` }))
      } else {
        toast.warning(`驗證沒過：${j.verify_msg}`, { duration: 8000 })
        setDescVerify(v => ({ ...v, [i]: j.verify_msg }))
      }
    } catch (e) {
      toast.error(`驗證失敗：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setDescribingIdx(null)
    }
  }
  const applyAnchorPatch = (i: number, patch: Partial<ComputerUseAction>) => {
    const next = [...(data.actions || [])]
    next[i] = { ...next[i], ...patch }
    onUpdate({ actions: next })
  }
  // 三層 fallback (UIA / CV / 座標) 各自獨立 toggle, 預設全 True
  // 對應 backend ComputerUseAction.{use_uia, use_cv, use_coord}
  // 全勾 = UIA → CV → 強制座標 (預設); 取消某層改變鏈, 全關 = 該 action 失敗
  const toggleLayer = (i: number, field: 'use_uia' | 'use_cv' | 'use_coord') => {
    const next = [...(data.actions || [])]
    const cur: any = { ...next[i] }
    // 預設視為 True (所有欄位都是預設 true)
    const currentlyOn = cur[field] !== false
    cur[field] = !currentlyOn
    next[i] = cur
    onUpdate({ actions: next })
  }

  // Preset 一鍵設好 3 個 toggle (對應 4 個常見模式:全 / 純UIA / 純CV / 純座標)
  // 舊「圖像比對」單鍵的等價回歸:點「🔍 純 CV」一鍵切到純 CV 模式
  const applyLayerPreset = (i: number, uia: boolean, cv: boolean, coord: boolean) => {
    const next = [...(data.actions || [])]
    next[i] = { ...next[i], use_uia: uia, use_cv: cv, use_coord: coord } as any
    onUpdate({ actions: next })
  }

  return (
    <div className="absolute top-0 right-0 h-full w-[420px] bg-white shadow-2xl border-l border-gray-100 flex flex-col z-30 overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-3 px-4 py-3.5 border-b" style={{ borderTopColor: NODE_COLOR, borderTopWidth: 3 }}>
        <span className="w-8 h-8 rounded-full flex items-center justify-center text-white shrink-0"
          style={{ background: NODE_COLOR }}><MousePointer2 className="w-4 h-4" strokeWidth={2.4} /></span>
        <div className="flex-1 min-w-0">
          <span className="font-semibold text-gray-800 text-sm block truncate">桌面自動化節點</span>
          <span className="text-xs text-gray-400">
            {(data.cuMode || 'pixel') === 'uia'
              ? 'UIA 控制(讀 GUI 結構、不靠座標)'
              : '錄製滑鼠/鍵盤操作，以圖像錨點穩定回放'}
          </span>
        </div>
        <button onClick={onDelete} title="刪除" className="text-gray-300 hover:text-red-400 transition-colors p-1">🗑</button>
        <button onClick={onClose} className="text-gray-400 hover:text-gray-600 transition-colors"><X className="w-4 h-4" /></button>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* Name */}
        <div>
          <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">節點名稱</label>
          <input value={data.name} onChange={e => onUpdate({ name: e.target.value })} className={`${inputCls} font-mono`} />
        </div>

        {/* 模式切換:Pixel(錄製座標) vs UIA(讀 GUI 結構) — 進階選項、預設藏起來
            預設只顯示 Pixel 模式(自動三層 fallback:UIA→CV→座標、無腦用)。
            想用獨立 UIA Inspector 才需要打開「顯示進階選項」。 */}
        {showAdvanced ? (
          <>
            <div className="rounded-xl border border-gray-200 overflow-hidden flex">
              <button
                type="button"
                onClick={() => onUpdate({ cuMode: 'pixel' })}
                className={`flex-1 px-3 py-2 text-sm font-medium transition-colors ${
                  (data.cuMode || 'pixel') === 'pixel'
                    ? 'bg-purple-600 text-white'
                    : 'bg-white text-gray-600 hover:bg-purple-50'
                }`}
              >
                🎯 Pixel 模式<span className="text-[10px] block mt-0.5 opacity-80">錄製座標 + CV/OCR/VLM</span>
              </button>
              <button
                type="button"
                onClick={() => onUpdate({ cuMode: 'uia' })}
                className={`flex-1 px-3 py-2 text-sm font-medium transition-colors ${
                  data.cuMode === 'uia'
                    ? 'bg-purple-600 text-white'
                    : 'bg-white text-gray-600 hover:bg-purple-50'
                }`}
              >
                🪟 UIA 模式<span className="text-[10px] block mt-0.5 opacity-80">讀 GUI 結構、座標漂免疫</span>
              </button>
            </div>

            {/* UIA 模式:走 inspector 抓元素、選元素、加動作 */}
            {data.cuMode === 'uia' && (
              <UiaInspectorPanel
                uiaWindow={data.uiaWindow || ''}
                onUpdateWindow={(w) => onUpdate({ uiaWindow: w })}
                onAddAction={(action) => {
                  const next = [...(data.actions || []), action]
                  onUpdate({ actions: next })
                }}
                workflowId={workflowId}
              />
            )}
          </>
        ) : (
          // 隱藏進階模式時、確保 cuMode 是 pixel(避免進階關閉但 cuMode 還停在 uia 導致面板亂)
          (() => {
            if (data.cuMode === 'uia') onUpdate({ cuMode: 'pixel' })
            return null
          })()
        )}

        {/* 錄製按鈕 — 只 Pixel 模式才顯示 */}
        {(data.cuMode || 'pixel') === 'pixel' && (
        <div className="p-3 rounded-lg border border-purple-200 bg-purple-50/50 space-y-2">
          <div className="flex items-center gap-2">
            {!recording ? (
              <button onClick={handleStart}
                className="flex-1 flex items-center justify-center gap-2 px-3 py-2 bg-red-500 hover:bg-red-600 text-white rounded-lg text-sm font-medium transition-colors">
                <Circle className="w-3.5 h-3.5 fill-current" />
                開始錄製
              </button>
            ) : (
              <div className="flex-1 flex flex-col gap-1">
                <div className="text-center text-xs font-semibold text-red-600 animate-pulse">
                  🎯 推薦:按 <kbd className="px-1.5 py-0.5 rounded bg-red-100 text-red-700 font-mono text-[11px]">F9</kbd> 停止(免回來點按鈕害目標 app 失焦)
                </div>
                <button onClick={handleStop}
                  className="flex items-center justify-center gap-2 px-3 py-2 bg-gray-700 hover:bg-gray-800 text-white rounded-lg text-sm font-medium transition-colors">
                  <StopIcon className="w-3.5 h-3.5" />
                  或點此停止錄製
                </button>
              </div>
            )}
          </div>
          {/* F7 待命模式 — 用熱鍵開啟錄製、不必回來點按鈕害目標 app 失焦 */}
          {!recording && (
            <div className="flex items-center gap-2">
              {!armed ? (
                <button onClick={handleArmHotkey}
                  className="flex-1 flex items-center justify-center gap-2 px-3 py-1.5 border border-purple-300 text-purple-700 hover:bg-purple-100 rounded-lg text-xs font-medium transition-colors">
                  📡 啟用 F7 待命(免點按鈕、按 F7 直接錄)
                </button>
              ) : (
                <button onClick={handleDisarmHotkey}
                  className="flex-1 flex items-center justify-center gap-2 px-3 py-1.5 border border-orange-400 bg-orange-50 text-orange-700 hover:bg-orange-100 rounded-lg text-xs font-medium transition-colors animate-pulse">
                  🔫 F7 已待命、按 F7 開始(點此取消)
                </button>
              )}
            </div>
          )}
          {recording && (
            <p className="text-xs text-red-600 flex items-center gap-1.5">
              <span className="inline-block w-2 h-2 rounded-full bg-red-500 animate-pulse" />
              {statusText}
            </p>
          )}
        </div>
        )}

        {/* 動作列表 */}
        <div>
          {/* 顯示進階選項 toggle — 從上面挪過來、放動作序列上方,跟列表視覺上一組 */}
          <div className="flex items-center justify-end mb-1.5">
            <label className="flex items-center gap-1.5 text-[11px] text-gray-400 hover:text-gray-600 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={showAdvanced}
                onChange={e => setShowAdvanced(e.target.checked)}
                className="w-3 h-3 accent-purple-500"
              />
              顯示進階選項(UIA Inspector、模式切換)
            </label>
          </div>
          <div className="flex items-center justify-between mb-2">
            <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide">
              動作序列（{data.actions?.length ?? 0}）
            </label>
            {data.actions && data.actions.length > 0 && (
              <button
                onClick={() => runAnchorCheck(data.actions!, data.assetsDir || defaultAssetsDir)}
                title="檢查每張錨點在錄製當下的畫面上是不是獨一無二"
                className="text-[11px] text-gray-400 hover:text-purple-600 mr-2"
              >檢查錨點</button>
            )}
            {data.actions && data.actions.length > 0 && (
              <button onClick={async () => {
                const dir = data.assetsDir || defaultAssetsDir
                const alsoDelete = confirm(
                  '清除所有動作？\n\n按「確定」會同時刪除磁碟上的錨點圖資料夾（建議，避免殘留檔）。\n按「取消」則只清空節點動作、保留磁碟檔（通常不需要）。'
                )
                onUpdate({ actions: [] })
                if (alsoDelete && dir) {
                  try {
                    const r = await deleteComputerUseAssets(dir)
                    if (r.deleted) toast.success(`已刪除錨點資料夾：${r.path}`)
                    else toast.info(r.reason || '資料夾不存在')
                  } catch (e) {
                    toast.error((e as Error).message)
                  }
                }
              }}
                className="text-[11px] text-red-500 hover:text-red-700">清除全部</button>
            )}
          </div>
          {recording && (
            <p className="text-[11px] text-purple-700 bg-purple-50 border border-purple-200 rounded px-2 py-1 mb-2">
              錄製中：按 <span className="font-mono font-bold">F9</span> 停止錄製
            </p>
          )}
          {(!data.actions || data.actions.length === 0) ? (
            <>
              <p className="text-xs text-gray-400 text-center py-6 border border-dashed border-gray-200 rounded-lg">
                尚未錄製任何動作
              </p>
            </>
          ) : (
            <div className="space-y-1.5">
              {data.actions.map((a: ComputerUseAction, i: number) => (
                <div key={i}>
                {/* 動作前的 ➕ 插入點 */}
                <div className="flex items-start gap-2 p-2 bg-gray-50 border border-gray-200 rounded-lg">
                  <span className="text-[10px] font-mono text-gray-400 pt-0.5">#{i + 1}</span>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <span className="text-[11px] px-1.5 py-0.5 rounded font-mono bg-purple-100 text-purple-700">
                        {a.type}
                      </span>
                      {a.image && <span className="text-[11px] text-gray-500 truncate">{a.image}</span>}
                      {/* 三層 fallback toggle (UIA / CV / 強制座標)
                          預設全勾 = UIA → CV → 強制座標(使用者零學習成本、最高命中率)
                          OCR 或 VLM 啟用時整組 disabled——那兩個自帶 primary 邏輯、三層不適用
                          只勾單一 = 嚴格模式(沒中就 fail)、組合 = 自定義 fallback 鏈 */}
                      {(a.type === 'click_image' || a.type === 'click_at') && (() => {
                        const ocrActive = a.use_ocr === true
                        const vlmActive = (a.vlm_mode || 'off') !== 'off'
                        const explicitPrimary = ocrActive || vlmActive
                        const useUia = (a as any).use_uia !== false
                        const useCv = (a as any).use_cv !== false
                        const useCoord = (a as any).use_coord !== false
                        const layerBtn = (label: string, field: 'use_uia' | 'use_cv' | 'use_coord', on: boolean, hint: string) => (
                          <button
                            key={field}
                            type="button"
                            onClick={() => toggleLayer(i, field)}
                            disabled={explicitPrimary}
                            title={explicitPrimary
                              ? `${ocrActive ? 'OCR' : 'VLM'} 啟用中、三層 fallback 不適用`
                              : hint}
                            className={`text-[10px] px-1.5 py-0.5 rounded border transition-colors ${
                              explicitPrimary
                                ? 'bg-gray-50 border-gray-200 text-gray-300 cursor-not-allowed'
                                : on
                                  ? 'bg-emerald-50 border-emerald-300 text-emerald-700'
                                  : 'bg-white border-gray-200 text-gray-400 hover:text-gray-700 hover:border-gray-400'
                            }`}
                          >
                            <span className="font-mono mr-0.5">{on ? '☑' : '☐'}</span>{label}
                          </button>
                        )
                        // Preset chip:常見模式一鍵切到對應的 3-toggle 組合
                        // 純 CV preset 設 use_coord=T (action 層級開), 真正座標 fallback 開關走 step-level cvCoordFallback
                        // 純 UIA / 純 座標 preset 嚴格、其他層 toggle 設 F → 沒中立即 fail
                        const isAll = useUia && useCv && useCoord
                        const isUiaOnly = useUia && !useCv && !useCoord
                        const isCvOnly = !useUia && useCv && useCoord
                        const isCoordOnly = !useUia && !useCv && useCoord
                        const currentMode: 'all' | 'uia-only' | 'cv-only' | 'coord-only' | 'custom' =
                          isAll ? 'all'
                            : isUiaOnly ? 'uia-only'
                            : isCvOnly ? 'cv-only'
                            : isCoordOnly ? 'coord-only'
                            : 'custom'
                        // step-level cvCoordFallback (預設 False) → 純 CV 模式下『座標 fallback 是否啟用』的真正 gate
                        // 純 CV 模式下 座標 checkbox 顯示 = cvCoordFallback、點擊 → toggle cvCoordFallback (而不是 action use_coord)
                        const cvCoordFallback = data.cvCoordFallback === true
                        const presetBtn = (label: string, active: boolean, onClick: () => void, hint: string) => (
                          <button
                            type="button"
                            onClick={onClick}
                            disabled={explicitPrimary}
                            title={explicitPrimary
                              ? `${ocrActive ? 'OCR' : 'VLM'} 啟用中、模式 preset 不適用`
                              : hint}
                            className={`text-[10px] px-1.5 py-0.5 rounded border transition-colors ${
                              explicitPrimary
                                ? 'bg-gray-50 border-gray-200 text-gray-300 cursor-not-allowed'
                                : active
                                  ? 'bg-purple-500 text-white border-purple-500'
                                  : 'bg-white text-gray-500 border-gray-200 hover:border-purple-300 hover:text-purple-600'
                            }`}
                          >{label}</button>
                        )
                        // 純 CV 模式下、座標 checkbox 的特製版:狀態 = cvCoordFallback、click = toggle cvCoordFallback
                        const coordBoxCvOnly = (
                          <button
                            key="coord-cv-only"
                            type="button"
                            onClick={() => onUpdate({ cvCoordFallback: !cvCoordFallback })}
                            disabled={explicitPrimary}
                            title={`純 CV 模式下、CV 找不到時是否退到錄製座標。狀態跟『CV 詳細設定 → CV 失敗退回錄製座標』連動(目前 ${cvCoordFallback ? '啟用' : '關閉'})`}
                            className={`text-[10px] px-1.5 py-0.5 rounded border transition-colors ${
                              explicitPrimary
                                ? 'bg-gray-50 border-gray-200 text-gray-300 cursor-not-allowed'
                                : cvCoordFallback
                                  ? 'bg-emerald-50 border-emerald-300 text-emerald-700'
                                  : 'bg-white border-gray-200 text-gray-400 hover:text-gray-700 hover:border-gray-400'
                            }`}
                          >
                            <span className="font-mono mr-0.5">{cvCoordFallback ? '☑' : '☐'}</span>📍 座標 (fallback)
                          </button>
                        )
                        // 顯示哪些 checkbox 依 currentMode(避免「純 UIA / 純 CV / 純 座標」preset 還顯示無關 layer 視覺重疊)
                        const showUiaBox = currentMode === 'all' || currentMode === 'uia-only' || currentMode === 'custom'
                        const showCvBox = (currentMode === 'all' || currentMode === 'cv-only' || currentMode === 'custom') && a.type === 'click_image'
                        const showCoordBox = currentMode === 'all' || currentMode === 'cv-only' || currentMode === 'coord-only' || currentMode === 'custom'
                        return (
                          <>
                            {/* 模式快速 preset(一鍵切常見組合) */}
                            {presetBtn('全 (三層)', isAll, () => applyLayerPreset(i, true, true, true),
                              '預設三層 fallback:UIA → CV → 強制座標,命中率最高。下方 3 個 checkbox 顯示全勾、可手動取消任一變組合模式')}
                            {presetBtn('🪟 純 UIA', isUiaOnly, () => applyLayerPreset(i, true, false, false),
                              '純 UIA 嚴格模式:只用 UI 結構定位、找不到立即 fail(適合自家程式 + 有 AutomationId)')}
                            {a.type === 'click_image' && presetBtn('🔍 純 CV', isCvOnly, () => applyLayerPreset(i, false, true, true),
                              '純圖像比對:UIA 跳過, CV 找不到時要不要退座標看『CV 詳細設定 → CV 失敗退回錄製座標』(下方座標 checkbox 動態反映此設定)')}
                            {presetBtn('📍 純 座標', isCoordOnly, () => applyLayerPreset(i, false, false, true),
                              '純座標模式:直接點錄製的 x/y、不嘗試任何識別(最快、視窗位置固定才安全)')}
                            <span className="text-[10px] text-gray-300 select-none">|</span>
                            {/* 細項 checkbox(依當前 preset 動態決定顯示哪幾個、避免跟 preset 視覺重疊) */}
                            {showUiaBox && layerBtn('🪟 UIA', 'use_uia', useUia,
                              '啟用 UIA element 結構定位(視窗位置變化最穩、自家程式有 AutomationId 命中率最高)。取消 = 跳過 UIA 直接走下一層')}
                            {showCvBox && layerBtn('🔍 CV', 'use_cv', useCv,
                              '啟用 CV 圖像比對(用錄製的錨點圖找)。取消 = 跳過 CV、UIA 沒中直接退強制座標')}
                            {showCoordBox && (currentMode === 'cv-only' ? coordBoxCvOnly : layerBtn('📍 座標', 'use_coord', useCoord,
                              '啟用強制座標(最終 fallback、直接點錄製的 x/y)。取消 = 前面層失敗就立即 fail、不退座標'))}
                          </>
                        )
                      })()}
                      {/* 手動編輯錨點（click_image/drag 有 full_image 時才顯示） */}
                      {(a.type === 'click_image' || a.type === 'drag') && a.full_image && (
                        <button onClick={() => setEditingAnchor(i)}
                          title="手動圈選錨點（用全螢幕截圖重新定義這個動作要比對的區域）"
                          className="text-[10px] px-1.5 py-0.5 rounded border bg-white border-purple-200 text-purple-600 hover:bg-purple-50">
                          <Pencil className="w-2.5 h-2.5 inline" /> 編輯錨點
                        </button>
                      )}
                    </div>
                    {a.description && <p className="text-xs text-gray-600 mt-0.5 truncate">{a.description}</p>}
                    {/* 警告只在「執行時真的搆得到」時出現，而且建議要對症下藥：
                        替身在搜尋半徑「內」的話，勾「只搜附近」完全沒用（它本來就在附近），
                        該做的是縮半徑／拉橘框；只有替身是「退回全螢幕才會撞到」的，
                        勾「只搜附近」才是正解。 */}
                    {anchorRisk[i]?.flat && (
                      <p className="text-[10px] text-red-700 bg-red-50 border border-red-200 rounded px-1.5 py-1 mt-1 leading-snug">
                        ⛔ 這張錨點<strong>幾乎沒有特徵</strong>（灰階變異數 {anchorRisk[i].variance}）。
                        它跟畫面上<strong>任何一塊平坦區域</strong>比對都會是滿分，所以
                        CV 可能命中完全無關的位置，「直接定位」的幻覺守門也擋不住。
                        <br />
                        請按「編輯錨點」重圈一個<strong>含文字或邊框</strong>的範圍。
                      </p>
                    )}
                    {anchorRisk[i] && !anchorRisk[i].flat && (() => {
                      const r = anchorRisk[i]
                      const nearRisk = (r.phases?.near || 0) > 0 || (r.phases?.box || 0) > 0
                      const onlyFullscreen = !nearRisk && (r.phases?.fullscreen || 0) > 0
                      return (
                        <p className="text-[10px] text-amber-700 bg-amber-50 border border-amber-200 rounded px-1.5 py-1 mt-1 leading-snug">
                          ⚠ 回放時搜尋範圍內有 {r.rivals} 個地方，
                          <strong>相似度逼近真目標</strong>
                          （目標 {r.targetScore?.toFixed(2)} vs 它 {r.rivalScore?.toFixed(2)}，
                          最近的在 {r.nearest}px 外）。
                          CV 取範圍內分數最高的，所以真目標只要掉一點分就可能被它搶走。
                          {r.scanned > r.rivals && (
                            <span className="text-amber-600">
                              （畫面上另有 {r.scanned - r.rivals} 個較像的地方，
                              但分數差得夠遠、搶不走，沒列入）
                            </span>
                          )}
                          <br />
                          {onlyFullscreen ? (
                            <>
                              只有在「橘框和附近都找不到、退回整個桌面」時才會撞到。建議
                              <button
                                type="button"
                                onClick={() => onUpdate({ cvSearchOnlyNear: true })}
                                className="underline hover:text-amber-900"
                              >勾「只搜錄製座標附近」</button>
                              （找不到就停，不會退回全桌面亂點）。
                            </>
                          ) : (
                            <>
                              它就在搜尋範圍內，所以<strong>勾「只搜附近」沒有用</strong>。
                              建議擇一：把 CV 搜尋半徑縮到 {r.nearest}px 以內／拖一個橘框把範圍鎖小／
                              改用 UIA 定位／把錨點框大一點含周邊文字讓它變獨特。
                            </>
                          )}
                        </p>
                      )
                    })()}
                    {a.text && <p className="text-xs text-gray-500 mt-0.5 truncate font-mono">"{a.text}"</p>}
                    {a.keys && a.keys.length > 0 && (
                      <p className="text-xs text-gray-500 mt-0.5 font-mono">{a.keys.join('+')}</p>
                    )}
                    {typeof a.seconds === 'number' && a.seconds > 0 && (
                      <p className="text-xs text-gray-500 mt-0.5">{a.seconds}s</p>
                    )}
                    {/* 地端 GUI 定位（click_image 專用）
                        關       → 走原本 UIA / CV / OCR / 座標
                        直接定位 → 地端 GUI 定位模型直接回座標。用的是專門訓練過 GUI
                                   定位的模型，不是通用視覺模型；失敗會自動退回 CV，
                                   不會讓整步掛掉。需要 NVIDIA GPU + 安裝外掛。
                        （Atlas 另有兩種雲端 VLM 模式，Atlas-Lite 不帶雲端 LLM，沒有。）*/}
                    {a.type === 'click_image' && (() => {
                      const vlmMode = (a.vlm_mode || 'off') as 'off' | 'grounding' | 'description'
                      const locked = gStatus !== null && !gStatus.available
                      const descLocked = vlmCfg !== null && !vlmCfg.available
                      return (
                        <div className="mt-1 space-y-1">
                          <div className="flex items-center gap-1 flex-wrap">
                            <span className="text-[10px] text-gray-500 mr-0.5">視覺輔助：</span>
                            {([
                              { v: 'off',         label: '關',        hint: '走原本 UIA / CV / OCR / 座標' },
                              { v: 'grounding',   label: '直接定位',  hint: '地端 GUI 定位模型直接給座標（連 CV 都點不準時用）。第一次呼叫要載入模型約 30 秒，之後每次約 2-7 秒。失敗自動退回 CV' },
                              { v: 'description', label: '描述→OCR', hint: '畫面上的文字是動態的、錄製時不知道會是什麼字（訂單編號、當日日期）時用。模型看圖回「目標實際顯示的文字」，座標交給 OCR 決定 —— 模型不碰座標，所以不會點到隔壁那顆。需要在設定頁指定視覺模型' },
                            ] as const).map(opt => {
                              // 條件不足時停用而不是藏起來 —— 藏起來使用者永遠不知道有這功能
                              const dis = (opt.v === 'grounding' && locked)
                                || (opt.v === 'description' && descLocked)
                              const disWhy = opt.v === 'grounding'
                                ? `${gStatus?.reason}\n${gStatus?.install_hint}`
                                : `${vlmCfg?.reason}\n${vlmCfg?.hint}`
                              return (
                                <button
                                  key={opt.v}
                                  type="button"
                                  disabled={dis}
                                  onClick={() => applyAnchorPatch(i, { vlm_mode: opt.v })}
                                  title={dis ? `無法使用：${disWhy}` : opt.hint}
                                  className={`text-[10px] px-1.5 py-0.5 rounded border transition-colors ${
                                    dis
                                      ? 'bg-gray-50 text-gray-300 border-gray-200 cursor-not-allowed'
                                      : vlmMode === opt.v
                                        ? 'bg-indigo-500 text-white border-indigo-500'
                                        : 'bg-white text-gray-500 border-gray-200 hover:border-indigo-300 hover:text-indigo-600'
                                  }`}
                                >{dis ? `${opt.label} 🔒` : opt.label}</button>
                              )
                            })}
                          </div>
                          {locked && (
                            <p className="text-[10px] text-gray-500 bg-gray-50 border border-gray-200 rounded px-1.5 py-1 leading-snug">
                              🔒「直接定位」無法使用：{gStatus?.reason}
                              <br />
                              {gStatus?.install_hint}
                            </p>
                          )}
                          {descLocked && (
                            <p className="text-[10px] text-gray-500 bg-gray-50 border border-gray-200 rounded px-1.5 py-1 leading-snug">
                              🔒「描述→OCR」無法使用：{vlmCfg?.reason}
                              <br />
                              {vlmCfg?.hint} 到<Link href="/settings" className="text-indigo-600 underline">設定頁</Link>設定。
                            </p>
                          )}
                          {vlmMode === 'description' && (
                            <>
                              <textarea
                                value={a.vlm_prompt || ''}
                                onChange={e => applyAnchorPatch(i, { vlm_prompt: e.target.value })}
                                placeholder="描述要點的東西，強調它「旁邊有什麼固定不變的字」（例：「訂單編號:」後面那組編號 / 表格第一列的客戶名稱）"
                                rows={2}
                                className="w-full text-[11px] px-1.5 py-1 rounded border border-indigo-300 bg-white outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-400/20 font-mono resize-y"
                              />
                              <p className="text-[10px] text-gray-500 leading-snug">
                                流程：模型看圖 → 回「目標實際顯示的文字」→ 用那段文字跑 OCR 定位 → 點中心。
                                座標由 OCR 決定，模型不碰，所以不會點到隔壁那顆。
                                <br />
                                <span className="text-emerald-700">
                                  適合：文字每次都不同（訂單編號、當日日期、登入者名稱），錄製時填不了「要找的文字」。
                                </span>
                                <br />
                                <span className="text-amber-600">
                                  文字每次都一樣就別用這個 —— 直接勾下面的 OCR 填死那串字，又快又不用模型。
                                </span>
                                <br />
                                描述要指向<strong>畫面上固定不變的相鄰文字</strong>；只說「那個編號」模型會不知道是哪個。
                                每次回放多一次推論（地端約 15-20 秒）。
                              </p>
                            </>
                          )}
                          {vlmMode === 'grounding' && (
                            <>
                              <textarea
                                value={a.vlm_prompt || ''}
                                onChange={e => applyAnchorPatch(i, { vlm_prompt: e.target.value })}
                                placeholder="描述要點什麼（例：工具列上的排序按鈕 / 左側導覽的「下載」）"
                                rows={2}
                                className="w-full text-[11px] px-1.5 py-1 rounded border border-indigo-300 bg-white outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-400/20 font-mono resize-y"
                              />
                              <div className="flex items-center gap-1.5">
                                <button
                                  type="button"
                                  disabled={describingIdx === i || !a.image || !(a.vlm_prompt || '').trim()}
                                  onClick={() => verifyGroundingDesc(i, a)}
                                  title={a.image
                                    ? '把描述餵給地端模型，看它會不會點回錄製時的那個位置'
                                    : '這個步驟沒有錨點圖，無法驗證'}
                                  className="text-[10px] px-1.5 py-0.5 rounded border border-indigo-300 text-indigo-600 bg-white hover:bg-indigo-50 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                                >{describingIdx === i ? '驗證中…' : '🎯 驗證這句描述'}</button>
                                <span className="text-[10px] text-gray-400">描述寫錯讀起來一樣通順，驗一次比較安心</span>
                              </div>
                              {descVerify[i]?.startsWith('OK:') && (
                                <p className="text-[10px] text-emerald-700 bg-emerald-50 border border-emerald-200 rounded px-1.5 py-1 leading-snug">
                                  ✓ {descVerify[i]!.slice(3)}
                                </p>
                              )}
                              {descVerify[i]?.startsWith('SKIP:') && (
                                <p className="text-[10px] text-gray-500 bg-gray-50 border border-gray-200 rounded px-1.5 py-1 leading-snug">
                                  ℹ {descVerify[i]!.slice(5)} —— 這段描述<strong>沒有被驗證過</strong>，請自己確認。
                                </p>
                              )}
                              {descVerify[i] && !descVerify[i]!.startsWith('SKIP:') && !descVerify[i]!.startsWith('OK:') && (
                                <p className="text-[10px] text-red-600 bg-red-50 border border-red-200 rounded px-1.5 py-1 leading-snug">
                                  ⚠ 驗證沒過：{descVerify[i]}
                                  <br />
                                  這段描述很可能會點到別的東西，請改寫（說出元件上的文字 + 所在區域）。
                                </p>
                              )}
                              <p className="text-[10px] text-gray-500 leading-snug">
                                地端模型直接算座標，不需要錨點圖。適合錨點常失效的元素（主題色會變、視窗會縮放）。
                                <span className="text-amber-600">失敗會自動退回 CV 比對</span>，所以錨點圖建議保留。
                                <br />
                                <span className="text-amber-600">描述不要用否定句</span>
                                （「不是⋯⋯的那個」會被忽略而點錯），改用正面且獨特的特徵。
                              </p>
                            </>
                          )}
                          {vlmMode !== 'off' && (
                            <p className="text-[10px] text-amber-600 leading-relaxed">
                              ⚠ 視覺輔助啟用中，下方 OCR / 圖像比對切換會被忽略（視覺模式永遠優先）。
                              每次回放多一次推論（直接定位約 2-7 秒；描述→OCR 地端約 15-20 秒）
                            </p>
                          )}
                        </div>
                      )
                    })()}
                    {/* 多形態錨點：同一顆按鈕會隨狀態換樣子（最大化↔還原、播放↔暫停、
                        亮↔暗主題）。每張都比一次、取分數最高的那張定位。
                        這是 Atlas 靠雲端模型 anchor_pick 解的問題，純 CV 更快更準也免金鑰。 */}
                    {a.type === 'click_image' && a.image && (a.vlm_mode || 'off') === 'off' && (() => {
                      const variants = (a.image_variants || []) as string[]
                      const open = variantOpenIdx === i
                      const pool = (assetFiles || []).filter(n => n !== a.image)
                      return (
                        <div className="mt-1 space-y-1">
                          <div className="flex items-center gap-1.5 flex-wrap">
                            <button
                              type="button"
                              onClick={() => openVariantPicker(i)}
                              title="同一顆按鈕會隨狀態換樣子時，把每種樣子各加一張。執行時每張都比一次、取最像的那張"
                              className={`text-[10px] px-1.5 py-0.5 rounded border transition-colors ${
                                variants.length > 0
                                  ? 'bg-purple-50 border-purple-300 text-purple-700'
                                  : 'bg-white border-gray-200 text-gray-500 hover:border-purple-300 hover:text-purple-600'
                              }`}
                            >🎭 多形態錨點{variants.length > 0 ? ` (${variants.length + 1})` : ''}</button>
                            {variants.map(v => (
                              <span key={v}
                                className="text-[10px] px-1 py-0.5 rounded bg-purple-100 text-purple-700 font-mono inline-flex items-center gap-1">
                                {v}
                                <button type="button" onClick={() => toggleVariant(i, v)}
                                  className="text-purple-400 hover:text-purple-700">×</button>
                              </span>
                            ))}
                          </div>
                          {open && (
                            <div className="border border-purple-200 rounded p-1.5 bg-purple-50/40 space-y-1">
                              <p className="text-[10px] text-gray-600 leading-snug">
                                勾選這顆按鈕的<strong>其他樣子</strong>。回放時每張都比一次、
                                取分數最高的那張定位 —— 不是「第一張過門檻就用」，
                                因為兩張都可能勉強過門檻，只有分數差得出來哪張是當下真正的樣子。
                              </p>
                              {assetFiles === null ? (
                                <p className="text-[10px] text-gray-400">載入中…</p>
                              ) : pool.length === 0 ? (
                                <p className="text-[10px] text-gray-400">
                                  這個資料夾沒有其他錨點圖。先把按鈕的另一種樣子錄下來
                                  （或用錨點編輯器手動圈一張）再回來勾。
                                </p>
                              ) : (
                                <div className="flex flex-wrap gap-1.5">
                                  {pool.map(n => {
                                    const on = variants.includes(n)
                                    return (
                                      <button key={n} type="button" onClick={() => toggleVariant(i, n)}
                                        className={`flex flex-col items-center gap-0.5 p-1 rounded border transition-colors ${
                                          on ? 'border-purple-400 bg-white' : 'border-gray-200 bg-white hover:border-purple-300'
                                        }`}
                                      >
                                        {/* eslint-disable-next-line @next/next/no-img-element */}
                                        <img src={assetImageUrl(data.assetsDir || defaultAssetsDir, n)}
                                          alt={n} className="max-h-8 max-w-[90px] object-contain" />
                                        <span className={`text-[9px] font-mono max-w-[90px] truncate ${
                                          on ? 'text-purple-700' : 'text-gray-400'}`}>
                                          {on ? '☑ ' : '☐ '}{n}
                                        </span>
                                      </button>
                                    )
                                  })}
                                </div>
                              )}
                            </div>
                          )}
                        </div>
                      )
                    })()}
                    {/* OCR 文字比對（只對 click_image action 顯示）
                        規則：
                          - checkbox 勾選 = use_ocr=true，input enable；OCR 變為 primary 方法
                          - 取消勾選 = use_ocr=false，但 ocr_text 保留（下次再勾就不用重打）
                          - 勾選 OCR 不改動 use_coord（primary mode 互相獨立；use_coord 只控制
                            OCR 關閉時用什麼）；失敗 fallback 行為由步驟層級 ocr_cv_fallback 控制 */}
                    {a.type === 'click_image' && (() => {
                      const ocrEnabled = a.use_ocr === true
                      const inputId = `ocr-input-${i}`
                      return (
                        <div className="mt-1 flex items-center gap-1.5">
                          <label className="flex items-center gap-1 shrink-0 cursor-pointer select-none"
                            title={ocrEnabled
                              ? '已啟用 OCR 文字比對；OCR 為主要方法（取代 CV）。預設失敗直接 FAIL（不退 CV），需在下方「OCR 比對設定」手動開啟 ocr_cv_fallback 才會退回 CV'
                              : '勾選啟用 Windows OCR 文字比對。需搭配右側輸入目標文字；取消時保留文字供下次使用'}>
                            <input
                              type="checkbox"
                              checked={ocrEnabled}
                              onChange={e => {
                                if (e.target.checked) {
                                  // 啟用 OCR。不動 use_coord、不動 ocr_text（可能有舊值，直接重用）
                                  applyAnchorPatch(i, { use_ocr: true })
                                  // 若沒文字就 focus input 提示使用者填
                                  if (!a.ocr_text) {
                                    setTimeout(() => {
                                      const el = document.getElementById(inputId) as HTMLInputElement | null
                                      el?.focus()
                                    }, 50)
                                  }
                                } else {
                                  // 只翻 use_ocr，保留 ocr_text（下次勾選可直接重用）
                                  applyAnchorPatch(i, { use_ocr: false })
                                }
                              }}
                              className="w-3 h-3 rounded accent-purple-600"
                            />
                            <span className={`text-[10px] ${ocrEnabled ? 'text-purple-700 font-medium' : 'text-gray-500'}`}>
                              🔤 OCR
                            </span>
                          </label>
                          <input
                            id={inputId}
                            type="text"
                            value={a.ocr_text || ''}
                            onChange={e => applyAnchorPatch(i, { ocr_text: e.target.value })}
                            disabled={!ocrEnabled}
                            placeholder={ocrEnabled ? '要找的文字（例：關閉、下載）' : '勾選 OCR 才能填寫（會保留上次輸入）'}
                            className={`flex-1 min-w-0 text-[11px] px-1.5 py-0.5 rounded border outline-none ${
                              ocrEnabled
                                ? 'border-purple-300 bg-white focus:border-purple-500 focus:ring-1 focus:ring-purple-400/20'
                                : 'border-gray-200 bg-gray-50 text-gray-400 cursor-not-allowed'
                            }`}
                          />
                        </div>
                      )
                    })()}
                  </div>
                  <div className="flex flex-col shrink-0">
                    <button onClick={() => moveAction(i, -1)} className="p-0.5 text-gray-400 hover:text-gray-700 disabled:opacity-30" disabled={i === 0}>
                      <ChevronUp className="w-3 h-3" />
                    </button>
                    <button onClick={() => moveAction(i, 1)} className="p-0.5 text-gray-400 hover:text-gray-700 disabled:opacity-30" disabled={i === (data.actions!.length - 1)}>
                      <ChevronDown className="w-3 h-3" />
                    </button>
                  </div>
                  <button onClick={() => deleteAction(i)} className="text-gray-300 hover:text-red-500 shrink-0">
                    <Trash2 className="w-3 h-3" />
                  </button>
                </div>
                </div>
              ))}
              {/* 列表最後的 ➕ 插入點 */}
            </div>
          )}
        </div>

        {/* Assets 目錄 */}
        <div>
          <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">
            錨點圖片資料夾（相對專案根或絕對路徑）
          </label>
          <input value={data.assetsDir} onChange={e => onUpdate({ assetsDir: e.target.value })}
            placeholder={defaultAssetsDir}
            className={`${inputCls} font-mono text-xs`} />
        </div>

        {/* 選項 */}
        <div className="space-y-2">
          <label className="flex items-center gap-2 text-sm cursor-pointer">
            <input type="checkbox" checked={data.failFast}
              onChange={e => onUpdate({ failFast: e.target.checked })} className="w-4 h-4 accent-purple-600" />
            <span className="text-gray-700">遇錯立即中止（fail_fast）</span>
          </label>
        </div>

        {/* CV 比對設定（可摺疊，預設收折） */}
        <div className="rounded-xl border border-gray-200 bg-gray-50/50 overflow-hidden">
          <button
            type="button"
            onClick={() => setCvOpen(v => !v)}
            className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-gray-100/80 transition-colors"
          >
            {cvOpen ? <ChevronUp className="w-3.5 h-3.5 text-gray-400" />
                    : <ChevronDown className="w-3.5 h-3.5 text-gray-400" />}
            <span className="text-xs font-semibold text-gray-500 uppercase tracking-wide flex-1">CV 比對設定</span>
            <span className="text-[11px] text-gray-400 font-mono">
              {(data.cvThreshold ?? 0.5)}{data.cvSearchOnlyNear ? ' · 只搜附近' : ''}{(data.cvTriggerHover ?? true) ? ` · hover ${data.cvHoverWaitMs ?? 200}ms` : ''}
            </span>
          </button>
          {cvOpen && (
            <div className="px-3 pb-3 space-y-3 border-t border-gray-200">
              <div className="pt-3" />
              {/* 比對門檻 3 段 */}
              <div>
                <label className="text-xs text-gray-600 block mb-1.5">比對門檻</label>
                <div className="grid grid-cols-3 gap-1">
                  {[
                    { v: 0.50, label: '寬鬆', hint: '容錯高，DPI / 主題色 / hover 差異容忍' },
                    { v: 0.80, label: '標準', hint: '預設 sweet spot' },
                    { v: 0.90, label: '嚴格', hint: '幾乎不誤判' },
                  ].map(opt => (
                    <button
                      key={opt.v}
                      type="button"
                      onClick={() => onUpdate({ cvThreshold: opt.v })}
                      title={opt.hint}
                      className={`px-2 py-1.5 rounded-lg text-xs font-medium transition-colors border ${
                        (data.cvThreshold ?? 0.5) === opt.v
                          ? 'bg-purple-500 text-white border-purple-500'
                          : 'bg-white text-gray-600 border-gray-200 hover:border-purple-300'
                      }`}
                    >
                      {opt.label} {opt.v}
                    </button>
                  ))}
                </div>
              </div>

              {/* 只搜附近 toggle */}
              <label className="flex items-center gap-2 text-sm cursor-pointer">
                <input type="checkbox" checked={data.cvSearchOnlyNear}
                  onChange={e => onUpdate({ cvSearchOnlyNear: e.target.checked })}
                  className="w-4 h-4 accent-purple-600" />
                <span className="text-gray-700">只搜錄製座標附近</span>
              </label>
              <p className="text-[11px] text-gray-400 leading-relaxed pl-6 -mt-1">
                {data.cvSearchOnlyNear
                  ? '開啟：只在附近搜尋，不擴大到全螢幕（避免跨螢幕找錯位置）'
                  : '關閉：附近找不到 → 擴大到全螢幕 CV 搜尋'}
              </p>

              {/* CV 失敗退回座標 toggle（預設 false：失敗就停、不亂點）*/}
              <label className="flex items-center gap-2 text-sm cursor-pointer">
                <input type="checkbox" checked={data.cvCoordFallback === true}
                  onChange={e => onUpdate({ cvCoordFallback: e.target.checked })}
                  className="w-4 h-4 accent-purple-600" />
                <span className="text-gray-700">CV 失敗時退回錄製座標</span>
              </label>
              <p className="text-[11px] text-gray-400 leading-relaxed pl-6 -mt-1">
                {data.cvCoordFallback === true
                  ? '開啟：CV 完全找不到 → 退回原錄製座標硬點下去（對畫面穩定的場景多一層保險）'
                  : '關閉（預設）：CV 失敗就直接 FAIL、不亂點。選擇 CV 就代表位置可能有偏差，盲點座標反而更危險'}
              </p>

              {/* 觸發 hover toggle */}
              <label className="flex items-center gap-2 text-sm cursor-pointer">
                <input type="checkbox" checked={data.cvTriggerHover ?? true}
                  onChange={e => onUpdate({ cvTriggerHover: e.target.checked })}
                  className="w-4 h-4 accent-purple-600" />
                <span className="text-gray-700">比對前觸發 hover 效果</span>
              </label>
              <p className="text-[11px] text-gray-400 leading-relaxed pl-6 -mt-1">
                {(data.cvTriggerHover ?? true)
                  ? '開啟（建議）：先把游標移到錄製座標 + 等待，讓 Windows hover highlight 出現後再比對。'
                  : '關閉：跳過 hover 觸發、每次 click_image 會快一點。若錨點不含 hover 變色區域可關掉'}
              </p>

              {/* hover 等待 2 段 */}
              {(data.cvTriggerHover ?? true) && (
                <div>
                  <label className="text-xs text-gray-600 block mb-1.5">Hover 等待時間</label>
                  <div className="grid grid-cols-2 gap-1">
                    {[
                      { v: 200, label: '快', hint: '200ms，夠大多數 Windows UI' },
                      { v: 400, label: '保險', hint: '400ms，應付 fade-in 較慢的動畫或遠端桌面' },
                    ].map(opt => (
                      <button
                        key={opt.v}
                        type="button"
                        onClick={() => onUpdate({ cvHoverWaitMs: opt.v })}
                        title={opt.hint}
                        className={`px-2 py-1.5 rounded-lg text-xs font-medium transition-colors border ${
                          (data.cvHoverWaitMs ?? 200) === opt.v
                            ? 'bg-purple-500 text-white border-purple-500'
                            : 'bg-white text-gray-600 border-gray-200 hover:border-purple-300'
                        }`}
                      >
                        {opt.label} {opt.v}ms
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {/* 搜尋半徑 */}
              <div>
                <label className="text-xs text-gray-600 block mb-1.5">
                  附近搜尋半徑
                  <span className="text-gray-400 font-normal">
                    （實際搜尋 {(data.cvSearchRadius ?? 400) * 2}×{(data.cvSearchRadius ?? 400) * 2} px）
                  </span>
                </label>
                <input
                  type="number"
                  min={50}
                  max={2000}
                  step={50}
                  value={data.cvSearchRadius ?? 400}
                  onChange={e => {
                    const v = parseInt(e.target.value) || 400
                    onUpdate({ cvSearchRadius: Math.max(50, Math.min(2000, v)) })
                  }}
                  className={inputCls}
                />
                <p className="text-[11px] text-gray-400 mt-1">
                  視窗很少移動 → 可調小（150-200）更快更準；常跨螢幕 → 調大（600-800）
                </p>
              </div>
            </div>
          )}
        </div>

        {/* OCR 比對設定（摺疊，預設收折）*/}
        <div className="rounded-xl border border-gray-200 bg-gray-50/50 overflow-hidden">
          <button
            type="button"
            onClick={() => setOcrOpen(v => !v)}
            className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-gray-100/80 transition-colors"
          >
            {ocrOpen ? <ChevronUp className="w-3.5 h-3.5 text-gray-400" />
                    : <ChevronDown className="w-3.5 h-3.5 text-gray-400" />}
            <span className="text-xs font-semibold text-gray-500 uppercase tracking-wide flex-1">🔤 OCR 比對設定</span>
            <span className="text-[11px] text-gray-400 font-mono">
              門檻 {(data.ocrThreshold ?? 0.6).toFixed(2)}{data.ocrCvFallback ? ' · fallback→CV' : ''}
            </span>
          </button>
          {ocrOpen && (
            <div className="px-3 pb-3 space-y-3 border-t border-gray-200">
              <div className="pt-3" />
              {/* OCR 最小 conf 門檻 */}
              <div>
                <label className="text-xs text-gray-600 block mb-1.5">最小匹配信心</label>
                <div className="grid grid-cols-4 gap-1">
                  {[
                    { v: 0.6, label: '模糊', hint: '包含大小寫+去空白的模糊匹配（最寬）' },
                    { v: 0.8, label: '跨詞', hint: '允許 CJK 被 OCR 拆字後行層級拼接匹配' },
                    { v: 0.9, label: '詞包含', hint: '目標必須是某個 OCR word 的子字串' },
                    { v: 1.0, label: '精確', hint: 'OCR word 必須完全等於目標文字' },
                  ].map(opt => (
                    <button
                      key={opt.v}
                      type="button"
                      onClick={() => onUpdate({ ocrThreshold: opt.v })}
                      title={opt.hint}
                      className={`px-2 py-1.5 rounded-lg text-[11px] font-medium transition-colors border ${
                        (data.ocrThreshold ?? 0.6) === opt.v
                          ? 'bg-purple-500 text-white border-purple-500'
                          : 'bg-white text-gray-600 border-gray-200 hover:border-purple-300'
                      }`}
                    >
                      {opt.label} {opt.v.toFixed(1)}
                    </button>
                  ))}
                </div>
                <p className="text-[11px] text-gray-400 mt-1.5 leading-relaxed">
                  低於此 conf 視為沒找到。繁中被 OCR 拆字時，"跨詞 0.8" 才能從分字結果拼回原目標。
                </p>
              </div>

              {/* OCR 失敗時的 fallback 行為 */}
              <label className="flex items-center gap-2 text-sm cursor-pointer">
                <input type="checkbox" checked={data.ocrCvFallback === true}
                  onChange={e => onUpdate({ ocrCvFallback: e.target.checked })}
                  className="w-4 h-4 accent-purple-600" />
                <span className="text-gray-700">OCR 失敗時退回 CV 比對</span>
              </label>
              <p className="text-[11px] text-gray-400 leading-relaxed pl-6 -mt-1">
                {data.ocrCvFallback === true
                  ? '開啟：OCR 找不到 → 接著跑 CV 圖像比對鏈（gray→edge），CV 再失敗時是否退座標看上方 CV 設定'
                  : '關閉（預設）：OCR 失敗就直接 FAIL，不退到 CV 或座標。選擇 OCR 代表目標位置/樣式會變、CV 不適用'}
              </p>
            </div>
          )}
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">超時（秒）</label>
            <input type="number" value={data.timeout}
              onChange={e => onUpdate({ timeout: parseInt(e.target.value) || 300 })} className={inputCls} />
          </div>
          <div>
            <label className="text-xs font-semibold text-gray-500 uppercase tracking-wide block mb-1.5">重試次數</label>
            <input type="number" value={data.retry}
              onChange={e => onUpdate({ retry: parseInt(e.target.value) || 0 })} className={inputCls} />
            <p className="text-[10px] text-gray-500 mt-1 leading-relaxed">
              預設 0:桌面自動化重試會從動作 #1 重頭跑一遍、可能重複點擊、造成副作用(例如重複送單)。建議 0;確定所有動作 idempotent 才調大
            </p>
          </div>
        </div>

        <div className="p-2.5 bg-yellow-50 border border-yellow-200 rounded-lg text-[11px] text-yellow-800 leading-relaxed">
          <strong>⚠ 安全提醒</strong>:執行時滑鼠會實際操作系統。失控時<strong>連按兩次 <kbd className="px-1 py-0.5 bg-white border border-yellow-300 rounded font-mono text-[10px]">Esc</kbd>(500ms 內)</strong> 立即中止;備援機制是滑鼠甩到螢幕左上角 (0,0)。動作數上限 500。
        </div>
      </div>

      {/* 手動圈選錨點 Modal */}
      {editingAnchor !== null && data.actions && data.actions[editingAnchor] && (
        <AnchorEditorModal
          action={data.actions[editingAnchor]}
          actionIndex={editingAnchor}
          assetsDir={data.assetsDir || defaultAssetsDir}
          defaultSearchRadius={data.cvSearchRadius || 400}
          onApply={(patch) => applyAnchorPatch(editingAnchor, patch)}
          onClose={() => setEditingAnchor(null)}
        />
      )}
    </div>
  )
}
