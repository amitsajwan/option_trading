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

function buildLineOption(points, yKey, name) {
  return {
    grid: { left: 52, right: 18, top: 24, bottom: 40 },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: (points || []).map(p => p.date || p.timestamp || '') },
    yAxis: { type: 'value', splitLine: { lineStyle: { color: 'rgba(11,15,20,0.07)' } } },
    series: [{ name, type: 'line', smooth: true, symbol: 'none', data: (points || []).map(p => Array.isArray(yKey) ? yKey.map(k => p[k]).find(v => v !== undefined && v !== null) : p[yKey]) }],
  };
}

function buildBarOption(points) {
  return {
    grid: { left: 52, right: 18, top: 24, bottom: 40 },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: (points || []).map(p => p.date || '') },
    yAxis: { type: 'value', splitLine: { lineStyle: { color: 'rgba(11,15,20,0.07)' } } },
    series: [{ name: 'Daily return', type: 'bar', data: (points || []).map(p => p.return_pct ?? p.day_return_pct ?? p.return) }],
  };
}

function EvalMonitor({ tweaks }) {
  const chartHeight = Number(tweaks?.evalChartHeight || 280);
  const [draft, setDraft] = _evalUseState({ ...EVAL_DEFAULTS, dataset: tweaks?.evalDefaultDataset || 'historical' });
  const [filters, setFilters] = _evalUseState({ ...EVAL_DEFAULTS, dataset: tweaks?.evalDefaultDataset || 'historical' });
  const [riskOpen, setRiskOpen] = _evalUseState(false);
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
  const [runStatus, setRunStatus] = _evalUseState(null);
  const [runEvent, setRunEvent] = _evalUseState(null);
  const [wsState, setWsState] = _evalUseState('idle');
  const [featureOpen, setFeatureOpen] = _evalUseState(Boolean(tweaks?.evalFeatureExpanded));
  const [models, setModels] = _evalUseState([]);
  const [featureModel, setFeatureModel] = _evalUseState('');
  const [featureData, setFeatureData] = _evalUseState(null);
  const clientRef = _evalUseRef(null);
  const pollRef = _evalUseRef(null);

  const runActive = runStatus && !['completed', 'failed'].includes(String(runStatus.status || '').toLowerCase());
  const progressPct = Number(runEvent?.progress_pct ?? runStatus?.progress_pct ?? 0);

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

  _evalUseEffect(() => {
    if (tweaks?.evalAutoRun !== false) loadData(filters, 1, 1, '');
    evalGet('/api/trading/models').then(payload => {
      const ready = (payload.models || []).filter(m => m.ready_to_run);
      setModels(ready);
      if (ready.length) setFeatureModel(ready[0].instance_key);
    }).catch(() => setModels([]));
  }, []);

  _evalUseEffect(() => {
    loadData(filters, dayPage, tradePage, selectedDay);
  }, [dayPage, tradePage, selectedDay]);

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
    loadData(next, 1, 1, '');
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
      setActiveRunId(result.run_id || '');
      setRunStatus(result);
      setFilters({ ...draft });
      connectRun(result.run_id);
    } catch (err) {
      setRunStatus({ status: 'failed', error: err.message || String(err), progress_pct: 0 });
      setError(err.message || String(err));
    }
  }

  const stopAnalysis = summary?.stop_analysis || {};
  const exitRows = Array.isArray(summary?.exit_reasons) ? summary.exit_reasons : [];
  const strategyRows = Array.isArray(summary?.by_strategy) ? summary.by_strategy : [];
  const regimeRows = Array.isArray(summary?.by_regime) ? summary.by_regime : [];
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
          <p className="page-sub">Offline research and backtesting in the Quiet Operator shell.</p>
        </div>
        <div className="head-right">
          <span className={`chip ${error ? 'neg' : loading ? 'warn' : 'info'}`}><span className="dot"></span>{error ? 'Error' : loading ? 'Loading' : 'Ready'}</span>
        </div>
      </div>

      <div className="panel">
        <div className="panel-body">
          <div className="eval-filter-bar">
            <label className="field"><span className="field-label">Dataset</span><select className="inp" value={draft.dataset} onChange={e => setDraft({ ...draft, dataset: e.target.value })}><option value="historical">Historical</option><option value="live">Live</option></select></label>
            <label className="field"><span className="field-label">From</span><input className="inp" type="date" value={draft.date_from} onChange={e => setDraft({ ...draft, date_from: e.target.value })} /></label>
            <label className="field"><span className="field-label">To</span><input className="inp" type="date" value={draft.date_to} onChange={e => setDraft({ ...draft, date_to: e.target.value })} /></label>
            <label className="field"><span className="field-label">Strategy</span><input className="inp" value={draft.strategy} onChange={e => setDraft({ ...draft, strategy: e.target.value })} placeholder="ORB" /></label>
            <label className="field"><span className="field-label">Regime</span><input className="inp" value={draft.regime} onChange={e => setDraft({ ...draft, regime: e.target.value })} placeholder="TRENDING" /></label>
            <button className="btn" onClick={() => setRiskOpen(v => !v)}>{riskOpen ? 'Hide Risk' : 'Risk'}</button>
            <button className="btn primary" onClick={applyFilters}>Apply</button>
            <button className="btn" onClick={runReplay}>Run Replay</button>
          </div>
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
        <div className="panel"><div className="panel-head"><div className="panel-title">Strategy Comparison</div></div><PaginatedTable rows={strategyRows} page={1} columns={[
          { key: 'strategy', label: 'Strategy' }, { key: 'trades', label: 'Trades', cls: 'r' }, { key: 'win_rate', label: 'Win Rate', cls: 'r', render: r => evalPct(r.win_rate) }, { key: 'avg_pnl_pct', label: 'Avg PnL', cls: 'r', render: r => evalPct(r.avg_pnl_pct) }, { key: 'total_pnl_pct', label: 'Total PnL', cls: 'r', render: r => evalPct(r.total_pnl_pct) }, { key: 'profit_factor', label: 'PF', cls: 'r', render: r => evalNum(r.profit_factor) },
        ]} /></div>
        <div className="panel"><div className="panel-head"><div className="panel-title">Regime Comparison</div></div><PaginatedTable rows={regimeRows} page={1} columns={[
          { key: 'regime', label: 'Regime' }, { key: 'trades', label: 'Trades', cls: 'r' }, { key: 'win_rate', label: 'Win Rate', cls: 'r', render: r => evalPct(r.win_rate) }, { key: 'avg_pnl_pct', label: 'Avg PnL', cls: 'r', render: r => evalPct(r.avg_pnl_pct) }, { key: 'total_pnl_pct', label: 'Total PnL', cls: 'r', render: r => evalPct(r.total_pnl_pct) }, { key: 'profit_factor', label: 'PF', cls: 'r', render: r => evalNum(r.profit_factor) },
        ]} /></div>
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
