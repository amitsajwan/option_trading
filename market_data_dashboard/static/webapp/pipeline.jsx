// pipeline.jsx — Pipeline Decision Monitor  v2
// Exports (global): RegimeBadge, StaleBanner, DepthMiniWidget, PipelineMonitor
/* global React */
const { useState, useEffect, useRef, useCallback, useMemo } = React;

// ── Constants ───────────────────────────────────────────────────────────────
const REGIME_COLORS = {
  TRENDING:    '#22c55e', SIDEWAYS: '#71717a', CHOP: '#eab308',
  BREAKOUT:    '#06b6d4', PANIC:    '#f97316', DEAD_MARKET: '#3f3f46',
  HIGH_VOL:    '#f59e0b', AVOID:    '#ef4444', EXPIRY:      '#a855f7',
  PRE_EXPIRY:  '#c084fc',
};

const STAGE_ORDER  = ['regime','entry','direction','depth','strike','risk','execution'];
const STAGE_LABELS = {
  regime:'Regime', entry:'Entry', direction:'Dir', depth:'Depth',
  strike:'Strike', risk:'Risk',  execution:'Exec',
};

// ── Tiny helpers ─────────────────────────────────────────────────────────────
function _mono(extra) {
  return { fontFamily: 'var(--f-mono)', ...extra };
}
function _chip(color, extra) {
  return {
    display: 'inline-flex', alignItems: 'center', gap: 4,
    background: color + '22', color, border: `1px solid ${color}55`,
    borderRadius: 4, padding: '2px 7px', whiteSpace: 'nowrap',
    ..._mono({ fontSize: 11, fontWeight: 600 }), ...extra,
  };
}

// ── RegimeBadge ───────────────────────────────────────────────────────────────
function RegimeBadge({ regime, confidence }) {
  const color = REGIME_COLORS[regime] || '#71717a';
  const pct   = confidence != null ? `${(confidence * 100).toFixed(0)}%` : '';
  return (
    <span style={_chip(color)}>
      <span style={{ width: 6, height: 6, borderRadius: '50%', background: color, flexShrink: 0 }} />
      {regime || '—'}
      {pct && <span style={{ opacity: 0.7, fontWeight: 400 }}>{pct}</span>}
    </span>
  );
}

// ── StageFlow ─────────────────────────────────────────────────────────────────
function StageFlow({ stages }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 2, flexWrap: 'wrap' }}>
      {STAGE_ORDER.map((s, i) => {
        const st      = stages?.[s];
        const out     = st?.outcome || '—';
        const blocked = out === 'blocked' || out === 'vetoed' || out === 'rejected';
        const skipped = out === 'skipped' || out === 'SKIP';
        const color   = blocked ? '#ef4444' : skipped ? '#71717a' : st ? '#22c55e' : '#52525b';
        return (
          <React.Fragment key={s}>
            {i > 0 && <span style={{ color: '#52525b', fontSize: 10 }}>›</span>}
            <span title={`${STAGE_LABELS[s]}: ${out}`}
              style={_chip(color, { fontSize: 10, padding: '1px 6px', borderRadius: 3 })}>
              {STAGE_LABELS[s]}
            </span>
          </React.Fragment>
        );
      })}
    </div>
  );
}

// ── BidGauge ──────────────────────────────────────────────────────────────────
function BidGauge({ confidence, label, width = 60 }) {
  const pct   = Math.max(0, Math.min(1, confidence ?? 0));
  const color = pct >= 0.7 ? '#22c55e' : pct >= 0.45 ? '#eab308' : '#ef4444';
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <div style={{ width, height: 5, background: '#27272a', borderRadius: 3, overflow: 'hidden' }}>
        <div style={{ width: `${pct * 100}%`, height: '100%', background: color, borderRadius: 3 }} />
      </div>
      <span style={_mono({ fontSize: 10, color })}>{label || `${(pct * 100).toFixed(0)}%`}</span>
    </div>
  );
}

// ── TraceTimeline ─────────────────────────────────────────────────────────────
function TraceTimeline({ trace }) {
  if (!trace?.stages?.length) {
    return <div style={_mono({ color: '#71717a', fontSize: 12 })}>No stages found</div>;
  }
  const sorted = [...trace.stages].sort((a, b) =>
    STAGE_ORDER.indexOf(a.stage.split(':')[0]) - STAGE_ORDER.indexOf(b.stage.split(':')[0])
  );

  const brain = trace.brain || {};
  const rc    = trace.regime_context || {};
  const cands = trace.candidates || [];

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 0 }}>

      {/* ── Gate chain ── */}
      {sorted.map((st, i) => {
        const blocked = st.outcome === 'blocked' || st.outcome === 'vetoed' || st.outcome === 'rejected';
        const skipped = st.outcome === 'SKIP';
        const isExec  = st.stage === 'execution' || st.stage.startsWith('execution');
        const color   = blocked || skipped ? '#ef4444' : isExec ? '#06b6d4' : '#22c55e';
        const p       = st.payload || {};
        const label   = STAGE_LABELS[st.stage.split(':')[0]] || st.stage;
        const gateId  = st.gate_id || st.plugin_id || '';
        const showGate = gateId && gateId !== label.toLowerCase() && gateId !== st.stage;

        return (
          <div key={st.stage + i} style={{ display: 'flex', gap: 12, paddingBottom: 14, position: 'relative' }}>
            {i < sorted.length - 1 && (
              <div style={{ position: 'absolute', left: 7, top: 18, width: 1,
                height: 'calc(100% - 6px)', background: '#3f3f46' }} />
            )}
            <div style={{ width: 14, height: 14, borderRadius: '50%', flexShrink: 0, marginTop: 2,
              background: color + '33', border: `2px solid ${color}`, zIndex: 1 }} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 3, flexWrap: 'wrap' }}>
                <span style={_mono({ fontSize: 11, fontWeight: 600, color: '#e4e4e7' })}>{label}</span>
                <span style={_mono({ fontSize: 10, color })}>{st.outcome || '—'}</span>
                {st.confidence != null && <BidGauge confidence={st.confidence} />}
                {showGate && <span style={_mono({ fontSize: 9.5, color: '#52525b' })}>{gateId}</span>}
                {st.plugin_version && (
                  <span style={_mono({ fontSize: 9, color: '#52525b' })}>v{st.plugin_version}</span>
                )}
              </div>

              {/* Gate message / reason */}
              {p.message && (
                <div style={_mono({ fontSize: 10, color: '#a1a1aa', marginBottom: 3 })}>{p.message}</div>
              )}
              {p.reason_code && (
                <div style={_mono({ fontSize: 10, color: '#ef4444', marginBottom: 2 })}>✗ {p.reason_code}</div>
              )}

              {/* Gate metrics as key:value chips */}
              {(() => {
                const skip = new Set(['gate_id','gate_group','message','reason_code','skip_reason',
                                      'evidence','reason','reason_codes','approved','vetoed']);
                const kvs = Object.entries(p).filter(([k,v]) =>
                  !skip.has(k) && v != null && typeof v !== 'object' && v !== ''
                );
                return kvs.length > 0 ? (
                  <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 2 }}>
                    {kvs.map(([k,v]) => (
                      <span key={k} style={_mono({ fontSize: 9, color: '#52525b' })}>
                        {k}:{typeof v === 'number' ? v.toFixed(4) : String(v)}
                      </span>
                    ))}
                  </div>
                ) : null;
              })()}

              {/* Direction consensus: per-source vote breakdown */}
              {st.gate_id === 'direction_consensus' && (() => {
                const srcEntries = Object.entries(p).filter(([k]) =>
                  k.startsWith('rule:') || k.startsWith('shadow:') || k.startsWith('momentum:') || k.startsWith('direction_ml:')
                );
                if (!srcEntries.length) return null;
                const ceEntries = srcEntries.filter(([k]) => k.endsWith(':CE'));
                const peEntries = srcEntries.filter(([k]) => k.endsWith(':PE'));
                const renderGroup = (entries, dir, color) => entries.length ? (
                  <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
                    <span style={_mono({ fontSize: 9, color, fontWeight: 600 })}>{dir}</span>
                    {entries.map(([k, v]) => {
                      const src = k.replace(`:${dir}`, '').replace('rule:', '').replace('direction_ml', 'ML');
                      return (
                        <span key={k} style={_mono({ fontSize: 9, color: '#52525b' })}>
                          {src}:{typeof v === 'number' ? v.toFixed(2) : v}
                        </span>
                      );
                    })}
                  </div>
                ) : null;
                return (
                  <div style={{ marginTop: 4, display: 'flex', flexDirection: 'column', gap: 3 }}>
                    {renderGroup(ceEntries, 'CE', '#22c55e')}
                    {renderGroup(peEntries, 'PE', '#f97316')}
                    {p.shadow_basis && (
                      <span style={_mono({ fontSize: 9, color: '#52525b' })}>shadow:{p.shadow_basis}</span>
                    )}
                  </div>
                );
              })()}

              {/* Regime evidence */}
              {(st.stage === 'regime' || st.gate_id === 'regime_classification') && p.evidence && (
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 3 }}>
                  {['r5m','r15m','r30m','vol_ratio','pcr'].filter(k => p.evidence[k] != null).map(k => (
                    <span key={k} style={_mono({ fontSize: 9, color: '#52525b' })}>
                      {k}:{p.evidence[k].toFixed(4)}
                    </span>
                  ))}
                </div>
              )}

              {/* Entry: reason codes */}
              {Array.isArray(p.reason_codes) && p.reason_codes.length > 0 && (
                <div style={{ marginTop: 4 }}>
                  {p.reason_codes.map((rc, j) => (
                    <div key={j} style={_mono({ fontSize: 10, color: '#ef4444', marginBottom: 2 })}>✗ {rc}</div>
                  ))}
                </div>
              )}

              {/* Depth gauges */}
              {p.ce_bid_strength != null || p.pe_bid_strength != null ? (
                <div style={{ display: 'flex', gap: 12, marginTop: 4, flexWrap: 'wrap' }}>
                  {p.ce_bid_strength != null && (
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <span style={_mono({ fontSize: 10, color: '#71717a' })}>CE</span>
                      <BidGauge confidence={p.ce_bid_strength} width={50} />
                    </div>
                  )}
                  {p.pe_bid_strength != null && (
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <span style={_mono({ fontSize: 10, color: '#71717a' })}>PE</span>
                      <BidGauge confidence={p.pe_bid_strength} width={50} />
                    </div>
                  )}
                  {p.skip_reason && <span style={_mono({ fontSize: 10, color: '#ef4444' })}>✗ {p.skip_reason}</span>}
                </div>
              ) : null}

              {/* Risk / direction text reasons */}
              {p.skip_reason && !p.ce_bid_strength && (
                <div style={_mono({ fontSize: 10, color: '#ef4444', marginTop: 3 })}>✗ {p.skip_reason}</div>
              )}
              {p.rejection_reason && (
                <div style={_mono({ fontSize: 10, color: '#ef4444', marginTop: 3 })}>✗ {p.rejection_reason}</div>
              )}
            </div>
          </div>
        );
      })}

      {/* ── Candidate panel ── */}
      {cands.length > 0 && (
        <div style={{ borderTop: '1px solid #27272a', marginTop: 8, paddingTop: 10 }}>
          <div style={_mono({ fontSize: 10, color: '#52525b', marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.06em' })}>
            Candidates ({cands.length})
          </div>
          {cands.map((c, i) => {
            const conf    = c.confidence;
            const selColor = c.selected ? '#4ade80' : '#52525b';
            const termColor = c.terminal_status === 'selected' ? '#4ade80'
                            : c.terminal_status === 'blocked' ? '#ef4444' : '#71717a';
            return (
              <div key={i} style={{ display: 'flex', gap: 10, alignItems: 'flex-start',
                marginBottom: 8, padding: '6px 0', borderBottom: '1px solid #18181b' }}>
                <div style={{ flex: 1 }}>
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                    <span style={_mono({ fontSize: 10, fontWeight: 600, color: '#e4e4e7' })}>
                      {c.strategy_name || '—'}
                    </span>
                    {c.direction && (
                      <span style={_mono({ fontSize: 10, color: c.direction === 'CE' ? '#22c55e' : '#f97316',
                        background: (c.direction === 'CE' ? '#22c55e' : '#f97316') + '22',
                        border: `1px solid ${c.direction === 'CE' ? '#22c55e' : '#f97316'}44`,
                        borderRadius: 3, padding: '1px 6px' })}>
                        {c.direction}
                      </span>
                    )}
                    {conf != null && <BidGauge confidence={conf} />}
                    <span style={_mono({ fontSize: 9, color: termColor })}>
                      {c.terminal_status || (c.selected ? 'selected' : 'blocked')}
                    </span>
                    {c.terminal_gate_id && (
                      <span style={_mono({ fontSize: 9, color: '#52525b' })}>@ {c.terminal_gate_id}</span>
                    )}
                  </div>
                  {c.metrics && Object.keys(c.metrics).length > 0 && (
                    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 3 }}>
                      {Object.entries(c.metrics).map(([k,v]) => (
                        <span key={k} style={_mono({ fontSize: 9, color: '#52525b' })}>
                          {k}:{typeof v === 'number' ? v.toFixed(3) : String(v)}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* ── Brain panel ── */}
      {(brain.day_score || rc.regime) && (
        <div style={{ borderTop: '1px solid #27272a', marginTop: 4, paddingTop: 10,
          display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
          {brain.day_score && (
            <div>
              <div style={_mono({ fontSize: 9, color: '#52525b', textTransform: 'uppercase', marginBottom: 3 })}>Brain</div>
              <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
                <span style={_mono({ fontSize: 10, color: brain.day_score === 'UNKNOWN' ? '#f97316'
                  : brain.day_score === 'GOOD' ? '#22c55e' : '#ef4444' })}>
                  {brain.day_score}
                </span>
                {brain.size_multiplier != null && brain.size_multiplier !== 1 && (
                  <span style={_mono({ fontSize: 9, color: '#71717a' })}>×{brain.size_multiplier}</span>
                )}
                {brain.carry_consecutive_losses > 0 && (
                  <span style={_mono({ fontSize: 9, color: '#ef4444' })}>losses:{brain.carry_consecutive_losses}</span>
                )}
                {brain.day_score_reason && (
                  <span style={_mono({ fontSize: 9, color: '#52525b' })}>{brain.day_score_reason}</span>
                )}
              </div>
            </div>
          )}
          {rc.regime && (
            <div>
              <div style={_mono({ fontSize: 9, color: '#52525b', textTransform: 'uppercase', marginBottom: 3 })}>Regime</div>
              <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                <RegimeBadge regime={rc.regime} confidence={rc.confidence} />
              </div>
              {rc.reason && (
                <div style={_mono({ fontSize: 9, color: '#71717a', marginTop: 2 })}>{rc.reason}</div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── DepthWidget (full-size, for Depth tab) ────────────────────────────────────
function DepthWidget({ runId }) {
  const [data, setData] = useState(null);
  const [age,  setAge]  = useState(null);

  useEffect(() => {
    let alive = true;
    const load = () => {
      const q = runId ? `?run_id=${encodeURIComponent(runId)}` : '';
      fetch(`/api/depth/current${q}`)
        .then(r => r.ok ? r.json() : null)
        .then(d => { if (!alive) return; setData(d); setAge(Date.now()); })
        .catch(() => {});
    };
    load();
    const id = setInterval(load, 5000);
    return () => { alive = false; clearInterval(id); };
  }, [runId]);

  if (!data) {
    return <div style={_mono({ color: '#52525b', fontSize: 12, padding: 16 })}>Loading depth…</div>;
  }
  if (!data.depth_available && !data.ce_bid_strength) {
    return (
      <div style={{ padding: 24, textAlign: 'center' }}>
        <div style={_mono({ color: '#ef4444', fontSize: 13, marginBottom: 6 })}>Depth feed offline</div>
        {data.error && <div style={_mono({ color: '#52525b', fontSize: 11 })}>{data.error}</div>}
      </div>
    );
  }

  const ce = data.ce_bid_strength ?? 0;
  const pe = data.pe_bid_strength ?? 0;
  const spread = data.spread_pct;
  const ageMs = age ? (Date.now() - age) / 1000 : null;

  return (
    <div style={{ padding: 16 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 16 }}>
        <span style={_mono({ fontSize: 13, fontWeight: 600, color: '#e4e4e7' })}>Depth Monitor</span>
        <span style={_mono({ fontSize: 10, color: data.depth_available ? '#22c55e' : '#ef4444' })}>
          {data.depth_available ? '● live' : '○ offline'}
        </span>
        {data.timestamp && (
          <span style={_mono({ fontSize: 10, color: '#52525b', marginLeft: 'auto' })}>
            {String(data.timestamp).slice(11, 19)}
          </span>
        )}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginBottom: 16 }}>
        {[['CE', ce, '#06b6d4'], ['PE', pe, '#a855f7']].map(([label, val, color]) => (
          <div key={label} style={{ background: '#18181b', borderRadius: 6, padding: 12 }}>
            <div style={_mono({ fontSize: 11, color: '#71717a', marginBottom: 6 })}>{label} Bid Strength</div>
            <div style={_mono({ fontSize: 22, fontWeight: 700, color, marginBottom: 8 })}>
              {(val * 100).toFixed(1)}%
            </div>
            <BidGauge confidence={val} width={120} />
          </div>
        ))}
      </div>

      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
        {spread != null && (
          <div style={{ background: '#18181b', borderRadius: 6, padding: '8px 12px' }}>
            <div style={_mono({ fontSize: 10, color: '#71717a' })}>Spread %</div>
            <div style={_mono({ fontSize: 14, fontWeight: 600, color: '#e4e4e7', marginTop: 2 })}>
              {(spread * 100).toFixed(2)}%
            </div>
          </div>
        )}
        <div style={{ background: '#18181b', borderRadius: 6, padding: '8px 12px' }}>
          <div style={_mono({ fontSize: 10, color: '#71717a' })}>Direction</div>
          <div style={_mono({ fontSize: 14, fontWeight: 600, color: '#e4e4e7', marginTop: 2 })}>
            {data.direction || '—'}
          </div>
        </div>
        <div style={{ background: '#18181b', borderRadius: 6, padding: '8px 12px' }}>
          <div style={_mono({ fontSize: 10, color: '#71717a' })}>Aligned</div>
          <div style={_mono({ fontSize: 14, fontWeight: 600,
            color: data.depth_aligned ? '#22c55e' : '#ef4444', marginTop: 2 })}>
            {data.depth_aligned ? 'YES' : 'NO'}
          </div>
        </div>
        {data.confidence != null && (
          <div style={{ background: '#18181b', borderRadius: 6, padding: '8px 12px' }}>
            <div style={_mono({ fontSize: 10, color: '#71717a' })}>Adj. Confidence</div>
            <div style={{ marginTop: 4 }}>
              <BidGauge confidence={data.confidence} />
            </div>
          </div>
        )}
      </div>
      {data.skip_reason && (
        <div style={_mono({ marginTop: 12, fontSize: 11, color: '#ef4444' })}>
          Skipped: {data.skip_reason}
        </div>
      )}
    </div>
  );
}

// ── DepthMiniWidget (for Live Terminal sidebar) ───────────────────────────────
function DepthMiniWidget({ runId }) {
  const [data, setData] = useState(null);
  useEffect(() => {
    let alive = true;
    const load = () => {
      const q = runId ? `?run_id=${encodeURIComponent(runId)}` : '';
      fetch(`/api/depth/current${q}`)
        .then(r => r.ok ? r.json() : null)
        .then(d => { if (alive) setData(d); })
        .catch(() => {});
    };
    load();
    const id = setInterval(load, 10000);
    return () => { alive = false; clearInterval(id); };
  }, [runId]);

  if (!data) return null;

  const ce = data.ce_bid_strength;
  const pe = data.pe_bid_strength;
  const offline = !data.depth_available && ce == null;

  return (
    <div style={{ flexShrink: 0 }}>
      <div style={{ borderTop: '1px solid var(--line-2)', padding: '6px 12px 0',
        fontFamily: 'var(--f-mono)', fontSize: 9.5, color: 'var(--fg-4)',
        textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 4 }}>
        Depth
      </div>
      <div style={{ padding: '4px 12px 8px' }}>
        {offline ? (
          <span style={{ fontFamily: 'var(--f-mono)', fontSize: 10, color: 'var(--neg)' }}>Feed offline</span>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
            {[['CE', ce, '#06b6d4'], ['PE', pe, '#a855f7']].map(([lbl, val, color]) => (
              <div key={lbl} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <span style={{ fontFamily: 'var(--f-mono)', fontSize: 9.5, color: 'var(--fg-4)', width: 14 }}>{lbl}</span>
                <div style={{ width: 50, height: 4, background: '#27272a', borderRadius: 2, overflow: 'hidden' }}>
                  <div style={{ width: `${(val ?? 0) * 100}%`, height: '100%', background: color }} />
                </div>
                <span style={{ fontFamily: 'var(--f-mono)', fontSize: 10, color }}>
                  {val != null ? `${(val * 100).toFixed(0)}%` : '—'}
                </span>
              </div>
            ))}
            <div style={{ fontFamily: 'var(--f-mono)', fontSize: 9.5, color: data.depth_aligned ? 'var(--neg)' : 'var(--pos)', marginTop: 1 }}>
              {(data.depth_dominant || (data.depth_aligned ? 'PE' : 'CE'))} dominant
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── StaleBanner (for Live Terminal) ──────────────────────────────────────────
function StaleBanner() {
  const [stale, setStale] = useState([]);
  useEffect(() => {
    let alive = true;
    const load = () => {
      fetch('/api/streams/health')
        .then(r => r.ok ? r.json() : null)
        .then(d => {
          if (!alive || !d) return;
          setStale((d.streams || []).filter(s => s.status === 'stale').map(s => s.stream));
        })
        .catch(() => {});
    };
    load();
    const id = setInterval(load, 30000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  if (!stale.length) return null;
  return (
    <div style={{
      background: '#7c2d12', color: '#fed7aa', borderBottom: '1px solid #9a3412',
      padding: '6px 16px', fontSize: 11, fontFamily: 'var(--f-mono)',
      display: 'flex', alignItems: 'center', gap: 8,
    }}>
      <span style={{ fontWeight: 700 }}>⚠ Stream STALE:</span>
      <span>{stale.join(' · ')}</span>
      <span style={{ opacity: 0.7, marginLeft: 4 }}>— consumer may be down</span>
    </div>
  );
}

// ── RegimeTimeline ────────────────────────────────────────────────────────────
function RegimeTimeline({ runId }) {
  const [data, setData]   = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    const q = runId ? `?run_id=${encodeURIComponent(runId)}&limit=300` : '?limit=300';
    fetch(`/api/regime/timeline${q}`)
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      .then(d => { if (alive) { setData(d); setError(null); } })
      .catch(e => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, [runId]);

  if (error) return <div style={_mono({ color: '#ef4444', fontSize: 12, padding: 16 })}>{error}</div>;
  if (!data)  return <div style={_mono({ color: '#52525b', fontSize: 12, padding: 16 })}>Loading…</div>;

  const regimes = data.regimes || [];
  if (!regimes.length) {
    return <div style={_mono({ color: '#52525b', fontSize: 12, padding: 16 })}>No regime events yet.</div>;
  }

  return (
    <div style={{ padding: 16 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
        <span style={_mono({ fontSize: 13, fontWeight: 600, color: '#e4e4e7' })}>Regime Timeline</span>
        <span style={_mono({ fontSize: 10, color: '#52525b' })}>{regimes.length} events</span>
      </div>

      {/* Swimlane bar */}
      <div style={{ display: 'flex', height: 20, borderRadius: 4, overflow: 'hidden', marginBottom: 16, gap: 1 }}>
        {regimes.map((r, i) => {
          const opacity = r.confidence != null ? Math.max(0.35, r.confidence) : 0.7;
          return (
            <div key={i} title={`${r.regime} (${r.timestamp?.slice(11,19)})`}
              style={{ flex: 1, background: r.color || '#71717a', opacity, minWidth: 2 }} />
          );
        })}
      </div>

      {/* Event list */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 0, maxHeight: 400, overflowY: 'auto' }}>
        {regimes.map((r, i) => (
          <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '5px 0',
            borderBottom: '1px solid #27272a' }}>
            <span style={_mono({ fontSize: 10, color: '#52525b', minWidth: 60 })}>
              {String(r.timestamp || '').slice(11, 19)}
            </span>
            <RegimeBadge regime={r.regime} confidence={r.confidence} />
            {r.evidence && Object.keys(r.evidence).length > 0 && (
              <span style={_mono({ fontSize: 9, color: '#52525b' })}>
                {Object.entries(r.evidence).slice(0, 3).map(([k,v]) => `${k}:${v}`).join(' ')}
              </span>
            )}
            {r.trace_id && (
              <span style={_mono({ fontSize: 9, color: '#3f3f46', marginLeft: 'auto' })}>
                {r.trace_id.slice(-8)}
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// ── StreamHealthTable ─────────────────────────────────────────────────────────
function StreamHealthTable() {
  const [data,    setData]    = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    const load = () => {
      fetch('/api/streams/health')
        .then(r => r.ok ? r.json() : null)
        .then(d => { if (alive) { setData(d); setLoading(false); } })
        .catch(() => { if (alive) setLoading(false); });
    };
    load();
    const id = setInterval(load, 5000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  if (!data) return (
    <div style={_mono({ color: '#52525b', fontSize: 12, padding: 16 })}>
      {loading ? 'Loading…' : 'No stream data'}
    </div>
  );

  const streams = data.streams || [];
  const note = data.note || data.error;

  return (
    <div style={{ padding: 16 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
        <span style={_mono({ fontSize: 13, fontWeight: 600, color: '#e4e4e7' })}>Stream Health</span>
        {note && <span style={_mono({ fontSize: 10, color: '#71717a' })}>{note}</span>}
      </div>

      {streams.length === 0 && (
        <div style={_mono({ color: '#52525b', fontSize: 12 })}>
          No streams found. Set SIM_RUN_ID env var to monitor sim streams.
        </div>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 0 }}>
        {streams.map(s => {
          const color = s.status === 'ok' ? '#22c55e' : s.status === 'warn' ? '#eab308' : s.status === 'stale' ? '#f97316' : '#ef4444';
          return (
            <div key={s.stream} style={{ display: 'grid', gridTemplateColumns: '160px 60px 80px 80px 1fr',
              gap: 10, alignItems: 'center', padding: '8px 0', borderBottom: '1px solid #27272a' }}>
              <span style={_mono({ fontSize: 11, color: '#e4e4e7' })}>{s.stream}</span>
              <span style={_chip(color, { fontSize: 10 })}>{s.status}</span>
              <span style={_mono({ fontSize: 10, color: '#71717a' })}>
                {s.lag != null ? `lag:${s.lag}` : '—'}
              </span>
              <span style={_mono({ fontSize: 10, color: s.last_event_age_sec > 30 ? '#f97316' : '#71717a' })}>
                {s.last_event_age_sec != null ? `${s.last_event_age_sec}s ago` : '—'}
              </span>
              {s.error && <span style={_mono({ fontSize: 9, color: '#ef4444' })}>{s.error}</span>}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── PluginRegistry ────────────────────────────────────────────────────────────
function PluginRegistry({ runId }) {
  const [data,  setData]  = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    const q = runId ? `?run_id=${encodeURIComponent(runId)}` : '';
    fetch(`/api/plugins/registry${q}`)
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      .then(d => { if (alive) { setData(d); setError(null); } })
      .catch(e => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, [runId]);

  if (error) return <div style={_mono({ color: '#ef4444', fontSize: 12, padding: 16 })}>{error}</div>;
  if (!data)  return <div style={_mono({ color: '#52525b', fontSize: 12, padding: 16 })}>Loading…</div>;

  const plugins = data.plugins || [];

  return (
    <div style={{ padding: 16 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
        <span style={_mono({ fontSize: 13, fontWeight: 600, color: '#e4e4e7' })}>Plugin Registry</span>
        <span style={_mono({ fontSize: 10, color: '#52525b' })}>{plugins.length} active</span>
      </div>

      {plugins.length === 0 && (
        <div style={_mono({ color: '#52525b', fontSize: 12 })}>No plugin events recorded yet.</div>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 0 }}>
        {plugins.map((p, i) => (
          <div key={i} style={{ display: 'grid', gridTemplateColumns: '90px 1fr 80px 80px',
            gap: 10, alignItems: 'center', padding: '8px 0', borderBottom: '1px solid #27272a' }}>
            <span style={_mono({ fontSize: 10, color: '#71717a' })}>
              {STAGE_LABELS[p.stage] || p.stage}
            </span>
            <div>
              <div style={_mono({ fontSize: 11, color: '#e4e4e7' })}>{p.plugin_id}</div>
              {p.plugin_version && (
                <div style={_mono({ fontSize: 9, color: '#52525b' })}>v{p.plugin_version}</div>
              )}
            </div>
            <span style={_mono({ fontSize: 10, color: '#71717a' })}>{p.parity_mode || '—'}</span>
            <span style={_mono({ fontSize: 9, color: '#52525b' })}>
              {p.last_seen ? String(p.last_seen).slice(11, 19) : '—'}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Trace list row ─────────────────────────────────────────────────────────────
function TraceRow({ trace, selected, onClick }) {
  const regime     = trace.regime || '';
  const regimeConf = trace.stages?.regime?.confidence;
  const signalType = trace.signal_type || trace.stages?.execution?.outcome || '—';
  const isActive   = signalType && signalType !== 'SKIP' && signalType !== '—';

  return (
    <div onClick={onClick} style={{
      display: 'grid', gridTemplateColumns: '1fr auto', gap: 10, alignItems: 'center',
      padding: '8px 12px', cursor: 'pointer', borderBottom: '1px solid #27272a',
      background: selected ? '#18181b' : 'transparent',
      borderLeft: `2px solid ${selected ? '#4ade80' : 'transparent'}`,
    }}>
      <div style={{ minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
          <span style={_mono({ fontSize: 10, color: '#71717a' })}>
            {trace.trace_id?.slice(-8) || '—'}
          </span>
          {regime && <RegimeBadge regime={regime} confidence={regimeConf} />}
          {isActive && (
            <span style={_chip('#4ade80', { fontSize: 10 })}>{signalType}</span>
          )}
        </div>
        <StageFlow stages={trace.stages} />
      </div>
      <div style={{ textAlign: 'right', color: '#52525b', fontSize: 10, fontFamily: 'var(--f-mono)' }}>
        {trace.parity_mode && (
          <div style={{ color: '#71717a', marginBottom: 2 }}>{trace.parity_mode}</div>
        )}
        {String(trace.timestamp || '').slice(11, 19)}
      </div>
    </div>
  );
}

// ── PipelineMonitor ────────────────────────────────────────────────────────────
const TABS = [
  { id: 'monitor',  label: 'Monitor'  },
  { id: 'traces',   label: 'Traces'   },
  { id: 'depth',    label: 'Depth'    },
  { id: 'regime',   label: 'Regime'   },
  { id: 'streams',  label: 'Streams'  },
  { id: 'plugins',  label: 'Plugins'  },
];

function PipelineMonitor() {
  const [tab,        setTab]        = useState('monitor');
  const [traces,     setTraces]     = useState([]);
  const [health,     setHealth]     = useState(null);
  const [selected,   setSelected]   = useState(null);
  const [detail,     setDetail]     = useState(null);
  const [traceInput, setTraceInput] = useState('');
  const [loading,    setLoading]    = useState(false);
  const [error,      setError]      = useState(null);
  const [wsStatus,   setWsStatus]   = useState('connecting');
  const [tradesOnly, setTradesOnly] = useState(false);
  const wsRef   = useRef(null);
  const pollRef = useRef(null);

  // run_id + date — readable from URL but also updatable without a page reload
  const _urlParams = () => {
    try {
      const p = new URLSearchParams(window.location.search);
      return { runId: p.get('run_id') || '', runDate: p.get('date') || '' };
    } catch (_) { return { runId: '', runDate: '' }; }
  };
  const [runId,   setRunId]   = useState(() => _urlParams().runId);
  const [runDate, setRunDate] = useState(() => _urlParams().runDate);

  // WebSocket — prefer WS for live updates, fall back to polling if it fails
  useEffect(() => {
    let alive = true;
    let retryId = null;

    const connect = () => {
      if (!alive) return;
      try {
        const proto = location.protocol === 'https:' ? 'wss' : 'ws';
        const ws = new WebSocket(`${proto}://${location.host}/ws/pipeline`);
        wsRef.current = ws;
        ws.onopen    = () => setWsStatus('connected');
        ws.onclose   = () => {
          setWsStatus('disconnected');
          if (alive) retryId = setTimeout(connect, 5000);
        };
        ws.onerror   = () => setWsStatus('error');
        ws.onmessage = (ev) => {
          try {
            const msg = JSON.parse(ev.data);
            if (msg.type === 'pipeline_update' && Array.isArray(msg.traces)) {
              setTraces(msg.traces);
              setError(null);
            }
          } catch (_) {}
        };
      } catch (_) {
        setWsStatus('error');
        if (alive) retryId = setTimeout(connect, 5000);
      }
    };

    connect();

    // Polling fallback — runs in parallel for initial load and when WS drops
    const poll = () => {
      const qs = runId ? `?limit=50&run_id=${encodeURIComponent(runId)}` : '?limit=50';
      fetch(`/api/pipeline/latest${qs}`)
        .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
        .then(d => { if (alive) { setTraces(d.traces || []); setError(null); } })
        .catch(e => { if (alive) setError(e.message); });
    };
    poll();
    pollRef.current = setInterval(poll, 10000);

    return () => {
      alive = false;
      clearTimeout(retryId);
      clearInterval(pollRef.current);
      if (wsRef.current) wsRef.current.close();
    };
  }, [runId]); // restart poll + WS when run switches

  // Stream health — compact bar, used across tabs
  useEffect(() => {
    let alive = true;
    const load = () => {
      fetch('/api/streams/health').then(r => r.ok ? r.json() : null)
        .then(d => { if (alive) setHealth(d); }).catch(() => {});
    };
    load();
    const id = setInterval(load, 10000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  const loadTrace = useCallback(async (traceId) => {
    if (!traceId) return;
    setLoading(true);
    try {
      const res = await fetch(`/api/pipeline/trace/${encodeURIComponent(traceId)}`);
      setDetail(res.ok ? await res.json() : { error: `HTTP ${res.status}` });
    } catch (e) {
      setDetail({ error: e.message });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { if (selected) loadTrace(selected); }, [selected, loadTrace]);

  const handleTraceSearch = (e) => {
    e.preventDefault();
    if (traceInput.trim()) loadTrace(traceInput.trim());
  };

  const staleStreams = (health?.streams || []).filter(s => s.status === 'stale');

  // ── Tab button helper ───────────────────────────────────────────────────
  const TabBtn = ({ id, label }) => (
    <button onClick={() => setTab(id)} style={{
      background: tab === id ? '#27272a' : 'transparent',
      color: tab === id ? '#e4e4e7' : '#71717a',
      border: `1px solid ${tab === id ? '#3f3f46' : 'transparent'}`,
      borderRadius: 4, padding: '4px 10px', cursor: 'pointer',
      ..._mono({ fontSize: 11 }),
    }}>{label}</button>
  );

  // ── Layout ──────────────────────────────────────────────────────────────
  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%',
      background: '#09090b', color: '#e4e4e7' }}>

      {/* STALE banner */}
      {staleStreams.length > 0 && (
        <div style={{ background: '#7c2d12', color: '#fed7aa',
          borderBottom: '1px solid #9a3412', padding: '5px 16px',
          fontSize: 11, ..._mono(), display: 'flex', gap: 8, alignItems: 'center' }}>
          <span style={{ fontWeight: 700 }}>⚠ STALE:</span>
          <span>{staleStreams.map(s => s.stream).join(' · ')}</span>
        </div>
      )}

      {/* Header */}
      <div style={{ padding: '10px 16px', borderBottom: '1px solid #27272a',
        display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
        <span style={_mono({ fontWeight: 600, fontSize: 13, marginRight: 6 })}>Pipeline</span>
        {TABS.map(t => <TabBtn key={t.id} id={t.id} label={t.label} />)}
        <button
          title="Jump to latest sim run"
          onClick={() => {
            fetch('/api/sim/runs?limit=1')
              .then(r => r.ok ? r.json() : null)
              .then(d => {
                const run = (d?.rows || d?.runs || [])[0];
                if (run?.run_id) {
                  const date = run.source_date || '';
                  setRunId(run.run_id);
                  setRunDate(date);
                  // Update URL without page reload
                  const url = `${window.location.pathname}?mode=replay&kind=sim&run_id=${run.run_id}${date ? `&date=${date}` : ''}`;
                  window.history.pushState({}, '', url);
                }
              })
              .catch(() => {});
          }}
          style={{ ..._mono({ fontSize: 9.5 }), marginLeft: 'auto', padding: '2px 8px',
            background: '#27272a', border: '1px solid #3f3f46', borderRadius: 3,
            color: '#a1a1aa', cursor: 'pointer' }}>
          Latest run
        </button>
        {(runId || runDate) && (
          <span style={_mono({ fontSize: 9.5, color: '#52525b', marginRight: 8 })}
            title={runId}>
            {runDate && <span style={{ color: '#71717a', marginRight: 6 }}>{runDate}</span>}
            {runId && <span>run:{runId.slice(0, 8)}…</span>}
          </span>
        )}
        <span style={_mono({ fontSize: 10, color: wsStatus === 'connected' ? '#22c55e' : '#71717a' })}>
          ● {wsStatus}
        </span>
        {error && <span style={_mono({ fontSize: 10, color: '#ef4444' })}>⚠ {error}</span>}
      </div>

      {/* Stream health compact bar */}
      {health?.streams?.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, padding: '6px 16px',
          borderBottom: '1px solid #18181b' }}>
          {health.streams.map(s => {
            const c = s.status === 'ok' ? '#22c55e' : s.status === 'warn' ? '#eab308' : '#f97316';
            return (
              <span key={s.stream} title={`lag=${s.lag ?? '?'} age=${s.last_event_age_sec ?? '?'}s`}
                style={_chip(c, { fontSize: 9.5, padding: '1px 6px' })}>
                {s.stream}
              </span>
            );
          })}
          <span style={_mono({ fontSize: 9.5, color: '#52525b', alignSelf: 'center' })}>
            {traces.length} traces
          </span>
        </div>
      )}

      {/* ── Monitor tab ──────────────────────────────────────────────── */}
      {tab === 'monitor' && (
        <div style={{ display: 'flex', flex: 1, overflow: 'hidden' }}>
          <div style={{ width: 420, flexShrink: 0, borderRight: '1px solid #27272a', display: 'flex', flexDirection: 'column' }}>
            <div style={{ padding: '6px 12px', borderBottom: '1px solid #27272a', display: 'flex', alignItems: 'center', gap: 8 }}>
              <button onClick={() => setTradesOnly(false)}
                style={{ ..._mono({ fontSize: 10 }), padding: '2px 8px', borderRadius: 3, border: 'none',
                  cursor: 'pointer', background: !tradesOnly ? '#4ade8033' : '#27272a',
                  color: !tradesOnly ? '#4ade80' : '#71717a' }}>
                All
              </button>
              <button onClick={() => setTradesOnly(true)}
                style={{ ..._mono({ fontSize: 10 }), padding: '2px 8px', borderRadius: 3, border: 'none',
                  cursor: 'pointer', background: tradesOnly ? '#4ade8033' : '#27272a',
                  color: tradesOnly ? '#4ade80' : '#71717a' }}>
                Trades only
              </button>
              <span style={_mono({ fontSize: 10, color: '#52525b', marginLeft: 'auto' })}>
                {traces.filter(tr => {
                  const sig = tr.signal_type || tr.stages?.execution?.outcome || '—';
                  return sig && sig !== 'SKIP' && sig !== '—';
                }).length} trades / {traces.length} total
              </span>
            </div>
            <div style={{ overflowY: 'auto', flex: 1 }}>
            {traces.length === 0 ? (
              <div style={{ padding: 24, color: '#52525b', textAlign: 'center', ..._mono({ fontSize: 12 }) }}>
                No pipeline events yet.<br />Start a sim run to see traces.
              </div>
            ) : (
              traces
                .filter(tr => {
                  if (!tradesOnly) return true;
                  const sig = tr.signal_type || tr.stages?.execution?.outcome || '—';
                  return sig && sig !== 'SKIP' && sig !== '—';
                })
                .map(tr => (
                  <TraceRow key={tr.trace_id} trace={tr}
                    selected={selected === tr.trace_id}
                    onClick={() => setSelected(tr.trace_id)} />
                ))
            )}
            </div>
          </div>
          <div style={{ flex: 1, overflowY: 'auto', padding: 16 }}>
            {!selected && (
              <div style={_mono({ color: '#52525b', fontSize: 12 })}>
                ← Select a trace to inspect the full decision chain
              </div>
            )}
            {loading && <div style={_mono({ color: '#71717a', fontSize: 12 })}>Loading…</div>}
            {!loading && detail && !detail.error && (
              <>
                <div style={{ marginBottom: 12 }}>
                  <span style={_mono({ fontSize: 11, color: '#71717a' })}>
                    trace_id: <span style={{ color: '#a1a1aa' }}>{detail.trace_id}</span>
                    <span style={{ marginLeft: 12, color: '#52525b' }}>
                      ({detail.stage_count} stages)
                    </span>
                  </span>
                </div>
                <TraceTimeline trace={detail} />
              </>
            )}
            {!loading && detail?.error && (
              <div style={_mono({ color: '#ef4444', fontSize: 12 })}>{detail.error}</div>
            )}
          </div>
        </div>
      )}

      {/* ── Traces tab ───────────────────────────────────────────────── */}
      {tab === 'traces' && (
        <div style={{ flex: 1, overflowY: 'auto', padding: 16 }}>
          <form onSubmit={handleTraceSearch} style={{ display: 'flex', gap: 8, marginBottom: 16 }}>
            <input value={traceInput} onChange={e => setTraceInput(e.target.value)}
              placeholder="Paste trace_id…"
              style={{ flex: 1, background: '#18181b', border: '1px solid #3f3f46',
                borderRadius: 4, color: '#e4e4e7', padding: '6px 10px', ..._mono({ fontSize: 12 }) }} />
            <button type="submit" style={{ background: '#27272a', border: '1px solid #3f3f46',
              color: '#e4e4e7', borderRadius: 4, padding: '6px 14px', cursor: 'pointer',
              ..._mono({ fontSize: 12 }) }}>Load</button>
          </form>
          {loading && <div style={_mono({ color: '#71717a', fontSize: 12 })}>Loading…</div>}
          {!loading && detail && !detail.error && (
            <>
              <div style={{ marginBottom: 12 }}>
                <span style={_mono({ fontSize: 11, color: '#71717a' })}>
                  trace_id: <span style={{ color: '#a1a1aa' }}>{detail.trace_id}</span>
                  <span style={{ marginLeft: 12, color: '#52525b' }}>({detail.stage_count} stages)</span>
                </span>
              </div>
              <TraceTimeline trace={detail} />
            </>
          )}
          {!loading && detail?.error && (
            <div style={_mono({ color: '#ef4444', fontSize: 12 })}>{detail.error}</div>
          )}
          {!detail && !loading && (
            <div style={_mono({ color: '#52525b', fontSize: 12 })}>
              Enter a trace_id above to explore a full decision chain.
            </div>
          )}
        </div>
      )}

      {/* ── Depth tab ────────────────────────────────────────────────── */}
      {tab === 'depth' && (
        <div style={{ flex: 1, overflowY: 'auto' }}>
          <DepthWidget runId={runId} />
        </div>
      )}

      {/* ── Regime tab ───────────────────────────────────────────────── */}
      {tab === 'regime' && (
        <div style={{ flex: 1, overflowY: 'auto' }}>
          <RegimeTimeline runId={runId} />
        </div>
      )}

      {/* ── Streams tab ──────────────────────────────────────────────── */}
      {tab === 'streams' && (
        <div style={{ flex: 1, overflowY: 'auto' }}>
          <StreamHealthTable />
        </div>
      )}

      {/* ── Plugins tab ──────────────────────────────────────────────── */}
      {tab === 'plugins' && (
        <div style={{ flex: 1, overflowY: 'auto' }}>
          <PluginRegistry runId={runId} />
        </div>
      )}
    </div>
  );
}
