import { useEffect, useMemo, useRef, useState } from "react";
import { Client } from "@stomp/stompjs";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ColumnDef, flexRender, getCoreRowModel, useReactTable } from "@tanstack/react-table";
import ReactECharts from "echarts-for-react";

type EvalFilters = {
  dataset: "historical" | "live";
  date_from: string;
  date_to: string;
  strategy: string;
  regime: string;
  initial_capital: number;
  cost_bps: number;
  stop_loss_pct: number;
  target_pct: number;
  trailing_enabled: boolean;
  trailing_activation_pct: number;
  trailing_offset_pct: number;
  trailing_lock_breakeven: boolean;
};

type RunRiskConfig = {
  stop_loss_pct?: number | null;
  target_pct?: number | null;
  trailing_enabled?: boolean | null;
  trailing_activation_pct?: number | null;
  trailing_offset_pct?: number | null;
  trailing_lock_breakeven?: boolean | null;
};

type RunStatus = {
  run_id: string;
  status: string;
  dataset?: string;
  date_from?: string;
  date_to?: string;
  submitted_at?: string;
  started_at?: string | null;
  ended_at?: string | null;
  progress_pct?: number | null;
  current_day?: string | null;
  total_days?: number | null;
  message?: string | null;
  error?: string | null;
  risk_config?: RunRiskConfig | null;
};

type RunEvent = {
  event_type: string;
  run_id: string;
  timestamp: string;
  progress_pct?: number;
  current_day?: string;
  total_days?: number;
  message?: string;
  error?: string;
};

type RunEventLike = Partial<RunEvent> & { data?: Partial<RunEvent> };
type DebugLogEntry = { timestamp: string; message: string };
type UrlState = {
  filters: EvalFilters;
  runId: string;
  debug: boolean;
  hasUrlOverride: boolean;
  featureModelKey: string;
};

type TradingModelCatalogEntry = {
  instance_key: string;
  title: string;
  source?: string;
  ready_to_run?: boolean;
  model_group?: string;
  profile_id?: string;
  run_id?: string;
  feature_profile?: string;
};

type FeatureSideScores = Record<string, number | null | undefined>;

type FeatureRankingRow = {
  feature_name: string;
  feature_label: string;
  group_key: string;
  group_label: string;
  side_scores?: FeatureSideScores;
  importance_score?: number | null;
  rank?: number | null;
};

type FeatureGroupRow = {
  group_key: string;
  group_label: string;
  contract_columns_total: number;
  selected_columns_count: number;
  inactive_columns_count: number;
  importance_mean?: number | null;
  features: Array<
    FeatureRankingRow & {
      is_selected: boolean;
    }
  >;
};

type FeatureScatterPoint = {
  feature_name: string;
  feature_label: string;
  group_key: string;
  group_label: string;
  x: number;
  y: number;
  importance_score?: number | null;
  rank?: number | null;
};

type FeatureIntelligenceResponse = {
  status: string;
  model?: {
    instance_key?: string;
    title?: string;
    source?: string;
    model_group?: string;
    profile_id?: string;
    run_id?: string;
    feature_profile?: string;
    selected_feature_set?: string;
    selected_model_name?: string;
    selected_model_family?: string;
    coverage?: {
      training_start?: string | null;
      training_end?: string | null;
      training_days?: number | null;
      requested_start?: string | null;
      requested_end?: string | null;
      requested_range_in_coverage?: boolean | null;
    };
  };
  contract?: {
    contract_id?: string;
    schema_version?: string;
  };
  summary?: {
    selected_v1_feature_count?: number;
    contract_group_count?: number;
    removed_legacy_feature_count?: number;
    unmapped_feature_count?: number;
    scatter_point_count?: number;
    requested_range_in_coverage?: boolean | null;
  };
  ranking?: {
    rows?: FeatureRankingRow[];
  };
  groups?: FeatureGroupRow[];
  scatter?: {
    x_axis_label?: string;
    y_axis_label?: string;
    points?: FeatureScatterPoint[];
  };
};

const API_BASE = (import.meta.env.VITE_DASHBOARD_API_BASE as string | undefined)?.trim() || "http://localhost:8008";
const WS_URL = (import.meta.env.VITE_DASHBOARD_WS_URL as string | undefined)?.trim() || API_BASE.replace(/^http/i, "ws") + "/ws";
const DEFAULT_FILTERS: EvalFilters = {
  dataset: "historical",
  date_from: "2024-01-01",
  date_to: "2024-01-31",
  strategy: "",
  regime: "",
  initial_capital: 1000,
  cost_bps: 0,
  stop_loss_pct: 40,
  target_pct: 80,
  trailing_enabled: false,
  trailing_activation_pct: 10,
  trailing_offset_pct: 5,
  trailing_lock_breakeven: true,
};

const FEATURE_GROUP_COLORS: Record<string, string> = {
  px: "#1f6f8b",
  ret: "#12664f",
  ema: "#4c6ef5",
  osc: "#d9480f",
  vwap: "#0f766e",
  dist: "#8f3b76",
  fut_flow: "#8c6d1f",
  opt_flow: "#b02a37",
  time: "#495057",
  ctx: "#5f3dc4",
};

function parseNumberParam(value: string | null, fallback: number) {
  if (value === null || value.trim() === "") return fallback;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function parseBoolParam(value: string | null, fallback: boolean) {
  if (value === null || value.trim() === "") return fallback;
  const text = value.trim().toLowerCase();
  if (["1", "true", "yes", "on"].includes(text)) return true;
  if (["0", "false", "no", "off"].includes(text)) return false;
  return fallback;
}

function readUrlState(): UrlState {
  if (typeof window === "undefined") {
    return { filters: DEFAULT_FILTERS, runId: "", debug: false, hasUrlOverride: false, featureModelKey: "" };
  }
  const params = new URLSearchParams(window.location.search);
  const dataset = params.get("dataset");
  const featureModelKey = (params.get("model") || params.get("feature_model") || "").trim();
  const nextFilters: EvalFilters = {
    dataset: dataset === "live" ? "live" : "historical",
    date_from: params.get("date_from") || params.get("dateFrom") || DEFAULT_FILTERS.date_from,
    date_to: params.get("date_to") || params.get("dateTo") || DEFAULT_FILTERS.date_to,
    strategy: params.get("strategy") || DEFAULT_FILTERS.strategy,
    regime: params.get("regime") || DEFAULT_FILTERS.regime,
    initial_capital: parseNumberParam(params.get("initial_capital") || params.get("initialCapital"), DEFAULT_FILTERS.initial_capital),
    cost_bps: parseNumberParam(params.get("cost_bps") || params.get("costBps"), DEFAULT_FILTERS.cost_bps),
    stop_loss_pct: parseNumberParam(params.get("stop_loss_pct") || params.get("stopLossPct"), DEFAULT_FILTERS.stop_loss_pct),
    target_pct: parseNumberParam(params.get("target_pct") || params.get("targetPct"), DEFAULT_FILTERS.target_pct),
    trailing_enabled: parseBoolParam(params.get("trailing_enabled") || params.get("trailingEnabled"), DEFAULT_FILTERS.trailing_enabled),
    trailing_activation_pct: parseNumberParam(
      params.get("trailing_activation_pct") || params.get("trailingActivationPct"),
      DEFAULT_FILTERS.trailing_activation_pct,
    ),
    trailing_offset_pct: parseNumberParam(
      params.get("trailing_offset_pct") || params.get("trailingOffsetPct"),
      DEFAULT_FILTERS.trailing_offset_pct,
    ),
    trailing_lock_breakeven: parseBoolParam(
      params.get("trailing_lock_breakeven") || params.get("trailingLockBreakeven"),
      DEFAULT_FILTERS.trailing_lock_breakeven,
    ),
  };
  const runId = (params.get("runId") || params.get("run_id") || "").trim();
  const debug = ["1", "true", "yes", "on"].includes((params.get("debug") || "").trim().toLowerCase());
  const hasUrlOverride =
    Boolean(runId) ||
    params.has("date_from") ||
    params.has("dateFrom") ||
    params.has("date_to") ||
    params.has("dateTo") ||
    params.has("dataset") ||
    params.has("strategy") ||
    params.has("regime") ||
    params.has("initial_capital") ||
    params.has("initialCapital") ||
    params.has("cost_bps") ||
    params.has("costBps") ||
    params.has("stop_loss_pct") ||
    params.has("stopLossPct") ||
    params.has("target_pct") ||
    params.has("targetPct") ||
    params.has("trailing_enabled") ||
    params.has("trailingEnabled") ||
    params.has("trailing_activation_pct") ||
    params.has("trailingActivationPct") ||
    params.has("trailing_offset_pct") ||
    params.has("trailingOffsetPct") ||
    params.has("trailing_lock_breakeven") ||
    params.has("trailingLockBreakeven");
  return { filters: nextFilters, runId, debug, hasUrlOverride, featureModelKey };
}

async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

async function apiPost<T>(path: string, payload: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

function qs(filters: EvalFilters, extra?: Record<string, string | number | undefined>) {
  const params = new URLSearchParams();
  params.set("dataset", filters.dataset);
  params.set("date_from", filters.date_from);
  params.set("date_to", filters.date_to);
  if (filters.strategy.trim()) params.set("strategy", filters.strategy.trim());
  if (filters.regime.trim()) params.set("regime", filters.regime.trim());
  params.set("initial_capital", String(filters.initial_capital));
  params.set("cost_bps", String(filters.cost_bps));
  for (const [k, v] of Object.entries(extra || {})) {
    if (v !== undefined && String(v).trim() !== "") params.set(k, String(v));
  }
  return params.toString();
}

function csvDownload(filename: string, rows: Record<string, unknown>[]) {
  if (!rows.length) return;
  const headers = Object.keys(rows[0]);
  const lines = [headers.join(",")];
  for (const row of rows) {
    lines.push(
      headers
        .map((h) => {
          const val = row[h];
          const text = val === null || val === undefined ? "" : String(val);
          const esc = text.replace(/"/g, '""');
          return `"${esc}"`;
        })
        .join(","),
    );
  }
  const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

const dayColumns: ColumnDef<any>[] = [
  { accessorKey: "date", header: "Date" },
  { accessorKey: "trades", header: "Trades" },
  { accessorKey: "wins", header: "Wins" },
  { accessorKey: "losses", header: "Losses" },
  { accessorKey: "win_rate", header: "Win Rate", cell: (ctx) => pct(ctx.getValue<number>()) },
  { accessorKey: "day_return_pct", header: "Capital Return", cell: (ctx) => pct(ctx.getValue<number>()) },
  { accessorKey: "day_pnl_amount", header: "Day PnL", cell: (ctx) => num(ctx.getValue<number>()) },
  { accessorKey: "equity_end", header: "Equity EOD", cell: (ctx) => num(ctx.getValue<number>()) },
  { accessorKey: "drawdown_pct_eod", header: "Drawdown EOD", cell: (ctx) => pct(ctx.getValue<number>()) },
];

const tradeColumns: ColumnDef<any>[] = [
  { accessorKey: "trade_date_ist", header: "Day" },
  { accessorKey: "entry_strategy", header: "Strategy" },
  { accessorKey: "regime", header: "Regime" },
  { accessorKey: "direction", header: "Dir" },
  { accessorKey: "entry_time", header: "Entry" },
  { accessorKey: "exit_time", header: "Exit" },
  { accessorKey: "capital_pnl_pct", header: "Capital PnL%", cell: (ctx) => pct(ctx.getValue<number>()) },
  { accessorKey: "capital_pnl_amount", header: "Capital PnL", cell: (ctx) => num(ctx.getValue<number>()) },
  { accessorKey: "pnl_pct_net", header: "Option PnL%", cell: (ctx) => pct(ctx.getValue<number>()) },
  { accessorKey: "mfe_pct", header: "MFE", cell: (ctx) => pct(ctx.getValue<number>()) },
  { accessorKey: "mae_pct", header: "MAE", cell: (ctx) => pct(ctx.getValue<number>()) },
  { accessorKey: "stop_loss_pct", header: "Stop%", cell: (ctx) => pct(ctx.getValue<number>()) },
  { accessorKey: "entry_stop_price", header: "Entry Stop", cell: (ctx) => num(ctx.getValue<number>()) },
  { accessorKey: "exit_stop_price", header: "Exit Stop", cell: (ctx) => num(ctx.getValue<number>()) },
  { accessorKey: "trailing_enabled", header: "Trail On", cell: (ctx) => boolLabel(ctx.getValue<boolean>()) },
  { accessorKey: "trailing_active", header: "Trail Active", cell: (ctx) => boolLabel(ctx.getValue<boolean>()) },
  { accessorKey: "bars_held", header: "Bars" },
  { accessorKey: "exit_reason", header: "Exit Reason" },
];

function num(v?: number | null) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "--";
  return Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function pct(v?: number | null) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "--";
  return `${(Number(v) * 100).toFixed(2)}%`;
}

function boolLabel(v?: boolean | null) {
  if (v === null || v === undefined) return "--";
  return v ? "Yes" : "No";
}

function score(v?: number | null) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "--";
  return Number(v).toFixed(3);
}

function applyRunRiskConfig(base: EvalFilters, risk?: RunRiskConfig | null): EvalFilters {
  if (!risk) return base;
  const next = { ...base };
  if (typeof risk.stop_loss_pct === "number" && Number.isFinite(risk.stop_loss_pct)) next.stop_loss_pct = risk.stop_loss_pct * 100;
  if (typeof risk.target_pct === "number" && Number.isFinite(risk.target_pct)) next.target_pct = risk.target_pct * 100;
  if (typeof risk.trailing_enabled === "boolean") next.trailing_enabled = risk.trailing_enabled;
  if (typeof risk.trailing_activation_pct === "number" && Number.isFinite(risk.trailing_activation_pct)) {
    next.trailing_activation_pct = risk.trailing_activation_pct * 100;
  }
  if (typeof risk.trailing_offset_pct === "number" && Number.isFinite(risk.trailing_offset_pct)) {
    next.trailing_offset_pct = risk.trailing_offset_pct * 100;
  }
  if (typeof risk.trailing_lock_breakeven === "boolean") next.trailing_lock_breakeven = risk.trailing_lock_breakeven;
  return next;
}

function toRunRiskPayload(filters: EvalFilters): Required<RunRiskConfig> {
  return {
    stop_loss_pct: Math.max(0, Number(filters.stop_loss_pct || 0)) / 100,
    target_pct: Math.max(0, Number(filters.target_pct || 0)) / 100,
    trailing_enabled: Boolean(filters.trailing_enabled),
    trailing_activation_pct: Math.max(0, Number(filters.trailing_activation_pct || 0)) / 100,
    trailing_offset_pct: Math.max(0, Number(filters.trailing_offset_pct || 0)) / 100,
    trailing_lock_breakeven: Boolean(filters.trailing_lock_breakeven),
  };
}

function normalizeRunEvent(input: unknown): RunEvent | null {
  if (!input || typeof input !== "object") return null;
  const payload = input as RunEventLike;
  const raw = payload.data && typeof payload.data === "object" ? payload.data : payload;
  const eventType = String(raw.event_type || "").trim();
  if (!eventType) return null;
  return {
    event_type: eventType,
    run_id: String(raw.run_id || payload.run_id || "").trim(),
    timestamp: String(raw.timestamp || payload.timestamp || new Date().toISOString()),
    progress_pct: typeof raw.progress_pct === "number" ? raw.progress_pct : undefined,
    current_day: raw.current_day ? String(raw.current_day) : undefined,
    total_days: typeof raw.total_days === "number" ? raw.total_days : undefined,
    message: raw.message ? String(raw.message) : undefined,
    error: raw.error ? String(raw.error) : undefined,
  };
}

export default function App() {
  const queryClient = useQueryClient();
  const initialUrlState = useMemo(() => readUrlState(), []);
  const [draft, setDraft] = useState<EvalFilters>(initialUrlState.filters);
  const [filters, setFilters] = useState<EvalFilters>(initialUrlState.filters);
  const [selectedFeatureModelKey, setSelectedFeatureModelKey] = useState<string>(initialUrlState.featureModelKey);
  const [runStatus, setRunStatus] = useState<RunStatus | null>(null);
  const [isSubmittingRun, setIsSubmittingRun] = useState(false);
  const [activeRunId, setActiveRunId] = useState<string>(initialUrlState.runId);
  const [activeRunRange, setActiveRunRange] = useState<{ date_from: string; date_to: string } | null>(
    initialUrlState.hasUrlOverride
      ? { date_from: initialUrlState.filters.date_from, date_to: initialUrlState.filters.date_to }
      : null,
  );
  const [runEvents, setRunEvents] = useState<RunEvent[]>([]);
  const [debugLogs, setDebugLogs] = useState<DebugLogEntry[]>([]);
  const [selectedDay, setSelectedDay] = useState<string>("");
  const [daysPage, setDaysPage] = useState(1);
  const [tradesPage, setTradesPage] = useState(1);
  const clientRef = useRef<Client | null>(null);
  const terminalRefreshRunRef = useRef<string>("");
  const [wsState, setWsState] = useState<"idle" | "connected" | "disconnected">("idle");
  const debugEnabled = initialUrlState.debug;
  const isRunActive = runStatus ? !["completed", "failed"].includes(String(runStatus.status || "").toLowerCase()) : false;
  const isRunPending = isSubmittingRun || isRunActive;
  const appendUiLog = (message: string) => {
    if (!debugEnabled) return;
    const entry = { timestamp: new Date().toISOString(), message };
    console.debug("[strategy-eval-ui]", entry.timestamp, message);
    setDebugLogs((prev) => [entry, ...prev].slice(0, 80));
  };
  const syncBrowserUrl = (
    nextFilters: EvalFilters,
    nextRunId: string,
    nextRange: { date_from: string; date_to: string } | null,
    nextFeatureModelKey: string,
  ) => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams();
    params.set("dataset", nextFilters.dataset);
    params.set("date_from", nextRange?.date_from || nextFilters.date_from);
    params.set("date_to", nextRange?.date_to || nextFilters.date_to);
    if (nextFilters.strategy.trim()) params.set("strategy", nextFilters.strategy.trim());
    if (nextFilters.regime.trim()) params.set("regime", nextFilters.regime.trim());
    params.set("initial_capital", String(nextFilters.initial_capital));
    params.set("cost_bps", String(nextFilters.cost_bps));
    params.set("stop_loss_pct", String(nextFilters.stop_loss_pct));
    params.set("target_pct", String(nextFilters.target_pct));
    params.set("trailing_enabled", nextFilters.trailing_enabled ? "1" : "0");
    params.set("trailing_activation_pct", String(nextFilters.trailing_activation_pct));
    params.set("trailing_offset_pct", String(nextFilters.trailing_offset_pct));
    params.set("trailing_lock_breakeven", nextFilters.trailing_lock_breakeven ? "1" : "0");
    if (nextRunId.trim()) params.set("runId", nextRunId.trim());
    if (nextFeatureModelKey.trim()) params.set("model", nextFeatureModelKey.trim());
    if (debugEnabled) params.set("debug", "1");
    const nextUrl = `${window.location.pathname}?${params.toString()}`;
    window.history.replaceState({}, "", nextUrl);
  };
  const [isMobile, setIsMobile] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    return window.matchMedia("(max-width: 900px)").matches;
  });

  const summaryQ = useQuery({
    queryKey: ["eval-summary", filters, activeRunId],
    queryFn: () => apiGet<any>(`/api/strategy/evaluation/summary?${qs(filters, { run_id: activeRunId || undefined })}`),
    enabled: !isRunPending,
  });
  const equityQ = useQuery({
    queryKey: ["eval-equity", filters, activeRunId],
    queryFn: () => apiGet<any>(`/api/strategy/evaluation/equity?${qs(filters, { run_id: activeRunId || undefined })}`),
    enabled: !isRunPending,
  });
  const daysQ = useQuery({
    queryKey: ["eval-days", filters, daysPage, activeRunId],
    queryFn: () => apiGet<any>(`/api/strategy/evaluation/days?${qs(filters, { page: daysPage, page_size: 50, run_id: activeRunId || undefined })}`),
    enabled: !isRunPending,
  });
  const tradesQ = useQuery({
    queryKey: ["eval-trades", filters, selectedDay, tradesPage, activeRunId],
    queryFn: () =>
      apiGet<any>(
        `/api/strategy/evaluation/trades?${qs(
          selectedDay
            ? { ...filters, date_from: selectedDay, date_to: selectedDay }
            : filters,
          { page: tradesPage, page_size: 50, sort_by: "exit_time", sort_dir: "desc", run_id: activeRunId || undefined },
        )}`,
      ),
    enabled: !isRunPending,
  });
  const modelsQ = useQuery({
    queryKey: ["trading-models"],
    queryFn: () => apiGet<{ models?: TradingModelCatalogEntry[] }>("/api/trading/models"),
  });
  const featureQ = useQuery({
    queryKey: ["feature-intelligence", selectedFeatureModelKey, filters.date_from, filters.date_to],
    queryFn: () => {
      const params = new URLSearchParams();
      params.set("model", selectedFeatureModelKey);
      params.set("date_from", filters.date_from);
      params.set("date_to", filters.date_to);
      return apiGet<FeatureIntelligenceResponse>(`/api/trading/feature-intelligence?${params.toString()}`);
    },
    enabled: Boolean(selectedFeatureModelKey),
  });

  const featureModels = useMemo(
    () => ((modelsQ.data?.models || []) as TradingModelCatalogEntry[]).filter((model) => Boolean(model.ready_to_run)),
    [modelsQ.data],
  );
  const activeFeatureModel = useMemo(
    () => featureModels.find((model) => model.instance_key === selectedFeatureModelKey) || null,
    [featureModels, selectedFeatureModelKey],
  );

  const dayRows = daysQ.data?.rows || [];
  const tradeRows = tradesQ.data?.rows || [];
  const dayTable = useReactTable({ data: dayRows, columns: dayColumns, getCoreRowModel: getCoreRowModel() });
  const tradeTable = useReactTable({ data: tradeRows, columns: tradeColumns, getCoreRowModel: getCoreRowModel() });

  const equityOption = useMemo(() => {
    const points = (equityQ.data?.equity_curve || []) as any[];
    return {
      grid: { left: 40, right: 20, top: 24, bottom: 40 },
      tooltip: { trigger: "axis" },
      xAxis: { type: "category", data: points.map((x) => x.date) },
      yAxis: { type: "value", scale: true },
      series: [{ type: "line", data: points.map((x) => x.equity), smooth: true, areaStyle: { opacity: 0.12 } }],
    };
  }, [equityQ.data]);

  const drawdownOption = useMemo(() => {
    const points = (equityQ.data?.drawdown_curve || []) as any[];
    return {
      grid: { left: 40, right: 20, top: 24, bottom: 40 },
      tooltip: { trigger: "axis" },
      xAxis: { type: "category", data: points.map((x) => x.date) },
      yAxis: { type: "value" },
      series: [{ type: "line", data: points.map((x) => (x.drawdown_pct || 0) * 100), smooth: true }],
    };
  }, [equityQ.data]);

  const dailyReturnOption = useMemo(() => {
    const points = (equityQ.data?.daily_returns || []) as any[];
    return {
      grid: { left: 40, right: 20, top: 24, bottom: 40 },
      tooltip: { trigger: "axis" },
      xAxis: { type: "category", data: points.map((x) => x.date), axisLabel: { hideOverlap: true } },
      yAxis: { type: "value" },
      series: [{ type: "bar", data: points.map((x) => (x.day_return_pct || 0) * 100) }],
    };
  }, [equityQ.data]);
  const featureRankingRows = featureQ.data?.ranking?.rows || [];
  const featureGroupRows = featureQ.data?.groups || [];
  const featureSummary = featureQ.data?.summary || {};
  const featureCoverage = featureQ.data?.model?.coverage || {};
  const featureScatterOption = useMemo(() => {
    const points = featureQ.data?.scatter?.points || [];
    const xAxisLabel = featureQ.data?.scatter?.x_axis_label || "Primary importance";
    const yAxisLabel = featureQ.data?.scatter?.y_axis_label || "Secondary importance";
    const grouped = new Map<string, FeatureScatterPoint[]>();
    for (const point of points) {
      const key = point.group_key || "other";
      const items = grouped.get(key) || [];
      items.push(point);
      grouped.set(key, items);
    }
    return {
      color: Array.from(grouped.keys()).map((key) => FEATURE_GROUP_COLORS[key] || "#355c7d"),
      grid: { left: 52, right: 18, top: 38, bottom: 50 },
      legend: { top: 6, textStyle: { fontSize: 11 } },
      tooltip: {
        trigger: "item",
        formatter: (params: any) => {
          const data = params?.data || {};
          const label = data.featureLabel || data.featureName || "--";
          const canonical = data.featureName || "--";
          const rank = data.rank ?? "--";
          return `${label}<br/>${canonical}<br/>Rank ${rank}<br/>${xAxisLabel}: ${score(data.value?.[0])}<br/>${yAxisLabel}: ${score(data.value?.[1])}`;
        },
      },
      xAxis: {
        type: "value",
        name: xAxisLabel,
        nameLocation: "middle",
        nameGap: 28,
        splitLine: { lineStyle: { color: "#e8eef3" } },
      },
      yAxis: {
        type: "value",
        name: yAxisLabel,
        nameLocation: "middle",
        nameGap: 38,
        splitLine: { lineStyle: { color: "#e8eef3" } },
      },
      series: Array.from(grouped.entries()).map(([groupKey, items]) => ({
        name: items[0]?.group_label || groupKey,
        type: "scatter",
        data: items.map((point) => ({
          value: [point.x, point.y],
          featureLabel: point.feature_label,
          featureName: point.feature_name,
          rank: point.rank,
        })),
        symbolSize: (value: number[], params: any) => {
          const row = items[params.dataIndex];
          return Math.max(10, Math.min(26, 10 + Number(row.importance_score || 0) * 16));
        },
        emphasis: { focus: "series" },
      })),
    };
  }, [featureQ.data]);

  const connectStomp = (runId: string) => {
    appendUiLog("ws connect requested run=" + runId);
    if (clientRef.current) {
      clientRef.current.deactivate();
      appendUiLog("ws previous client deactivated");
    }
    const client = new Client({
      brokerURL: WS_URL,
      reconnectDelay: 3000,
      debug: () => undefined,
      onConnect: () => {
        setWsState("connected");
        appendUiLog("ws connected run=" + runId);
        client.subscribe(`/topic/strategy/eval/run/${runId}`, (msg) => {
          try {
            const parsed = JSON.parse(msg.body);
            const event = normalizeRunEvent(parsed);
            if (!event) {
              appendUiLog("ws event ignored run=" + runId);
              return;
            }
            appendUiLog(
              "ws event type=" +
                event.event_type +
                " run=" +
                (event.run_id || runId) +
                " progress=" +
                String(event.progress_pct ?? "--") +
                " message=" +
                String(event.message || ""),
            );
            setRunEvents((prev) => [event, ...prev].slice(0, 200));
            if (event.event_type === "run_completed" || event.event_type === "evaluation_ready" || event.event_type === "run_failed") {
              appendUiLog("ws terminal event invalidating queries run=" + runId);
              queryClient.invalidateQueries({ queryKey: ["eval-summary"] });
              queryClient.invalidateQueries({ queryKey: ["eval-equity"] });
              queryClient.invalidateQueries({ queryKey: ["eval-days"] });
              queryClient.invalidateQueries({ queryKey: ["eval-trades"] });
              terminalRefreshRunRef.current = runId;
              fetchRun(runId).catch(() => undefined);
            }
          } catch {
            appendUiLog("ws event parse failed run=" + runId);
            return;
          }
        });
        appendUiLog("ws subscribed run=" + runId);
        fetchRun(runId).catch(() => undefined);
      },
      onStompError: () => {
        appendUiLog("ws stomp error run=" + runId);
        setWsState("disconnected");
      },
      onWebSocketClose: () => {
        appendUiLog("ws closed run=" + runId);
        setWsState("disconnected");
      },
    });
    client.activate();
    clientRef.current = client;
  };


  const fetchRun = async (runId: string) => {
    appendUiLog("fetch run start run=" + runId);
    const value = await apiGet<RunStatus>(`/api/strategy/evaluation/runs/${runId}`);
    appendUiLog(
      "fetch run result run=" +
        String(value?.run_id || runId) +
        " status=" +
        String(value?.status || "") +
        " range=" +
        String(value?.date_from || "") +
        ".." +
        String(value?.date_to || ""),
    );
    setRunStatus(value);
    if (value?.run_id) {
      setActiveRunId(String(value.run_id));
    }
    if (value?.date_from && value?.date_to) {
      setActiveRunRange({ date_from: String(value.date_from), date_to: String(value.date_to) });
    }
    const status = String(value?.status || "").toLowerCase();
    const terminal = status === "completed" || status === "failed";
    if (terminal && value.run_id && terminalRefreshRunRef.current !== value.run_id) {
      queryClient.invalidateQueries({ queryKey: ["eval-summary"] });
      queryClient.invalidateQueries({ queryKey: ["eval-equity"] });
      queryClient.invalidateQueries({ queryKey: ["eval-days"] });
      queryClient.invalidateQueries({ queryKey: ["eval-trades"] });
      terminalRefreshRunRef.current = value.run_id;
    }
    if (terminal && clientRef.current) {
      clientRef.current.deactivate();
      setWsState("disconnected");
    }
    return value;
  };

  useEffect(() => {
    const media = window.matchMedia("(max-width: 900px)");
    const onChange = (event: MediaQueryListEvent) => setIsMobile(event.matches);
    setIsMobile(media.matches);
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    if (initialUrlState.hasUrlOverride) {
      appendUiLog(
        "url override detected run=" +
          initialUrlState.runId +
          " range=" +
          initialUrlState.filters.date_from +
          ".." +
          initialUrlState.filters.date_to,
      );
    }
  }, [initialUrlState]);

  useEffect(() => {
    const loadLatestRun = async () => {
      if (initialUrlState.hasUrlOverride) {
        appendUiLog("skip latest-run bootstrap because url override is present");
        return;
      }
      try {
        const latest = await apiGet<RunStatus>("/api/strategy/evaluation/runs/latest?dataset=historical&status=completed");
        setRunStatus((prev) => prev ?? latest);
        if (latest?.run_id) {
          setActiveRunId(String(latest.run_id));
        }
        if (latest?.date_from && latest?.date_to) {
          const nextFilters = applyRunRiskConfig(
            {
              ...initialUrlState.filters,
              dataset: latest.dataset === "live" ? "live" : initialUrlState.filters.dataset,
              date_from: String(latest.date_from),
              date_to: String(latest.date_to),
            },
            latest.risk_config,
          );
          setDraft(nextFilters);
          setFilters(nextFilters);
          setActiveRunRange({ date_from: String(latest.date_from), date_to: String(latest.date_to) });
        }
      } catch {
        return;
      }
    };
    loadLatestRun().catch(() => undefined);
  }, [initialUrlState.hasUrlOverride]);

  useEffect(() => {
    if (!featureModels.length) return;
    const hasSelection = featureModels.some((model) => model.instance_key === selectedFeatureModelKey);
    if (hasSelection) return;
    const preferred = initialUrlState.featureModelKey
      ? featureModels.find((model) => model.instance_key === initialUrlState.featureModelKey)
      : null;
    setSelectedFeatureModelKey((preferred || featureModels[0]).instance_key);
  }, [featureModels, selectedFeatureModelKey, initialUrlState.featureModelKey]);

  useEffect(() => {
    if (!initialUrlState.runId) return;
    let cancelled = false;
    const loadRunFromUrl = async () => {
      try {
        const value = await fetchRun(initialUrlState.runId);
        if (cancelled) return;
        if (value?.date_from && value?.date_to) {
          const nextFilters = applyRunRiskConfig(
            {
              ...initialUrlState.filters,
              dataset: value.dataset === "live" ? "live" : initialUrlState.filters.dataset,
              date_from: String(value.date_from),
              date_to: String(value.date_to),
            } as EvalFilters,
            value.risk_config,
          );
          setDraft(nextFilters);
          setFilters(nextFilters);
          setActiveRunRange({ date_from: String(value.date_from), date_to: String(value.date_to) });
        }
        const status = String(value?.status || "").toLowerCase();
        if (status && !["completed", "failed"].includes(status)) {
          connectStomp(initialUrlState.runId);
        }
      } catch (error) {
        const message = error instanceof Error ? error.message : "unknown";
        appendUiLog("url run bootstrap failed run=" + initialUrlState.runId + " error=" + message);
      }
    };
    loadRunFromUrl().catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [initialUrlState]);

  useEffect(() => {
    if (!runStatus?.run_id) return;
    const interval = window.setInterval(() => {
      fetchRun(runStatus.run_id).catch(() => undefined);
    }, isRunActive ? 2_000 : 10_000);
    return () => window.clearInterval(interval);
  }, [runStatus?.run_id, isRunActive]);

  useEffect(() => {
    syncBrowserUrl(filters, activeRunId, activeRunRange, selectedFeatureModelKey);
  }, [filters, activeRunId, activeRunRange, selectedFeatureModelKey]);

  useEffect(() => {
    return () => {
      if (clientRef.current) {
        clientRef.current.deactivate();
      }
    };
  }, []);

  const onApply = () => {
    appendUiLog(
      "apply filters range=" +
        draft.date_from +
        ".." +
        draft.date_to +
        " strategy=" +
        draft.strategy +
        " regime=" +
        draft.regime,
    );
    setDaysPage(1);
    setTradesPage(1);
    setSelectedDay("");
    setFilters({ ...draft });
  };

  const onRun = async () => {
    const runFilters: EvalFilters = { ...draft };
    appendUiLog(
      "run click range=" +
        runFilters.date_from +
        ".." +
        runFilters.date_to +
        " strategy=" +
        runFilters.strategy +
        " regime=" +
        runFilters.regime,
    );
    setIsSubmittingRun(true);
    setFilters(runFilters);
    setDaysPage(1);
    setTradesPage(1);
    setSelectedDay("");
    setRunEvents([]);
    setWsState("idle");
    setRunStatus(null);
    setActiveRunId("");
    setActiveRunRange({ date_from: runFilters.date_from, date_to: runFilters.date_to });
    terminalRefreshRunRef.current = "";

    try {
      appendUiLog("run queue request posting dataset=" + runFilters.dataset);
      const result = await apiPost<RunStatus>("/api/strategy/evaluation/runs", {
        dataset: runFilters.dataset,
        date_from: runFilters.date_from,
        date_to: runFilters.date_to,
        speed: 0,
        ...toRunRiskPayload(runFilters),
      });
      if (!result?.run_id) {
        throw new Error("Run queued without run_id");
      }
      const queuedRunId = String(result.run_id);
      appendUiLog("run queued run=" + queuedRunId + " status=" + String(result.status || ""));
      setActiveRunId(queuedRunId);
      setRunStatus(result);
      connectStomp(queuedRunId);
      fetchRun(queuedRunId).catch(() => undefined);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to start run";
      appendUiLog("run failed before start: " + message);
      setRunStatus({
        run_id: "",
        status: "failed",
        dataset: runFilters.dataset,
        date_from: runFilters.date_from,
        date_to: runFilters.date_to,
        progress_pct: 0,
        message,
        error: message,
        risk_config: toRunRiskPayload(runFilters),
      });
      setWsState("disconnected");
    } finally {
      setIsSubmittingRun(false);
    }
  };

  const summary = summaryQ.data;
  useEffect(() => {
    if (isRunPending) {
      return;
    }
    const resolvedRunId = String(summary?.resolved_run_id || "").trim();
    const resolvedRange = summary?.resolved_run_range;
    if (resolvedRunId) {
      setActiveRunId((prev) => (prev === resolvedRunId ? prev : resolvedRunId));
    }
    if (resolvedRange?.date_from && resolvedRange?.date_to) {
      setActiveRunRange({ date_from: String(resolvedRange.date_from), date_to: String(resolvedRange.date_to) });
    }
  }, [isRunPending, summary?.resolved_run_id, summary?.resolved_run_range?.date_from, summary?.resolved_run_range?.date_to]);
  const loading = isRunPending || summaryQ.isLoading || equityQ.isLoading || daysQ.isLoading || tradesQ.isLoading;

  useEffect(() => {
    appendUiLog(
      "query state summary=" +
        summaryQ.status +
        "/" +
        summaryQ.fetchStatus +
        " equity=" +
        equityQ.status +
        "/" +
        equityQ.fetchStatus +
        " days=" +
        daysQ.status +
        "/" +
        daysQ.fetchStatus +
        " trades=" +
        tradesQ.status +
        "/" +
        tradesQ.fetchStatus +
        " activeRun=" +
        activeRunId +
        " filterRange=" +
        filters.date_from +
        ".." +
        filters.date_to,
    );
  }, [
    activeRunId,
    filters.date_from,
    filters.date_to,
    summaryQ.status,
    summaryQ.fetchStatus,
    equityQ.status,
    equityQ.fetchStatus,
    daysQ.status,
    daysQ.fetchStatus,
    tradesQ.status,
    tradesQ.fetchStatus,
  ]);
  const hasData = Number(summary?.counts?.closed_trades || 0) > 0;
  const exitReasonRows = Array.isArray(summary?.exit_reasons) ? summary.exit_reasons : [];
  const byStrategyRows = Array.isArray(summary?.by_strategy) ? summary.by_strategy : [];
  const byRegimeRows = Array.isArray(summary?.by_regime) ? summary.by_regime : [];
  const stopAnalysis = summary?.stop_analysis || {};
  const chartHeight = isMobile ? 230 : 280;
  const fullChartHeight = isMobile ? 220 : 260;
  const canApply = Boolean(draft.date_from && draft.date_to);
  const displayedRunRisk = runStatus?.risk_config ? applyRunRiskConfig(DEFAULT_FILTERS, runStatus.risk_config) : null;
  const featureCoverageBadgeClass =
    featureCoverage.requested_range_in_coverage === false ? "warn" : featureQ.data?.status === "ok" ? "ok" : "";
  const featureCoverageLabel =
    featureCoverage.requested_range_in_coverage === false
      ? "Date Range Outside Coverage"
      : featureCoverage.training_start && featureCoverage.training_end
        ? `Coverage ${featureCoverage.training_start} to ${featureCoverage.training_end}`
        : "Coverage Pending";
  const featureStatusText =
    modelsQ.isLoading || (featureQ.isLoading && !featureQ.data)
      ? "Loading feature contract..."
      : featureQ.error instanceof Error
        ? featureQ.error.message
        : featureModels.length === 0
          ? "No ready model artifacts found."
          : featureQ.data?.status === "no_data"
            ? "No mapped v1 feature intelligence available for this model."
            : "";
  const featureRankingPreview = featureRankingRows.slice(0, 12);

  return (
    <div className="page">
      <header className="hero">
        <div>
          <h1>Strategy Evaluation</h1>
          <p>Historical strategy analytics with replay + live progress (STOMP)</p>
        </div>
        <div className="badge-row">
          <span className="badge">{filters.dataset.toUpperCase()}</span>
          <span className={`badge ${wsState === "connected" ? "ok" : "warn"}`}>WS {wsState}</span>
        </div>
      </header>

      <section className="panel sticky">
        <div className="filter-grid">
          <label>From<input type="date" value={draft.date_from} onChange={(e) => setDraft({ ...draft, date_from: e.target.value })} /></label>
          <label>To<input type="date" value={draft.date_to} onChange={(e) => setDraft({ ...draft, date_to: e.target.value })} /></label>
          <label>Strategy<input placeholder="ORB,OI_BUILDUP" value={draft.strategy} onChange={(e) => setDraft({ ...draft, strategy: e.target.value })} /></label>
          <label>Regime<input placeholder="TRENDING,SIDEWAYS" value={draft.regime} onChange={(e) => setDraft({ ...draft, regime: e.target.value })} /></label>
          <label>Initial Capital<input type="number" value={draft.initial_capital} onChange={(e) => setDraft({ ...draft, initial_capital: Number(e.target.value || 0) })} /></label>
          <label>Cost (bps)<input type="number" value={draft.cost_bps} onChange={(e) => setDraft({ ...draft, cost_bps: Number(e.target.value || 0) })} /></label>
        </div>
        <div className="section-caption">Replay Risk Config</div>
        <div className="filter-grid">
          <label>Stop Loss %<input type="number" value={draft.stop_loss_pct} onChange={(e) => setDraft({ ...draft, stop_loss_pct: Number(e.target.value || 0) })} /></label>
          <label>Target %<input type="number" value={draft.target_pct} onChange={(e) => setDraft({ ...draft, target_pct: Number(e.target.value || 0) })} /></label>
          <label>
            Trailing Enabled
            <select value={draft.trailing_enabled ? "true" : "false"} onChange={(e) => setDraft({ ...draft, trailing_enabled: e.target.value === "true" })}>
              <option value="false">Off</option>
              <option value="true">On</option>
            </select>
          </label>
          <label>Trail Activate %<input type="number" value={draft.trailing_activation_pct} onChange={(e) => setDraft({ ...draft, trailing_activation_pct: Number(e.target.value || 0) })} /></label>
          <label>Trail Offset %<input type="number" value={draft.trailing_offset_pct} onChange={(e) => setDraft({ ...draft, trailing_offset_pct: Number(e.target.value || 0) })} /></label>
          <label>
            Lock Breakeven
            <select value={draft.trailing_lock_breakeven ? "true" : "false"} onChange={(e) => setDraft({ ...draft, trailing_lock_breakeven: e.target.value === "true" })}>
              <option value="true">Yes</option>
              <option value="false">No</option>
            </select>
          </label>
        </div>
        <div className="actions">
          <button disabled={!canApply} onClick={onApply}>Apply</button>
          <button onClick={() => setDraft({ ...filters })}>Reset</button>
          <button disabled={isRunPending || !canApply} onClick={onRun}>Run Replay + Evaluate</button>
        </div>
      </section>

      <section className="kpi-grid">
        <Kpi title="Closed Trades" value={summary?.counts?.closed_trades} loading={loading} />
        <Kpi title="Win Rate" value={pct(summary?.overall?.win_rate)} loading={loading} />
        <Kpi title="Capital Return" value={pct(summary?.equity?.net_return_pct)} loading={loading} />
        <Kpi title="End Equity" value={num(summary?.equity?.end_capital)} loading={loading} />
        <Kpi title="Max Drawdown" value={pct(summary?.equity?.max_drawdown_pct)} loading={loading} />
        <Kpi title="Profit Factor" value={num(summary?.overall?.profit_factor)} loading={loading} />
        <Kpi title="Trade Loss Streak" value={summary?.streaks?.max_trade_loss_streak} loading={loading} />
        <Kpi title="Day Loss Streak" value={summary?.streaks?.max_day_loss_streak} loading={loading} />
      </section>

      <section className="summary-grid">
        <div className="panel summary-panel">
          <div className="table-head compact">
            <h3>Stop Analytics</h3>
          </div>
          <div className="kpi-grid compact-kpis">
            <Kpi title="Stop Loss Exit %" value={pct(stopAnalysis.stop_loss_exit_pct)} loading={loading} />
            <Kpi title="Trailing Stop Exit %" value={pct(stopAnalysis.trailing_stop_exit_pct)} loading={loading} />
            <Kpi title="Trailing Active %" value={pct(stopAnalysis.trailing_active_trade_pct)} loading={loading} />
            <Kpi title="Avg Locked Gain" value={pct(stopAnalysis.avg_locked_gain_pct_before_trailing_exit)} loading={loading} />
            <Kpi title="Trail Profit Capture" value={pct(stopAnalysis.avg_trailing_profit_capture_pct)} loading={loading} />
            <Kpi title="Avg Stop Config" value={pct(stopAnalysis.avg_configured_stop_loss_pct)} loading={loading} />
            <Kpi title="Avg Target Config" value={pct(stopAnalysis.avg_configured_target_pct)} loading={loading} />
            <Kpi title="Trailing Stops" value={stopAnalysis.trailing_stop_exits ?? "--"} loading={loading} />
          </div>
        </div>
        <div className="panel summary-panel">
          <div className="table-head compact">
            <h3>Exit Reason Breakdown</h3>
          </div>
          {isMobile ? (
            <div className="mobile-list">
              {exitReasonRows.map((row: any) => (
                <div key={String(row.exit_reason || "UNKNOWN")} className="mobile-card">
                  <div className="mobile-card-head">
                    <strong>{row.exit_reason || "UNKNOWN"}</strong>
                    <span>{pct(row.pct)}</span>
                  </div>
                  <div className="mobile-card-grid">
                    <span>Count: {row.count ?? 0}</span>
                    <span>Avg Capital PnL: {pct(row.avg_capital_pnl_pct)}</span>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="table-wrap summary-table">
              <table>
                <thead>
                  <tr>
                    <th>Exit Reason</th>
                    <th>Count</th>
                    <th>% of Trades</th>
                    <th>Avg Capital PnL%</th>
                  </tr>
                </thead>
                <tbody>
                  {exitReasonRows.map((row: any) => (
                    <tr key={String(row.exit_reason || "UNKNOWN")}>
                      <td>{row.exit_reason || "UNKNOWN"}</td>
                      <td>{row.count ?? 0}</td>
                      <td>{pct(row.pct)}</td>
                      <td>{pct(row.avg_capital_pnl_pct)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>

      <section className="summary-grid">
        <div className="panel summary-panel">
          <div className="table-head compact">
            <h3>Strategy Comparison</h3>
          </div>
          <div className="table-wrap summary-table">
            <table>
              <thead>
                <tr>
                  <th>Strategy</th>
                  <th>Trades</th>
                  <th>Win Rate</th>
                  <th>Avg Capital PnL%</th>
                  <th>Total Capital PnL%</th>
                  <th>Profit Factor</th>
                </tr>
              </thead>
              <tbody>
                {byStrategyRows.map((row: any) => (
                  <tr key={String(row.entry_strategy || "UNKNOWN")}>
                    <td>{row.entry_strategy || "UNKNOWN"}</td>
                    <td>{row.trades ?? 0}</td>
                    <td>{pct(row.win_rate)}</td>
                    <td>{pct(row.avg_capital_pnl_pct)}</td>
                    <td>{pct(row.total_capital_pnl_pct)}</td>
                    <td>{num(row.profit_factor)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
        <div className="panel summary-panel">
          <div className="table-head compact">
            <h3>Regime Comparison</h3>
          </div>
          <div className="table-wrap summary-table">
            <table>
              <thead>
                <tr>
                  <th>Regime</th>
                  <th>Trades</th>
                  <th>Win Rate</th>
                  <th>Avg Capital PnL%</th>
                  <th>Total Capital PnL%</th>
                  <th>Profit Factor</th>
                </tr>
              </thead>
              <tbody>
                {byRegimeRows.map((row: any) => (
                  <tr key={String(row.regime || "UNKNOWN")}>
                    <td>{row.regime || "UNKNOWN"}</td>
                    <td>{row.trades ?? 0}</td>
                    <td>{pct(row.win_rate)}</td>
                    <td>{pct(row.avg_capital_pnl_pct)}</td>
                    <td>{pct(row.total_capital_pnl_pct)}</td>
                    <td>{num(row.profit_factor)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <section className="panel run-panel">
        <h2>Run Status</h2>
        <div className="run-meta">
          <span>Run: {activeRunId || "--"}</span>
          <span>Range: {activeRunRange ? `${activeRunRange.date_from} to ${activeRunRange.date_to}` : "--"}</span>
          <span>Status: {runStatus?.status || "--"}</span>
          <span>Progress: {runStatus?.progress_pct ?? 0}%</span>
          <span>Message: {runStatus?.message || "--"}</span>
        </div>
        <div className="run-meta">
          <span>Stop Loss: {displayedRunRisk ? `${num(displayedRunRisk.stop_loss_pct)}%` : "--"}</span>
          <span>Target: {displayedRunRisk ? `${num(displayedRunRisk.target_pct)}%` : "--"}</span>
          <span>Trailing: {displayedRunRisk ? boolLabel(displayedRunRisk.trailing_enabled) : "--"}</span>
          <span>Trail Activate: {displayedRunRisk ? `${num(displayedRunRisk.trailing_activation_pct)}%` : "--"}</span>
          <span>Trail Offset: {displayedRunRisk ? `${num(displayedRunRisk.trailing_offset_pct)}%` : "--"}</span>
          <span>Lock Breakeven: {displayedRunRisk ? boolLabel(displayedRunRisk.trailing_lock_breakeven) : "--"}</span>
        </div>
        <div className="run-meta">
          <span>Sharable URL: {typeof window !== "undefined" ? window.location.search || "--" : "--"}</span>
        </div>
        <div className="run-events">
          {runEvents.slice(0, 6).map((item, idx) => (
            <div key={`${item.timestamp}-${idx}`} className="run-event">
              <strong>{item.event_type}</strong>
              <span>{item.message || "--"}</span>
              <span>{item.progress_pct ?? "--"}%</span>
            </div>
          ))}
        </div>
        {debugEnabled ? (
          <details className="debug-panel">
            <summary>UI Debug Log</summary>
            <div className="run-events">
              {debugLogs.length ? (
                debugLogs.map((item, idx) => (
                  <div key={`${item.timestamp}-${idx}`} className="run-event">
                    <strong>{item.timestamp}</strong>
                    <span>{item.message}</span>
                  </div>
                ))
              ) : (
                <div className="run-event">
                  <span>No UI logs yet.</span>
                </div>
              )}
            </div>
          </details>
        ) : null}
      </section>

      <section className="panel intelligence-panel">
        <div className="table-head">
          <div>
            <h3>Feature Intelligence</h3>
            <div className="section-caption feature-caption">snapshot_ml_flat_v1 grouped feature dictionary</div>
          </div>
          <div className="badge-row">
            <span className={`badge ${featureCoverageBadgeClass}`}>{featureCoverageLabel}</span>
            <span className="badge">{featureQ.data?.contract?.contract_id || "snapshot_ml_flat_v1"}</span>
          </div>
        </div>

        <div className="feature-toolbar">
          <label>
            Model
            <select disabled={!featureModels.length} value={selectedFeatureModelKey} onChange={(e) => setSelectedFeatureModelKey(e.target.value)}>
              {featureModels.map((model) => (
                <option key={model.instance_key} value={model.instance_key}>
                  {model.title}
                </option>
              ))}
            </select>
          </label>
          <div className="feature-context-grid">
            <div className="feature-context-item">
              <span className="feature-context-label">Model Run</span>
              <strong>{featureQ.data?.model?.run_id || activeFeatureModel?.run_id || "--"}</strong>
            </div>
            <div className="feature-context-item">
              <span className="feature-context-label">Profile</span>
              <strong>{featureQ.data?.model?.profile_id || activeFeatureModel?.profile_id || "--"}</strong>
            </div>
            <div className="feature-context-item">
              <span className="feature-context-label">Applied Date Range</span>
              <strong>{filters.date_from} to {filters.date_to}</strong>
            </div>
            <div className="feature-context-item">
              <span className="feature-context-label">Model Spec</span>
              <strong>{featureQ.data?.model?.selected_model_name || "--"}</strong>
            </div>
          </div>
        </div>

        {featureStatusText ? (
          <div className="feature-empty">{featureStatusText}</div>
        ) : (
          <>
            <div className="feature-stat-grid">
              <FeatureStat title="Active V1 Features" value={featureSummary.selected_v1_feature_count} />
              <FeatureStat title="Contract Groups" value={featureSummary.contract_group_count} />
              <FeatureStat title="Scatter Points" value={featureSummary.scatter_point_count} />
              <FeatureStat title="Legacy Inputs Removed" value={featureSummary.removed_legacy_feature_count} />
            </div>

            {(Number(featureSummary.removed_legacy_feature_count || 0) > 0 || Number(featureSummary.unmapped_feature_count || 0) > 0) && (
              <div className="feature-note">
                UI is showing mapped `snapshot_ml_flat_v1` fields only. Hidden inputs: removed legacy {featureSummary.removed_legacy_feature_count ?? 0}, unmapped {featureSummary.unmapped_feature_count ?? 0}.
              </div>
            )}

            <div className="feature-analysis-grid">
              <div className="feature-block">
                <div className="feature-block-head">
                  <h4>Ranking</h4>
                  <span>{featureQ.isFetching ? "Refreshing..." : `Top ${featureRankingPreview.length}`}</span>
                </div>
                <div className="table-wrap">
                  <table className="feature-ranking-table">
                    <thead>
                      <tr>
                        <th>Rank</th>
                        <th>Feature</th>
                        <th>Group</th>
                        <th>Importance</th>
                      </tr>
                    </thead>
                    <tbody>
                      {featureRankingPreview.map((row) => (
                        <tr key={row.feature_name}>
                          <td>{row.rank ?? "--"}</td>
                          <td>
                            <div className="feature-name-cell">
                              <strong>{row.feature_label}</strong>
                              <span>{row.feature_name}</span>
                            </div>
                          </td>
                          <td>{row.group_label}</td>
                          <td>{score(row.importance_score)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>

              <div className="feature-block">
                <div className="feature-block-head">
                  <h4>Scatter</h4>
                  <span>{featureQ.data?.scatter?.x_axis_label || "Primary"} vs {featureQ.data?.scatter?.y_axis_label || "Secondary"}</span>
                </div>
                <ReactECharts option={featureScatterOption} style={{ height: isMobile ? 260 : 320 }} />
              </div>
            </div>

            <div className="feature-groups-grid">
              {featureGroupRows.map((group) => (
                <section key={group.group_key} className="feature-group-card">
                  <div className="feature-group-head">
                    <div>
                      <h4>{group.group_label}</h4>
                      <span>{group.selected_columns_count}/{group.contract_columns_total} active</span>
                    </div>
                    <strong>{score(group.importance_mean)}</strong>
                  </div>
                  <div className="feature-list">
                    {group.features.map((feature) => (
                      <div
                        key={feature.feature_name}
                        className={`feature-chip ${feature.is_selected ? "active" : "inactive"}`}
                        style={
                          feature.is_selected
                            ? { borderColor: FEATURE_GROUP_COLORS[group.group_key] || "#c4d2dc" }
                            : undefined
                        }
                      >
                        <div className="feature-chip-head">
                          <strong>{feature.feature_label}</strong>
                          <span>{feature.is_selected ? score(feature.importance_score) : "Not in model"}</span>
                        </div>
                        <div className="feature-chip-meta">{feature.feature_name}</div>
                      </div>
                    ))}
                  </div>
                </section>
              ))}
            </div>
          </>
        )}
      </section>

      {!hasData && !loading && <section className="panel empty">No closed trades in selected range.</section>}

      <section className="chart-grid">
        <div className="panel chart"><h3>Equity Curve</h3><ReactECharts option={equityOption} style={{ height: chartHeight }} /></div>
        <div className="panel chart"><h3>Drawdown</h3><ReactECharts option={drawdownOption} style={{ height: chartHeight }} /></div>
        <div className="panel chart full"><h3>Daily Returns</h3><ReactECharts option={dailyReturnOption} style={{ height: fullChartHeight }} /></div>
      </section>

      <section className="panel">
        <div className="table-head">
          <h3>Per-day Summary</h3>
          <div className="table-actions">
            <button onClick={() => csvDownload("days.csv", dayRows)}>Export CSV</button>
          </div>
        </div>
        {isMobile ? (
          <div className="mobile-list">
            {dayRows.map((row: any) => (
              <button
                key={String(row.date)}
                className={`mobile-card day-card ${selectedDay === row.date ? "active" : ""}`}
                onClick={() => setSelectedDay(String(row.date || ""))}
              >
                <div className="mobile-card-head">
                  <strong>{row.date}</strong>
                  <span>{pct(row.day_return_pct)}</span>
                </div>
                <div className="mobile-card-grid">
                  <span>Trades: {row.trades ?? 0}</span>
                  <span>Win: {pct(row.win_rate)}</span>
                  <span>PnL: {num(row.day_pnl_amount)}</span>
                  <span>EOD: {num(row.equity_end)}</span>
                </div>
              </button>
            ))}
          </div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                {dayTable.getHeaderGroups().map((hg) => (
                  <tr key={hg.id}>
                    {hg.headers.map((header) => (
                      <th key={header.id}>{header.isPlaceholder ? null : flexRender(header.column.columnDef.header, header.getContext())}</th>
                    ))}
                  </tr>
                ))}
              </thead>
              <tbody>
                {dayTable.getRowModel().rows.map((row) => (
                  <tr key={row.id} onClick={() => setSelectedDay(String(row.original.date || ""))}>
                    {row.getVisibleCells().map((cell) => (
                      <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="panel">
        <div className="table-head">
          <h3>Trades {selectedDay ? `(Filtered: ${selectedDay})` : ""}</h3>
          <div className="table-actions">
            <button onClick={() => setSelectedDay("")}>Clear Day Filter</button>
            <button onClick={() => csvDownload("trades.csv", tradeRows)}>Export CSV</button>
          </div>
        </div>
        {isMobile ? (
          <div className="mobile-list">
            {tradeRows.map((row: any, idx: number) => (
              <div key={`${row.position_id || "p"}-${idx}`} className="mobile-card trade-card">
                <div className="mobile-card-head">
                  <strong>{row.trade_date_ist || "--"}</strong>
                  <span>{pct(row.capital_pnl_pct)}</span>
                </div>
                <div className="mobile-card-grid">
                  <span>{row.entry_strategy || "--"}</span>
                  <span>{row.regime || "--"}</span>
                  <span>{row.direction || "--"}</span>
                  <span>Capital PnL: {num(row.capital_pnl_amount)}</span>
                  <span>Option PnL: {pct(row.pnl_pct_net)}</span>
                  <span>Bars: {row.bars_held ?? "--"}</span>
                  <span>MFE: {pct(row.mfe_pct)}</span>
                  <span>MAE: {pct(row.mae_pct)}</span>
                  <span>Stop: {pct(row.stop_loss_pct)}</span>
                  <span>Entry Stop: {num(row.entry_stop_price)}</span>
                  <span>Exit Stop: {num(row.exit_stop_price)}</span>
                  <span>Trail On: {boolLabel(row.trailing_enabled)}</span>
                  <span>Trail Active: {boolLabel(row.trailing_active)}</span>
                  <span>Entry: {row.entry_time || "--"}</span>
                  <span>Exit: {row.exit_time || "--"}</span>
                  <span>Exit Reason: {row.exit_reason || "--"}</span>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                {tradeTable.getHeaderGroups().map((hg) => (
                  <tr key={hg.id}>
                    {hg.headers.map((header) => (
                      <th key={header.id}>{header.isPlaceholder ? null : flexRender(header.column.columnDef.header, header.getContext())}</th>
                    ))}
                  </tr>
                ))}
              </thead>
              <tbody>
                {tradeTable.getRowModel().rows.map((row) => (
                  <tr key={row.id}>
                    {row.getVisibleCells().map((cell) => (
                      <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

function Kpi({ title, value, loading }: { title: string; value: any; loading: boolean }) {
  return (
    <div className="kpi panel">
      <div className="kpi-title">{title}</div>
      <div className="kpi-value">{loading ? "..." : value ?? "--"}</div>
    </div>
  );
}

function FeatureStat({ title, value }: { title: string; value: any }) {
  return (
    <div className="feature-stat">
      <span>{title}</span>
      <strong>{value ?? "--"}</strong>
    </div>
  );
}


