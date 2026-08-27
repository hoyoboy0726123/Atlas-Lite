'use client'
/**
 * 喚醒視窗插入器 —— 一鍵加入 activate_window + wait 兩個動作。
 * 場景:目標網頁閒置數小時被 Edge 睡眠分頁卸載,UIA 找得到視窗卻讀不到
 * 任何元素,背景喚醒叫不醒 —— 操作前要明確拉到前景讓分頁重載。
 */
import { useEffect, useState } from 'react'
import { AppWindow } from 'lucide-react'
import { toast } from 'sonner'
import type { ComputerUseAction } from './_helpers'

interface Props {
  index: number
  isOpen: boolean
  /** 節點的目標視窗 pattern(自動去掉 * 當預設關鍵字) */
  defaultTitle: string
  openMenu: () => void
  closeMenu: () => void
  onAdd: (index: number, action: ComputerUseAction) => void
}

export default function WakeWindowInserter({ index, isOpen, defaultTitle, openMenu, closeMenu, onAdd }: Props) {
  const [title, setTitle] = useState('')
  const [waitSec, setWaitSec] = useState('1.5')
  // 開啟時帶入節點目標視窗(去 * 取關鍵字);使用者改過就不覆蓋
  useEffect(() => {
    if (isOpen && !title.trim() && defaultTitle) {
      setTitle(defaultTitle.replace(/\*/g, '').trim())
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen])

  const submit = () => {
    if (!title.trim()) { toast.error('請填視窗標題關鍵字'); return }
    const t = title.trim()
    onAdd(index, {
      type: 'activate_window',
      title_contains: t,
      description: `喚醒「${t}」視窗(拉到前景、睡眠分頁重載)`,
    } as ComputerUseAction)
    const sec = Number(waitSec) || 1.5
    onAdd(index + 1, { type: 'wait', seconds: sec, description: `等視窗載入 ${sec}s` })
    toast.success(`已插入喚醒「${t}」＋等待 ${sec}s`)
    closeMenu()
  }

  if (!isOpen) {
    return (
      <div className="flex justify-center -my-0.5">
        <button
          data-vlm-insert-trigger
          type="button"
          onClick={openMenu}
          title="喚醒視窗（activate_window + 等待）：閒置太久的網頁會被瀏覽器卸載，操作前要拉到前景重載"
          className="opacity-30 hover:opacity-100 transition-opacity flex items-center gap-0.5 px-2 py-0.5 rounded-full whitespace-nowrap text-[9px] text-cyan-600 border border-dashed border-cyan-300 hover:bg-cyan-50"
        >
          <AppWindow className="w-2.5 h-2.5" /> 喚醒
        </button>
      </div>
    )
  }

  return (
    // data-vlm-insert-popover:面板「點外面關閉」只認這個屬性
    <div data-vlm-insert-popover
         className="my-1 rounded-lg border border-cyan-200 bg-cyan-50/60 p-2 space-y-1.5">
      <div className="flex items-center gap-1 text-[11px] font-semibold text-cyan-800">
        <AppWindow className="w-3 h-3 shrink-0" />
        <span>喚醒視窗（拉到前景 + 等載入）</span>
      </div>
      <label className="block">
        <span className="text-[10px] text-gray-500">視窗標題關鍵字（包含比對）</span>
        <input value={title} onChange={e => setTitle(e.target.value)}
          placeholder="例：E-Quote測試靶"
          className="w-full text-[11px] px-1.5 py-1 rounded border border-gray-300 outline-none focus:border-cyan-500" />
      </label>
      <label className="flex items-center gap-1 text-[10px] text-gray-500 whitespace-nowrap">
        喚醒後等
        <input value={waitSec} onChange={e => setWaitSec(e.target.value)}
          className="w-14 text-[11px] px-1.5 py-1 rounded border border-gray-300 text-right" />
        秒（睡眠分頁重載需要時間）
      </label>
      <div className="flex gap-1 pt-0.5">
        <button type="button" onClick={submit}
          className="flex-1 text-[10px] bg-cyan-600 text-white px-2 py-1 rounded hover:bg-cyan-700">插入（共 2 個動作）</button>
        <button type="button" onClick={closeMenu}
          className="text-[10px] text-gray-500 hover:text-gray-800 px-2 py-1 rounded border border-gray-200">取消</button>
      </div>
      <p className="text-[9px] text-gray-500 leading-snug">
        需要的場景：排程半夜跑、頁面開著一整天不動。日常「人開著頁面按執行」不用加。
        會把該視窗拉到前景（搶焦點是喚醒的必要行為）。
      </p>
    </div>
  )
}
