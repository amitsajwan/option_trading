/* global React, KpiStrip, EChartPanel, PaginatedTable */
const {
  useEffect: _evalUseEffect,
  useMemo: _evalUseMemo,
  useRef: _evalUseRef,
  useState: _evalUseState,
} = React;

const EVAL_DEFAULTS = {
  dataset: 'historical',
  date_from: '2024-01-01',
  date_to: '2024-01-31',
  strategy: '',
  regime: '',
  initial_capital: 1000,
  cost_bps: 0,
  stop_loss_pct: 40,
  target_pct: 80,
  trailing_enabled: true,
  trailing_activation_pct: 35,
  trailing_offset_pct: 15,
  trailing_lock_breakeven: true,
};

const EVAL_R1S_TOP3_PRESET = {
  date_from: '2024-05-01',
  date_to: '2024-07-31',
  strategy: 'R1S_TOP3_SHORT_CE',
  stop_loss_pct: 100,
  target_pct: 50,
  trailing_enabled: false,
  trailing_activation_pct: 0,
  trailing_offset_pct: 0,
  trailing_lock_breakeven: false,
};

const EVAL_TRADER_MASTER_PRESET = {
  date_from: '2024-05-01',
  date_to: '2024-07-31',
  strategy: '',
  regime: '',
  stop_loss_pct: 25,
  target_pct: 70,
  trailing_enabled: true,
  trailing_activation_pct: 12,
  trailing_offset_pct: 6,
  trailing_lock_breakeven: true,
};

const EVAL_DEBIT_MULTI_PRESET = {
  date_from: '2024-10-31',
  date_to: '2024-10-31',
  strategy: '',
  regime: '',
  stop_loss_pct: 10,
  target_pct: 60,
  trailing_enabled: false,
  trailing_activation_pct: 0,
  trailing_offset_pct: 0,
  trailing_lock_breakeven: false,
};

const EVAL_DATE_PRESETS = [
  { id: '', label: 'Custom dates' },
  { id: 'oct31_2024', label: '2024-10-31 (debit smoke day)', date_from: '2024-10-31', date_to: '2024-10-31' },
  { id: 'may_jul_2024', label: 'May–Jul 2024', date_from: '2024-05-01', date_to: '2024-07-31' },
  { id: 'jan_2024', label: 'Jan 2024', date_from: '2024-01-01', date_to: '2024-01-31' },
  { id: 'q3_2024', label: 'Jul–Sep 2024', date_from: '2024-07-01', date_to: '2024-09-30' },
];

const EVAL_REGIME_OPTIONS = ['', 'TRENDING', 'SIDEWAYS', 'EXPIRY', 'PRE_EXPIRY', 'HIGH_VOL', 'AVOID'];

function evalDatePresetId(from, to) {
  const match = EVAL_DATE_PRESETS.find(p => p.date_from === from && p.date_to === to);
  return match ? match.id : '';
}

function evalApplyDatePreset(draft, presetId) {
  const preset = EVAL_DATE_PRESETS.find(p => p.id === presetId);
  if (!preset || !preset.date_from) return draft;
  return { ...draft, date_from: preset.date_from, date_to: preset.date_to };
}

function evalStrategyLabel(id) {
  const text = String(id || '').trim();
  if (!text) return 'All strategies';
  return text.replace(/_/g, ' ');
}

function evalNormalizeBreakdownRows(rows, keyField) {
  return (rows || []).map(row => {
    const key = row[keyField] || row.strategy || row.regime || 'UNKNOWN';
    return {
      ...row,
      [keyField]: key,
      strategy: row.strategy || (keyField === 'entry_strategy' ? key : row.strategy),
      regime: row.regime || (keyField === 'regime' ? key : row.regime),
      avg_pnl_pct: row.avg_pnl_pct ?? row.avg_capital_pnl_pct,
      total_pnl_pct: row.total_pnl_pct ?? row.total_capital_pnl_pct,
      avg_option_pnl_pct: row.avg_option_pnl_pct ?? row.avg_trade_pnl_pct,
    };
  });
}

/** Entry playbooks only (regime map). Excludes ORB/OI exit helpers from exit_strategies. */
function evalProfileEntryStrategyIds(profile) {
  if (!profile) return [];
  const ids = new Set();
  Object.values(profile.regime_entry_map || {}).forEach(strategies => {
    (strategies || []).forEach(s => { if (s && s !== 'IV_FILTER') ids.add(s); });
  });
  return Array.from(ids).sort();
}

/** Display-filter options: active VM profile + strategies seen in this eval run (not full catalog). */
function evalCollectStrategyIds(catalog, strategyRows, activeProfile, includeFullCatalog = false) {
  const ids = new Set();
  evalProfileEntryStrategyIds(activeProfile).forEach(s => ids.add(s));
  (strategyRows || []).forEach(r => {
    const sid = r.entry_strategy || r.strategy;
    if (sid) ids.add(sid);
  });
  if (includeFullCatalog || !ids.size) {
    (catalog?.all_entry_strategy_ids || []).forEach(s => ids.add(s));
  }
  return Array.from(ids).sort();
}

function evalProfileForId(catalog, profileId) {
  return (catalog?.profiles || []).find(p => p.profile_id === profileId) || null;
}

function evalDefaultsFromTweaks(tweaks) {
  const base = { ...EVAL_DEFAULTS, dataset: tweaks?.evalDefaultDataset || 'historical' };
  if (tweaks?.evalPreset === 'r1s_top3') {
    return { ...base, ...EVAL_R1S_TOP3_PRESET };
  }
  if ((tweaks?.evalDefaultStrategy || '').trim()) {
    base.strategy = String(tweaks.evalDefaultStrategy).trim();
  }
  return base;
}

function evalNum(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '--';
  return Number(v).toLocaleString(undefined, { maximumFractionDigits: digits });
}

function evalPct(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '--';
  return `${(Number(v) * 100).toFixed(digits)}%`;
}

function evalBool(v) {
  if (v === null || v === undefined) return '--';
  return v ? 'Yes' : 'No';
}

function evalScore(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '--';
  return Number(v).toFixed(3);
}

async function evalGet(path) {
  const res = await fetch(path);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
  return body;
}

async function evalPost(path, payload) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
  return body;
}

function evalQuery(filters, extra) {
  const params = new URLSearchParams();
  params.set('dataset', filters.dataset || 'historical');
  params.set('date_from', filters.date_from || '');
  params.set('date_to', filters.date_to || '');
  if ((filters.strategy || '').trim()) params.set('strategy', filters.strategy.trim());
  if ((filters.regime || '').trim()) params.set('regime', filters.regime.trim());
  params.set('initial_capital', String(filters.initial_capital || 1000));
  params.set('cost_bps', String(filters.cost_bps || 0));
  Object.entries(extra || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && String(v).trim() !== '') params.set(k, String(v));
  });
  return params.toString();
}

function evalCsv(filename, rows) {
  const safeRows = rows || [];
  if (!safeRows.length) return;
  const headers = Object.keys(safeRows[0]);
  const lines = [headers.join(',')].concat(safeRows.map(row =>
    headers.map(h => `"${String(row[h] ?? '').replace(/"/g, '""')}"`).join(',')
  ));
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function evalShortRunId(runId) {
  const text = String(runId || '').trim();
  if (!text) return '--';
  if (text.length <= 14) return text;
  return `${text.slice(0, 8)}…${text.slice(-4)}`;
}

function evalFormatIstTimestamp(ts) {
  const raw = String(ts || '').trim();
  if (!raw) return '--';
  try {
    const d = new Date(raw);
    if (Number.isNaN(d.getTime())) return raw.slice(0, 19).replace('T', ' ');
    return d.toLocaleString('en-IN', {
      timeZone: 'Asia/Kolkata',
      year: 'numeric',
      month: 'short',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch (_) {
    return raw.slice(0, 19).replace('T', ' ');
  }
}

function evalSortRunsDesc(rows) {
  const list = Array.isArray(rows) ? [...rows] : [];
  list.sort((a, b) => {
    const key = r => String(r?.ended_at || r?.submitted_at || r?.updated_at || r?.date_to || '').trim();
    const ta = Date.parse(key(a)) || 0;
    const tb = Date.parse(key(b)) || 0;
    if (tb !== ta) return tb - ta;
    return String(b?.run_id || '').localeCompare(String(a?.run_id || ''));
  });
  return list;
}

function evalRunOptionLabel(run) {
  const from = String(run?.date_from || '').slice(0, 10);
  const to = String(run?.date_to || '').slice(0, 10);
  const range = from && to ? `${from} → ${to}` : (from || to || 'window ?');
  const trades = run?.trade_count != null ? `${run.trade_count}T` : '';
  const profile = String(run?.strategy_profile_id || '').trim();

  // Derive a short human tag from message or profile
  const msg = String(run?.message || '').trim();
  let tag = '';
  if (msg && !msg.startsWith('Replay finished') && !msg.startsWith('tmux replay')) {
    // custom message — use up to 30 chars
    tag = msg.length > 30 ? msg.slice(0, 28) + '…' : msg;
  } else if (profile) {
    // shorten known long profile names
    tag = profile
      .replace('trader_master_ml_entry_det_dir_v1', 'tm_ml_det_dir')
      .replace('trader_master_ml_entry_v1', 'tm_ml_entry')
      .replace('trader_master_v1', 'tm_v1')
      .replace('playbook_v1_paper_v1', 'pbv1')
      .replace('debit_multi_v1', 'debit_multi')
      .replace('r1s_top3_paper_v1', 'r1s_top3')
      .replace('ml_pure_staged_v1', 'ml_pure');
  }

  // Date of this run — prefer ended_at for registered runs, submitted_at otherwise
  const runDate = run?.ended_at || run?.submitted_at || run?.updated_at || '';
  const dateStr = runDate ? evalFormatIstTimestamp(runDate).slice(0, 11).trim() : '';

  const discovered = run?.discovered ? '⊕' : '';
  const parts = [range, tag, trades, dateStr, discovered, evalShortRunId(run?.run_id)].filter(Boolean);
  return parts.join(' · ');
}

function EvalRunDetails({ run, vmProfileId }) {
  if (!run || !String(run.run_id || '').trim()) return null;
  const profile = String(run.strategy_profile_id || '').trim();
  const vmProfile = String(vmProfileId || '').trim();
  const profileMismatch = profile && vmProfile && profile !== vmProfile;
  const from = String(run.date_from || '').slice(0, 10);
  const to = String(run.date_to || '').slice(0, 10);
  const discovered = Boolean(run.discovered);
  return (
    <div className="eval-run-details">
      <div className="eval-run-details-grid">
        <div>
          <div className="field-label">Run ID</div>
          <code className="mono tiny eval-run-id-full">{String(run.run_id || '')}</code>
        </div>
        <div>
          <div className="field-label">Replay window</div>
          <strong>{from && to ? `${from} → ${to}` : (from || to || '--')}</strong>
        </div>
        <div>
          <div className="field-label">Queued (IST)</div>
          <strong>{evalFormatIstTimestamp(run.submitted_at)}</strong>
        </div>
        <div>
          <div className="field-label">Finished (IST)</div>
          <strong>{evalFormatIstTimestamp(run.ended_at || run.updated_at)}</strong>
        </div>
        <div>
          <div className="field-label">Profile (from trades)</div>
          <strong className="mono">{profile || (discovered ? '— check trades —' : '--')}</strong>
        </div>
        <div>
          <div className="field-label">Status</div>
          <strong>{String(run.status || 'unknown')}</strong>
          {run.trade_count != null ? (
            <span className="muted tiny"> · {run.trade_count} closed trades</span>
          ) : null}
        </div>
        {run.message ? (
          <div style={{gridColumn: '1 / -1'}}>
            <div className="field-label">Notes</div>
            <span className="muted tiny">{String(run.message)}</span>
          </div>
        ) : null}
      </div>
      {profileMismatch && (
        <p className="muted tiny eval-run-details-note">
          Trades used <code>{profile}</code>; VM is now <code>{vmProfile}</code> (redeploy or different engine).
        </p>
      )}
      <p className="muted tiny eval-run-details-note">
        Dropdown lines summarize each run; details above are for the selection. Profile is read from Mongo trades for this <code>run_id</code>, not the Trader book chip (that is today&apos;s VM only).
      </p>
    </div>
  );
}

function evalReplayDeepLink({ runId, dateFrom, dateTo }) {
  try {
    const url = new URL(window.location.href);
    url.searchParams.set('mode', 'replay');
    const day = String(dateFrom || dateTo || '').slice(0, 10);
    if (day) url.searchParams.set('date', day);
    else url.searchParams.delete('date');
    if (runId) url.searchParams.set('run_id', runId);
    else url.searchParams.delete('run_id');
    return url.toString();
  } catch (_) {
    return '/app/?mode=replay';
  }
}

function evalSyncEvalUrl({ runId, filters }) {
  try {
    const url = new URL(window.location.href);
    url.searchParams.set('mode', 'eval');
    if (runId) url.searchParams.set('run_id', runId);
    else url.searchParams.delete('run_id');
    if (filters?.date_from) url.searchParams.set('date_from', filters.date_from);
    else url.searchParams.delete('date_from');
    if (filters?.date_to) url.searchParams.set('date_to', filters.date_to);
    else url.searchParams.delete('date_to');
    if (filters?.dataset) url.searchParams.set('dataset', filters.dataset);
    window.history.replaceState({}, '', url);
  } catch (_) {
    /* ignore */
  }
}

async function evalCopyText(text) {
  const value = String(text || '').trim();
  if (!value) return;
  try {
    await navigator.clipboard.writeText(value);
  } catch (_) {
    window.prompt('Copy run id', value);
  }
}

function normalizeRunEvent(input) {
  const raw = input && typeof input === 'object' && input.data ? input.data : input;
  if (!raw || typeof raw !== 'object') return null;
  const type = String(raw.event_type || '').trim();
  if (!type) return null;
  return {
    event_type: type,
    run_id: String(raw.run_id || '').trim(),
    progress_pct: typeof raw.progress_pct === 'number' ? raw.progress_pct : undefined,
    current_day: raw.current_day ? String(raw.current_day) : '',
    total_days: typeof raw.total_days === 'number' ? raw.total_days : undefined,
    message: raw.message ? String(raw.message) : '',
    error: raw.error ? String(raw.error) : '',
    timestamp: String(raw.timestamp || new Date().toISOString()),
  };
}

function _evalGridColor() {
  return (document.body.classList.contains('app-dark') || document.body.classList.contains('live-terminal'))
    ? 'rgba(255,255,255,0.07)' : 'rgba(11,15,20,0.07)';
}

function buildLineOption(points, yKey, name) {
  return {
    grid: { left: 52, right: 18, top: 24, bottom: 40 },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: (points || []).map(p => p.date || p.timestamp || '') },
    yAxis: { type: 'value', splitLine: { lineStyle: { color: _evalGridColor() } } },
    series: [{ name, type: 'line', smooth: true, symbol: 'none', data: (points || []).map(p => Array.isArray(yKey) ? yKey.map(k => p[k]).find(v => v !== undefined && v !== null) : p[yKey]) }],
  };
}

function buildBarOption(points) {
  return {
    grid: { left: 52, right: 18, top: 24, bottom: 40 },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: (points || []).map(p => p.date || '') },
    yAxis: { type: 'value', splitLine: { lineStyle: { color: _evalGridColor() } } },
    series: [{ name: 'Daily return', type: 'bar', data: (points || []).map(p => p.return_pct ?? p.day_return_pct ?? p.return) }],
  };
}

function EvalMonitor({ tweaks }) {
  const chartHeight = Number(tweaks?.evalChartHeight || 280);
  const [draft, setDraft] = _evalUseState(() => evalDefaultsFromTweaks(tweaks));
  const [filters, setFilters] = _evalUseState(() => evalDefaultsFromTweaks(tweaks));
  const [riskOpen, setRiskOpen] = _evalUseState(false);
  const [brainData, setBrainData] = _evalUseState(null);
  const [brainLoading, setBrainLoading] = _evalUseState(false);
  const [brainError, setBrainError] = _evalUseState('');
  const [summary, setSummary] = _evalUseState(null);
  const [equity, setEquity] = _evalUseState(null);
  const [days, setDays] = _evalUseState([]);
  const [trades, setTrades] = _evalUseState([]);
  const [dayPage, setDayPage] = _evalUseState(1);
  const [tradePage, setTradePage] = _evalUseState(1);
  const [selectedDay, setSelectedDay] = _evalUseState('');
  const [loading, setLoading] = _evalUseState(false);
  const [error, setError] = _evalUseState('');
  const [activeRunId, setActiveRunId] = _evalUseState('');
  const [runs, setRuns] = _evalUseState([]);
  const [runsLoading, setRunsLoading] = _evalUseState(false);
  const [runStatus, setRunStatus] = _evalUseState(null);
  const [runEvent, setRunEvent] = _evalUseState(null);
  const [wsState, setWsState] = _evalUseState('idle');
  const [featureOpen, setFeatureOpen] = _evalUseState(Boolean(tweaks?.evalFeatureExpanded));
  const [models, setModels] = _evalUseState([]);
  const [featureModel, setFeatureModel] = _evalUseState('');
  const [featureData, setFeatureData] = _evalUseState(null);
  const [profileCatalog, setProfileCatalog] = _evalUseState(null);
  const [vmRuntime, setVmRuntime] = _evalUseState(null);
  const [bookOpen, setBookOpen] = _evalUseState(true);
  const [datePreset, setDatePreset] = _evalUseState('');
  const [strategyFilterAllCatalog, setStrategyFilterAllCatalog] = _evalUseState(false);
  const clientRef = _evalUseRef(null);
  const pollRef = _evalUseRef(null);

  const runActive = runStatus && !['completed', 'failed', 'cancelled'].includes(String(runStatus.status || '').toLowerCase());
  const progressPct = Number(runEvent?.progress_pct ?? runStatus?.progress_pct ?? 0);
  const resolvedRunId = String(activeRunId || summary?.resolved_run_id || '').trim();
  const closedTrades = Number(summary?.counts?.closed_trades ?? 0);
  const showEmptyRun = !loading && resolvedRunId && closedTrades === 0;
  const showNoRunsHint = !loading && !runsLoading && !runs.length && !resolvedRunId;

  function loadData(nextFilters = filters, nextDayPage = dayPage, nextTradePage = tradePage, nextSelectedDay = selectedDay, runId = activeRunId) {
    setLoading(true);
    setError('');
    const tradeFilters = nextSelectedDay ? { ...nextFilters, date_from: nextSelectedDay, date_to: nextSelectedDay } : nextFilters;
    Promise.all([
      evalGet(`/api/strategy/evaluation/summary?${evalQuery(nextFilters, { run_id: runId || undefined })}`),
      evalGet(`/api/strategy/evaluation/equity?${evalQuery(nextFilters, { run_id: runId || undefined })}`),
      evalGet(`/api/strategy/evaluation/days?${evalQuery(nextFilters, { page: nextDayPage, page_size: 50, run_id: runId || undefined })}`),
      evalGet(`/api/strategy/evaluation/trades?${evalQuery(tradeFilters, { page: nextTradePage, page_size: 50, sort_by: 'exit_time', sort_dir: 'desc', run_id: runId || undefined })}`),
    ]).then(([s, e, d, t]) => {
      setSummary(s);
      setEquity(e);
      setDays(d.rows || []);
      setTrades(t.rows || []);
    }).catch(err => setError(err.message || String(err))).finally(() => setLoading(false));
  }

  function loadFeatureData(modelKey = featureModel) {
    if (!modelKey) return;
    const params = new URLSearchParams({ model: modelKey });
    if (filters.date_from) params.set('date_from', filters.date_from);
    if (filters.date_to) params.set('date_to', filters.date_to);
    evalGet(`/api/trading/feature-intelligence?${params.toString()}`)
      .then(setFeatureData)
      .catch(err => setFeatureData({ status: 'error', error: err.message || String(err) }));
  }

  async function refreshRunsList(dataset = filters.dataset || 'historical') {
    setRunsLoading(true);
    try {
      const payload = await evalGet(
        `/api/strategy/evaluation/runs?dataset=${encodeURIComponent(dataset)}&limit=80`
      );
      const listed = payload?.rows ?? payload?.runs;
      setRuns(evalSortRunsDesc(listed));
    } catch (err) {
      setRuns([]);
      setError(err.message || String(err));
    } finally {
      setRunsLoading(false);
    }
  }

  function selectRun(run, { syncUrl = true } = {}) {
    const runId = String(run?.run_id || '').trim();
    if (!runId) return;
    const next = {
      ...filters,
      dataset: String(run?.dataset || filters.dataset || 'historical'),
      date_from: String(run?.date_from || filters.date_from || '').slice(0, 10) || filters.date_from,
      date_to: String(run?.date_to || filters.date_to || '').slice(0, 10) || filters.date_to,
    };
    setActiveRunId(runId);
    setDraft(next);
    setFilters(next);
    setDayPage(1);
    setTradePage(1);
    setSelectedDay('');
    setRunStatus(run);
    setRunEvent(null);
    if (syncUrl) evalSyncEvalUrl({ runId, filters: next });
    const state = String(run?.status || '').toLowerCase();
    if (state === 'running' || state === 'queued') connectRun(runId);
    else loadData(next, 1, 1, '', runId);
  }

  _evalUseEffect(() => {
    let cancelled = false;
    const boot = async () => {
      const params = new URLSearchParams(window.location.search);
      const urlRunId = String(params.get('run_id') || '').trim();
      const urlFrom = String(params.get('date_from') || '').trim().slice(0, 10);
      const urlTo = String(params.get('date_to') || '').trim().slice(0, 10);
      const urlDataset = String(params.get('dataset') || tweaks?.evalDefaultDataset || 'historical').trim();

      let nextFilters = { ...filters, dataset: urlDataset || filters.dataset };
      if (urlFrom) nextFilters = { ...nextFilters, date_from: urlFrom };
      if (urlTo) nextFilters = { ...nextFilters, date_to: urlTo };

      if (!cancelled) {
        setDraft(nextFilters);
        setFilters(nextFilters);
      }

      await refreshRunsList(nextFilters.dataset);

      let runId = urlRunId;
      let runDoc = null;
      if (runId) {
        try {
          runDoc = await evalGet(`/api/strategy/evaluation/runs/${encodeURIComponent(runId)}`);
        } catch (_) {
          runDoc = null;
        }
      }
      if (!runDoc && runId) {
        try {
          const listPayload = await evalGet(
            `/api/strategy/evaluation/runs?dataset=${encodeURIComponent(nextFilters.dataset)}&limit=80`
          );
          const listed = listPayload?.rows ?? listPayload?.runs ?? [];
          runDoc = (listed || []).find(r => String(r?.run_id || '').trim() === runId) || null;
        } catch (_) {
          runDoc = null;
        }
      }
      if (!runDoc && !urlRunId) {
        try {
          runDoc = await evalGet(
            `/api/strategy/evaluation/runs/latest?dataset=${encodeURIComponent(nextFilters.dataset)}&status=completed`
          );
          runId = String(runDoc?.run_id || '').trim();
        } catch (_) {
          runDoc = null;
          runId = '';
        }
      } else if (runDoc) {
        runId = String(runDoc?.run_id || runId).trim();
      }

      if (cancelled) return;

      if (runDoc?.date_from) {
        nextFilters = { ...nextFilters, date_from: String(runDoc.date_from).slice(0, 10) };
      }
      if (runDoc?.date_to) {
        nextFilters = { ...nextFilters, date_to: String(runDoc.date_to).slice(0, 10) };
      }
      setDraft(nextFilters);
      setFilters(nextFilters);

      if (runId) {
        setActiveRunId(runId);
        setRunStatus(runDoc);
        evalSyncEvalUrl({ runId, filters: nextFilters });
        const state = String(runDoc?.status || '').toLowerCase();
        if (state === 'running' || state === 'queued') connectRun(runId);
        else if (tweaks?.evalAutoRun !== false) loadData(nextFilters, 1, 1, '', runId);
      } else if (tweaks?.evalAutoRun !== false) {
        loadData(nextFilters, 1, 1, '', '');
      }
    };
    boot();
    evalGet('/api/trading/models').then(payload => {
      const ready = (payload.models || []).filter(m => m.ready_to_run);
      setModels(ready);
      if (ready.length) setFeatureModel(ready[0].instance_key);
    }).catch(() => setModels([]));
    evalGet('/api/strategy/profiles/catalog').then(setProfileCatalog).catch(() => setProfileCatalog(null));
    evalGet('/api/strategy/current/state?mode=replay&latest_n=0')
      .then(setVmRuntime)
      .catch(() => setVmRuntime(null));
    return () => { cancelled = true; };
  }, []);

  _evalUseEffect(() => {
    loadData(filters, dayPage, tradePage, selectedDay);
  }, [dayPage, tradePage, selectedDay]);

  const brainMode = filters.dataset === 'historical' ? 'replay' : 'live';

  _evalUseEffect(() => {
    setBrainLoading(true);
    fetch(`/api/strategy/brain/status?mode=${brainMode}`)
      .then(r => r.ok ? r.json() : Promise.resolve({ available: false, reason: `HTTP ${r.status}` }))
      .then(data => { setBrainData(data); setBrainLoading(false); })
      .catch(err => { setBrainError(err.message || String(err)); setBrainLoading(false); });
  }, [brainMode]);

  _evalUseEffect(() => {
    if (featureOpen && featureModel) loadFeatureData(featureModel);
  }, [featureOpen, featureModel, filters.date_from, filters.date_to]);

  _evalUseEffect(() => () => {
    if (clientRef.current) clientRef.current.deactivate();
    if (pollRef.current) window.clearInterval(pollRef.current);
  }, []);

  function applyFilters() {
    if (draft.date_from && draft.date_to && draft.date_from > draft.date_to) {
      setError('"From" date must be on or before "To" date.');
      return;
    }
    const next = { ...draft };
    setFilters(next);
    setDayPage(1);
    setTradePage(1);
    setSelectedDay('');
    loadData(next, 1, 1, '', activeRunId);
    evalSyncEvalUrl({ runId: activeRunId, filters: next });
  }

  function connectRun(runId) {
    if (clientRef.current) clientRef.current.deactivate();
    if (pollRef.current) window.clearInterval(pollRef.current);
    const wsUrl = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws`;
    const Client = window.StompJs && window.StompJs.Client;
    const pollRun = () => evalGet(`/api/strategy/evaluation/runs/${runId}`).then(status => {
      setRunStatus(status);
      if (['completed', 'failed'].includes(String(status.status || '').toLowerCase())) {
        if (pollRef.current) window.clearInterval(pollRef.current);
        setWsState('disconnected');
        refreshRunsList(filters.dataset);
        loadData(filters, 1, 1, '', runId);
      }
    }).catch(() => undefined);

    if (!Client) {
      setWsState('polling');
      pollRef.current = window.setInterval(pollRun, 3000);
      pollRun();
      return;
    }

    const client = new Client({
      brokerURL: wsUrl,
      reconnectDelay: 3000,
      debug: () => undefined,
      onConnect: () => {
        setWsState('connected');
        client.subscribe(`/topic/strategy/eval/run/${runId}`, msg => {
          const event = normalizeRunEvent(JSON.parse(msg.body || '{}'));
          if (!event) return;
          setRunEvent(event);
          if (event.event_type === 'run_completed' || event.event_type === 'evaluation_ready' || event.event_type === 'run_failed') {
            pollRun();
          }
        });
        pollRun();
      },
      onWebSocketClose: () => setWsState('disconnected'),
      onStompError: () => setWsState('disconnected'),
    });
    client.activate();
    clientRef.current = client;
  }

  async function runReplay() {
    setError('');
    setRunEvent(null);
    setRunStatus({ status: 'queued', progress_pct: 0, date_from: draft.date_from, date_to: draft.date_to });
    const payload = {
      dataset: draft.dataset,
      date_from: draft.date_from,
      date_to: draft.date_to,
      speed: 0,
      stop_loss_pct: Number(draft.stop_loss_pct || 0) / 100,
      target_pct: Number(draft.target_pct || 0) / 100,
      trailing_enabled: Boolean(draft.trailing_enabled),
      trailing_activation_pct: Number(draft.trailing_activation_pct || 0) / 100,
      trailing_offset_pct: Number(draft.trailing_offset_pct || 0) / 100,
      trailing_lock_breakeven: Boolean(draft.trailing_lock_breakeven),
    };
    try {
      const result = await evalPost('/api/strategy/evaluation/runs', payload);
      const runId = String(result.run_id || '').trim();
      setActiveRunId(runId);
      setRunStatus(result);
      const next = { ...draft };
      setFilters(next);
      setDraft(next);
      evalSyncEvalUrl({ runId, filters: next });
      refreshRunsList(next.dataset);
      connectRun(runId);
    } catch (err) {
      setRunStatus({ status: 'failed', error: err.message || String(err), progress_pct: 0 });
      setError(err.message || String(err));
    }
  }

  const stopAnalysis = summary?.stop_analysis || {};
  const exitRows = Array.isArray(summary?.exit_reasons) ? summary.exit_reasons : [];
  const strategyRows = evalNormalizeBreakdownRows(
    Array.isArray(summary?.by_strategy) ? summary.by_strategy : [],
    'entry_strategy'
  );
  const regimeRows = evalNormalizeBreakdownRows(
    Array.isArray(summary?.by_regime) ? summary.by_regime : [],
    'regime'
  );
  const vmProfileId = String(
    vmRuntime?.runtime_config?.strategy_profile_id
    || vmRuntime?.runtime_config?.model?.strategy_profile_id
    || ''
  ).trim();
  const vmProfile = evalProfileForId(profileCatalog, vmProfileId);
  const strategyOptions = evalCollectStrategyIds(
    profileCatalog,
    strategyRows,
    vmProfile,
    strategyFilterAllCatalog
  );
  const profileEntryIds = evalProfileEntryStrategyIds(vmProfile);
  const profileExitIds = (vmProfile?.exit_strategies || []).filter(s => s && s !== 'IV_FILTER');
  const appliedRangeLabel = filters.date_from && filters.date_to
    ? `${filters.date_from} → ${filters.date_to}`
    : '—';
  const runRangeLabel = runStatus?.date_from && runStatus?.date_to
    ? `${String(runStatus.date_from).slice(0, 10)} → ${String(runStatus.date_to).slice(0, 10)}`
    : appliedRangeLabel;
  const kpiItems = [
    { label: 'Closed Trades', value: summary?.counts?.closed_trades ?? '--', sub: loading ? 'loading' : 'trades' },
    { label: 'Win Rate', value: evalPct(summary?.overall?.win_rate), sub: 'closed' },
    { label: 'Capital Return', value: evalPct(summary?.equity?.net_return_pct), cls: Number(summary?.equity?.net_return_pct || 0) >= 0 ? 'pos' : 'neg', sub: 'net' },
    { label: 'End Equity', value: evalNum(summary?.equity?.end_capital), sub: 'capital' },
    { label: 'Max Drawdown', value: evalPct(summary?.equity?.max_drawdown_pct), cls: 'neg', sub: 'peak to trough' },
    { label: 'Profit Factor', value: evalNum(summary?.overall?.profit_factor), sub: 'gross' },
    { label: 'Loss Streak', value: summary?.streaks?.max_trade_loss_streak ?? '--', sub: 'trade' },
    { label: 'Loss Streak', value: summary?.streaks?.max_day_loss_streak ?? '--', sub: 'day' },
  ];

  const equityOption = _evalUseMemo(() => buildLineOption(equity?.equity_curve || [], 'equity', 'Equity'), [equity]);
  const drawdownOption = _evalUseMemo(() => buildLineOption(equity?.drawdown_curve || [], ['drawdown', 'drawdown_pct'], 'Drawdown'), [equity]);
  const dailyOption = _evalUseMemo(() => buildBarOption(equity?.daily_returns || []), [equity]);

  const scatterOption = _evalUseMemo(() => {
    const points = featureData?.scatter?.points || [];
    return {
      grid: { left: 52, right: 18, top: 30, bottom: 46 },
      tooltip: { trigger: 'item', formatter: p => `${p.data.label}<br/>${p.data.name}<br/>Rank ${p.data.rank}` },
      xAxis: { type: 'value', name: featureData?.scatter?.x_axis_label || 'Primary', nameLocation: 'middle', nameGap: 28 },
      yAxis: { type: 'value', name: featureData?.scatter?.y_axis_label || 'Secondary', nameLocation: 'middle', nameGap: 38 },
      series: [{
        type: 'scatter',
        data: points.map(p => ({ value: [p.x, p.y], name: p.feature_name, label: p.feature_label, rank: p.rank })),
        symbolSize: 12,
      }],
    };
  }, [featureData]);

  const dayColumns = [
    { key: 'date', label: 'Date' },
    { key: 'trades', label: 'Trades', cls: 'r' },
    { key: 'wins', label: 'Wins', cls: 'r' },
    { key: 'win_rate', label: 'Win Rate', cls: 'r', render: r => evalPct(r.win_rate) },
    { key: 'day_return_pct', label: 'Capital Return', cls: 'r', render: r => evalPct(r.day_return_pct) },
    { key: 'day_pnl_amount', label: 'Day PnL', cls: 'r', render: r => evalNum(r.day_pnl_amount) },
    { key: 'equity_end', label: 'Equity EOD', cls: 'r', render: r => evalNum(r.equity_end) },
    { key: 'drawdown_pct_eod', label: 'Drawdown', cls: 'r', render: r => evalPct(r.drawdown_pct_eod) },
  ];
  const tradeColumns = [
    { key: 'trade_date_ist', label: 'Day' },
    { key: 'entry_strategy', label: 'Strategy' },
    { key: 'regime', label: 'Regime' },
    { key: 'direction', label: 'Dir' },
    { key: 'entry_time', label: 'Entry' },
    { key: 'exit_time', label: 'Exit' },
    { key: 'capital_pnl_pct', label: 'Capital PnL%', cls: 'r', render: r => evalPct(r.capital_pnl_pct) },
    { key: 'capital_pnl_amount', label: 'Capital PnL', cls: 'r', render: r => evalNum(r.capital_pnl_amount) },
    { key: 'pnl_pct_net', label: 'Option PnL%', cls: 'r', render: r => evalPct(r.pnl_pct_net) },
    { key: 'mfe_pct', label: 'MFE', cls: 'r', render: r => evalPct(r.mfe_pct) },
    { key: 'mae_pct', label: 'MAE', cls: 'r', render: r => evalPct(r.mae_pct) },
    { key: 'stop_loss_pct', label: 'Stop%', cls: 'r', render: r => evalPct(r.stop_loss_pct) },
    { key: 'entry_stop_price', label: 'Entry Stop', cls: 'r', render: r => evalNum(r.entry_stop_price) },
    { key: 'exit_stop_price', label: 'Exit Stop', cls: 'r', render: r => evalNum(r.exit_stop_price) },
    { key: 'trailing_enabled', label: 'Trail', render: r => evalBool(r.trailing_enabled) },
    { key: 'bars_held', label: 'Bars', cls: 'r' },
    { key: 'exit_reason', label: 'Exit Reason' },
  ];

  const ranking = (featureData?.ranking?.rows || []).slice(0, 12);
  const groups = featureData?.groups || [];
  const featureSummary = featureData?.summary || {};
  const coverage = featureData?.model?.coverage || {};

  return (
    <div className="monitor-shell mode-eval">
      <div className="page-head">
        <div>
          <div className="page-crumbs">Operator / Evaluation</div>
          <h1 className="page-title">Strategy Evaluation</h1>
          <p className="page-sub">
            Replay runs store trades by <code>run_id</code>. Strategy / regime filters only narrow the tables below — engine profile is set on the VM (<code>STRATEGY_PROFILE_ID</code>).
          </p>
        </div>
        <div className="head-right">
          {vmProfileId && (
            <span className="chip info" title={vmProfile?.summary || vmProfileId}>
              <span className="dot"></span>VM replay: {vmProfile?.title || vmProfileId}
            </span>
          )}
          <span className={`chip ${error ? 'neg' : loading ? 'warn' : 'info'}`}><span className="dot"></span>{error ? 'Error' : loading ? 'Loading' : 'Ready'}</span>
        </div>
      </div>

      <div className="panel eval-trader-book-panel">
        <div className="panel-head" onClick={() => setBookOpen(v => !v)} style={{ cursor: 'pointer' }}>
          <div className="panel-title">{bookOpen ? '▼' : '▶'} Trader book — profiles &amp; strategies</div>
          <div className="panel-actions">
            <span className="chip info">{profileCatalog?.profiles?.length ?? '…'} profiles</span>
            <span className="chip info">{profileCatalog?.all_entry_strategy_ids?.length ?? '…'} strategy IDs</span>
          </div>
        </div>
        {bookOpen && (
          <div className="panel-body">
            <p className="muted eval-runs-hint" style={{ marginTop: 0 }}>
              <strong>Selected run performance</strong> uses range <code>{runRangeLabel}</code>
              {resolvedRunId ? <> · run <code>{evalShortRunId(resolvedRunId)}</code></> : ' · pick a run or Apply filters'}
              . Comparison tables reflect closed trades in that window (after optional strategy/regime filter).
            </p>
            <div className="eval-profile-grid">
              {(profileCatalog?.profiles || []).map(profile => (
                <div
                  key={profile.profile_id}
                  className={`eval-profile-card${profile.profile_id === vmProfileId ? ' active' : ''}${profile.operator_focus ? ' focus' : ''}`}
                >
                  <div className="eval-profile-card-head">
                    <strong>{profile.title || profile.profile_id}</strong>
                    <span className="mono tiny">{profile.profile_id}</span>
                  </div>
                  {profile.summary && <p className="muted tiny">{profile.summary}</p>}
                  <div className="eval-profile-regimes">
                    {Object.entries(profile.regime_entry_map || {}).map(([regime, strategies]) => (
                      <div key={regime} className="eval-profile-regime-row">
                        <span className="chip info">{regime}</span>
                        <span className="mono tiny">
                          {(strategies || []).filter(s => s !== 'IV_FILTER').join(', ') || '—'}
                        </span>
                      </div>
                    ))}
                  </div>
                  {(profile.exit_strategies || []).length > 0 && (
                    <p className="muted tiny" style={{ marginTop: 6 }}>
                      Exit helpers (not entry playbooks): {(profile.exit_strategies || []).join(', ')}
                    </p>
                  )}
                  {profile.risk_config && Object.keys(profile.risk_config).length > 0 && (
                    <p className="muted tiny" style={{ marginTop: 8 }}>
                      Risk: stop {(Number(profile.risk_config.stop_loss_pct || 0) * 100).toFixed(0)}%
                      · target {(Number(profile.risk_config.target_pct || 0) * 100).toFixed(0)}%
                      · trail {profile.risk_config.trailing_enabled ? 'on' : 'off'}
                    </p>
                  )}
                </div>
              ))}
            </div>
            {!profileCatalog && (
              <p className="muted tiny">Could not load profile catalog — restart dashboard after deploy.</p>
            )}
          </div>
        )}
      </div>

      <div className="panel eval-runs-panel">
        <div className="panel-head">
          <div className="panel-title">Replay runs</div>
          <div className="panel-actions">
            <button type="button" className="btn sm" onClick={() => refreshRunsList(filters.dataset)} disabled={runsLoading}>
              {runsLoading ? 'Refreshing…' : 'Refresh list'}
            </button>
          </div>
        </div>
        <div className="panel-body">
          <div className="eval-runs-row">
            <label className="field eval-run-picker">
              <span className="field-label">Select run</span>
              <select
                className="inp"
                value={activeRunId}
                onChange={e => {
                  const runId = String(e.target.value || '').trim();
                  const run = (runs || []).find(r => String(r.run_id || '') === runId);
                  if (run) selectRun(run);
                }}
              >
                <option value="">{runs.length ? '— pick a replay run —' : '— no runs yet —'}</option>
                {(runs || []).map((run, idx) => (
                  <option key={run.run_id} value={run.run_id}>
                    {(idx === 0 ? '★ LATEST  ' : '') + evalRunOptionLabel(run)}
                  </option>
                ))}
              </select>
            </label>
            {resolvedRunId && (
              <>
                <button type="button" className="btn sm" onClick={() => evalCopyText(resolvedRunId)}>Copy run ID</button>
                <button type="button" className="btn sm" onClick={() => evalCopyText(window.location.href)}>Copy link</button>
                {resolvedRunId && runStatus?.date_from && (
                  <a
                    className="btn sm"
                    href={evalReplayDeepLink({
                      runId: resolvedRunId,
                      dateFrom: runStatus.date_from,
                      dateTo: runStatus.date_to,
                    })}
                  >
                    Open in Replay
                  </a>
                )}
              </>
            )}
          </div>
          <EvalRunDetails run={runStatus} vmProfileId={vmProfileId} />
          <p className="muted eval-runs-hint">
            Each option shows replay window, profile (if trades exist), trade count, and when queued. Full details appear above after you select.
          </p>
        </div>
      </div>

      {showNoRunsHint && (
        <div className="panel eval-empty-panel">
          <div className="panel-body">
            <strong>No replay runs found.</strong>
            <p className="muted">Set a date range below and click <strong>Run Replay</strong>, or wait for an in-flight replay to finish then hit Refresh list.</p>
          </div>
        </div>
      )}

      {showEmptyRun && (
        <div className="panel eval-empty-panel">
          <div className="panel-body">
            <strong>Run selected but no closed trades in this window.</strong>
            <p className="muted">
              Run <code>{evalShortRunId(resolvedRunId)}</code> may still be running, failed, or replayed while{' '}
              <code>strategy_app_historical</code> was down (snapshots emitted, 0 trades persisted).
              On the VM run <code>sudo python3 ops/gcp/preflight_historical_replay.py</code> then re-queue.
              Compare <strong>Option PnL%</strong> in the trades table (premium move); Capital PnL% uses full notional vs $1k and exaggerates tail days.
            </p>
          </div>
        </div>
      )}

      <div className="panel">
        <div className="panel-body">
          <div className="eval-filter-bar eval-filter-bar-wide">
            <label className="field">
              <span className="field-label">Date preset</span>
              <select
                className="inp"
                value={datePreset || evalDatePresetId(draft.date_from, draft.date_to)}
                onChange={e => {
                  const id = e.target.value;
                  setDatePreset(id);
                  const next = evalApplyDatePreset(draft, id);
                  setDraft(next);
                }}
              >
                {EVAL_DATE_PRESETS.map(p => (
                  <option key={p.id || 'custom'} value={p.id}>{p.label}</option>
                ))}
              </select>
            </label>
            <label className="field"><span className="field-label">Dataset</span><select className="inp" value={draft.dataset} onChange={e => setDraft({ ...draft, dataset: e.target.value })}><option value="historical">Historical</option><option value="live">Live</option></select></label>
            <label className="field"><span className="field-label">From</span><input className="inp" type="date" value={draft.date_from} onChange={e => { setDatePreset(''); setDraft({ ...draft, date_from: e.target.value }); }} /></label>
            <label className="field"><span className="field-label">To</span><input className="inp" type="date" value={draft.date_to} onChange={e => { setDatePreset(''); setDraft({ ...draft, date_to: e.target.value }); }} /></label>
            <label className="field">
              <span className="field-label">Strategy (display filter)</span>
              <select className="inp" value={draft.strategy} onChange={e => setDraft({ ...draft, strategy: e.target.value })}>
                <option value="">All in scope</option>
                {strategyOptions.map(sid => (
                  <option key={sid} value={sid}>{evalStrategyLabel(sid)}</option>
                ))}
              </select>
            </label>
            {vmProfileId && (
              <label className="field eval-filter-toggle" title="Show every strategy ID from the trader book (other profiles). Still display-only.">
                <span className="field-label">Catalog</span>
                <select
                  className="inp"
                  value={strategyFilterAllCatalog ? 'all' : 'profile'}
                  onChange={e => setStrategyFilterAllCatalog(e.target.value === 'all')}
                >
                  <option value="profile">Active profile only</option>
                  <option value="all">All profiles (11)</option>
                </select>
              </label>
            )}
            <label className="field">
              <span className="field-label">Regime (display filter)</span>
              <select className="inp" value={draft.regime} onChange={e => setDraft({ ...draft, regime: e.target.value })}>
                {EVAL_REGIME_OPTIONS.map(r => (
                  <option key={r || 'all'} value={r}>{r ? r : 'All regimes'}</option>
                ))}
              </select>
            </label>
            <button className="btn" onClick={() => setRiskOpen(v => !v)}>{riskOpen ? 'Hide Risk' : 'Risk'}</button>
            <button className="btn" type="button" title="Trader master eval window (May–Jul 2024)" onClick={() => {
              const next = { ...draft, ...EVAL_TRADER_MASTER_PRESET };
              setDatePreset('may_jul_2024');
              setDraft(next);
              setFilters(next);
              setDayPage(1);
              setTradePage(1);
              setSelectedDay('');
              loadData(next, 1, 1, '', activeRunId);
            }}>Master preset</button>
            <button className="btn" type="button" title="Debit multi smoke day — clear strategy filter" onClick={() => {
              const next = { ...draft, ...EVAL_DEBIT_MULTI_PRESET };
              setDatePreset('oct31_2024');
              setDraft(next);
              setFilters(next);
              setDayPage(1);
              setTradePage(1);
              setSelectedDay('');
              loadData(next, 1, 1, '', activeRunId);
            }}>Debit preset</button>
            <button className="btn" type="button" onClick={() => {
              const next = { ...draft, ...EVAL_R1S_TOP3_PRESET };
              setDatePreset('may_jul_2024');
              setDraft(next);
              setFilters(next);
              setDayPage(1);
              setTradePage(1);
              setSelectedDay('');
              loadData(next, 1, 1, '', activeRunId);
            }}>R1S preset</button>
            <button className="btn primary" onClick={applyFilters}>Apply</button>
            <button className="btn" onClick={runReplay}>Run Replay</button>
          </div>
          <p className="muted eval-runs-hint">
            Strategy filter scope:{' '}
            <strong>
              {strategyFilterAllCatalog
                ? 'full trader book (all profiles)'
                : vmProfile
                  ? `${vmProfile.title || vmProfileId} entries: ${profileEntryIds.join(', ') || '—'}${profileExitIds.length ? ` · exit helpers: ${profileExitIds.join(', ')}` : ''}`
                  : 'trades in this run'}
            </strong>
            . Does not change the engine — only narrows tables below.
            {' '}Regime: <strong>{draft.regime || 'all'}</strong> · range <code>{draft.date_from}</code>–<code>{draft.date_to}</code>.
            Trust <strong>Option PnL%</strong> for premium moves.
          </p>
          {error && <div className="muted" style={{ marginTop: 8, color: 'var(--neg)', fontSize: 12 }}>{error}</div>}
          {riskOpen && (
            <div className="eval-risk-grid">
              <label className="field"><span className="field-label">Stop %</span><input className="inp" type="number" value={draft.stop_loss_pct} onChange={e => setDraft({ ...draft, stop_loss_pct: Number(e.target.value || 0) })} /></label>
              <label className="field"><span className="field-label">Target %</span><input className="inp" type="number" value={draft.target_pct} onChange={e => setDraft({ ...draft, target_pct: Number(e.target.value || 0) })} /></label>
              <label className="field"><span className="field-label">Trail Activation %</span><input className="inp" type="number" value={draft.trailing_activation_pct} onChange={e => setDraft({ ...draft, trailing_activation_pct: Number(e.target.value || 0) })} /></label>
              <label className="field"><span className="field-label">Trail Offset %</span><input className="inp" type="number" value={draft.trailing_offset_pct} onChange={e => setDraft({ ...draft, trailing_offset_pct: Number(e.target.value || 0) })} /></label>
              <label className="field"><span className="field-label">Trailing</span><select className="inp" value={String(draft.trailing_enabled)} onChange={e => setDraft({ ...draft, trailing_enabled: e.target.value === 'true' })}><option value="true">On</option><option value="false">Off</option></select></label>
              <label className="field"><span className="field-label">Lock BE</span><select className="inp" value={String(draft.trailing_lock_breakeven)} onChange={e => setDraft({ ...draft, trailing_lock_breakeven: e.target.value === 'true' })}><option value="true">On</option><option value="false">Off</option></select></label>
            </div>
          )}
        </div>
      </div>

      {runActive && (
        <div className="panel">
          <div className="eval-progress-strip">
            <div className="progress-bar"><div className="progress-fill" style={{ width: `${Math.max(0, Math.min(100, progressPct))}%` }} /></div>
            <strong>{evalNum(progressPct, 0)}%</strong>
            <span>{runEvent?.current_day || runStatus?.date_from || '--'} / {runStatus?.date_to || '--'}</span>
            <span>run_id: {(activeRunId || runStatus?.run_id || '').slice(0, 8) || '--'}</span>
            <span className={`chip ${wsState === 'connected' ? 'pos' : 'warn'}`}><span className="dot"></span>{wsState}</span>
          </div>
        </div>
      )}

      <BrainPanel
        brainData={brainData}
        loading={brainLoading}
        error={brainError}
        onRefresh={() => {
          setBrainLoading(true); setBrainError('');
          fetch(`/api/strategy/brain/status?mode=${brainMode}`)
            .then(r => r.ok ? r.json() : Promise.resolve({ available: false }))
            .then(d => { setBrainData(d); setBrainLoading(false); })
            .catch(e => { setBrainError(e.message || String(e)); setBrainLoading(false); });
        }}
      />

      <KpiStrip items={kpiItems} cols={8} />

      <div className="eval-grid-2">
        <div className="panel"><div className="panel-head"><div className="panel-title">Stop Analytics</div></div><div className="panel-body"><KpiStrip cols={4} items={[
          { label: 'Stop Loss Exit %', value: evalPct(stopAnalysis.stop_loss_exit_pct), sub: 'exits' },
          { label: 'Trail Stop Exit %', value: evalPct(stopAnalysis.trailing_stop_exit_pct), sub: 'exits' },
          { label: 'Trail Active %', value: evalPct(stopAnalysis.trailing_active_trade_pct), sub: 'trades' },
          { label: 'Avg Locked Gain', value: evalPct(stopAnalysis.avg_locked_gain_pct_before_trailing_exit), sub: 'before exit' },
          { label: 'Trail Capture', value: evalPct(stopAnalysis.avg_trailing_profit_capture_pct), sub: 'profit' },
          { label: 'Avg Stop', value: evalPct(stopAnalysis.avg_configured_stop_loss_pct), sub: 'config' },
          { label: 'Avg Target', value: evalPct(stopAnalysis.avg_configured_target_pct), sub: 'config' },
          { label: 'Trail Stops', value: stopAnalysis.trailing_stop_exits ?? '--', sub: 'count' },
        ]} /></div></div>
        <div className="panel"><div className="panel-head"><div className="panel-title">Exit Reason Breakdown</div></div><PaginatedTable columns={[
          { key: 'exit_reason', label: 'Exit Reason' }, { key: 'count', label: 'Count', cls: 'r' }, { key: 'pct', label: '%', cls: 'r', render: r => evalPct(r.pct) }, { key: 'avg_capital_pnl_pct', label: 'Avg Capital PnL', cls: 'r', render: r => evalPct(r.avg_capital_pnl_pct) },
        ]} rows={exitRows} page={1} emptyText="No exit reasons." /></div>
      </div>

      <div className="eval-grid-2">
        <div className="panel">
          <div className="panel-head">
            <div className="panel-title">Strategy performance</div>
            <span className="chip info">{runRangeLabel}</span>
          </div>
          <PaginatedTable rows={strategyRows} page={1} emptyText="No closed trades in this window." columns={[
            { key: 'entry_strategy', label: 'Strategy', render: r => r.entry_strategy || r.strategy || '—' },
            { key: 'trades', label: 'Trades', cls: 'r' },
            { key: 'win_rate', label: 'Win Rate', cls: 'r', render: r => evalPct(r.win_rate) },
            { key: 'avg_option_pnl_pct', label: 'Avg Option PnL%', cls: 'r', render: r => evalPct(r.avg_option_pnl_pct) },
            { key: 'avg_pnl_pct', label: 'Avg Capital PnL%', cls: 'r', render: r => evalPct(r.avg_pnl_pct) },
            { key: 'total_pnl_pct', label: 'Total Capital PnL%', cls: 'r', render: r => evalPct(r.total_pnl_pct) },
            { key: 'profit_factor', label: 'PF', cls: 'r', render: r => evalNum(r.profit_factor) },
          ]} />
        </div>
        <div className="panel">
          <div className="panel-head">
            <div className="panel-title">Regime performance</div>
            <span className="chip info">{runRangeLabel}</span>
          </div>
          <PaginatedTable rows={regimeRows} page={1} emptyText="No closed trades in this window." columns={[
            { key: 'regime', label: 'Regime' },
            { key: 'trades', label: 'Trades', cls: 'r' },
            { key: 'win_rate', label: 'Win Rate', cls: 'r', render: r => evalPct(r.win_rate) },
            { key: 'avg_option_pnl_pct', label: 'Avg Option PnL%', cls: 'r', render: r => evalPct(r.avg_option_pnl_pct) },
            { key: 'avg_pnl_pct', label: 'Avg Capital PnL%', cls: 'r', render: r => evalPct(r.avg_pnl_pct) },
            { key: 'total_pnl_pct', label: 'Total Capital PnL%', cls: 'r', render: r => evalPct(r.total_pnl_pct) },
            { key: 'profit_factor', label: 'PF', cls: 'r', render: r => evalNum(r.profit_factor) },
          ]} />
        </div>
      </div>

      <div className="eval-chart-grid">
        <div className="panel"><div className="panel-head"><div className="panel-title">Equity Curve</div></div><EChartPanel option={equityOption} height={chartHeight} /></div>
        <div className="panel"><div className="panel-head"><div className="panel-title">Drawdown</div></div><EChartPanel option={drawdownOption} height={chartHeight} /></div>
        <div className="panel full"><div className="panel-head"><div className="panel-title">Daily Returns</div></div><EChartPanel option={dailyOption} height={chartHeight} /></div>
      </div>

      <div className="panel"><div className="panel-head"><div className="panel-title">Per-Day Summary</div></div><PaginatedTable rows={days} columns={dayColumns} page={dayPage} onPage={setDayPage} onRowClick={r => { setSelectedDay(String(r.date || '')); setTradePage(1); }} selectedKey={selectedDay} onExportCsv={() => evalCsv('days.csv', days)} /></div>

      <div className="panel"><div className="panel-head"><div className="panel-title">Trades {selectedDay && <span className="chip info" style={{ marginLeft: 8 }}>{selectedDay}</span>}</div><div>{selectedDay && <button className="btn sm ghost" onClick={() => setSelectedDay('')}>Clear Day</button>}</div></div><PaginatedTable rows={trades} columns={tradeColumns} page={tradePage} onPage={setTradePage} onExportCsv={() => evalCsv('trades.csv', trades)} /></div>

      <div className="panel collapsible-panel">
        <div className="panel-head" onClick={() => setFeatureOpen(v => !v)}>
          <div className="panel-title">{featureOpen ? 'v' : '>'} Feature Intelligence</div>
          <div className="row gap-s"><span className="chip info">snapshot_ml_flat</span><span className={`chip ${coverage.requested_range_in_coverage === false ? 'warn' : 'pos'}`}>{coverage.training_start ? `${coverage.training_start} to ${coverage.training_end}` : 'Coverage'}</span></div>
        </div>
        {featureOpen && (
          <div className="panel-body">
            <div className="eval-filter-bar" style={{ gridTemplateColumns: 'minmax(240px, 1fr) repeat(4, minmax(120px, 1fr))' }}>
              <label className="field"><span className="field-label">Model</span><select className="inp" value={featureModel} onChange={e => setFeatureModel(e.target.value)}>{models.map(m => <option key={m.instance_key} value={m.instance_key}>{m.title || m.instance_key}</option>)}</select></label>
              <div><div className="field-label">Model Run</div><strong>{featureData?.model?.run_id || '--'}</strong></div>
              <div><div className="field-label">Profile</div><strong>{featureData?.model?.profile_id || '--'}</strong></div>
              <div><div className="field-label">Applied Range</div><strong>{filters.date_from}..{filters.date_to}</strong></div>
              <div><div className="field-label">Model Spec</div><strong>{featureData?.model?.selected_model_name || '--'}</strong></div>
            </div>
            <KpiStrip cols={4} items={[
              { label: 'Active V1 Features', value: featureSummary.selected_v1_feature_count ?? '--', sub: 'mapped' },
              { label: 'Contract Groups', value: featureSummary.contract_group_count ?? '--', sub: 'groups' },
              { label: 'Scatter Points', value: featureSummary.scatter_point_count ?? '--', sub: 'points' },
              { label: 'Legacy Removed', value: featureSummary.removed_legacy_feature_count ?? '--', sub: 'hidden' },
            ]} />
            <div className="eval-grid-2" style={{ marginTop: 14 }}>
              <div className="panel"><div className="panel-head"><div className="panel-title">Ranking</div></div><PaginatedTable page={1} rows={ranking} columns={[
                { key: 'rank', label: 'Rank', cls: 'r' }, { key: 'feature_label', label: 'Feature' }, { key: 'group_label', label: 'Group' }, { key: 'importance_score', label: 'Importance', cls: 'r', render: r => evalScore(r.importance_score) },
              ]} /></div>
              <div className="panel"><div className="panel-head"><div className="panel-title">Scatter</div></div><EChartPanel option={scatterOption} height={chartHeight} /></div>
            </div>
            <div className="feature-group-grid" style={{ marginTop: 14 }}>
              {groups.map(group => <div className="feature-group-card" key={group.group_key}>
                <div className="rowx"><strong>{group.group_label || group.group_key}</strong><span className="mono tiny">{group.active_count || 0} active / mean {evalScore(group.importance_mean)}</span></div>
                <div className="feature-chip-list">{(group.features || []).slice(0, 12).map(f => <span key={f.feature_name} className={`feature-chip ${f.is_selected ? 'active' : 'inactive'}`}>{f.feature_label || f.feature_name}</span>)}</div>
              </div>)}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

Object.assign(window, { EvalMonitor });
