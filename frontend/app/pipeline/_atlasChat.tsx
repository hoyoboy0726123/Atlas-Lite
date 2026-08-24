'use client'
/**
 * AI 助手聊天面板 —— 從 Atlas 的 _atlasChat 移植（sidebar 模式）。
 *
 * 跟 Atlas 相同：左下角收合列、展開蓋 sidebar 75%、Markdown 渲染、
 * 工具呼叫進度塊、對話歷史跟著工作流走（切工作流就切對話）。
 *
 * 跟 Atlas 不同的兩點（都是 Lite 的後端條件不同，不是設計取捨）：
 * - 沒有逐字串流：AiHub v0.9 沒有 streaming，文字只能整段等。串的是工具事件，
 *   等待期間泡泡顯示工具進度 + 打字游標。
 * - 沒有「套用 YAML」按鈕：Lite 的助手直接用 patch_node_actions /
 *   save_workflow_yaml 工具寫入（兩步核准），不產 YAML 給使用者手動套。
 */
import { useState, useRef, useEffect } from 'react'
import { Bot, ChevronUp, ChevronDown, Send, Loader2 } from 'lucide-react'
import { toast } from 'sonner'
import ReactMarkdown from 'react-markdown'
import rehypeRaw from 'rehype-raw'
// GFM：表格 / 刪除線 / 工作清單。gpt-oss 很愛用表格回答，
// 沒這個的話 Markdown 表格會原字直出一排 | 符號。
import remarkGfm from 'remark-gfm'
import { useWorkflowStore } from './_store'
import {
  chatStream, chatStatus, type ChatStatus,
  getWorkflowChat, appendWorkflowChat, clearWorkflowChat,
} from '@/lib/api'

// ── LaTeX → Unicode（跟 Atlas 同一份）──────────────────────
// 沒裝 KaTeX、ReactMarkdown 會原字顯示一坨 "$\rightarrow$" 很醜。
// 渲染前把常見 LaTeX 命令換成 Unicode；沒 cover 的至少剝掉錢字號。
const _LATEX_CMD_TO_UNICODE: Record<string, string> = {
  rightarrow: '→', leftarrow: '←', Rightarrow: '⇒', Leftarrow: '⇐',
  to: '→', gets: '←', leftrightarrow: '↔', Leftrightarrow: '⇔',
  uparrow: '↑', downarrow: '↓', updownarrow: '↕',
  times: '×', div: '÷', pm: '±', mp: '∓',
  cdot: '·', cdots: '⋯', ldots: '…', dots: '…',
  leq: '≤', le: '≤', geq: '≥', ge: '≥', neq: '≠', ne: '≠',
  approx: '≈', equiv: '≡',
  alpha: 'α', beta: 'β', gamma: 'γ', delta: 'δ', epsilon: 'ε',
  theta: 'θ', lambda: 'λ', mu: 'μ', pi: 'π', sigma: 'σ', tau: 'τ',
  phi: 'φ', omega: 'ω',
  infty: '∞', forall: '∀', exists: '∃', in: '∈', notin: '∉',
  subset: '⊂', supset: '⊃', cup: '∪', cap: '∩',
  text: '', mathrm: '', mathbf: '', mathit: '',
}

export function cleanLatexInChat(text: string): string {
  if (!text || (!text.includes('$') && !text.includes('\\'))) return text
  let out = text.replace(/\$([^\$\n]+?)\$/g, (_m, body: string) => {
    return body.replace(/\\([a-zA-Z]+)/g, (_full, cmd: string) =>
      _LATEX_CMD_TO_UNICODE[cmd] !== undefined ? _LATEX_CMD_TO_UNICODE[cmd] : cmd
    ).trim()
  })
  out = out.replace(/\\([a-zA-Z]+)/g, (m, cmd: string) =>
    _LATEX_CMD_TO_UNICODE[cmd] !== undefined ? _LATEX_CMD_TO_UNICODE[cmd] : m
  )
  return out
}

// ── 型別 ──────────────────────────────────────────────────
interface ToolBlock {
  name: string
  args: Record<string, unknown>
  status: 'running' | 'done'
  preview?: string
  mutating?: boolean
}

export interface ChatMsg {
  role: 'user' | 'assistant'
  content: string
  toolBlocks?: ToolBlock[]   // 等待時顯示的工具呼叫紀錄（ephemeral、不落地）
  streaming?: boolean
}

const WELCOME: ChatMsg = {
  role: 'assistant',
  content: `你好！我是這個工作流的 AI 助手 🤖

卡住的時候直接用白話問 —— 我看得到你目前的工作流，**能直接改的就直接改**
（加動作、接變數、排順序），需要你動手錄製的我會講清楚在哪個面板按什麼。

例如：
- 「第一步讀到的金額要怎麼填到第二步？」
- 「上次為什麼跑失敗？」
- 「幫我在最後加一個等待 2 秒」`,
}

// ── 主元件 ────────────────────────────────────────────────
export default function AtlasChat() {
  const { workflows, activeId } = useWorkflowStore()

  const [showChat, setShowChat] = useState(false)
  // 「問 AI」按鈕帶進來的節點狀態 + 開場白。有值就自動展開並送出。
  const askAiContext = useWorkflowStore(s => s.askAiContext)
  const askAiQuestion = useWorkflowStore(s => s.askAiQuestion)
  const assistantOpen = useWorkflowStore(s => s.assistantOpen)
  const closeAssistant = useWorkflowStore(s => s.closeAssistant)
  const clearAskAiSeed = useWorkflowStore(s => s.clearAskAiSeed)

  const [messages, setMessages] = useState<ChatMsg[]>([WELCOME])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [status, setStatus] = useState<ChatStatus | null>(null)
  const chatEndRef = useRef<HTMLDivElement>(null)
  // 防止 initial load 把載入的歷史又 persist 回去
  const loadingRef = useRef(false)

  // openAssistant() → 展開
  useEffect(() => {
    if (assistantOpen) setShowChat(true)
  }, [assistantOpen])

  // 展開時查一次助手可用狀態（模型徽章 + 未設定的提示）
  useEffect(() => {
    if (!showChat) return
    chatStatus().then(setStatus).catch(() => setStatus({
      available: false, reason: '連不上後端', provider: '', model: '',
      data_scope: '', data_scope_label: '', data_stays_local: false,
    }))
  }, [showChat])

  // ── 對話歷史：有 activeId → 後端 per-workflow；沒有 → localStorage 暫存 ──
  const SCRATCH_LS_KEY = 'atlas-lite-chat-scratch-v1'
  useEffect(() => {
    loadingRef.current = true
    const applyLoaded = (loaded: ChatMsg[]) => {
      setMessages(loaded.length > 0 ? loaded : [WELCOME])
      // 讓 React render 完再釋放，避免緊接著的 setMessages 被誤 persist
      setTimeout(() => { loadingRef.current = false }, 0)
    }
    if (activeId) {
      getWorkflowChat(activeId)
        .then(msgs => applyLoaded(msgs as ChatMsg[]))
        .catch(() => applyLoaded([]))
    } else {
      try {
        const raw = localStorage.getItem(SCRATCH_LS_KEY)
        const parsed = raw ? JSON.parse(raw) : []
        applyLoaded(Array.isArray(parsed) ? parsed : [])
      } catch {
        applyLoaded([])
      }
    }
  }, [activeId])

  // welcome 單條不算歷史 —— 不落地，避免每次載入都把它當歷史寫回
  const isWelcomeOnly = (msgs: ChatMsg[]) =>
    msgs.length === 1 && msgs[0].role === 'assistant' && !msgs[0].toolBlocks

  const persistAppend = async (msg: ChatMsg) => {
    if (loadingRef.current) return
    if (activeId) {
      try { await appendWorkflowChat(activeId, msg.role, msg.content) } catch { /* 不擋 UI */ }
    } else {
      setTimeout(() => {
        setMessages(curr => {
          try {
            localStorage.setItem(SCRATCH_LS_KEY, JSON.stringify(
              curr.filter(m => !m.streaming).map(m => ({ role: m.role, content: m.content }))))
          } catch { /* quota */ }
          return curr
        })
      }, 0)
    }
  }

  useEffect(() => {
    if (showChat) chatEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, showChat])

  // ── 送出 ──────────────────────────────────────────────
  const handleSend = async (textArg?: string, extraCtx?: string) => {
    const text = (textArg ?? input).trim()
    if (!text || loading) return
    const userMsg: ChatMsg = { role: 'user', content: text }
    const baseMsgs = isWelcomeOnly(messages) ? [] : messages
    const newMsgs = [...baseMsgs, userMsg]
    // user msg + 空的 assistant 泡泡；工具事件邊來邊填、done 時填內容
    setMessages([...newMsgs, { role: 'assistant', content: '', streaming: true, toolBlocks: [] }])
    setInput('')
    setLoading(true)
    persistAppend(userMsg).catch(() => { /* 同上 */ })

    const patchLast = (fn: (m: ChatMsg) => ChatMsg) =>
      setMessages(prev => {
        const copy = [...prev]
        const last = copy[copy.length - 1]
        if (last && last.role === 'assistant' && last.streaming) {
          copy[copy.length - 1] = fn(last)
        }
        return copy
      })

    try {
      await chatStream(
        {
          messages: newMsgs.map(m => ({ role: m.role, content: m.content })),
          extra_context: extraCtx ?? '',
        },
        (ev) => {
          if (ev.type === 'tool_start') {
            patchLast(m => ({ ...m, toolBlocks: [...(m.toolBlocks || []),
              { name: ev.name, args: ev.args || {}, status: 'running', mutating: ev.mutating }] }))
          } else if (ev.type === 'tool_end') {
            patchLast(m => {
              const blocks = [...(m.toolBlocks || [])]
              for (let i = blocks.length - 1; i >= 0; i--) {
                if (blocks[i].name === ev.name && blocks[i].status === 'running') {
                  blocks[i] = { ...blocks[i], status: 'done', preview: ev.result_preview }
                  break
                }
              }
              return { ...m, toolBlocks: blocks }
            })
          } else if (ev.type === 'done') {
            patchLast(m => ({ ...m, content: ev.reply, streaming: false }))
            persistAppend({ role: 'assistant', content: ev.reply }).catch(() => { /* 同上 */ })
          } else if (ev.type === 'error') {
            throw new Error(ev.detail || '串流錯誤')
          }
        },
      )
    } catch (e) {
      const errMsg = e instanceof Error ? e.message : '未知錯誤'
      toast.error(`AI 回應失敗：${errMsg.slice(0, 220)}`)
      patchLast(() => ({ role: 'assistant', content: `❌ ${errMsg}`, streaming: false }))
    } finally {
      setLoading(false)
    }
  }

  // 「問 AI」帶著開場白進來 → 自動送出（context 只附這一次）
  useEffect(() => {
    if (!showChat || !askAiQuestion || loading) return
    const q = askAiQuestion
    const ctx = askAiContext
    clearAskAiSeed()
    void handleSend(q, ctx)
    // handleSend 依賴 messages，放進 deps 會無限重跑
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showChat, askAiQuestion])

  const handleClearChat = async () => {
    if (loading) return
    if (!confirm('清除這個工作流的對話紀錄？\n（不影響工作流本身）')) return
    setMessages([WELCOME])
    if (activeId) {
      try { await clearWorkflowChat(activeId) } catch { /* UI 已清 */ }
    } else {
      try { localStorage.removeItem(SCRATCH_LS_KEY) } catch { /* ignore */ }
    }
    toast.success('已清除對話（仍綁定當前工作流）')
  }

  // ── 渲染（跟 Atlas sidebar 模式同一套版型）─────────────────
  return (
    <div
      className={
        showChat
          ? 'absolute inset-x-0 bottom-0 top-1/4 bg-white border-t border-gray-100 flex flex-col z-20 shadow-lg'
          : 'border-t border-gray-100 flex flex-col'
      }
    >
      {/* 收合列 —— 左下角一眼看到 AI 助手 */}
      <button
        onClick={() => {
          const next = !showChat
          setShowChat(next)
          if (!next) closeAssistant()
        }}
        title="點開 AI 助手，綁定目前工作流、用白話描述就能修改 / 診斷它"
        className={`w-full flex items-center gap-2.5 px-4 py-3 transition-colors ${
          showChat
            ? 'text-indigo-700 bg-indigo-50'
            : 'text-indigo-700 bg-gradient-to-r from-indigo-50 to-purple-50 hover:from-indigo-100 hover:to-purple-100'
        }`}
      >
        <span className={`flex items-center justify-center w-8 h-8 rounded-lg shrink-0 ${
          showChat ? 'bg-indigo-100 text-indigo-600' : 'bg-white/80 text-indigo-600 shadow-sm'}`}>
          <Bot className="w-5 h-5" />
        </span>
        <span className="flex-1 text-left min-w-0">
          <span className="block text-[15px] font-bold leading-tight">AI 助手</span>
          {!showChat && <span className="block text-[11px] text-indigo-500/90 leading-tight">需要幫忙？點我用 AI 修改 / 診斷工作流</span>}
        </span>
        {loading && <Loader2 className="w-4 h-4 animate-spin text-indigo-500 shrink-0" />}
        {!loading && (showChat ? <ChevronDown className="w-4 h-4 shrink-0" /> : <ChevronUp className="w-4 h-4 shrink-0" />)}
      </button>

      {showChat && (
        <div className="flex flex-col flex-1 min-h-0 border-t border-gray-100">
          {/* 綁定指示 + 模型徽章 + 清除 */}
          <div className="flex flex-col gap-1 px-2.5 py-1.5 bg-gray-50/50 border-b border-gray-100 text-[11px] text-gray-500">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0 break-words leading-snug flex-1">
                {activeId ? (
                  <span className="flex flex-col gap-0.5">
                    <span className="text-[13px] text-gray-600">💾 對話綁定工作流</span>
                    <span className="text-[14px] font-bold text-blue-700 break-all">
                      {workflows.find(w => w.id === activeId)?.name || activeId}
                    </span>
                  </span>
                ) : (
                  <>📝 暫存模式（未選工作流；建立 / 選取後才會持久保存）</>
                )}
              </div>
              <button
                onClick={handleClearChat}
                disabled={loading}
                title="清除這個工作流的對話紀錄（只洗掉聊天歷史；AI 仍記得當前工作流）"
                className="shrink-0 flex items-center gap-1 px-1.5 py-0.5 rounded text-[11px] whitespace-nowrap text-gray-400 hover:text-red-500 hover:bg-red-50 transition-colors"
              >
                🗑 清除對話
              </button>
            </div>
            {status?.available && (
              <span className="text-[10px] text-gray-400" title={
                status.data_scope === 'local' ? '模型跑在這台電腦上，對話內容不會離開本機'
                : status.data_scope === 'internal' ? '模型跑在華碩自建的伺服器上，內容不會送到外部廠商'
                : '模型在外部雲端廠商，對話內容會送到公司外'}>
                {status.model}・{status.data_scope_label}
                {/* 沒有逐字串流 —— 長答案要等 10-20 秒，先講明免得以為當掉 */}
                ・回覆整段產生（可能等 10~20 秒）
              </span>
            )}
          </div>

          {/* 未設定 → 講清楚缺什麼，不要只反灰 */}
          {status && !status.available && (
            <div className="mx-2.5 mt-2 p-2.5 rounded-lg bg-amber-50 border border-amber-200 text-[11px] text-amber-900 leading-relaxed">
              <div className="font-medium mb-0.5">助手還不能用</div>
              <div>{status.reason}</div>
              <a href="/settings" className="inline-block mt-1 underline hover:no-underline">去設定頁設定 →</a>
            </div>
          )}

          {/* Messages */}
          <div className="flex-1 overflow-y-auto p-2.5 space-y-2.5">
            {messages.map((msg, i) => (
              <div key={i} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                {msg.role === 'assistant' && (
                  <div className="w-5 h-5 rounded-full bg-indigo-100 flex items-center justify-center shrink-0 mt-0.5 mr-1.5">
                    <Bot className="w-3 h-3 text-indigo-600" />
                  </div>
                )}
                <div className={`max-w-[88%] min-w-0 rounded-xl px-2.5 py-1.5 text-xs leading-relaxed break-words overflow-hidden ${
                  msg.role === 'user'
                    ? 'bg-indigo-600 text-white rounded-br-sm'
                    : 'bg-gray-100 text-gray-700 rounded-bl-sm'
                }`} style={{ overflowWrap: 'anywhere', wordBreak: 'break-word' }}>
                  {/* 工具呼叫進度塊 —— 助手動了什麼要看得見；會寫入的標黃 */}
                  {msg.role === 'assistant' && msg.toolBlocks && msg.toolBlocks.length > 0 && (
                    <div className="mb-1.5 space-y-1">
                      {msg.toolBlocks.map((tb, ti) => (
                        <div
                          key={ti}
                          className={`text-[11px] px-2 py-1 rounded border ${
                            tb.status === 'running'
                              ? 'bg-indigo-50 border-indigo-200 text-indigo-700'
                              : tb.mutating
                                ? 'bg-amber-50 border-amber-200 text-amber-800'
                                : 'bg-gray-50 border-gray-200 text-gray-600'
                          }`}
                        >
                          {tb.status === 'running' ? (
                            <span className="flex items-center gap-1.5">
                              <Loader2 className="w-3 h-3 animate-spin shrink-0" />
                              {/* truncate：sidebar 很窄，不截斷的話長工具名會被逐字擠成直排 */}
                              <span className="font-mono truncate min-w-0">{tb.name}</span>
                              <span className="text-[10px] text-indigo-500/70 truncate">
                                {Object.entries(tb.args).slice(0, 2).map(([k, v]) =>
                                  `${k}=${typeof v === 'string' ? `"${v.slice(0, 30)}"` : JSON.stringify(v).slice(0, 30)}`
                                ).join(', ')}
                              </span>
                            </span>
                          ) : (
                            <span className="flex items-center gap-1.5">
                              <span className="text-emerald-500 shrink-0">✓</span>
                              <span className="font-mono truncate min-w-0">{tb.name}</span>
                              {tb.mutating && <span className="text-[10px] shrink-0">會改資料</span>}
                              {tb.preview && (
                                <span className="text-[10px] text-gray-500 truncate" title={tb.preview}>
                                  {tb.preview.slice(0, 60)}
                                </span>
                              )}
                            </span>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                  {msg.role === 'assistant' ? (
                    <div className="prose prose-xs max-w-none prose-p:my-0.5 prose-pre:text-xs prose-pre:whitespace-pre-wrap">
                      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeRaw]}>{cleanLatexInChat(msg.content)}</ReactMarkdown>
                      {msg.streaming && (
                        <span className="inline-block w-1.5 h-3 ml-0.5 bg-indigo-500 animate-pulse align-middle" />
                      )}
                    </div>
                  ) : (
                    <span className="whitespace-pre-wrap">{msg.content}</span>
                  )}
                </div>
              </div>
            ))}
            {loading && (
              <div className="flex items-center gap-2 text-xs text-gray-400 pl-7">
                <Loader2 className="w-3 h-3 animate-spin" /> 思考中…（沒有逐字輸出，可能要等十幾秒）
              </div>
            )}
            <div ref={chatEndRef} />
          </div>

          {/* Input */}
          <div className="p-2 border-t border-gray-100 flex gap-1.5 items-end">
            <textarea
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={e => {
                // Shift+Enter 送出；Enter 純換行（輸入框小、避免誤送）—— 跟 Atlas 一致
                if (e.key === 'Enter' && e.shiftKey) {
                  e.preventDefault()
                  if (input.trim() && !loading) void handleSend()
                }
              }}
              placeholder="卡在哪？（Shift+Enter 送出 · Enter 換行）"
              disabled={loading || status?.available === false}
              rows={2}
              className="flex-1 border border-gray-200 rounded-xl px-2.5 py-1.5 text-xs outline-none focus:border-indigo-300 resize-none disabled:bg-gray-50 disabled:text-gray-400"
            />
            <button
              onClick={() => void handleSend()}
              disabled={!input.trim() || loading || status?.available === false}
              className="w-7 h-7 flex items-center justify-center bg-indigo-600 text-white rounded-xl hover:bg-indigo-500 disabled:bg-gray-200 disabled:text-gray-400 shrink-0 transition-colors"
            >
              <Send className="w-3 h-3" />
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
