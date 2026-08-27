'use client'
/**
 * 逐筆迴圈插入器 —— 建立 for_each 動作。
 * 主要模式是「把目前序列的全部動作包進迴圈」:實務流程是先把單筆調通,
 * 再一鍵包成逐筆;清單用多行 textarea 貼(一行一筆,Excel 直行直接貼)。
 */
import { useState } from 'react'
import { Repeat } from 'lucide-react'
import { toast } from 'sonner'
import type { ComputerUseAction } from './_helpers'

interface Props {
  index: number
  isOpen: boolean
  hasActions: boolean
  openMenu: () => void
  closeMenu: () => void
  onInsert: (index: number, action: ComputerUseAction) => void
  onWrapAll: (action: ComputerUseAction) => void
}

export default function ForEachInserter({ index, isOpen, hasActions, openMenu, closeMenu, onInsert, onWrapAll }: Props) {
  const [items, setItems] = useState('')
  const [saveAs, setSaveAs] = useState('品規')
  const [cont, setCont] = useState(true)
  const [mode, setMode] = useState<'wrap' | 'empty'>('wrap')

  const submit = () => {
    if (!items.trim()) { toast.error('請貼清單（或填 {{變數}}）'); return }
    if (!saveAs.trim()) { toast.error('請填變數名稱'); return }
    const fe: ComputerUseAction = {
      type: 'for_each',
      items: items.trim(),
      save_as: saveAs.trim(),
      continue_on_error: cont,
      do: [],
      description: `逐筆迴圈 → {{${saveAs.trim()}}}`,
    }
    if (mode === 'wrap' && hasActions) {
      onWrapAll(fe)
      toast.success(`已把現有動作包進迴圈 — 記得把「填值」動作的文字改成 {{${saveAs.trim()}}}`, { duration: 8000 })
    } else {
      onInsert(index, fe)
      toast.success('已插入空迴圈 — 子動作用 YAML 或 AI 助手放進 do:')
    }
    closeMenu()
  }

  if (!isOpen) {
    return (
      <div className="flex justify-center -my-0.5">
        <button
          data-vlm-insert-trigger
          type="button"
          onClick={openMenu}
          title="逐筆迴圈：清單裡每一筆各跑一遍子動作（例：5 個品規逐一查詢匯出）"
          className="opacity-30 hover:opacity-100 transition-opacity flex items-center gap-0.5 px-2 py-0.5 rounded-full whitespace-nowrap text-[9px] text-fuchsia-600 border border-dashed border-fuchsia-300 hover:bg-fuchsia-50"
        >
          <Repeat className="w-2.5 h-2.5" /> 逐筆
        </button>
      </div>
    )
  }

  return (
    // data-vlm-insert-popover:面板「點外面關閉」只認這個屬性
    <div data-vlm-insert-popover
         className="my-1 rounded-lg border border-fuchsia-200 bg-fuchsia-50/60 p-2 space-y-1.5">
      <div className="flex items-center gap-1 text-[11px] font-semibold text-fuchsia-800">
        <Repeat className="w-3 h-3 shrink-0" />
        <span>逐筆迴圈：每一筆各跑一遍</span>
      </div>
      <label className="block">
        <span className="text-[10px] text-gray-500">
          {'清單（一行一筆，可從 Excel 直行貼上；清單在別的視窗時填 {{變數}}，先用「讀文字」抓下來）'}
        </span>
        <textarea
          value={items}
          onChange={e => setItems(e.target.value)}
          rows={5}
          placeholder={'UX3407%\nRC71L%\nGU605%\n或 {{清單原文}} / {{ input.品規清單 }}'}
          className="w-full text-[11px] px-1.5 py-1 rounded border border-gray-300 outline-none focus:border-fuchsia-500 font-mono resize-y"
        />
      </label>
      <div className="flex items-center gap-2 flex-wrap">
        <label className="flex items-center gap-1 text-[10px] text-gray-500 whitespace-nowrap">
          每輪存到變數
          <input value={saveAs} onChange={e => setSaveAs(e.target.value)}
            className="w-20 text-[11px] px-1.5 py-1 rounded border border-gray-300 font-mono" />
        </label>
        <label className="flex items-center gap-1 text-[10px] text-gray-600 whitespace-nowrap cursor-pointer">
          <input type="checkbox" checked={cont} onChange={e => setCont(e.target.checked)} />
          某筆失敗跳下一筆繼續
        </label>
      </div>
      <div className="space-y-0.5 text-[10px] text-gray-600">
        <label className="flex items-center gap-1 cursor-pointer">
          <input type="radio" checked={mode === 'wrap'} onChange={() => setMode('wrap')} disabled={!hasActions} />
          <span className={hasActions ? '' : 'opacity-40'}>
            把目前序列的<b>全部動作</b>包進迴圈（先調通單筆再套迴圈，推薦）
          </span>
        </label>
        <label className="flex items-center gap-1 cursor-pointer">
          <input type="radio" checked={mode === 'empty' || !hasActions} onChange={() => setMode('empty')} />
          建立空迴圈，插入在此位置（子動作之後用 YAML / AI 助手放進去）
        </label>
      </div>
      <div className="flex gap-1 pt-0.5">
        <button type="button" onClick={submit}
          className="flex-1 text-[10px] bg-fuchsia-600 text-white px-2 py-1 rounded hover:bg-fuchsia-700">建立迴圈</button>
        <button type="button" onClick={closeMenu}
          className="text-[10px] text-gray-500 hover:text-gray-800 px-2 py-1 rounded border border-gray-200">取消</button>
      </div>
      <p className="text-[9px] text-gray-500 leading-snug">
        {'包進迴圈後，把「填值」動作的文字改成 {{變數名}}（✎ 編輯），每輪就會帶入當前那一筆；{{變數名_序號}} 是第幾筆。'}
      </p>
    </div>
  )
}
