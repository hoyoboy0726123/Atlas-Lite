'use client'
/**
 * AI 助手側邊欄。
 *
 * ## 為什麼看不到文字逐字出現
 * AiHub v0.9 沒有 streaming（實測改 response_type 五個模型全部回伺服器端錯誤），
 * 所以文字只能整段等。串出來的是**工具事件** —— 一次提問可能跑好幾輪工具、
 * 每輪 5-20 秒，把「正在讀哪個工作流」顯示出來，是沒有 token 串流時唯一能給
 * 使用者的「還活著」訊號。全部做完才顯示等於讓人盯二十幾秒白屏。
 *
 * ## 為什麼工具呼叫要顯示給使用者看
 * 助手會**直接改工作流**。把它動了什麼攤在畫面上，使用者才有機會喊停；
 * 會寫入的工具（mutating）另外標色 —— 「讀了什麼」跟「改了什麼」不能長一樣。
 */
import { useEffect, useRef, useState } from 'react'
import {
  Loader2, Send, Sparkles, Wrench, X, AlertTriangle, PenLine, Square,
} from 'lucide-react'
import { chatStatus, chatStream, type ChatEvent, type ChatMessage, type ChatStatus } from '@/lib/api'

interface ToolTrace {
  name: string
  mutating: boolean
  done: boolean
  preview?: string
}

interface Turn {
  role: 'user' | 'assistant'
  content: string
  tools?: ToolTrace[]
  error?: boolean
}

/** 工具名 → 人話。直接顯示函式名等於要使用者讀原始碼。 */
const TOOL_LABEL: Record<string, string> = {
  list_workflows: '列出工作流',
  get_workflow_yaml: '讀工作流內容',
  list_workflow_variables: '查有哪些變數',
  get_recent_runs: '看最近的執行紀錄',
  get_run_log: '讀執行 log',
  save_workflow_yaml: '覆寫整份工作流',
  patch_node_actions: '修改動作序列',
}

interface Props {
  open: boolean
  onClose: () => void
  /** 「問 AI」按鈕帶進來的當前節點狀態，助手要接著這個講 */
  seedContext?: string
  /** 帶了 seedContext 時要自動送出的第一句話 */
  seedQuestion?: string
  onSeedConsumed?: () => void
}

export default function AssistantPanel({
  open, onClose, seedContext, seedQuestion, onSeedConsumed,
}: Props) {
  const [status, setStatus] = useState<ChatStatus | null>(null)
  const [turns, setTurns] = useState<Turn[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  // seedContext 只在第一次提問帶上去 —— 之後對話歷史裡已經有了，
  // 每輪都塞一次會佔掉 context 且讓模型以為狀態不斷在變
  const ctxRef = useRef<string>('')

  useEffect(() => {
    if (!open) return
    chatStatus().then(setStatus).catch(() => setStatus({
      available: false, reason: '連不上後端', provider: '', model: '',
      data_stays_local: false,
    }))
  }, [open])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [turns, busy])

  const send = async (text: string, ctx?: string) => {
    const q = text.trim()
    if (!q || busy) return
    setInput('')
    const history: ChatMessage[] = [...turns, { role: 'user' as const, content: q }]
      .filter(t => !t.error)
      .map(t => ({ role: t.role, content: t.content }))

    setTurns(prev => [...prev, { role: 'user', content: q },
                                { role: 'assistant', content: '', tools: [] }])
    setBusy(true)
    const ac = new AbortController()
    abortRef.current = ac

    const patchLast = (fn: (t: Turn) => Turn) =>
      setTurns(prev => prev.map((t, i) => (i === prev.length - 1 ? fn(t) : t)))

    try {
      await chatStream({ messages: history, extra_context: ctx ?? '' }, (ev: ChatEvent) => {
        if (ev.type === 'tool_start') {
          patchLast(t => ({ ...t, tools: [...(t.tools ?? []),
            { name: ev.name, mutating: ev.mutating, done: false }] }))
        } else if (ev.type === 'tool_end') {
          patchLast(t => {
            const tools = [...(t.tools ?? [])]
            for (let i = tools.length - 1; i >= 0; i--) {
              if (tools[i].name === ev.name && !tools[i].done) {
                tools[i] = { ...tools[i], done: true, preview: ev.result_preview }
                break
              }
            }
            return { ...t, tools }
          })
        } else if (ev.type === 'done') {
          patchLast(t => ({ ...t, content: ev.reply }))
        } else if (ev.type === 'error') {
          patchLast(t => ({ ...t, content: ev.detail, error: true }))
        }
      }, ac.signal)
    } catch (e: any) {
      if (e?.name !== 'AbortError') {
        patchLast(t => ({ ...t, content: String(e?.message || e), error: true }))
      }
    } finally {
      setBusy(false)
      abortRef.current = null
    }
  }

  // 「問 AI」按鈕帶著狀態進來 → 自動送出第一句
  useEffect(() => {
    if (!open || !seedQuestion || busy) return
    ctxRef.current = seedContext ?? ''
    void send(seedQuestion, ctxRef.current)
    onSeedConsumed?.()
    // send 依賴 turns，放進 deps 會無限重跑
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, seedQuestion])

  if (!open) return null

  return (
    <div className="fixed right-0 top-0 h-full w-[420px] max-w-[92vw] z-40 flex flex-col
                    bg-white border-l border-gray-200 shadow-xl">
      {/* 標題列 */}
      <div className="flex items-center gap-2 px-3 py-2.5 border-b border-gray-200 shrink-0">
        <Sparkles className="w-4 h-4 text-indigo-600 shrink-0" />
        <span className="text-sm font-medium text-gray-800">AI 助手</span>
        {status?.available && (
          <span className={`text-[10px] px-1.5 py-0.5 rounded-full whitespace-nowrap ${
            status.data_stays_local
              ? 'bg-emerald-50 text-emerald-700 border border-emerald-200'
              : 'bg-amber-50 text-amber-700 border border-amber-200'}`}
            title={status.data_stays_local
              ? '這個模型跑在本機，對話內容不會離開這台電腦'
              : '這個模型在雲端，對話內容會送出去'}>
            {status.model}{status.data_stays_local ? '・地端' : '・雲端'}
          </span>
        )}
        <button type="button" onClick={onClose}
          className="ml-auto p-1 rounded hover:bg-gray-100 text-gray-500 shrink-0"
          title="關閉">
          <X className="w-4 h-4" />
        </button>
      </div>

      {/* 沒設定 → 講清楚缺什麼，不要只反灰 */}
      {status && !status.available && (
        <div className="m-3 p-3 rounded-lg bg-amber-50 border border-amber-200">
          <div className="flex items-start gap-2">
            <AlertTriangle className="w-4 h-4 text-amber-600 mt-0.5 shrink-0" />
            <div className="text-xs text-amber-900 leading-relaxed">
              <div className="font-medium mb-1">助手還不能用</div>
              <div>{status.reason}</div>
              <a href="/settings" className="inline-block mt-2 underline hover:no-underline">
                去設定頁設定 →
              </a>
            </div>
          </div>
        </div>
      )}

      {/* 對話 */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-3 py-3 space-y-3">
        {turns.length === 0 && status?.available && (
          <div className="text-xs text-gray-400 leading-relaxed py-6 text-center">
            卡住的時候就問 —— 助手看得到你的工作流，<br />
            能直接幫你改設定的就直接改。
          </div>
        )}
        {turns.map((t, i) => (
          <div key={i} className={t.role === 'user' ? 'flex justify-end' : ''}>
            {t.role === 'user' ? (
              <div className="max-w-[85%] px-3 py-2 rounded-2xl rounded-br-sm bg-indigo-600
                              text-white text-[13px] leading-relaxed whitespace-pre-wrap break-words">
                {t.content}
              </div>
            ) : (
              <div className="space-y-1.5">
                {/* 工具軌跡 —— 助手動了什麼要看得見 */}
                {(t.tools ?? []).map((tool, j) => (
                  <div key={j}
                    className={`flex items-center gap-1.5 px-2 py-1 rounded-lg text-[11px] border ${
                      tool.mutating
                        ? 'bg-amber-50 border-amber-200 text-amber-800'
                        : 'bg-gray-50 border-gray-200 text-gray-600'}`}
                    title={tool.preview}>
                    {tool.done
                      ? (tool.mutating
                          ? <PenLine className="w-3 h-3 shrink-0" />
                          : <Wrench className="w-3 h-3 shrink-0" />)
                      : <Loader2 className="w-3 h-3 shrink-0 animate-spin" />}
                    <span className="truncate">
                      {TOOL_LABEL[tool.name] ?? tool.name}
                    </span>
                    {tool.mutating && (
                      <span className="ml-auto shrink-0 text-[10px] whitespace-nowrap">會改資料</span>
                    )}
                  </div>
                ))}
                {t.content ? (
                  <div className={`px-3 py-2 rounded-2xl rounded-bl-sm text-[13px]
                                   leading-relaxed whitespace-pre-wrap break-words ${
                    t.error
                      ? 'bg-red-50 border border-red-200 text-red-800'
                      : 'bg-gray-100 text-gray-800'}`}>
                    {t.content}
                  </div>
                ) : busy && i === turns.length - 1 ? (
                  <div className="flex items-center gap-2 px-3 py-2 text-[12px] text-gray-400">
                    <Loader2 className="w-3.5 h-3.5 animate-spin shrink-0" />
                    {/* 沒有 token 串流，等待可能長達 20 秒 —— 明說才不會以為當掉 */}
                    <span>思考中（沒有逐字輸出，可能要等十幾秒）…</span>
                  </div>
                ) : null}
              </div>
            )}
          </div>
        ))}
      </div>

      {/* 輸入 */}
      <div className="border-t border-gray-200 p-2.5 shrink-0">
        <div className="flex items-end gap-2">
          <textarea
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send(input, ctxRef.current) }
            }}
            rows={2}
            disabled={!status?.available || busy}
            placeholder={status?.available ? '卡在哪？（Enter 送出，Shift+Enter 換行）' : '助手未設定'}
            className="flex-1 min-w-0 resize-none px-2.5 py-2 text-[13px] rounded-lg border
                       border-gray-200 focus:border-indigo-400 focus:outline-none
                       disabled:bg-gray-50 disabled:text-gray-400"
          />
          {busy ? (
            <button type="button" onClick={() => abortRef.current?.abort()}
              title="停止"
              className="shrink-0 p-2 rounded-lg bg-gray-100 hover:bg-gray-200 text-gray-600">
              <Square className="w-4 h-4" />
            </button>
          ) : (
            <button type="button" onClick={() => void send(input, ctxRef.current)}
              disabled={!status?.available || !input.trim()}
              title="送出"
              className="shrink-0 p-2 rounded-lg bg-indigo-600 hover:bg-indigo-700
                         disabled:bg-gray-200 disabled:text-gray-400 text-white">
              <Send className="w-4 h-4" />
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
