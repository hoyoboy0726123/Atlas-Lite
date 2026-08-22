'use client'

/**
 * 設定頁。
 *
 * Atlas 的設定頁有 3090 行 —— 模型選擇、副模型、thinking 模式、skill 套件、
 * MCP server、subagent 角色、長期記憶、網路搜尋、沙盒模式。那些在 Atlas-Lite
 * 全都不存在，所以這裡只剩：路徑、Telegram 通知、桌面自動化、視覺模型、AI 助手、Secrets。
 */
import { useCallback, useEffect, useState } from 'react'
import Link from 'next/link'
import { ArrowLeft, Check, Eye, EyeOff, Loader2, Plus, Trash2 } from 'lucide-react'
import { toast, Toaster } from 'sonner'

import {
  deleteSecret, getComputerUseSettings, getEnvPaths, getGroundingStatus,
  getLlmSettings, getNotificationSettings, getVlmSettings, listLlmModels,
  listSecrets, probeLlm, probeVlm, saveComputerUseSettings,
  saveNotificationSettings, saveVlmSettings, setSecret, updateLlmSettings,
  type ComputerUseSettings, type EnvPaths, type GroundingStatus,
  type LlmSettings, type NotificationSettings, type SecretMeta, type VlmSettings,
} from '@/lib/api'

const cardCls = 'bg-white rounded-2xl border border-gray-200 shadow-sm overflow-hidden'
const inputCls =
  'w-full border border-gray-200 rounded-lg px-3 py-2 text-sm outline-none focus:border-indigo-400 focus:ring-1 focus:ring-indigo-400/20'

function Section({ title, desc, children }: {
  title: string; desc?: string; children: React.ReactNode
}) {
  return (
    <div className={cardCls}>
      <div className="px-5 py-4 border-b border-gray-100">
        <h2 className="font-semibold text-gray-800">{title}</h2>
        {desc && <p className="text-xs text-gray-500 mt-1 leading-relaxed">{desc}</p>}
      </div>
      <div className="p-5 space-y-4">{children}</div>
    </div>
  )
}

export default function SettingsPage() {
  return (
    <div className="h-full overflow-y-auto bg-gray-50">
      <Toaster richColors position="top-right" />
      <div className="max-w-3xl mx-auto px-6 py-8 space-y-5">
        <div className="flex items-center gap-3">
          <Link href="/pipeline"
            className="p-2 rounded-lg text-gray-400 hover:text-gray-700 hover:bg-gray-100 transition-colors">
            <ArrowLeft className="w-4 h-4" />
          </Link>
          <h1 className="text-xl font-bold text-gray-900">設定</h1>
        </div>

        <PathsSection />
        <NotificationSection />
        <ComputerUseSection />
        <VlmSection />
        <LlmSection />
        <SecretsSection />
      </div>
    </div>
  )
}

// ── 路徑 ─────────────────────────────────────────────────────────────

function PathsSection() {
  const [paths, setPaths] = useState<EnvPaths | null>(null)
  useEffect(() => { getEnvPaths().then(setPaths).catch(() => {}) }, [])

  const rows: Array<[string, string]> = paths ? [
    ['資料庫與設定', paths.data_dir],
    ['工作流產出', paths.workflow_dir],
    ['執行 Log', paths.log_dir],
    ['你的腳本專案', paths.external_projects_dir],
    ['時區', paths.timezone],
  ] : []

  return (
    <Section title="路徑" desc="東西存在哪。要改資料目錄請在 .env 設 ATLASLITE_DATA。">
      {!paths ? (
        <p className="text-sm text-gray-400">載入中…</p>
      ) : (
        <dl className="space-y-2">
          {rows.map(([k, v]) => (
            <div key={k} className="flex items-baseline gap-3 text-sm">
              <dt className="w-28 shrink-0 text-gray-500">{k}</dt>
              <dd className="font-mono text-xs text-gray-700 break-all">{v}</dd>
            </div>
          ))}
        </dl>
      )}
    </Section>
  )
}

// ── Telegram 通知 ────────────────────────────────────────────────────

function NotificationSection() {
  const [s, setS] = useState<NotificationSettings | null>(null)
  const [token, setToken] = useState('')
  const [chatId, setChatId] = useState('')
  const [showToken, setShowToken] = useState(false)
  const [saving, setSaving] = useState(false)

  const load = useCallback(() => {
    getNotificationSettings().then(d => { setS(d); setChatId(d.telegram_chat_id) }).catch(() => {})
  }, [])
  useEffect(load, [load])

  const save = async (patch: Parameters<typeof saveNotificationSettings>[0]) => {
    setSaving(true)
    try {
      const d = await saveNotificationSettings(patch)
      setS(d)
      setChatId(d.telegram_chat_id)
      setToken('')
      toast.success('已儲存')
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '儲存失敗')
    } finally { setSaving(false) }
  }

  if (!s) return <Section title="Telegram 通知"><p className="text-sm text-gray-400">載入中…</p></Section>

  return (
    <Section
      title="Telegram 通知"
      desc="選用。設了之後，工作流暫停等人確認、或步驟失敗時會推播到手機，按鈕可以直接決定重試 / 跳過 / 中止。沒設也能用，只是要回到網頁上按。"
    >
      <div>
        <label className="text-xs font-medium text-gray-500 block mb-1.5">Bot Token</label>
        <div className="flex gap-2">
          <input
            type={showToken ? 'text' : 'password'}
            value={token}
            onChange={e => setToken(e.target.value)}
            placeholder={s.telegram_bot_token_set
              ? `已設定（${s.telegram_bot_token_masked}）—— 要換再填`
              : '從 @BotFather 取得'}
            className={inputCls}
          />
          <button type="button" onClick={() => setShowToken(v => !v)}
            className="px-2.5 rounded-lg border border-gray-200 text-gray-400 hover:text-gray-700">
            {showToken ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
          </button>
        </div>
      </div>

      <div>
        <label className="text-xs font-medium text-gray-500 block mb-1.5">Chat ID</label>
        <input value={chatId} onChange={e => setChatId(e.target.value)}
          placeholder="跟 bot 說一句話後，開 /getUpdates 就看得到" className={inputCls} />
      </div>

      <button
        onClick={() => save({
          ...(token.trim() ? { telegram_bot_token: token.trim() } : {}),
          telegram_chat_id: chatId.trim(),
        })}
        disabled={saving}
        className="px-4 py-2 text-sm bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-60 inline-flex items-center gap-2"
      >
        {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Check className="w-3.5 h-3.5" />}
        儲存
      </button>

      <label className="flex items-start gap-2.5 pt-2 border-t border-gray-100 cursor-pointer">
        <input
          type="checkbox"
          checked={s.telegram_remote_control}
          onChange={e => save({ telegram_remote_control: e.target.checked })}
          className="mt-0.5 w-4 h-4 rounded accent-indigo-600"
        />
        <span className="text-sm">
          <span className="font-medium text-gray-800">允許從 Telegram 啟動工作流</span>
          <span className="block text-xs text-gray-500 leading-relaxed mt-0.5">
            開了之後，上面那個 chat 可以用 /menu 直接在<strong>你這台電腦上</strong>執行工作流
            （含桌面自動化）。預設關閉是刻意的 —— 只有你自己會用手機遙控時才開。
          </span>
        </span>
      </label>
    </Section>
  )
}

// ── 桌面自動化 ───────────────────────────────────────────────────────

function ComputerUseSection() {
  const [s, setS] = useState<ComputerUseSettings | null>(null)
  const [g, setG] = useState<GroundingStatus | null>(null)

  useEffect(() => {
    getComputerUseSettings().then(setS).catch(() => {})
    getGroundingStatus().then(setG).catch(() => {})
  }, [])

  const save = async (patch: Partial<ComputerUseSettings>) => {
    try {
      setS(await saveComputerUseSettings(patch))
      toast.success('已儲存')
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '儲存失敗')
    }
  }

  if (!s) return <Section title="桌面自動化"><p className="text-sm text-gray-400">載入中…</p></Section>

  return (
    <Section title="桌面自動化">
      <label className="flex items-start gap-2.5 cursor-pointer">
        <input
          type="checkbox"
          checked={s.auto_minimize_for_computer_use}
          onChange={e => save({ auto_minimize_for_computer_use: e.target.checked })}
          className="mt-0.5 w-4 h-4 rounded accent-indigo-600"
        />
        <span className="text-sm">
          <span className="font-medium text-gray-800">執行前自動縮小這個視窗</span>
          <span className="block text-xs text-gray-500 leading-relaxed mt-0.5">
            含桌面自動化節點的工作流開始時把瀏覽器縮小、結束後還原，避免擋住要操作的程式。
          </span>
        </span>
      </label>

      <div className="pt-3 border-t border-gray-100">
        <div className="flex items-center gap-2 mb-2">
          <span className="text-sm font-medium text-gray-800">地端 GUI 定位模型</span>
          {g && (
            <span className={`text-[11px] px-2 py-0.5 rounded-full ${
              g.available ? 'bg-emerald-100 text-emerald-700' : 'bg-gray-100 text-gray-500'
            }`}>
              {g.available ? `可用 · ${g.precision} · ${g.vram_gb}GB` : '不可用'}
            </span>
          )}
        </div>
        {g && !g.available ? (
          <p className="text-xs text-gray-500 leading-relaxed bg-gray-50 border border-gray-200 rounded-lg px-3 py-2">
            {g.reason}
            <br />
            <span className="font-mono text-[11px]">{g.install_hint}</span>
          </p>
        ) : (
          <>
            <label className="text-xs font-medium text-gray-500 block mb-1.5">推論精度</label>
            <div className="flex gap-2">
              {(['auto', 'fp16', 'int4'] as const).map(v => (
                <button key={v} type="button" onClick={() => save({ grounding_precision: v })}
                  className={`px-3 py-1.5 rounded-lg text-sm border transition-colors ${
                    s.grounding_precision === v
                      ? 'bg-indigo-600 text-white border-indigo-600'
                      : 'text-gray-600 border-gray-200 hover:border-indigo-400'
                  }`}
                >{v === 'auto' ? '自動' : v}</button>
              ))}
            </div>
            <p className="text-xs text-gray-500 mt-1.5 leading-relaxed">
              自動 = 依目前可用的顯卡記憶體選（≥11GB 用 fp16，否則 int4）。
              int4 記憶體約 3.7GB、精度幾乎一樣，只是慢一點。改了會重新載入模型。
            </p>
          </>
        )}
      </div>
    </Section>
  )
}

// ── 視覺模型（選用）─────────────────────────────────────────────────
// 只服務桌面自動化的「描述→OCR」模式。沒設定的話所有其他功能照常運作，
// 面板上那個選項會反灰 —— 這是刻意的：藏起來使用者永遠不知道有這功能。

const VLM_PROVIDER_LABEL: Record<string, string> = {
  ollama: 'Ollama（地端，免金鑰）',
  openai: 'OpenAI',
  groq: 'Groq',
  gemini: 'Google Gemini',
  anthropic: 'Anthropic',
}
const VLM_MODEL_PLACEHOLDER: Record<string, string> = {
  ollama: 'qwen2.5vl:7b',
  openai: 'gpt-4o-mini',
  groq: 'meta-llama/llama-4-scout-17b-16e-instruct',
  gemini: 'gemini-2.5-flash',
  anthropic: 'claude-sonnet-5',
}
const VLM_ENV_NAME: Record<string, string> = {
  openai: 'OPENAI_API_KEY',
  groq: 'GROQ_API_KEY',
  gemini: 'GEMINI_API_KEY',
  anthropic: 'ANTHROPIC_API_KEY',
}

function VlmSection() {
  const [v, setV] = useState<VlmSettings | null>(null)
  const [model, setModel] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [showKey, setShowKey] = useState(false)
  const [probing, setProbing] = useState(false)
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    getVlmSettings().then(x => {
      setV(x); setModel(x.vlm_model); setBaseUrl(x.vlm_base_url)
    }).catch(() => {})
  }, [])
  useEffect(load, [load])

  const provider = v?.vlm_provider || ''
  const needsKey = provider !== '' && provider !== 'ollama'

  const save = async (patch: Parameters<typeof saveVlmSettings>[0]) => {
    setBusy(true)
    try {
      const next = await saveVlmSettings(patch)
      setV(next); setModel(next.vlm_model); setBaseUrl(next.vlm_base_url)
      if ('vlm_api_key' in patch) setApiKey('')
      toast.success('已儲存')
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '儲存失敗')
    } finally { setBusy(false) }
  }

  const runProbe = async () => {
    setProbing(true)
    try {
      const r = await probeVlm()
      if (r.ok) toast.success(`可以看圖（模型回「${r.answer}」）`)
      else toast.error(r.reason || '測試失敗', { description: r.hint || undefined, duration: 12000 })
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '測試失敗')
    } finally { setProbing(false) }
  }

  return (
    <Section
      title="視覺模型（選用）"
      desc="只給桌面自動化的「描述→OCR」用：畫面上的文字是動態的、錄製時不知道會是什麼字（訂單編號、當日日期）時，讓模型看圖告訴系統目標的實際文字，再交給 OCR 定位。模型不負責座標，所以不會點到隔壁那顆。不設定的話那個選項會反灰，其他功能完全不受影響。"
    >
      <div>
        <label className="text-sm text-gray-700 block mb-1.5">供應商</label>
        <div className="flex flex-wrap gap-1.5">
          {['', 'ollama', 'openai', 'groq', 'gemini', 'anthropic'].map(p => {
            // .env 已經有這家金鑰的話標一顆綠點 —— 使用者才知道選過去就能直接用，
            // 不用再回設定頁填一次
            const hasEnv = p !== '' && p !== 'ollama' && (v?.env_keys || []).includes(p)
            return (
              <button key={p || 'none'} type="button" disabled={busy}
                onClick={() => save({ vlm_provider: p })}
                title={hasEnv ? `.env 已有 ${VLM_ENV_NAME[p]}，選過去就能用` : undefined}
                className={`text-xs px-2.5 py-1.5 rounded-lg border transition-colors disabled:opacity-50 ${
                  provider === p
                    ? 'bg-indigo-500 text-white border-indigo-500'
                    : 'bg-white text-gray-600 border-gray-200 hover:border-indigo-300'
                }`}
              >{p === '' ? '不使用' : VLM_PROVIDER_LABEL[p]}{hasEnv && ' ●'}</button>
            )
          })}
        </div>
        {provider === 'ollama' && (
          <p className="text-xs text-emerald-700 mt-1.5 leading-relaxed">
            地端推論：不需要 API 金鑰，<strong>截圖不會離開這台機器</strong>。
            需要先 <code className="bg-gray-100 px-1 rounded">ollama pull qwen2.5vl:7b</code>
            （一般文字模型看不懂圖，一定要選多模態的）。
          </p>
        )}
        {needsKey && (
          <p className="text-xs text-amber-600 mt-1.5 leading-relaxed">
            ⚠ 雲端模式會<strong>把螢幕截圖上傳到 {VLM_PROVIDER_LABEL[provider]}</strong> 並依用量計費。
            介意的話改用 Ollama。
          </p>
        )}
      </div>

      {provider !== '' && (
        <>
          <div>
            <label className="text-sm text-gray-700 block mb-1.5">模型名稱</label>
            <div className="flex gap-2">
              <input className={inputCls} value={model} onChange={e => setModel(e.target.value)}
                placeholder={VLM_MODEL_PLACEHOLDER[provider] || ''} />
              <button type="button" disabled={busy || model === v?.vlm_model}
                onClick={() => save({ vlm_model: model.trim() })}
                className="shrink-0 px-3 py-2 rounded-lg bg-indigo-500 text-white text-sm hover:bg-indigo-600 disabled:opacity-40">
                儲存
              </button>
            </div>
            <p className="text-xs text-gray-500 mt-1.5">
              必須是<strong>看得懂圖片</strong>的多模態模型。
              {provider === 'ollama' && (
                <> 實測建議 <code className="bg-gray-100 px-1 rounded">qwen2.5vl:7b</code>
                （讀對 19/20、每次約 15 秒）。<strong>不要用 qwen3-vl:8b-instruct</strong> ——
                版本較新卻只有 4/8 且慢 3 倍。</>
              )}
            </p>
          </div>

          {needsKey && (
            <div>
              <label className="text-sm text-gray-700 block mb-1.5">
                API 金鑰
                {v?.key_source === 'settings' && (
                  <span className="ml-2 text-xs text-emerald-600">已設定 {v.vlm_api_key_masked}</span>
                )}
                {v?.key_source === 'env' && (
                  <span className="ml-2 text-xs text-emerald-600">
                    ✓ 已從 .env 的 {VLM_ENV_NAME[provider]} 讀到，不用再填
                  </span>
                )}
              </label>
              <div className="flex gap-2">
                <div className="relative flex-1">
                  <input className={inputCls} type={showKey ? 'text' : 'password'} value={apiKey}
                    onChange={e => setApiKey(e.target.value)}
                    placeholder={v?.key_source === 'env'
                      ? '（留空 = 用 .env 那把；填了會蓋過去）'
                      : v?.vlm_api_key_set ? '（留空 = 不變更）' : 'sk-…'} />
                  <button type="button" onClick={() => setShowKey(k => !k)}
                    className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600">
                    {showKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                  </button>
                </div>
                <button type="button" disabled={busy || !apiKey.trim()}
                  onClick={() => save({ vlm_api_key: apiKey.trim() })}
                  className="shrink-0 px-3 py-2 rounded-lg bg-indigo-500 text-white text-sm hover:bg-indigo-600 disabled:opacity-40">
                  儲存
                </button>
                {v?.vlm_api_key_set && (
                  <button type="button" disabled={busy}
                    onClick={() => save({ vlm_api_key: '' })}
                    title=".env 有設的話會退回用 .env 那把"
                    className="shrink-0 px-3 py-2 rounded-lg border border-gray-200 text-gray-500 text-sm hover:bg-gray-50 disabled:opacity-40">
                    清除
                  </button>
                )}
              </div>
              <p className="text-xs text-gray-500 mt-1.5">
                金鑰是<strong>每家一把</strong>：`.env` 可以同時放 OPENAI_API_KEY、GEMINI_API_KEY…，
                切供應商時自動取對應那把。設定頁填的優先，清掉就退回 .env。
                存在本機設定檔，永遠不會回傳給前端顯示完整內容。
              </p>
            </div>
          )}

          <div>
            <label className="text-sm text-gray-700 block mb-1.5">
              自訂端點 <span className="text-xs text-gray-400">（選填，自架或走代理才需要）</span>
            </label>
            <div className="flex gap-2">
              <input className={inputCls} value={baseUrl} onChange={e => setBaseUrl(e.target.value)}
                placeholder={provider === 'ollama' ? 'http://localhost:11434/v1' : '留空使用官方端點'} />
              <button type="button" disabled={busy || baseUrl === v?.vlm_base_url}
                onClick={() => save({ vlm_base_url: baseUrl.trim() })}
                className="shrink-0 px-3 py-2 rounded-lg bg-indigo-500 text-white text-sm hover:bg-indigo-600 disabled:opacity-40">
                儲存
              </button>
            </div>
          </div>

          <div className="flex items-center gap-3 pt-1">
            <button type="button" disabled={probing || !v?.available} onClick={runProbe}
              className="px-3 py-2 rounded-lg border border-indigo-300 text-indigo-600 text-sm hover:bg-indigo-50 disabled:opacity-40 flex items-center gap-1.5">
              {probing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
              測試連線
            </button>
            {/* 光是「設定填滿了」不代表能用 —— 純文字模型會安靜地忽略圖片，
                那比連不上更糟，所以測試是真的送一張圖去問顏色 */}
            <span className="text-xs text-gray-500">
              {v?.available
                ? '送一張小圖問顏色，確認模型真的讀得到圖片。'
                : `尚不可用：${v?.reason || '設定未完成'}`}
            </span>
          </div>
        </>
      )}
    </Section>
  )
}

// ── LLM（AI 助手）──────────────────────────────────────────────────
// 只有兩家。刻意的 —— 不搬 Atlas 那一整包 langchain 依賴。
// 沒設定的話助手反灰，工作流的錄製與執行完全不受影響。

const LLM_PROVIDER_LABEL: Record<string, string> = {
  ollama: 'Ollama（地端，免金鑰）',
  aihub: 'AiHub（華碩內部）',
}

function LlmSection() {
  const [v, setV] = useState<LlmSettings | null>(null)
  const [model, setModel] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [models, setModels] = useState<string[]>([])
  const [modelsNote, setModelsNote] = useState('')
  const [probing, setProbing] = useState(false)
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    getLlmSettings().then(x => {
      setV(x); setModel(x.llm_model); setBaseUrl(x.llm_base_url)
    }).catch(() => {})
  }, [])
  useEffect(load, [load])

  const provider = v?.llm_provider || ''

  // 模型清單：Ollama 問本機拿真實清單，AiHub 回白名單。
  // 讓使用者用選的 —— 打錯一個字（qwen3:8B）就是「找不到模型」。
  useEffect(() => {
    if (!provider) { setModels([]); setModelsNote(''); return }
    listLlmModels().then(r => {
      setModels(r.models || [])
      setModelsNote(r.ok ? (r.note || '') : (r.error || ''))
    }).catch(() => { setModels([]); setModelsNote('') })
  }, [provider])

  const save = async (patch: Parameters<typeof updateLlmSettings>[0]) => {
    setBusy(true)
    try {
      const next = await updateLlmSettings(patch)
      setV(next); setModel(next.llm_model); setBaseUrl(next.llm_base_url)
      toast.success('已儲存')
    } catch (e: any) {
      toast.error(String(e?.message || e))
    } finally { setBusy(false) }
  }

  const probe = async () => {
    setProbing(true)
    try {
      const r = await probeLlm()
      if (r.ok) {
        toast.success(`可以用（${r.elapsed_ms} ms，回「${r.reply}」）`, {
          // 三態：本機 / 華碩內部 / 外部廠商。「不出這台電腦」跟
          // 「不出公司」是不同等級的保證，不能都寫成「地端」。
          description: r.data_scope === 'local'
            ? '模型跑在這台電腦上，內容不會離開本機'
            : r.data_scope === 'internal'
              ? '模型跑在華碩自建的伺服器上：內容會離開這台電腦，但不會送到外部廠商'
              : '⚠ 模型在外部雲端廠商，對話內容會送到公司外',
        })
      } else {
        // 失敗訊息是寫給使用者看的（缺什麼、下一步怎麼做）—— 原樣顯示
        toast.error('不能用', { description: r.error, duration: 12000 })
      }
    } catch (e: any) {
      toast.error(String(e?.message || e))
    } finally { setProbing(false) }
  }

  return (
    <Section
      title="AI 助手"
      desc="卡住的時候可以問它。它看得到你的工作流，能直接幫你改動作設定。沒設定的話助手反灰，其他功能完全不受影響。"
    >
      {/* 供應商 */}
      <div>
        <label className="block text-sm text-gray-700 mb-1.5">供應商</label>
        <div className="flex flex-wrap gap-2">
          {['', 'ollama', 'aihub'].map(p => (
            <button key={p} type="button" disabled={busy}
              onClick={() => save({ llm_provider: p })}
              className={`px-3 py-1.5 rounded-lg text-sm border whitespace-nowrap transition-colors ${
                provider === p
                  ? 'bg-indigo-600 border-indigo-600 text-white'
                  : 'bg-white border-gray-200 text-gray-700 hover:border-gray-300'}`}>
              {p === '' ? '不使用' : LLM_PROVIDER_LABEL[p]}
            </button>
          ))}
        </div>
      </div>

      {provider === 'ollama' && (
        <p className="text-xs text-emerald-700 bg-emerald-50 border border-emerald-200
                      rounded-lg px-3 py-2 leading-relaxed">
          ✓ 地端模式：對話內容<strong>不會離開這台電腦</strong>。
          需要先啟動 Ollama（<code className="font-mono">ollama serve</code>）並
          <code className="font-mono">ollama pull</code> 你要用的模型。
        </p>
      )}
      {provider === 'aihub' && (
        <div className="text-xs text-amber-800 bg-amber-50 border border-amber-200
                        rounded-lg px-3 py-2 leading-relaxed space-y-1">
          <p>
            AiHub 底下掛了多家模型，只有 <code className="font-mono">gpt-oss</code> 是
            <strong>華碩自建部署</strong>；其餘（Azure / Google / AWS）的請求內容會送到外部廠商。
          </p>
          <p>
            本專案在程式層鎖定 gpt-oss，要放寬得改
            <code className="font-mono"> backend/engine/llm.py </code>的白名單 —— 這是刻意的摩擦。
          </p>
          <p>
            金鑰請放 <code className="font-mono">.env</code> 的
            <code className="font-mono"> AIHUB_API_KEY</code>，或用下面的
            Secrets 保險箱存成 <code className="font-mono">AIHUB_API_KEY</code>。
            {v?.key_source
              ? <span className="text-emerald-700 font-medium">
                  {' '}✓ 已從 {v.key_source === 'vault' ? '保險箱' : '.env'} 讀到。
                </span>
              : <span className="text-red-700 font-medium">{' '}目前還沒有金鑰。</span>}
          </p>
        </div>
      )}

      {provider && (
        <>
          {/* 模型 */}
          <div>
            <label className="block text-sm text-gray-700 mb-1.5">模型</label>
            {models.length > 0 && (
              <div className="flex flex-wrap gap-1.5 mb-2">
                {models.map(m => (
                  <button key={m} type="button" disabled={busy}
                    onClick={() => { setModel(m); void save({ llm_model: m }) }}
                    className={`px-2 py-1 rounded-md text-xs font-mono border whitespace-nowrap
                                transition-colors ${
                      v?.llm_model === m
                        ? 'bg-indigo-50 border-indigo-300 text-indigo-700'
                        : 'bg-white border-gray-200 text-gray-600 hover:border-gray-300'}`}>
                    {m}
                  </button>
                ))}
              </div>
            )}
            <div className="flex gap-2">
              <input className={inputCls} value={model}
                onChange={e => setModel(e.target.value)}
                placeholder={provider === 'ollama' ? 'qwen3:8b' : 'gpt-oss'} />
              <button type="button" disabled={busy || model === v?.llm_model}
                onClick={() => save({ llm_model: model.trim() })}
                className="shrink-0 px-3 py-2 rounded-lg text-sm bg-gray-900 text-white
                           disabled:bg-gray-200 disabled:text-gray-400 whitespace-nowrap">
                儲存
              </button>
            </div>
            {modelsNote && (
              <p className="text-xs text-gray-400 mt-1.5 leading-relaxed">{modelsNote}</p>
            )}
          </div>

          {/* AiHub 環境 */}
          {provider === 'aihub' && (
            <div>
              <label className="block text-sm text-gray-700 mb-1.5">環境</label>
              <div className="flex gap-2">
                {(v?.aihub_envs || ['prod', 'stage']).map(e => (
                  <button key={e} type="button" disabled={busy}
                    onClick={() => save({ llm_aihub_env: e })}
                    className={`px-3 py-1.5 rounded-lg text-sm border whitespace-nowrap ${
                      v?.llm_aihub_env === e
                        ? 'bg-indigo-600 border-indigo-600 text-white'
                        : 'bg-white border-gray-200 text-gray-700 hover:border-gray-300'}`}>
                    {e === 'prod' ? '正式區' : '測試區'}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* 端點（自架 / 代理才要填）*/}
          <div>
            <label className="block text-sm text-gray-700 mb-1.5">
              端點<span className="text-gray-400 text-xs
              ">（留空用內建預設；自架 / 走代理才填）</span>
            </label>
            <div className="flex gap-2">
              <input className={inputCls} value={baseUrl}
                onChange={e => setBaseUrl(e.target.value)}
                placeholder={provider === 'ollama' ? 'http://localhost:11434' : '（用內建預設）'} />
              <button type="button" disabled={busy || baseUrl === v?.llm_base_url}
                onClick={() => save({ llm_base_url: baseUrl.trim() })}
                className="shrink-0 px-3 py-2 rounded-lg text-sm bg-gray-900 text-white
                           disabled:bg-gray-200 disabled:text-gray-400 whitespace-nowrap">
                儲存
              </button>
            </div>
          </div>

          {/* 測試連線 —— 只看設定填沒填分不出「金鑰過期」「Ollama 沒開」「模型沒 pull」 */}
          <div className="flex items-center gap-3 pt-1">
            <button type="button" onClick={probe} disabled={probing}
              className="px-3 py-2 rounded-lg text-sm bg-indigo-600 text-white
                         hover:bg-indigo-700 disabled:bg-gray-200 disabled:text-gray-400
                         inline-flex items-center gap-1.5 whitespace-nowrap">
              {probing ? <Loader2 className="w-3.5 h-3.5 animate-spin shrink-0" />
                       : <Check className="w-3.5 h-3.5 shrink-0" />}
              <span>測試連線</span>
            </button>
            {v && !v.available && (
              <span className="text-xs text-amber-700 leading-relaxed">{v.reason}</span>
            )}
            {v?.available && (
              <span className="text-xs text-emerald-700">設定完整，助手已可使用</span>
            )}
          </div>
        </>
      )}
    </Section>
  )
}

// ── Secrets ──────────────────────────────────────────────────────────

function SecretsSection() {
  const [list, setList] = useState<SecretMeta[]>([])
  const [name, setName] = useState('')
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => { listSecrets().then(setList).catch(() => {}) }, [])
  useEffect(load, [load])

  const add = async () => {
    if (!name.trim() || !value.trim()) { toast.error('名稱和值都要填'); return }
    setBusy(true)
    try {
      await setSecret(name.trim(), value)
      setName(''); setValue('')
      load()
      toast.success('已儲存')
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '儲存失敗')
    } finally { setBusy(false) }
  }

  const del = async (n: string) => {
    if (!confirm(`刪除 secret「${n}」？引用它的工作流會直接報錯。`)) return
    try { await deleteSecret(n); load(); toast.success('已刪除') }
    catch { toast.error('刪除失敗') }
  }

  return (
    <Section
      title="Secrets"
      desc="API key、密碼之類的東西存這裡，工作流用 {{ secrets.名稱 }} 引用，不用把它們明文寫進 YAML。值加密後存在本機資料庫，前端永遠讀不回明文。"
    >
      <div className="flex gap-2">
        <input value={name} onChange={e => setName(e.target.value)}
          placeholder="名稱（英數與 _ -）" className={`${inputCls} flex-1`} />
        <input type="password" value={value} onChange={e => setValue(e.target.value)}
          placeholder="值" className={`${inputCls} flex-1`} />
        <button onClick={add} disabled={busy}
          className="px-3 rounded-lg bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-60">
          {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
        </button>
      </div>

      {list.length === 0 ? (
        <p className="text-sm text-gray-400">還沒有任何 secret</p>
      ) : (
        <ul className="divide-y divide-gray-100 border border-gray-100 rounded-lg">
          {list.map(sec => (
            <li key={sec.name} className="flex items-center gap-3 px-3 py-2">
              <code className="text-sm text-gray-800 flex-1">{`{{ secrets.${sec.name} }}`}</code>
              <button onClick={() => del(sec.name)}
                className="p-1 text-gray-300 hover:text-red-500 transition-colors">
                <Trash2 className="w-4 h-4" />
              </button>
            </li>
          ))}
        </ul>
      )}

      <p className="text-xs text-gray-500 leading-relaxed">
        誠實說明：加密金鑰檔就在同一台機器上（data/.vault_key）。這擋的是「資料庫檔案
        或匯出包外流時裡面沒有明文」，不是擋拿得到你電腦的人。
      </p>
    </Section>
  )
}
