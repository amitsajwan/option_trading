/* Postmortem tab — trade-by-trade analysis from stored positions + traces.
   Renders in mode === 'postmortem' inside the main App shell.
   No build step: Babel transforms this at runtime like the other JSX files. */

/* global React, window */

(function () {
  const { useState, useEffect, useCallback } = React;

  const TAG_COLOR = {
    COST_MISS:      { bg: '#fff3cd', border: '#ffc107', text: '#856404' },
    EXIT_MISS:      { bg: '#cff4fc', border: '#0dcaf0', text: '#055160' },
    DIRECTION_MISS: { bg: '#f8d7da', border: '#dc3545', text: '#842029' },
    ENTRY_MISS:     { bg: '#e2d9f3', border: '#6f42c1', text: '#432874' },
    NOISE:          { bg: '#e2e3e5', border: '#adb5bd', text: '#41464b' },
    UNKNOWN:        { bg: '#f8f9fa', border: '#dee2e6', text: '#6c757d' },
  };

  const TAG_ICON = {
    COST_MISS:      '💸',
    EXIT_MISS:      '🏃',
    DIRECTION_MISS: '↔️',
    ENTRY_MISS:     '🎯',
    NOISE:          '〰️',
    UNKNOWN:        '❓',
  };

  function pct(v, digits = 2) {
    if (v == null) return '—';
    return (v >= 0 ? '+' : '') + v.toFixed(digits) + '%';
  }

  function pnlColor(v) {
    if (v == null) return 'var(--ink-3)';
    return v >= 0 ? '#0a8f5c' : '#c0392b';
  }

  // ── Tag chip ──────────────────────────────────────────────────────────────
  function AutopsyTag({ tag, size = 'sm' }) {
    const colors = TAG_COLOR[tag] || TAG_COLOR.UNKNOWN;
    const icon = TAG_ICON[tag] || '❓';
    const pad = size === 'lg' ? '6px 14px' : '3px 9px';
    const fs = size === 'lg' ? '12px' : '11px';
    return (
      <span style={{
        display: 'inline-flex', alignItems: 'center', gap: '5px',
        background: colors.bg, border: `1px solid ${colors.border}`, color: colors.text,
        borderRadius: '12px', padding: pad, fontSize: fs,
        fontFamily: 'var(--f-mono)', fontWeight: 600, whiteSpace: 'nowrap',
      }}>
        {icon} {tag || 'UNKNOWN'}
      </span>
    );
  }

  // ── P&L bar (MFE / MAE / exit) ────────────────────────────────────────────
  function PnlBar({ gross, net, mfe, mae }) {
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
    const scale = 5; // ±5% = full bar
    const mfePct = clamp((mfe || 0) / scale * 50, 0, 50);
    const maePct = clamp(Math.abs(mae || 0) / scale * 50, 0, 50);
    const netPx = clamp((net || 0) / scale * 50, -50, 50);

    return (
      <div style={{ marginTop: '8px' }}>
        <div style={{ position: 'relative', height: '18px', background: '#f0f0f0', borderRadius: '3px', overflow: 'hidden' }}>
          {/* MFE green */}
          <div style={{
            position: 'absolute', left: '50%', width: mfePct + '%',
            top: 0, bottom: 0, background: 'rgba(10,143,92,0.25)',
          }} />
          {/* MAE red */}
          <div style={{
            position: 'absolute', right: '50%', width: maePct + '%',
            top: 0, bottom: 0, background: 'rgba(192,57,43,0.25)',
          }} />
          {/* Net exit line */}
          <div style={{
            position: 'absolute', left: `calc(50% + ${netPx}%)`,
            top: '2px', bottom: '2px', width: '2px',
            background: net >= 0 ? '#0a8f5c' : '#c0392b', borderRadius: '1px',
          }} />
          {/* Centre line */}
          <div style={{
            position: 'absolute', left: '50%', top: 0, bottom: 0,
            width: '1px', background: '#ccc',
          }} />
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '10px', color: 'var(--ink-3)', marginTop: '2px', fontFamily: 'var(--f-mono)' }}>
          <span style={{ color: '#c0392b' }}>MAE {pct(mae)}</span>
          <span>|</span>
          <span style={{ color: '#0a8f5c' }}>MFE {pct(mfe)}</span>
        </div>
      </div>
    );
  }

  // ── Trade card (list view) ─────────────────────────────────────────────────
  function TradeCard({ trade, selected, onClick }) {
    const tag = trade.autopsy?.tag || 'UNKNOWN';
    const isWin = (trade.net_pnl_pct || 0) >= 0;
    const border = selected ? '2px solid var(--ink)' : `2px solid ${TAG_COLOR[tag]?.border || '#dee2e6'}`;

    return (
      <div
        onClick={onClick}
        style={{
          border, borderRadius: '8px', padding: '12px 14px', cursor: 'pointer',
          background: selected ? 'var(--paper-2)' : 'var(--paper)',
          marginBottom: '8px', transition: 'all 0.1s',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '6px' }}>
          <span style={{ fontFamily: 'var(--f-mono)', fontSize: '11px', color: 'var(--ink-3)' }}>
            {trade.entry_time?.slice(0, 5)} → {trade.exit_time?.slice(0, 5)}
          </span>
          <span style={{
            fontFamily: 'var(--f-mono)', fontSize: '12px', fontWeight: 700,
            background: trade.direction === 'CE' ? '#e8f4fd' : '#fdecea',
            color: trade.direction === 'CE' ? '#1565c0' : '#b71c1c',
            padding: '1px 7px', borderRadius: '4px',
          }}>
            {trade.direction} {trade.strike}
          </span>
          <span style={{ marginLeft: 'auto' }}>
            <AutopsyTag tag={tag} />
          </span>
        </div>

        <div style={{ display: 'flex', gap: '16px', fontSize: '12px', fontFamily: 'var(--f-mono)' }}>
          <span>
            Net: <strong style={{ color: pnlColor(trade.net_pnl_pct) }}>{pct(trade.net_pnl_pct)}</strong>
          </span>
          <span style={{ color: 'var(--ink-3)' }}>
            Gross: {pct(trade.gross_pnl_pct)}
          </span>
          {trade.signals?.entry_diag?.entry_prob != null && (
            <span style={{ color: 'var(--ink-3)' }}>
              prob: {trade.signals.entry_diag.entry_prob}
            </span>
          )}
          <span style={{ color: 'var(--ink-3)', marginLeft: 'auto' }}>
            {trade.exit_reason}
          </span>
        </div>

        <PnlBar gross={trade.gross_pnl_pct} net={trade.net_pnl_pct} mfe={trade.mfe_pct} mae={trade.mae_pct} />
      </div>
    );
  }

  // ── Signal row ────────────────────────────────────────────────────────────
  function SignalRow({ label, value, good }) {
    const color = good === true ? '#0a8f5c' : good === false ? '#c0392b' : 'var(--ink-3)';
    return (
      <div style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', borderBottom: '1px solid var(--line-1)', fontSize: '12px', fontFamily: 'var(--f-mono)' }}>
        <span style={{ color: 'var(--ink-3)' }}>{label}</span>
        <span style={{ color, fontWeight: 600 }}>{value ?? '—'}</span>
      </div>
    );
  }

  // ── Detail panel ─────────────────────────────────────────────────────────
  function TradeDetail({ pos_id, kind }) {
    const [detail, setDetail] = useState(null);
    const [loading, setLoading] = useState(false);
    const [err, setErr] = useState(null);

    useEffect(() => {
      if (!pos_id) return;
      setLoading(true);
      setErr(null);
      fetch(`/api/postmortem/position/${pos_id}?kind=${kind}`)
        .then(r => r.ok ? r.json() : r.json().then(e => Promise.reject(e.detail || 'fetch error')))
        .then(d => { setDetail(d); setLoading(false); })
        .catch(e => { setErr(String(e)); setLoading(false); });
    }, [pos_id, kind]);

    if (!pos_id) return <div style={{ padding: '32px', color: 'var(--ink-3)', fontSize: '13px', fontFamily: 'var(--f-mono)' }}>← Select a trade to inspect</div>;
    if (loading) return <div style={{ padding: '32px', color: 'var(--ink-3)', fontSize: '12px' }}>Loading…</div>;
    if (err) return <div style={{ padding: '32px', color: '#c0392b', fontSize: '12px', fontFamily: 'var(--f-mono)' }}>Error: {err}</div>;
    if (!detail) return null;

    const tag = detail.autopsy?.tag || 'UNKNOWN';
    const tagColors = TAG_COLOR[tag] || TAG_COLOR.UNKNOWN;
    const sig = detail.signals || {};
    const diag = sig.entry_diag || {};
    const trace = detail.trace || {};

    return (
      <div style={{ padding: '20px', overflowY: 'auto', height: '100%' }}>
        {/* Header */}
        <div style={{ marginBottom: '16px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '6px' }}>
            <span style={{ fontFamily: 'var(--f-mono)', fontSize: '11px', color: 'var(--ink-3)' }}>
              {detail.trade_date} · {detail.position_id_short}
            </span>
            <span style={{
              fontFamily: 'var(--f-mono)', fontSize: '14px', fontWeight: 700,
              background: detail.direction === 'CE' ? '#e8f4fd' : '#fdecea',
              color: detail.direction === 'CE' ? '#1565c0' : '#b71c1c',
              padding: '2px 10px', borderRadius: '5px',
            }}>
              {detail.direction} {detail.strike}
            </span>
            <AutopsyTag tag={tag} size="lg" />
          </div>

          {/* Autopsy explanation */}
          <div style={{
            background: tagColors.bg, border: `1px solid ${tagColors.border}`,
            borderRadius: '6px', padding: '10px 14px', fontSize: '12px',
            color: tagColors.text, lineHeight: '1.5',
          }}>
            {detail.autopsy?.explanation || ''}
            {detail.autopsy?.source === 'reconstructed' && (
              <span style={{ fontSize: '10px', opacity: 0.7, marginLeft: '8px' }}>(reconstructed)</span>
            )}
          </div>
        </div>

        {/* P&L summary */}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: '8px', marginBottom: '16px' }}>
          {[
            { label: 'Net P&L', value: pct(detail.net_pnl_pct), color: pnlColor(detail.net_pnl_pct) },
            { label: 'Gross P&L', value: pct(detail.gross_pnl_pct), color: pnlColor(detail.gross_pnl_pct) },
            { label: 'MFE', value: pct(detail.mfe_pct), color: '#0a8f5c' },
            { label: 'MAE', value: pct(detail.mae_pct), color: '#c0392b' },
          ].map(({ label, value, color }) => (
            <div key={label} style={{ background: 'var(--paper-2)', borderRadius: '6px', padding: '10px 12px', textAlign: 'center' }}>
              <div style={{ fontSize: '10px', color: 'var(--ink-3)', fontFamily: 'var(--f-mono)', marginBottom: '4px' }}>{label}</div>
              <div style={{ fontSize: '15px', fontWeight: 700, fontFamily: 'var(--f-mono)', color }}>{value}</div>
            </div>
          ))}
        </div>
        <PnlBar gross={detail.gross_pnl_pct} net={detail.net_pnl_pct} mfe={detail.mfe_pct} mae={detail.mae_pct} />

        {/* Timeline */}
        <div style={{ margin: '16px 0' }}>
          <div style={{ fontSize: '11px', fontWeight: 600, color: 'var(--ink-3)', marginBottom: '6px', textTransform: 'uppercase', letterSpacing: '0.06em' }}>Timeline</div>
          <div style={{ fontFamily: 'var(--f-mono)', fontSize: '12px' }}>
            <SignalRow label="Entry" value={detail.entry_time?.slice(0, 8)} />
            <SignalRow label="Exit" value={`${detail.exit_time?.slice(0, 8)} · ${detail.exit_reason}`} />
            <SignalRow label="Bars held" value={detail.bars_held} />
            <SignalRow label="Entry premium" value={detail.entry_premium != null ? `₹${detail.entry_premium.toFixed(1)}` : '—'} />
            <SignalRow label="Exit premium" value={detail.exit_premium != null ? `₹${detail.exit_premium.toFixed(1)}` : '—'} />
          </div>
        </div>

        {/* Entry model signals */}
        <div style={{ margin: '16px 0' }}>
          <div style={{ fontSize: '11px', fontWeight: 600, color: 'var(--ink-3)', marginBottom: '6px', textTransform: 'uppercase', letterSpacing: '0.06em' }}>Entry Model</div>
          <div style={{ fontFamily: 'var(--f-mono)', fontSize: '12px' }}>
            {diag.error ? (
              <div style={{ color: '#c0392b', padding: '6px 0', fontSize: '11px' }}>
                ⚠ Prediction failed: {diag.error}
              </div>
            ) : (
              <>
                <SignalRow label="Entry prob" value={diag.entry_prob != null ? diag.entry_prob : (sig.entry_prob ?? sig.ml_entry_prob)} />
                <SignalRow label="Threshold" value={diag.threshold} />
                <SignalRow label="Fired" value={diag.fired != null ? (diag.fired ? 'YES' : 'NO') : '—'} good={diag.fired} />
              </>
            )}
          </div>
        </div>

        {/* Direction signals */}
        <div style={{ margin: '16px 0' }}>
          <div style={{ fontSize: '11px', fontWeight: 600, color: 'var(--ink-3)', marginBottom: '6px', textTransform: 'uppercase', letterSpacing: '0.06em' }}>Direction Signals</div>
          <div style={{ fontFamily: 'var(--f-mono)', fontSize: '12px' }}>
            <SignalRow label="CE prob" value={sig.ml_ce_prob ?? sig.direction_ce_prob} />
            <SignalRow label="PE prob" value={sig.ml_pe_prob ?? sig.direction_pe_prob} />
            <SignalRow label="VWAP side" value={sig.vwap_side} />
            <SignalRow label="Up prob" value={sig.ml_direction_up_prob} />
          </div>
        </div>

        {/* Regime */}
        <div style={{ margin: '16px 0' }}>
          <div style={{ fontSize: '11px', fontWeight: 600, color: 'var(--ink-3)', marginBottom: '6px', textTransform: 'uppercase', letterSpacing: '0.06em' }}>Regime</div>
          <div style={{ fontFamily: 'var(--f-mono)', fontSize: '12px' }}>
            <SignalRow label="Regime" value={detail.regime || trace.regime} />
            <SignalRow label="Confidence" value={trace.regime_conf != null ? (trace.regime_conf * 100).toFixed(1) + '%' : '—'} />
            <SignalRow label="Outcome" value={trace.outcome} />
            <SignalRow label="Blocker" value={trace.blocker} />
          </div>
        </div>

        {/* Gate trace */}
        {trace.flow_gates && trace.flow_gates.length > 0 && (
          <div style={{ margin: '16px 0' }}>
            <div style={{ fontSize: '11px', fontWeight: 600, color: 'var(--ink-3)', marginBottom: '6px', textTransform: 'uppercase', letterSpacing: '0.06em' }}>Gate Trace</div>
            {trace.flow_gates.map((g, i) => (
              <div key={i} style={{
                display: 'flex', alignItems: 'center', gap: '8px',
                padding: '4px 0', borderBottom: '1px solid var(--line-1)',
                fontSize: '11px', fontFamily: 'var(--f-mono)',
              }}>
                <span style={{
                  color: g.status === 'passed' ? '#0a8f5c' : '#c0392b',
                  fontWeight: 700, minWidth: '12px',
                }}>
                  {g.status === 'passed' ? '✓' : '✗'}
                </span>
                <span style={{ color: 'var(--ink-3)', minWidth: '120px' }}>{g.gate_id}</span>
                <span style={{ color: 'var(--ink-3)', fontSize: '10px' }}>{g.note || ''}</span>
              </div>
            ))}
          </div>
        )}

        {/* Manage timeline (mini sparkline as text) */}
        {detail.manage_events && detail.manage_events.length > 0 && (
          <div style={{ margin: '16px 0' }}>
            <div style={{ fontSize: '11px', fontWeight: 600, color: 'var(--ink-3)', marginBottom: '6px', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
              Intraday P&L ({detail.manage_events.length} bars)
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '3px' }}>
              {detail.manage_events.map((e, i) => {
                const v = e.unrealized_pct;
                const color = v == null ? '#ccc' : v >= 0 ? '#0a8f5c' : '#c0392b';
                return (
                  <div key={i} title={`${e.market_time_ist?.slice(0, 5)} · ${v != null ? pct(v) : '—'}`}
                    style={{
                      width: '8px', height: '24px', borderRadius: '2px',
                      background: color, opacity: 0.7,
                      display: 'flex', alignItems: 'flex-end',
                    }}
                  />
                );
              })}
            </div>
          </div>
        )}
      </div>
    );
  }

  // ── Summary bar ───────────────────────────────────────────────────────────
  function SummaryBar({ trades }) {
    if (!trades || trades.length === 0) return null;
    const wins = trades.filter(t => (t.net_pnl_pct || 0) >= 0).length;
    const totalNet = trades.reduce((s, t) => s + (t.net_pnl_pct || 0), 0);
    const tags = {};
    trades.forEach(t => {
      const tag = t.autopsy?.tag || 'UNKNOWN';
      tags[tag] = (tags[tag] || 0) + 1;
    });

    return (
      <div style={{
        padding: '10px 14px', background: 'var(--paper-2)', borderBottom: '1px solid var(--line-1)',
        display: 'flex', gap: '20px', alignItems: 'center', flexWrap: 'wrap',
        fontSize: '12px', fontFamily: 'var(--f-mono)',
      }}>
        <span><strong>{trades.length}</strong> trades</span>
        <span>Win rate: <strong style={{ color: wins / trades.length >= 0.5 ? '#0a8f5c' : '#c0392b' }}>{(wins / trades.length * 100).toFixed(0)}%</strong></span>
        <span>Net: <strong style={{ color: pnlColor(totalNet) }}>{pct(totalNet)}</strong></span>
        <span style={{ color: 'var(--ink-3)' }}>avg {pct(totalNet / trades.length)}</span>
        <div style={{ display: 'flex', gap: '6px', marginLeft: 'auto', flexWrap: 'wrap' }}>
          {Object.entries(tags).map(([tag, count]) => (
            <span key={tag} style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}>
              <AutopsyTag tag={tag} />
              <span style={{ fontSize: '11px', color: 'var(--ink-3)' }}>×{count}</span>
            </span>
          ))}
        </div>
      </div>
    );
  }

  // ── Main PostmortemMonitor component ──────────────────────────────────────
  function PostmortemMonitor() {
    const today = new Date().toISOString().slice(0, 10);
    const [date, setDate] = useState(today);
    const [kind, setKind] = useState('live');
    const [trades, setTrades] = useState([]);
    const [loading, setLoading] = useState(false);
    const [err, setErr] = useState(null);
    const [selectedId, setSelectedId] = useState(null);

    const load = useCallback((d, k) => {
      setLoading(true);
      setErr(null);
      setSelectedId(null);
      fetch(`/api/postmortem/positions?date=${d}&kind=${k}`)
        .then(r => r.ok ? r.json() : r.json().then(e => Promise.reject(e.detail || 'fetch error')))
        .then(data => { setTrades(data); setLoading(false); })
        .catch(e => { setErr(String(e)); setLoading(false); });
    }, []);

    useEffect(() => { load(date, kind); }, [date, kind]);

    return (
      <div style={{ display: 'flex', flexDirection: 'column', height: '100vh', background: 'var(--paper)' }}>
        {/* Toolbar */}
        <div style={{
          padding: '10px 16px', borderBottom: '1px solid var(--line-1)',
          display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap',
          background: 'var(--paper)',
        }}>
          <span style={{ fontFamily: 'var(--f-mono)', fontSize: '13px', fontWeight: 600 }}>🔬 Postmortem</span>
          <input
            type="date"
            value={date}
            onChange={e => setDate(e.target.value)}
            style={{
              fontFamily: 'var(--f-mono)', fontSize: '12px', padding: '5px 10px',
              border: '1px solid var(--line-1)', borderRadius: '6px',
              background: 'var(--paper)', color: 'var(--ink)',
            }}
          />
          <div style={{ display: 'inline-flex', background: 'var(--paper-2)', border: '1px solid var(--line-1)', borderRadius: '6px', padding: '2px' }}>
            {['live', 'sim'].map(k => (
              <button key={k} onClick={() => setKind(k)} style={{
                background: kind === k ? 'var(--paper)' : 'transparent',
                border: 0, fontFamily: 'var(--f-mono)', fontSize: '11px',
                color: kind === k ? 'var(--ink)' : 'var(--ink-3)',
                padding: '4px 10px', borderRadius: '4px', cursor: 'pointer',
                boxShadow: kind === k ? '0 1px 2px rgba(0,0,0,0.08)' : 'none',
              }}>
                {k.toUpperCase()}
              </button>
            ))}
          </div>
          <button onClick={() => load(date, kind)} style={{
            fontFamily: 'var(--f-mono)', fontSize: '11px', padding: '5px 12px',
            border: '1px solid var(--line-1)', borderRadius: '6px',
            background: 'var(--paper)', cursor: 'pointer', color: 'var(--ink)',
          }}>↻ Refresh</button>
          {loading && <span style={{ fontSize: '11px', color: 'var(--ink-3)', fontFamily: 'var(--f-mono)' }}>Loading…</span>}
          {err && <span style={{ fontSize: '11px', color: '#c0392b', fontFamily: 'var(--f-mono)' }}>Error: {err}</span>}
        </div>

        {/* Summary bar */}
        {!loading && trades.length > 0 && <SummaryBar trades={trades} />}

        {/* Body: trade list + detail panel */}
        <div style={{ display: 'flex', flex: 1, overflow: 'hidden' }}>
          {/* Left: trade list */}
          <div style={{
            width: selectedId ? '380px' : '100%', minWidth: '300px',
            borderRight: selectedId ? '1px solid var(--line-1)' : 'none',
            overflowY: 'auto', padding: '12px 14px',
            transition: 'width 0.15s',
          }}>
            {!loading && trades.length === 0 && !err && (
              <div style={{ padding: '32px', textAlign: 'center', color: 'var(--ink-3)', fontSize: '13px', fontFamily: 'var(--f-mono)' }}>
                No closed positions for {date}
              </div>
            )}
            {trades.map(t => (
              <TradeCard
                key={t.position_id}
                trade={t}
                selected={selectedId === t.position_id}
                onClick={() => setSelectedId(t.position_id === selectedId ? null : t.position_id)}
              />
            ))}
          </div>

          {/* Right: detail panel */}
          {selectedId && (
            <div style={{ flex: 1, overflowY: 'auto', position: 'relative' }}>
              <button
                onClick={() => setSelectedId(null)}
                style={{
                  position: 'absolute', top: '12px', right: '14px', zIndex: 2,
                  fontFamily: 'var(--f-mono)', fontSize: '11px', padding: '4px 10px',
                  border: '1px solid var(--line-1)', borderRadius: '6px',
                  background: 'var(--paper)', cursor: 'pointer', color: 'var(--ink-3)',
                }}
              >✕ Close</button>
              <TradeDetail pos_id={selectedId} kind={kind} />
            </div>
          )}
        </div>
      </div>
    );
  }

  window.PostmortemMonitor = PostmortemMonitor;
})();
