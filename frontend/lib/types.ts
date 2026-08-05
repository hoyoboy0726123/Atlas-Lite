// ── 執行紀錄 ──────────────────────────────────────────────

export interface StepResult {
  step_index: number
  step_name: string
  exit_code: number
  stdout_tail: string
  stderr_tail: string
  validation_status: 'ok' | 'warning' | 'failed'
  validation_reason: string
  validation_suggestion: string
  retries_used: number
  /** 這步實際產出的主要檔案（後端用 dir-snapshot 比 mtime 算出來的）。 */
  actual_output_path?: string
  /** 這步匯出的具名變數：_step_export.json 的內容 + .json 輸出被攤平的純量欄位。 */
  step_vars?: Record<string, unknown>
  started_at?: string
  ended_at?: string
}

export interface PipelineRun {
  run_id: string
  pipeline_name: string
  current_step: number
  step_results: StepResult[]
  status: 'running' | 'awaiting_human' | 'completed' | 'failed' | 'aborted'
  log_path: string
  started_at: string
  ended_at: string | null
  config_dict: {
    name: string
    steps: Array<{
      name: string
      batch: string
      timeout: number
      retry: number
      output?: { path: string; expect: string }
    }>
  }
  awaiting_type?: 'failure' | 'human_confirm' | 'missing_dependency'
  awaiting_message?: string
  awaiting_suggestion?: string
  input_params?: Record<string, string>
  workflow_id?: string | null
}

// ── 排程 ──────────────────────────────────────────────────

export interface ScheduledTask {
  id: string
  name: string
  yaml_path: string
  schedule_type: 'cron' | 'once'
  schedule_expr: string
  next_run: string | null
  last_run: string | null
  enabled: boolean
}

// ── 檔案瀏覽器（腳本節點的「選擇檔案」用）─────────────────

export interface FileItem {
  name: string
  path: string
  is_dir: boolean
  ext?: string
}
