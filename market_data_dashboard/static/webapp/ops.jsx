// ops.jsx — Daily ops: configure + sim today + compare actual vs sim
// Tab: OPS  (added to nav in index.html)

const _r  = React.useRef;
const _s  = React.useState;
const _e  = React.useEffect;
const _cb = React.useCallback;
const _m  = React.useMemo;

// ── helpers ──────────────────────────────────────────────────────────────────
function pct(v, digits = 2) {
  if (v == null) return '—';
  const n = typeof v === 'string' ? parseFloat(v) : v;
  if (isNaN(n)) return '—';
  const sign = n >= 0 ? '+' : '';
  return `${sign}${(n * 100).toFixed(digits)}%`;
}
function num(v) { return typeof v === 'string' ? parseFloat(v) : v; }

// ── colour tokens ─────────────────────────────────────────────────────────────
const CSS = `
.ops-page { display: flex; flex-direction: column; gap: 16px; padding: 16px 20px; max-width: 1400px; }

/* Config panel */
.ops-config { background: var(--paper-2); border: 1px solid var(--line-1); border-radius: var(--r-3);
  padding: 14px 18px; display: flex; flex-direction: column; gap: 12px; }
.ops-config-header { display: flex; align-items: center; gap: 12px; }
.ops-config-title { font-family: var(--f-mono); font-size: 11px; font-weight: 600;
  letter-spacing: 0.06em; text-transform: uppercase; color: var(--ink-3); }
.ops-config-date { font-family: var(--f-mono); font-size: 11px; color: var(--ink-2);
  background: var(--paper); border: 1px solid var(--line-1); border-radius: var(--r-2);
  padding: 3px 8px; }
.ops-changes-badge { margin-left: auto; font-family: var(--f-mono); font-size: 10px;
  color: #d97706; background: rgba(217,119,6,0.12); border: 1px solid rgba(217,119,6,0.25);
  border-radius: 10px; padding: 2px 8px; }
.ops-config-groups { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
@media (max-width: 1100px) { .ops-config-groups { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 700px)  { .ops-config-groups { grid-template-columns: 1fr; } }

.ops-group-label { font-family: var(--f-mono); font-size: 9.5px; font-weight: 600;
  letter-spacing: 0.08em; text-transform: uppercase; color: var(--ink-3);
  margin-bottom: 8px; }
.ops-control { display: flex; flex-direction: column; gap: 5px; margin-bottom: 8px; }
.ops-control-row { display: flex; align-items: center; justify-content: space-between; }
.ops-control-label { font-size: 11.5px; color: var(--ink-2); }
.ops-control-value { font-family: var(--f-mono); font-size: 11px; color: var(--ink);
  min-width: 52px; text-align: right; }
.ops-control-value.changed { color: #d97706; font-weight: 600; }
.ops-slider { appearance: none; -webkit-appearance: none; width: 100%; height: 3px;
  border-radius: 999px; background: var(--line-2); outline: none; cursor: pointer; }
.ops-slider::-webkit-slider-thumb { -webkit-appearance: none; width: 13px; height: 13px;
  border-radius: 50%; background: var(--ink); border: 2px solid var(--paper); cursor: pointer;
  box-shadow: 0 1px 3px rgba(0,0,0,0.2); }
.ops-slider.changed::-webkit-slider-thumb { background: #d97706; }
.ops-toggle-row { display: flex; align-items: center; justify-content: space-between;
  padding: 4px 0; }
.ops-toggle { position: relative; width: 32px; height: 18px; border: 0; border-radius: 999px;
  background: var(--line-2); cursor: pointer; padding: 0; transition: background .15s; }
.ops-toggle[data-on="1"] { background: var(--pos); }
.ops-toggle i { position: absolute; top: 2px; left: 2px; width: 14px; height: 14px;
  border-radius: 50%; background: #fff; box-shadow: 0 1px 2px rgba(0,0,0,.25);
  transition: transform .15s; pointer-events: none; }
.ops-toggle[data-on="1"] i { transform: translateX(14px); }
.ops-seg { display: flex; background: var(--paper); border: 1px solid var(--line-1);
  border-radius: var(--r-2); overflow: hidden; }
.ops-seg button { flex: 1; border: 0; background: transparent; color: var(--ink-3);
  font: inherit; font-size: 11px; font-family: var(--f-mono); padding: 4px 0;
  cursor: pointer; transition: background .1s, color .1s; }
.ops-seg button.active { background: var(--ink); color: var(--paper); }
.ops-seg button.changed { color: #d97706; font-weight: 600; }
.ops-seg button.active.changed { background: #d97706; color: #fff; }

/* Action bar */
.ops-actions { display: flex; align-items: center; gap: 10px; padding-top: 4px;
  border-top: 1px solid var(--line-1); margin-top: 4px; }
.ops-reset-btn { appearance: none; border: 1px solid var(--line-2); background: transparent;
  color: var(--ink-3); font: inherit; font-size: 11.5px; border-radius: var(--r-2);
  padding: 5px 14px; cursor: pointer; }
.ops-reset-btn:hover { border-color: var(--ink-3); color: var(--ink); }
.ops-run-btn { appearance: none; border: 0; background: var(--ink); color: var(--paper);
  font: inherit; font-size: 12px; font-weight: 600; border-radius: var(--r-2);
  padding: 6px 18px; cursor: pointer; display: flex; align-items: center; gap: 6px; }
.ops-run-btn:hover { opacity: .85; }
.ops-run-btn:disabled { opacity: .4; cursor: default; }
.ops-run-btn.running { background: var(--ink-3); }

/* Progress bar */
.ops-progress { background: var(--paper-2); border: 1px solid var(--line-1);
  border-radius: var(--r-3); padding: 10px 14px; display: flex; align-items: center; gap: 12px; }
.ops-progress-bar-track { flex: 1; height: 4px; background: var(--line-2); border-radius: 2px; overflow: hidden; }
.ops-progress-bar-fill { height: 100%; background: var(--ink); border-radius: 2px;
  transition: width .3s; }
.ops-progress-label { font-family: var(--f-mono); font-size: 10.5px; color: var(--ink-3);
  white-space: nowrap; min-width: 120px; text-align: right; }

/* Results */
.ops-results { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 900px) { .ops-results { grid-template-columns: 1fr; } }
.ops-result-panel { background: var(--paper-2); border: 1px solid var(--line-1);
  border-radius: var(--r-3); overflow: hidden; }
.ops-result-header { display: flex; align-items: center; gap: 10px;
  padding: 10px 14px; border-bottom: 1px solid var(--line-1); }
.ops-result-title { font-family: var(--f-mono); font-size: 10.5px; font-weight: 600;
  letter-spacing: 0.06em; text-transform: uppercase; }
.ops-result-title.actual { color: var(--ink-3); }
.ops-result-title.sim    { color: var(--ink); }
.ops-summary-chips { display: flex; gap: 8px; flex-wrap: wrap; font-family: var(--f-mono);
  font-size: 10px; }
.ops-chip { background: var(--paper); border: 1px solid var(--line-1); border-radius: 8px;
  padding: 2px 7px; color: var(--ink-2); }
.ops-chip.pos { color: var(--pos); border-color: rgba(10,143,92,.2); background: var(--pos-wash); }
.ops-chip.neg { color: var(--neg); border-color: rgba(194,62,47,.2); background: var(--neg-wash); }

.ops-trades-table { width: 100%; border-collapse: collapse; font-size: 11px; }
.ops-trades-table thead { position: sticky; top: 0; background: var(--paper-2); }
.ops-trades-table th { font-family: var(--f-mono); font-size: 9.5px; font-weight: 600;
  letter-spacing: .04em; text-transform: uppercase; color: var(--ink-3);
  padding: 5px 8px; text-align: right; border-bottom: 1px solid var(--line-1); }
.ops-trades-table th:first-child { text-align: left; }
.ops-trades-table td { padding: 4px 8px; text-align: right; border-bottom: 1px solid var(--line-1);
  font-family: var(--f-mono); font-size: 10.5px; color: var(--ink-2); }
.ops-trades-table td:first-child { text-align: left; color: var(--ink-3); }
.ops-trades-table tr:last-child td { border-bottom: 0; }
.ops-trades-table .pos { color: var(--pos); }
.ops-trades-table .neg { color: var(--neg); }
.ops-trades-table .exit-tag { font-size: 9.5px; background: var(--paper);
  border: 1px solid var(--line-1); border-radius: 4px; padding: 1px 4px; color: var(--ink-3); }
.ops-trades-table .exit-tag.trailing  { border-color: rgba(59,130,246,.3); color: #3b82f6; }
.ops-trades-table .exit-tag.target    { border-color: rgba(10,143,92,.3);  color: var(--pos); }
.ops-trades-table .exit-tag.thesis    { border-color: rgba(217,119,6,.3);  color: #d97706; }
.ops-trades-table .exit-tag.timestop  { color: var(--ink-3); }
.ops-table-scroll { overflow-y: auto; max-height: 380px; }

.ops-empty { display: flex; align-items: center; justify-content: center;
  height: 120px; color: var(--ink-3); font-family: var(--f-mono); font-size: 11px; }
`;

// ── exit tag classifier ───────────────────────────────────────────────────────
function exitClass(label) {
  if (!label) return 'timestop';
  const l = label.toLowerCase();
  if (l.includes('trail') || l.includes('trailing_stop')) return 'trailing';
  if (l.includes('target') || l.includes('target_hit')) return 'target';
  if (l.includes('thesis')) return 'thesis';
  if (l.includes('exit_stack')) return 'target';  // exit_stack with exit_reason = actual trigger
  return 'timestop';
}

// ── Control components ────────────────────────────────────────────────────────
function Ctrl({ label, valueStr, changed, children }) {
  return (
    <div className="ops-control">
      <div className="ops-control-row">
        <span className="ops-control-label">{label}</span>
        <span className={`ops-control-value${changed ? ' changed' : ''}`}>{valueStr}</span>
      </div>
      {children}
    </div>
  );
}

function SliderCtrl({ label, value, live, min, max, step = 0.001, format, onChange }) {
  const changed = Math.abs(num(value) - num(live)) > step * 0.5;
  const fmt = format || ((v) => (v * 100).toFixed(1) + '%');
  const pct = Math.round(((num(value) - num(min)) / (num(max) - num(min))) * 100);
  const fillStyle = {
    background: `linear-gradient(to right, ${changed ? '#d97706' : 'var(--ink)'} 0%, ${changed ? '#d97706' : 'var(--ink)'} ${pct}%, var(--line-2) ${pct}%, var(--line-2) 100%)`
  };
  return (
    <Ctrl label={label} valueStr={fmt(num(value))} changed={changed}>
      <input type="range" className={`ops-slider${changed ? ' changed' : ''}`}
             min={min} max={max} step={step} value={value}
             style={fillStyle}
             onChange={(e) => onChange(parseFloat(e.target.value))} />
    </Ctrl>
  );
}

function ToggleCtrl({ label, value, live, onChange }) {
  const changed = String(value) !== String(live);
  const on = String(value) === '1' || value === true;
  return (
    <div className="ops-toggle-row">
      <span className={`ops-control-label${changed ? ' changed' : ''}`}>{label}</span>
      <button className="ops-toggle" data-on={on ? '1' : '0'} onClick={() => onChange(on ? '0' : '1')}>
        <i />
      </button>
    </div>
  );
}

function SegCtrl({ label, value, live, options, onChange }) {
  const changed = value !== live;
  return (
    <Ctrl label={label} valueStr={value} changed={changed}>
      <div className="ops-seg">
        {options.map(o => (
          <button key={o.value} onClick={() => onChange(o.value)}
                  className={[
                    value === o.value ? 'active' : '',
                    o.value !== live && value === o.value ? 'changed' : '',
                  ].join(' ')}>
            {o.label}
          </button>
        ))}
      </div>
    </Ctrl>
  );
}

// ── Summary bar ───────────────────────────────────────────────────────────────
function SummaryBar({ trades, isActual }) {
  if (!trades || trades.length === 0) return (
    <div className="ops-summary-chips"><span className="ops-chip">No trades</span></div>
  );
  const pnls = trades.map(t => t.pnl_pct);
  const wins = pnls.filter(p => p > 0);
  const total = pnls.reduce((a, b) => a + b, 0);
  const mfes  = trades.map(t => t.mfe_pct || 0);
  const caps  = pnls.map((p, i) => mfes[i] > 0 ? p / mfes[i] : null).filter(v => v !== null);
  const avgCap = caps.length ? caps.reduce((a, b) => a + b, 0) / caps.length : 0;
  const avgPrem = trades.map(t => t.prem_in).reduce((a, b) => a + b, 0) / trades.length;
  const cls = total >= 0 ? 'pos' : 'neg';
  return (
    <div className="ops-summary-chips">
      <span className={`ops-chip ${cls}`}>{pct(total)}</span>
      <span className="ops-chip">{wins.length}/{trades.length} wins</span>
      <span className="ops-chip">MFE capture {(avgCap * 100).toFixed(0)}%</span>
      <span className="ops-chip">avg ₹{avgPrem.toFixed(0)}</span>
    </div>
  );
}

// ── Trade table ───────────────────────────────────────────────────────────────
function TradeTable({ trades }) {
  if (!trades || trades.length === 0) return <div className="ops-empty">no trades</div>;
  return (
    <div className="ops-table-scroll">
      <table className="ops-trades-table">
        <thead>
          <tr>
            <th style={{textAlign:'left'}}>time</th>
            <th>dir</th>
            <th>strike</th>
            <th>prem</th>
            <th>p&amp;l</th>
            <th>mfe</th>
            <th>exit</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((t, i) => {
            const cls = t.pnl_pct >= 0 ? 'pos' : 'neg';
            const eClass = exitClass(t.exit);
            return (
              <tr key={i}>
                <td style={{color:'var(--ink-3)'}}>{t.time_in}→{t.time_out}</td>
                <td>{t.direction}</td>
                <td>{t.strike || '?'}</td>
                <td>{t.prem_in ? t.prem_in.toFixed(0) : '—'}</td>
                <td className={cls}>{pct(t.pnl_pct)}</td>
                <td>{pct(t.mfe_pct)}</td>
                <td><span className={`exit-tag ${eClass}`}>{t.exit || 'TIME_STOP'}</span></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ── Main OPS component ────────────────────────────────────────────────────────
function OpsPage() {
  const today = new Date().toISOString().slice(0, 10);

  // Live config from server
  const [liveEnv, setLiveEnv] = _s({});
  // Local config state (starts as a copy of live)
  const [cfg, setCfg] = _s({});
  const [configLoaded, setConfigLoaded] = _s(false);

  // Job state
  const [jobId, setJobId]   = _s(null);
  const [job, setJob]       = _s(null);
  const [polling, setPolling] = _s(false);

  // Load config on mount
  _e(() => {
    fetch('/api/ops/config')
      .then(r => r.json())
      .then(data => {
        const env = data.ops_env || {};
        setLiveEnv(env);
        setCfg({...env});
        setConfigLoaded(true);
      })
      .catch(() => setConfigLoaded(true));
  }, []);

  // Poll job status
  _e(() => {
    if (!jobId || !polling) return;
    const iv = setInterval(() => {
      fetch(`/api/ops/sim/${jobId}`)
        .then(r => r.json())
        .then(data => {
          setJob(data);
          if (data.status === 'done' || data.status === 'error') {
            setPolling(false);
          }
        })
        .catch(() => {});
    }, 1500);
    return () => clearInterval(iv);
  }, [jobId, polling]);

  const setVal = (key) => (val) => setCfg(prev => ({...prev, [key]: String(val)}));

  const changedKeys = Object.keys(cfg).filter(k => cfg[k] !== liveEnv[k]);
  const isRunning = job && (job.status === 'queued' || job.status === 'loading' || job.status === 'running');
  const progress = job ? (job.total > 0 ? job.progress / job.total : 0) : 0;

  const handleRun = () => {
    const overrides = {};
    changedKeys.forEach(k => { overrides[k] = cfg[k]; });
    fetch('/api/ops/sim/today', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ date: today, overrides }),
    })
      .then(r => r.json())
      .then(data => {
        setJobId(data.job_id);
        setPolling(true);
        setJob({status: 'queued', progress: 0, total: 0, trades: [], actual_trades: []});
      });
  };

  const handleReset = () => setCfg({...liveEnv});

  // Strategy mode preset. Lottery snaps entry/risk to its philosophy (rare,
  // high-conviction, let it run); user can still tweak any slider after.
  const setStrategyMode = (mode) => {
    setCfg(prev => {
      const next = {...prev, EXIT_STRATEGY_MODE: mode};
      if (mode === 'lottery') {
        next.CONSENSUS_BYPASS_MIN_CONFIDENCE = '0.80';
        next.RISK_MAX_SESSION_TRADES = '3';
        next.STRATEGY_STRIKE_SELECTION_POLICY = 'smart_strike';
      } else {
        next.CONSENSUS_BYPASS_MIN_CONFIDENCE = liveEnv.CONSENSUS_BYPASS_MIN_CONFIDENCE || '0.65';
        next.RISK_MAX_SESSION_TRADES = liveEnv.RISK_MAX_SESSION_TRADES || '6';
      }
      return next;
    });
  };

  const v = (key, fallback = '0') => cfg[key] ?? liveEnv[key] ?? fallback;
  const lv = (key, fallback = '0') => liveEnv[key] ?? fallback;
  const curMode = v('EXIT_STRATEGY_MODE', 'scalper');

  const simTrades   = job?.trades || [];
  const actualTrades = job?.actual_trades || [];

  if (!configLoaded) return (
    <div className="ops-page" style={{paddingTop: 40, textAlign: 'center',
      color: 'var(--ink-3)', fontFamily: 'var(--f-mono)', fontSize: 12}}>
      Loading config…
    </div>
  );

  return (
    <>
      <style>{CSS}</style>
      <div className="ops-page">

        {/* ── Config panel ── */}
        <div className="ops-config">
          <div className="ops-config-header">
            <span className="ops-config-title">Sim Config</span>
            <span className="ops-config-date">{today}</span>
            {changedKeys.length > 0 && (
              <span className="ops-changes-badge">{changedKeys.length} change{changedKeys.length > 1 ? 's' : ''} from live</span>
            )}
          </div>

          {/* ── Strategy mode — the big choice ── */}
          <div style={{display:'flex', alignItems:'center', gap:14, padding:'4px 0 10px', borderBottom:'1px solid var(--line-1)', marginBottom:4}}>
            <span style={{fontFamily:'var(--f-mono)', fontSize:11, fontWeight:600, letterSpacing:'0.04em', color:'var(--ink-2)'}}>STRATEGY MODE</span>
            <div className="ops-seg" style={{width:260}}>
              <button className={curMode==='scalper'?'active':''} onClick={()=>setStrategyMode('scalper')}>Scalper</button>
              <button className={curMode==='lottery'?'active':''} onClick={()=>setStrategyMode('lottery')}>🎟 Lottery</button>
            </div>
            <span style={{fontFamily:'var(--f-mono)', fontSize:10, color:'var(--ink-3)'}}>
              {curMode==='lottery'
                ? 'rare high-conviction bets · let winners run · lose small often'
                : 'frequent · capture small gains · tight exits'}
            </span>
          </div>

          <div className="ops-config-groups">

            {/* Exit strategy — scalper params (shown when scalper) */}
            {curMode !== 'lottery' && (
            <div>
              <div className="ops-group-label">Exit Strategy</div>
              <ToggleCtrl label="Exit stack enabled"
                value={v('EXIT_POLICY_STACK_ENABLED','0')}
                live={lv('EXIT_POLICY_STACK_ENABLED','0')}
                onChange={setVal('EXIT_POLICY_STACK_ENABLED')} />
              <SliderCtrl label="Trail activation"
                value={parseFloat(v('EXIT_TRAILING_ACTIVATION_PCT','0.01'))}
                live={parseFloat(lv('EXIT_TRAILING_ACTIVATION_PCT','0.01'))}
                min={0.005} max={0.04} step={0.001}
                format={v => (v*100).toFixed(1)+'%'}
                onChange={setVal('EXIT_TRAILING_ACTIVATION_PCT')} />
              <SliderCtrl label="Trail amount"
                value={parseFloat(v('EXIT_TRAILING_TRAIL_PCT','0.005'))}
                live={parseFloat(lv('EXIT_TRAILING_TRAIL_PCT','0.005'))}
                min={0.002} max={0.015} step={0.001}
                format={v => (v*100).toFixed(1)+'%'}
                onChange={setVal('EXIT_TRAILING_TRAIL_PCT')} />
              <SliderCtrl label="Emergency target"
                value={parseFloat(v('EXIT_PREMIUM_TARGET_PCT','0.04'))}
                live={parseFloat(lv('EXIT_PREMIUM_TARGET_PCT','0.04'))}
                min={0.015} max={0.08} step={0.005}
                format={v => (v*100).toFixed(1)+'%'}
                onChange={setVal('EXIT_PREMIUM_TARGET_PCT')} />
            </div>
            )}

            {/* Lottery exit params (shown when lottery) */}
            {curMode === 'lottery' && (
            <div>
              <div className="ops-group-label">🎟 Lottery Exit</div>
              <SliderCtrl label="Hard stop (cap loss)"
                value={parseFloat(v('LOTTERY_HARD_STOP_PCT','0.25'))}
                live={parseFloat(lv('LOTTERY_HARD_STOP_PCT','0.25'))}
                min={0.10} max={0.50} step={0.05}
                format={v => '-'+(v*100).toFixed(0)+'%'}
                onChange={setVal('LOTTERY_HARD_STOP_PCT')} />
              <SliderCtrl label="Big target (take win)"
                value={parseFloat(v('LOTTERY_BIG_TARGET_PCT','0.40'))}
                live={parseFloat(lv('LOTTERY_BIG_TARGET_PCT','0.40'))}
                min={0.20} max={1.00} step={0.05}
                format={v => '+'+(v*100).toFixed(0)+'%'}
                onChange={setVal('LOTTERY_BIG_TARGET_PCT')} />
              <SliderCtrl label="Runner activates at"
                value={parseFloat(v('LOTTERY_RUNNER_ACTIVATION_MFE','0.10'))}
                live={parseFloat(lv('LOTTERY_RUNNER_ACTIVATION_MFE','0.10'))}
                min={0.10} max={0.50} step={0.05}
                format={v => '+'+(v*100).toFixed(0)+'%'}
                onChange={setVal('LOTTERY_RUNNER_ACTIVATION_MFE')} />
              <SliderCtrl label="Runner giveback"
                value={parseFloat(v('LOTTERY_RUNNER_GIVEBACK_FRAC','0.35'))}
                live={parseFloat(lv('LOTTERY_RUNNER_GIVEBACK_FRAC','0.35'))}
                min={0.20} max={0.60} step={0.05}
                format={v => (v*100).toFixed(0)+'%'}
                onChange={setVal('LOTTERY_RUNNER_GIVEBACK_FRAC')} />
            </div>
            )}

            {/* Entry gate */}
            <div>
              <div className="ops-group-label">Entry Gate</div>
              <SliderCtrl label="Min confidence"
                value={parseFloat(v('CONSENSUS_BYPASS_MIN_CONFIDENCE','0.65'))}
                live={parseFloat(lv('CONSENSUS_BYPASS_MIN_CONFIDENCE','0.65'))}
                min={0.50} max={0.85} step={0.01}
                format={v => v.toFixed(2)}
                onChange={setVal('CONSENSUS_BYPASS_MIN_CONFIDENCE')} />
              <SliderCtrl label="SIDEWAYS margin"
                value={parseFloat(v('DIRECTION_MIN_MARGIN_SIDEWAYS','2.0'))}
                live={parseFloat(lv('DIRECTION_MIN_MARGIN_SIDEWAYS','2.0'))}
                min={1.0} max={4.0} step={0.25}
                format={v => v.toFixed(2)}
                onChange={setVal('DIRECTION_MIN_MARGIN_SIDEWAYS')} />
            </div>

            {/* Strike selection */}
            <div>
              <div className="ops-group-label">Strike Selection</div>
              <SegCtrl label="Policy"
                value={v('STRATEGY_STRIKE_SELECTION_POLICY','atm')}
                live={lv('STRATEGY_STRIKE_SELECTION_POLICY','atm')}
                options={[{value:'atm',label:'ATM'},{value:'smart_strike',label:'Smart'}]}
                onChange={setVal('STRATEGY_STRIKE_SELECTION_POLICY')} />
              <SliderCtrl label="Max premium"
                value={parseFloat(v('SMART_STRIKE_MAX_PREMIUM','800'))}
                live={parseFloat(lv('SMART_STRIKE_MAX_PREMIUM','800'))}
                min={300} max={1500} step={50}
                format={v => '₹'+v.toFixed(0)}
                onChange={setVal('SMART_STRIKE_MAX_PREMIUM')} />
              <SliderCtrl label="Max OTM steps"
                value={parseFloat(v('STRATEGY_STRIKE_MAX_OTM_STEPS','8'))}
                live={parseFloat(lv('STRATEGY_STRIKE_MAX_OTM_STEPS','8'))}
                min={0} max={12} step={1}
                format={v => v.toFixed(0)+' steps'}
                onChange={setVal('STRATEGY_STRIKE_MAX_OTM_STEPS')} />
            </div>

          </div>

          <div className="ops-actions">
            {changedKeys.length > 0 && (
              <button className="ops-reset-btn" onClick={handleReset}>Reset to live</button>
            )}
            <button className={`ops-run-btn${isRunning ? ' running' : ''}`}
                    disabled={!!isRunning}
                    onClick={handleRun}>
              {isRunning ? '⏳ Running…' : '▶  Run Sim'}
            </button>
            {job?.status === 'done' && (
              <span style={{fontFamily:'var(--f-mono)', fontSize:10.5, color:'var(--pos)', marginLeft:4}}>
                ✓ done — {simTrades.length} trades
              </span>
            )}
            {job?.status === 'error' && (
              <span style={{fontFamily:'var(--f-mono)', fontSize:10.5, color:'var(--neg)', marginLeft:4}}>
                ✗ {job.error}
              </span>
            )}
          </div>
        </div>

        {/* ── Progress bar ── */}
        {isRunning && (
          <div className="ops-progress">
            <span style={{fontFamily:'var(--f-mono)', fontSize:10.5, color:'var(--ink-3)'}}>
              {job.status === 'queued' ? 'Queued' : job.status === 'loading' ? 'Loading engine…' : 'Replaying'}
            </span>
            <div className="ops-progress-bar-track">
              <div className="ops-progress-bar-fill" style={{width: `${(progress * 100).toFixed(0)}%`}} />
            </div>
            <span className="ops-progress-label">
              {job.total > 0 ? `${job.progress} / ${job.total} snapshots` : '…'}
            </span>
          </div>
        )}

        {/* ── Sim diagnostics (shown after run) ── */}
        {job?.status === 'done' && (
          <div style={{fontFamily:'var(--f-mono)', fontSize:10, color:'var(--ink-3)',
            background:'var(--paper-2)', border:'1px solid var(--line-1)', borderRadius:'var(--r-2)',
            padding:'8px 14px', display:'flex', gap:20, flexWrap:'wrap'}}>
            <span style={{color:'var(--ink-2)', fontWeight:600}}>SIM DIAG</span>
            {job.diag && <>
              <span>snapshots: {job.diag.evaluated}</span>
              <span>signals: {job.diag.signals}</span>
              <span>entries: {job.diag.entries}</span>
              {job.diag.eval_errors > 0 && <span style={{color:'var(--neg)'}}>errors: {job.diag.eval_errors}</span>}
            </>}
            <span>exit_mode: <span style={{color:'var(--ink)'}}>{job.exit_stack || '—'}</span></span>
            {job.overrides_applied && Object.keys(job.overrides_applied).length > 0 && (
              <span>overrides: {Object.entries(job.overrides_applied).map(([k,v]) =>
                `${k.replace(/^(EXIT_|LOTTERY_|STRATEGY_|RISK_|CONSENSUS_|DIRECTION_)/,'').toLowerCase()}=${v}`
              ).join(' · ')}</span>
            )}
          </div>
        )}

        {/* ── Results (actual vs sim side by side) ── */}
        {(job?.status === 'done' || actualTrades.length > 0) && (
          <div className="ops-results">

            <div className="ops-result-panel">
              <div className="ops-result-header">
                <span className="ops-result-title actual">Actual — {today}</span>
                <SummaryBar trades={actualTrades} isActual />
              </div>
              <TradeTable trades={actualTrades} />
            </div>

            <div className="ops-result-panel">
              <div className="ops-result-header">
                <span className="ops-result-title sim">
                  Sim
                  {changedKeys.length > 0
                    ? ` — ${changedKeys.length} override${changedKeys.length > 1 ? 's' : ''}`
                    : ' — same as live'}
                </span>
                {job?.status === 'done' && <SummaryBar trades={simTrades} />}
                {isRunning && <span style={{fontFamily:'var(--f-mono)', fontSize:10, color:'var(--ink-3)'}}>running…</span>}
              </div>
              <TradeTable trades={simTrades} />
            </div>

          </div>
        )}

      </div>
    </>
  );
}

window.OpsPage = OpsPage;
