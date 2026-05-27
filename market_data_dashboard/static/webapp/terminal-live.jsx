// terminal-live.jsx — Dark Bloomberg terminal for live + replay modes  v16
/* global React, TradingCore, LWChart */
const { useState: _s, useEffect: _e, useMemo: _m, useRef: _r, useCallback: _cb } = React;
const TC = window.TradingCore;

// ── Data helpers ─────────────────────────────────────────────────────────
function _barLabel(session, idx) {
  const c = session?.candles?.[idx];
  if (!c) return '—';
  if (c.label) return c.label;
  return new Date(c.t).toLocaleTimeString('en-IN',
    { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Kolkata' });
}

// Normalize trade fields — real session may use .entry/.exit instead of .entryPx/.exitPx
function _bridgeTrade(tr) {
  const ep = tr.entryPx ?? tr.entry ?? 0;
  const xp = tr.exitPx  ?? tr.exit  ?? 0;
  const rng = Math.abs(ep) * 0.002;
  const rawDir = tr.dir || tr.direction || 'LONG';
  const optionType = tr.optionType ?? tr.option_type ?? (rawDir === 'PE' || rawDir === 'CE' ? rawDir : null);
  const positionSide = String(tr.positionSide || tr.position_side || '').trim().toUpperCase();
  // Chart bias (LONG/SHORT triangles) follows delta; tape "Dir" shows leg + buy/sell.
  const dirMap = { PE: 'SHORT', CE: 'LONG' };
  const chartBias = dirMap[rawDir] || dirMap[rawDir.toUpperCase()] || rawDir;
  const legDir = optionType || rawDir;
  const isLongPremium = positionSide === 'LONG' || (positionSide !== 'SHORT' && chartBias === 'LONG');
  return {
    ...tr, dir: chartBias, legDir, positionSide,
    entryPx: ep, exitPx: xp,
    strat: tr.strat || tr.strategy_name || '—',
    stopPx:   tr.stopPx   ?? (isLongPremium ? ep - rng : ep + rng),
    targetPx: tr.targetPx ?? (isLongPremium ? ep + rng * 2 : ep - rng * 2),
    heldBars: tr.heldBars ?? ((tr.exitIdx ?? 0) - (tr.entryIdx ?? 0)),
    conf:     tr.conf ?? tr.confidence ?? 0.65,
    regime:   tr.regime ?? '—',
    exitReason:  tr.exitReason ?? 'CLOSED',
    entryDetail: tr.entryDetail ?? `Entry at ${ep.toFixed(2)}`,
    exitDetail:  tr.exitDetail  ?? `Exit at ${xp.toFixed(2)} · ${tr.exitReason ?? 'closed'}`,
    strike: tr.strike ?? null,
    optionType,
  };
}

function _makeStrategies(trades) {
  const map = {};
  trades.forEach(tr => {
    const k = tr.strat || '—';
    if (!map[k]) map[k] = { id: k, name: k, pnl: 0, trades: 0, wins: 0 };
    map[k].pnl   += tr.pnlPct || 0;
    map[k].trades++;
    if ((tr.pnlPct || 0) > 0) map[k].wins++;
  });
  return Object.values(map).map(s => ({
    ...s,
    wr:     s.trades ? Math.round(s.wins / s.trades * 100) : 0,
    weight: 1 / Math.max(1, Object.keys(map).length),
    status: s.pnl < -0.1 ? 'penalty' : 'armed',
  })).sort((a, b) => b.pnl - a.pnl);
}

function _makeQuote(session, upToIdx) {
  if (!session?.candles?.length) return null;
  const cands = session.candles;
  const end   = Math.min(upToIdx, cands.length - 1);
  const cur   = cands[end];
  const first = cands[0];
  return {
    symbol:    session.instrument || 'BANKNIFTY FUT',
    spot:      cur.c,
    spotChg:   cur.c - first.o,
    spotChgPct: ((cur.c - first.o) / first.o) * 100,
    dayHigh:   Math.max(...cands.slice(0, end + 1).map(c => c.h)),
    dayLow:    Math.min(...cands.slice(0, end + 1).map(c => c.l)),
  };
}

// ── TickerBar ────────────────────────────────────────────────────────────
function TickerBar({ quote, instrument }) {
  const [clock, setClock] = _s(TC.fmtClock(new Date()));
  _e(() => { const id = setInterval(() => setClock(TC.fmtClock(new Date())), 1000); return () => clearInterval(id); }, []);
  if (!quote) return <div className="ticker-bar"><div className="t-brand"><span className="t-brand-mark"/><b>QUANT</b><span>OPS</span></div><div className="ticker-clock"><span className="t-live-pulse"/><span>{clock}</span></div></div>;
  const cls = quote.spotChg >= 0 ? 'pos' : 'neg';
  return (
    <div className="ticker-bar">
      <div className="t-brand">
        <span className="t-brand-mark"/>
        <b>QUANT</b><span>OPS</span>
        <span className="ver">live</span>
      </div>
      <div className="ticker-pairs">
        <div className="t-spot-big">
          <span className="lbl" style={{fontSize:'9.5px',color:'var(--fg-3)',letterSpacing:'0.10em',textTransform:'uppercase'}}>{quote.symbol}</span>
          <span className="val">{quote.spot.toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2})}</span>
          <span className={`delta ${cls}`}>{TC.fmtSigned(quote.spotChg,2)} ({TC.fmtSigned(quote.spotChgPct,2,'%')})</span>
        </div>
        <div className="pair"><span className="lbl">H</span><span className="val">{quote.dayHigh.toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2})}</span></div>
        <div className="pair"><span className="lbl">L</span><span className="val">{quote.dayLow.toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2})}</span></div>
      </div>
      <div className="ticker-clock">
        <span className="t-live-pulse"/>
        <span>{clock}</span>
      </div>
    </div>
  );
}

// ── StatusBar ────────────────────────────────────────────────────────────
function StatusBar({ sessionPnl, tradesCount, winRate, regime, engine, ws, onModeSwitch, onHaltClick }) {
  const pnlCls = sessionPnl >= 0 ? 'pos' : 'neg';
  return (
    <div className="t-status-bar">
      <div className="t-mode-toggle">
        <button className="active"><span className="dot live"/>Live</button>
        <button onClick={() => onModeSwitch('replay')}><span className="dot replay"/>Replay</button>
        <button onClick={() => onModeSwitch('eval')}><span className="dot eval"/>Eval</button>
      </div>
      <div className="t-status-cells">
        <div className="t-scell big"><span className="k">Session P&amp;L</span><span className={`v ${pnlCls}`}>{TC.fmtPct(sessionPnl,2)}</span></div>
        <div className="t-scell"><span className="k">Trades</span><span className="v">{tradesCount}<span className="sub">/ {winRate}% WR</span></span></div>
        <div className="t-scell"><span className="k">Regime</span><span className="v" style={{color:'var(--info)',fontSize:'11px'}}>{regime || '—'}</span></div>
        <div className="t-scell"><span className="k">Engine</span><span className="v" style={{fontSize:'11px'}}>{engine || '—'}</span></div>
        <div className="t-scell"><span className="k">WS</span><span className={`v ${ws === 'connected' ? 'pos' : 'warn'}`} style={{fontSize:'11px'}}>{ws}</span></div>
      </div>
      <div className="t-status-actions">
        <button className="t-btn danger" onClick={onHaltClick}>⏻ Halt <span className="kbd">⌘.</span></button>
      </div>
    </div>
  );
}

// ── Brain Status (compact dark-theme widget) ──────────────────────────────
function BrainStatusCompact({ brainData }) {
  if (!brainData || !brainData.available) return null;
  const d = brainData;
  const score = String(d.day_score || 'UNKNOWN').toUpperCase();
  const scoreColor = {
    CALM:     'var(--pos)',
    NEUTRAL:  'var(--fg-3)',
    VOLATILE: 'var(--warn)',
    AVOID:    'var(--neg)',
    UNKNOWN:  'var(--fg-4)',
  }[score] || 'var(--fg-4)';
  const fmtF = v => v != null ? Number(v).toFixed(4) : '—';
  const fmtPct = v => v != null ? (Number(v)>=0?'+':'') + (Number(v)*100).toFixed(2)+'%' : '—';
  const sizeWarn = d.size_multiplier != null && Number(d.size_multiplier) < 1.0;
  const carryWarn = (d.carry_consecutive_losses || 0) >= 2 || (d.losing_streak_days || 0) >= 1;
  return (
    <div style={{flexShrink:0}}>
      <div className="t-section-head">Brain</div>
      <div style={{padding:'6px 12px',display:'grid',gap:4}}>
        <div style={{display:'flex',alignItems:'center',justifyContent:'space-between'}}>
          <span style={{fontFamily:'var(--f-mono)',fontSize:9.5,color:'var(--fg-4)'}}>day_score</span>
          <span style={{fontFamily:'var(--f-mono)',fontSize:11,fontWeight:700,color:scoreColor}}>{score}</span>
        </div>
        <div style={{display:'flex',alignItems:'center',justifyContent:'space-between'}}>
          <span style={{fontFamily:'var(--f-mono)',fontSize:9.5,color:'var(--fg-4)'}}>conf</span>
          <span style={{fontFamily:'var(--f-mono)',fontSize:11,color:'var(--fg-2)'}}>
            {d.day_score_confidence != null ? (Number(d.day_score_confidence)*100).toFixed(0)+'%' : '—'}
          </span>
        </div>
        <div style={{display:'flex',alignItems:'center',justifyContent:'space-between'}}>
          <span style={{fontFamily:'var(--f-mono)',fontSize:9.5,color:'var(--fg-4)'}}>size_mult</span>
          <span style={{fontFamily:'var(--f-mono)',fontSize:11,color:sizeWarn?'var(--warn)':'var(--pos)'}}>
            {d.size_multiplier != null ? Number(d.size_multiplier).toFixed(2)+'×' : '—'}
          </span>
        </div>
        <div style={{display:'flex',alignItems:'center',justifyContent:'space-between'}}>
          <span style={{fontFamily:'var(--f-mono)',fontSize:9.5,color:'var(--fg-4)'}}>carry</span>
          <span style={{fontFamily:'var(--f-mono)',fontSize:11,color:carryWarn?'var(--warn)':'var(--fg-3)'}}>
            {d.carry_consecutive_losses || 0} L · {d.losing_streak_days || 0}d
          </span>
        </div>
        {d.regime_rv20 != null && (
          <div style={{display:'flex',alignItems:'center',justifyContent:'space-between'}}>
            <span style={{fontFamily:'var(--f-mono)',fontSize:9.5,color:'var(--fg-4)'}}>rv20</span>
            <span style={{fontFamily:'var(--f-mono)',fontSize:11,color:Number(d.regime_rv20)>0.018?'var(--neg)':Number(d.regime_rv20)<0.010?'var(--pos)':'var(--warn)'}}>
              {fmtF(d.regime_rv20)}
            </span>
          </div>
        )}
        {d.regime_sma20_slope != null && (
          <div style={{display:'flex',alignItems:'center',justifyContent:'space-between'}}>
            <span style={{fontFamily:'var(--f-mono)',fontSize:9.5,color:'var(--fg-4)'}}>sma_slope</span>
            <span style={{fontFamily:'var(--f-mono)',fontSize:11,color:Number(d.regime_sma20_slope)>=0?'var(--pos)':'var(--neg)'}}>
              {fmtPct(d.regime_sma20_slope)}
            </span>
          </div>
        )}
        {d.day_score_reason && (
          <div style={{marginTop:2,fontFamily:'var(--f-mono)',fontSize:9,color:'var(--fg-4)',
                       wordBreak:'break-all',lineHeight:1.4}}>
            {d.day_score_reason.slice(0, 60)}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Engine Roster (left rail) ────────────────────────────────────────────
function EngineRoster({ strategies, dailyRisk, brainData }) {
  const risk = dailyRisk || 0;
  return (
    <div className="t-rail">
      <BrainStatusCompact brainData={brainData} />
      <div className="t-section-head" style={{borderTop:0}}>Strategy Roster</div>
      <div style={{overflowY:'auto',flexShrink:0}}>
        {strategies.length === 0
          ? <div style={{padding:'10px 12px',color:'var(--fg-4)',fontFamily:'var(--f-mono)',fontSize:10}}>No trades yet</div>
          : strategies.map(s => (
            <div key={s.id} className="t-strat-row">
              <div>
                <div className="t-strat-name">{s.name}</div>
                <div className="t-strat-meta">
                  <span>{s.trades}t</span><span>·</span>
                  <span>{s.wr}%wr</span>
                </div>
                <div className={`t-strat-bar ${s.pnl < 0 ? 'neg' : ''}`}>
                  <span style={{width: Math.min(100, Math.abs(s.pnl) / 0.5 * 100) + '%'}}/>
                </div>
              </div>
              <div style={{textAlign:'right'}}>
                <div className={`t-strat-pnl ${s.pnl >= 0 ? 'pos' : 'neg'}`}>{TC.fmtPct(s.pnl,2)}</div>
                <div className={`t-strat-status ${s.status}`}>{s.status}</div>
              </div>
            </div>
          ))
        }
      </div>
      <div className="t-section-head">Risk</div>
      <div style={{flexShrink:0}}>
        <div className="t-gauge">
          <span className="lbl">Daily risk used</span>
          <span className="val">{(risk*100).toFixed(0)}%</span>
          <div className="track"><span style={{width:Math.min(100,risk*100)+'%'}}/></div>
        </div>
      </div>
      <div style={{flex:1}}/>
      <div style={{padding:'6px 12px',borderTop:'1px solid var(--line-2)',background:'var(--bg-0)',fontFamily:'var(--f-mono)',fontSize:'9.5px',color:'var(--fg-3)'}}>
        <div style={{display:'flex',justifyContent:'space-between'}}>
          <span>OPS · amitsajwan</span>
          <span style={{color:'var(--pos)'}}>● ACTIVE</span>
        </div>
      </div>
    </div>
  );
}

// ── SVG Candlestick Chart ────────────────────────────────────────────────
function TermChart({ session, candles, trades, selectedTrade, onSelectTrade, upToIdx, flashIdx }) {
  const W = 1000, H = 360;
  const PAD = { l: 8, r: 60, t: 8, b: 22 };
  const iW = W - PAD.l - PAD.r, iH = H - PAD.t - PAD.b;
  const visible = candles.slice(0, upToIdx + 1);
  const slot = iW / Math.max(1, visible.length);
  const bodyW = Math.max(2, slot - 1.5);

  const [pMin, pMax] = _m(() => {
    let lo = Infinity, hi = -Infinity;
    visible.forEach(c => { lo = Math.min(lo, c.l); hi = Math.max(hi, c.h); });
    const pad = (hi - lo) * 0.10;
    return [lo - pad, hi + pad];
  }, [visible]);

  const xOf = i => PAD.l + i * slot + slot / 2;
  const yOf = p => PAD.t + (pMax - p) / (pMax - pMin) * iH;

  const vwapPath = _m(() => {
    let cv = 0, cc = 0;
    return visible.map((c, i) => {
      const tp = (c.h + c.l + c.c) / 3;
      cv += tp * (c.v || 1); cc += (c.v || 1);
      return `${i === 0 ? 'M' : 'L'} ${xOf(i).toFixed(1)} ${yOf(cv/cc).toFixed(1)}`;
    }).join(' ');
  }, [visible, pMin, pMax]);

  const ticks = _m(() => {
    const out = [];
    const step = Math.ceil((pMax - pMin) / 6 / 10) * 10 || 10;
    let p = Math.ceil(pMin / step) * step;
    while (p < pMax) { out.push(p); p += step; }
    return out;
  }, [pMin, pMax]);

  const timeTicks = _m(() => {
    const out = [];
    const n = visible.length;
    const step = Math.max(1, Math.floor(n / 6));
    for (let i = 0; i < n; i += step) out.push(i);
    if (n > 0 && out[out.length - 1] !== n - 1) out.push(n - 1);
    return out;
  }, [visible.length]);

  const [hover, setHover] = _s(null);
  const svgRef = _r(null);

  const handleMove = _cb(e => {
    const r = svgRef.current?.getBoundingClientRect();
    if (!r) return;
    const x = (e.clientX - r.left) * (W / r.width);
    const i = Math.floor((x - PAD.l) / slot);
    setHover(i >= 0 && i < visible.length ? i : null);
  }, [visible.length, slot]);

  const curPx = visible[visible.length - 1]?.c || 0;
  const curY  = yOf(curPx);

  return (
    <div className="t-chart-area">
      <svg ref={svgRef} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none"
           onMouseMove={handleMove} onMouseLeave={() => setHover(null)}>
        <g className="t-chart-grid">
          {ticks.map(p => <line key={p} x1={PAD.l} x2={W-PAD.r} y1={yOf(p)} y2={yOf(p)}/>)}
        </g>

        {selectedTrade && (() => {
          const t = selectedTrade;
          const eiN = Math.min(t.entryIdx, visible.length - 1);
          const exN = Math.min(t.exitIdx,  visible.length - 1);
          if (eiN < 0) return null;
          return (
            <g>
              <rect x={xOf(eiN)-bodyW/2} y={Math.min(yOf(t.stopPx),yOf(t.targetPx))}
                width={xOf(exN)-xOf(eiN)+bodyW}
                height={Math.abs(yOf(t.stopPx)-yOf(t.targetPx))} className="t-chart-trade-band"/>
              <line x1={PAD.l} x2={W-PAD.r} y1={yOf(t.stopPx)}   y2={yOf(t.stopPx)}   className="t-chart-stop-line"/>
              <line x1={PAD.l} x2={W-PAD.r} y1={yOf(t.targetPx)} y2={yOf(t.targetPx)} className="t-chart-target-line"/>
              <text x={W-PAD.r+3} y={yOf(t.stopPx)+3} fill="var(--neg)" fontFamily="var(--f-mono)" fontSize="9">STP {t.stopPx.toFixed(0)}</text>
              <text x={W-PAD.r+3} y={yOf(t.targetPx)+3} fill="var(--pos)" fontFamily="var(--f-mono)" fontSize="9">TGT {t.targetPx.toFixed(0)}</text>
            </g>
          );
        })()}

        {visible.map((c, i) => {
          const up = c.c >= c.o;
          const x = xOf(i);
          const bTop = yOf(Math.max(c.o, c.c));
          const bBot = yOf(Math.min(c.o, c.c));
          const flash = i === flashIdx ? ' flash' : '';
          return (
            <g key={i}>
              <line x1={x} x2={x} y1={yOf(c.h)} y2={yOf(c.l)} className={`t-candle-wick ${up?'up':'down'}`} strokeWidth="1"/>
              <rect x={x-bodyW/2} y={bTop} width={bodyW} height={Math.max(1,bBot-bTop)}
                    className={`t-candle-body ${up?'up':'down'}${flash}`} strokeWidth="0.5"/>
            </g>
          );
        })}

        <path d={vwapPath} className="t-chart-vwap"/>

        {trades.filter(t => t.entryIdx < visible.length).map(t => {
          const eiN = Math.min(t.entryIdx, visible.length - 1);
          const exN = Math.min(t.exitIdx ?? t.entryIdx, visible.length - 1);
          const x1 = xOf(eiN);
          const y1 = yOf(candles[eiN]?.c ?? t.entryPx);
          const x2 = xOf(exN);
          const y2 = yOf(candles[exN]?.c ?? candles[eiN]?.c ?? t.exitPx ?? t.entryPx);
          const pnl = Number(t.pnlPct || 0);
          const color = pnl > 0 ? 'var(--pos)' : pnl < 0 ? 'var(--neg)' : 'var(--fg-3)';
          const isSel = selectedTrade?.id === t.id;
          return (
            <g key={"link-"+t.id} opacity={isSel?1:0.7}>
              <line x1={x1} y1={y1} x2={x2} y2={y2}
                    stroke={color} strokeWidth={isSel?2:1} strokeDasharray={isSel?"":"3 3"}/>
              <circle cx={x2} cy={y2} r={isSel?3.5:2.5} fill={color} stroke={color}/>
            </g>
          );
        })}

        {trades.filter(t => t.entryIdx < visible.length).map(t => {
          const isSel = selectedTrade?.id === t.id;
          const cx = xOf(Math.min(t.entryIdx, visible.length - 1));
          const cy = yOf(t.entryPx);
          const color = t.dir === 'LONG' ? 'var(--pos)' : 'var(--neg)';
          const d = t.dir === 'LONG'
            ? `M ${cx} ${cy+13} L ${cx-4} ${cy+21} L ${cx+4} ${cy+21} Z`
            : `M ${cx} ${cy-13} L ${cx-4} ${cy-21} L ${cx+4} ${cy-21} Z`;
          return (
            <path key={t.id} d={d} fill={color} fillOpacity={isSel?1:0.55}
                  stroke={color} strokeWidth="1" className="t-trade-marker"
                  onClick={() => onSelectTrade(t)}/>
          );
        })}

        <g>
          <line x1={PAD.l} x2={W-PAD.r} y1={curY} y2={curY} stroke="var(--accent)" strokeWidth="1" strokeDasharray="1 2" opacity="0.6"/>
          <rect x={W-PAD.r+2} y={curY-9} width={54} height={18} fill="var(--accent)" rx="2"/>
          <text x={W-PAD.r+5} y={curY+4} fill="#1a1100" fontFamily="var(--f-mono)" fontSize="10" fontWeight="700">{curPx.toFixed(0)}</text>
        </g>

        <g>
          {ticks.map(p => <text key={p} x={W-PAD.r+3} y={yOf(p)+3} fontFamily="var(--f-mono)" fontSize="9" fill="var(--fg-3)">{p.toFixed(0)}</text>)}
        </g>
        <g>
          {timeTicks.map(i => (
            <text key={i} x={xOf(i)} y={H-6} fontFamily="var(--f-mono)" fontSize="9" fill="var(--fg-3)" textAnchor="middle">
              {_barLabel(session, i)}
            </text>
          ))}
        </g>

        {hover != null && (() => {
          const c = visible[hover];
          if (!c) return null;
          const x = xOf(hover);
          const tipX = hover > visible.length * 0.7 ? x - 170 : x + 8;
          return (
            <g>
              <line x1={x} x2={x} y1={PAD.t} y2={H-PAD.b} className="t-chart-crosshair"/>
              <rect x={tipX} y={PAD.t+4} width={164} height={80} fill="var(--bg-2)" stroke="var(--line-3)" rx="2"/>
              <text x={tipX+8} y={PAD.t+18} fontFamily="var(--f-mono)" fontSize="9.5" fill="var(--fg-2)">{_barLabel(session,hover)}</text>
              <text x={tipX+8} y={PAD.t+34} fontFamily="var(--f-mono)" fontSize="9.5" fill="var(--fg-3)">O <tspan fill="var(--fg-1)" fontWeight="600">{c.o.toFixed(0)}</tspan></text>
              <text x={tipX+70} y={PAD.t+34} fontFamily="var(--f-mono)" fontSize="9.5" fill="var(--fg-3)">H <tspan fill="var(--pos)" fontWeight="600">{c.h.toFixed(0)}</tspan></text>
              <text x={tipX+8} y={PAD.t+50} fontFamily="var(--f-mono)" fontSize="9.5" fill="var(--fg-3)">L <tspan fill="var(--neg)" fontWeight="600">{c.l.toFixed(0)}</tspan></text>
              <text x={tipX+70} y={PAD.t+50} fontFamily="var(--f-mono)" fontSize="9.5" fill="var(--fg-3)">C <tspan fill={c.c>=c.o?'var(--pos)':'var(--neg)'} fontWeight="600">{c.c.toFixed(0)}</tspan></text>
            </g>
          );
        })()}
      </svg>
    </div>
  );
}

// ── DiagPanel: current model + available models + blocker funnel ─────────
// Operator answer to "why no trades on this date?" — reads runtime_config +
// available_models from /api/strategy/current/state and per-date blocker
// counts from /api/strategy/blocker-funnel.
function _fmtProb(p) {
  if (p == null || isNaN(p)) return '—';
  return (p * 100).toFixed(1) + '%';
}
function _stageDiag(row, stage) {
  const md = row?.model_diagnostics || {};
  return md?.[stage] || {};
}
function _shortHash(h) {
  const s = String(h || '').trim();
  return s ? s.slice(0, 8) : '—';
}
function _outcomeColor(oc) {
  if (oc === 'entry_taken' || oc === 'exit_taken') return 'var(--pos)';
  if (oc === 'blocked') return 'var(--neg)';
  if (oc === 'hold') return 'var(--warn)';
  return 'var(--fg-3)';
}
function OutcomePill({ oc, gate }) {
  const label = oc==='entry_taken' ? 'IN'
              : oc==='hold'        ? 'HOLD'
              : (!gate || gate==='warmup' || gate==='entry_phase') ? 'SKIP'
              : 'BLOCK';
  const styles = {
    IN:    { background:'var(--pos)',             color:'var(--bg-0)', fontWeight:700 },
    HOLD:  { background:'rgba(245,165,36,0.28)',  color:'var(--warn)', fontWeight:600 },
    SKIP:  { background:'rgba(255,255,255,0.08)', color:'var(--fg-4)', fontWeight:400 },
    BLOCK: { background:'rgba(220,50,50,0.22)',   color:'var(--neg)',  fontWeight:600 },
  }[label];
  return (
    <span style={{display:'inline-block',padding:'1px 5px',borderRadius:2,fontSize:8.5,letterSpacing:'0.06em',...styles}}>{label}</span>
  );
}

function DiagPanel({ diag }) {
  const [tlFilter, setTlFilter] = _s('blocked');
  const [tlCollapse, setTlCollapse] = _s(true);
  const [configOpen, setConfigOpen] = _s(false);
  const [selRow, setSelRow] = _s(null);
  if (!diag) {
    return <div style={{padding:'12px',color:'var(--fg-3)',fontFamily:'var(--f-mono)',fontSize:11}}>Diagnostics unavailable in this mode.</div>;
  }
  const { runtimeConfig, availableModels, blockerFunnel, timeline, brainData, loading, error, date, onRefresh, onTimelineFilterChange, onTimelineCollapseChange, activeRunId } = diag;
  const rc = runtimeConfig || {};
  const models = Array.isArray(availableModels) ? availableModels : [];
  const bf = blockerFunnel || {};
  const gates = Array.isArray(bf.primary_blocker_gates) ? bf.primary_blocker_gates : [];
  const reasons = Array.isArray(bf.blocking_reasons) ? bf.blocking_reasons : [];
  const out = bf.outcomes || {};
  const outKeys = Object.keys(out);
  const tl = timeline || {};
  const decisions = Array.isArray(tl.decisions) ? tl.decisions : [];
  const tlTotal = tl.matched_filter ?? tl.total_for_date ?? 0;
  const totalDecisions = outKeys.reduce((s, k) => s + (out[k] || 0), 0);

  function setFilterAndFetch(v) {
    setTlFilter(v);
    setSelRow(null);
    if (onTimelineFilterChange) onTimelineFilterChange(v === 'all' ? '' : v);
  }
  function setCollapseAndFetch(v) {
    setTlCollapse(v);
    setSelRow(null);
    if (onTimelineCollapseChange) onTimelineCollapseChange(v);
  }

  // ── compact brain badge ──────────────────────────────────────────────────
  const brainBadge = (() => {
    if (!brainData?.available) {
      return <div style={{marginBottom:10,padding:'5px 8px',background:'var(--bg-2)',borderRadius:3,border:'1px solid var(--line-3)',color:'var(--fg-4)',fontSize:9.5}}>brain: unavailable</div>;
    }
    const bd = brainData;
    const score = String(bd.day_score || 'UNKNOWN').toUpperCase();
    const scoreColor = { CALM:'var(--pos)', NEUTRAL:'var(--fg-2)', VOLATILE:'var(--warn)', AVOID:'var(--neg)', UNKNOWN:'var(--fg-4)' }[score] || 'var(--fg-4)';
    const conf = bd.day_score_confidence != null ? (Number(bd.day_score_confidence)*100).toFixed(0)+'%' : null;
    const mult = bd.size_multiplier != null ? Number(bd.size_multiplier).toFixed(2)+'×' : null;
    const carry = bd.carry_consecutive_losses || 0;
    const rv20  = bd.regime_rv20 != null ? Number(bd.regime_rv20).toFixed(4) : null;
    const slope = bd.regime_sma20_slope != null ? (Number(bd.regime_sma20_slope)*100).toFixed(2)+'%' : null;
    const sizeWarn  = bd.size_multiplier != null && Number(bd.size_multiplier) < 1.0;
    const carryWarn = carry >= 2 || (bd.losing_streak_days || 0) >= 1;
    return (
      <div style={{marginBottom:10,padding:'5px 8px',background:'var(--bg-2)',borderRadius:3,border:'1px solid var(--line-3)',display:'flex',alignItems:'center',gap:10,flexWrap:'wrap',fontFamily:'var(--f-mono)',fontSize:10}}>
        <span style={{fontWeight:700,fontSize:11,color:scoreColor,letterSpacing:'0.04em'}}>{score}</span>
        {conf  && <span style={{color:'var(--fg-3)'}}>{conf}</span>}
        {mult  && <span style={{color:sizeWarn?'var(--warn)':'var(--fg-2)'}}>{mult}</span>}
        <span style={{color:carryWarn?'var(--warn)':'var(--fg-4)'}}>{carry}L</span>
        {rv20  && <span><span style={{color:'var(--fg-4)'}}>rv20 </span><span style={{color:Number(bd.regime_rv20)>0.018?'var(--neg)':Number(bd.regime_rv20)<0.010?'var(--pos)':'var(--warn)'}}>{rv20}</span></span>}
        {slope && <span><span style={{color:'var(--fg-4)'}}>slope </span><span style={{color:Number(bd.regime_sma20_slope)>=0?'var(--pos)':'var(--neg)'}}>{slope}</span></span>}
        {bd.trade_date && <span style={{marginLeft:'auto',color:'var(--fg-4)',fontSize:9}}>{bd.trade_date}</span>}
      </div>
    );
  })();

  // ── filter button helper (closure over tlFilter / setFilterAndFetch) ──────
  function TlBtn({ v, label }) {
    const active = tlFilter === v;
    return (
      <button onClick={()=>setFilterAndFetch(v)}
              style={{fontSize:9,padding:'2px 7px',borderRadius:2,cursor:'pointer',border:'none',
                      background:active?'var(--accent)':'var(--bg-3)',
                      color:active?'var(--bg-0)':'var(--fg-3)',fontFamily:'var(--f-mono)'}}>
        {label || v}
      </button>
    );
  }

  return (
    <div style={{padding:'10px 12px',fontFamily:'var(--f-mono)',fontSize:10.5,color:'var(--fg-2)',overflowY:'auto',maxHeight:'100%'}}>
      {error && <div style={{color:'var(--neg)',marginBottom:8}}>error: {error}</div>}
      {loading && <div style={{color:'var(--fg-3)',marginBottom:6}}>loading…</div>}

      {/* ── Brain badge (compact 1-line) ── */}
      {brainBadge}

      {/* ── Blocker funnel ── */}
      <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',marginBottom:6}}>
        <span style={{color:'var(--fg-4)',fontSize:9.5,letterSpacing:'0.10em',textTransform:'uppercase'}}>
          Funnel <span style={{textTransform:'none',color:'var(--fg-4)',fontWeight:400}}>{date || '—'}</span>
        </span>
        {onRefresh && <button onClick={onRefresh} style={{fontSize:9,padding:'2px 7px',borderRadius:2,cursor:'pointer',border:'none',background:'var(--bg-3)',color:'var(--fg-3)',fontFamily:'var(--f-mono)'}}>refresh</button>}
      </div>
      {!bf.narrative ? (
        <div style={{color:'var(--fg-4)',marginBottom:14}}>no decision traces loaded — pick a date</div>
      ) : (
        <div style={{marginBottom:14}}>
          {/* outcome chips */}
          <div style={{display:'flex',gap:8,marginBottom:8,flexWrap:'wrap'}}>
            {outKeys.map(k => {
              const chipColor = k==='entry_taken'?'var(--pos)':k==='hold'?'var(--warn)':k==='blocked'?'var(--neg)':'var(--fg-3)';
              return (
                <span key={k} style={{fontSize:9.5}}>
                  <span style={{color:chipColor,fontWeight:600}}>{out[k]}</span>
                  <span style={{color:'var(--fg-4)',marginLeft:3}}>{k.replace('_',' ')}</span>
                </span>
              );
            })}
          </div>
          {/* gate funnel bars */}
          {gates.slice(0, 10).map((g, i) => {
            const pct = totalDecisions ? Math.round(g.count / totalDecisions * 100) : 0;
            return (
              <div key={i} style={{display:'flex',alignItems:'center',gap:5,marginBottom:4}}>
                <span style={{width:86,color:'var(--fg-4)',fontSize:9,textAlign:'right',flexShrink:0,overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>{g.gate}</span>
                <div style={{flex:1,height:4,background:'var(--bg-3)',borderRadius:2}}>
                  <div style={{width:pct+'%',height:'100%',background:'rgba(220,50,50,0.65)',borderRadius:2}}/>
                </div>
                <span style={{width:30,textAlign:'right',color:'var(--fg-2)',fontSize:9,flexShrink:0}}>{g.count}</span>
                <span style={{width:28,textAlign:'right',color:'var(--fg-4)',fontSize:9,flexShrink:0}}>{pct}%</span>
              </div>
            );
          })}
          {/* top reason codes */}
          {reasons.length > 0 && (
            <div style={{marginTop:8,paddingTop:6,borderTop:'1px solid var(--line-3)'}}>
              <div style={{color:'var(--fg-4)',fontSize:9,marginBottom:4,letterSpacing:'0.08em',textTransform:'uppercase'}}>top reasons</div>
              {reasons.slice(0, 5).map((r, i) => (
                <div key={i} style={{display:'flex',justifyContent:'space-between',marginBottom:2}}>
                  <span style={{color:'var(--fg-3)',fontSize:9.5}}>{r.reason_code}</span>
                  <span style={{color:'var(--fg-2)',fontSize:9.5}}>{r.count}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* ── Per-minute decision timeline ── */}
      <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',marginBottom:5}}>
        <span style={{color:'var(--fg-4)',fontSize:9.5,letterSpacing:'0.10em',textTransform:'uppercase'}}>
          Decisions <span style={{textTransform:'none',color:'var(--fg-4)',fontWeight:400}}>({decisions.length}/{tlTotal})</span>
        </span>
        <div style={{display:'flex',gap:3,alignItems:'center'}}>
          <TlBtn v="all" label="all"/>
          <TlBtn v="blocked" label="block"/>
          <TlBtn v="hold" label="hold"/>
          <TlBtn v="entry_taken" label="in"/>
          <button onClick={()=>setCollapseAndFetch(!tlCollapse)}
                  title="merge consecutive rows with identical Stage-1 output"
                  style={{fontSize:9,padding:'2px 7px',borderRadius:2,cursor:'pointer',border:'none',marginLeft:4,
                          background:tlCollapse?'rgba(245,165,36,0.25)':'var(--bg-3)',
                          color:tlCollapse?'var(--warn)':'var(--fg-3)',fontFamily:'var(--f-mono)'}}>
            collapse
          </button>
        </div>
      </div>
      {decisions.length === 0 ? (
        <div style={{color:'var(--fg-4)',marginBottom:14}}>{tl.traces_path_exists === false ? 'no decision_traces.jsonl for this mode' : 'no rows match the current filter'}</div>
      ) : (
        <div style={{maxHeight:300,overflowY:'auto',border:'1px solid var(--line-3)',borderRadius:2,marginBottom:14}}>
          <table style={{width:'100%',borderCollapse:'collapse',fontSize:10}}>
            <thead style={{position:'sticky',top:0,background:'var(--bg-2)'}}>
              <tr style={{color:'var(--fg-4)',fontSize:9}}>
                <th style={{textAlign:'left',padding:'3px 5px',width:52,fontWeight:400}}>time</th>
                <th style={{textAlign:'left',padding:'3px 5px',width:42,fontWeight:400}}>status</th>
                <th style={{textAlign:'left',padding:'3px 5px',fontWeight:400}}>gate / reason</th>
                <th style={{textAlign:'right',padding:'3px 5px',width:60,fontWeight:400}}>shadow</th>
                <th style={{textAlign:'right',padding:'3px 5px',width:40,fontWeight:400}}>ep%</th>
              </tr>
            </thead>
            <tbody>
              {decisions.map((d, i) => {
                const runMins = d.run_minutes || 1;
                const isRun = runMins > 1;
                const oc = d.outcome || '';
                const isSel = selRow === i;
                const rowBg = isSel ? 'rgba(255,176,0,0.07)' : oc==='entry_taken' ? 'rgba(0,200,80,0.07)' : isRun ? 'rgba(245,165,36,0.03)' : 'transparent';
                const mt = d.metrics || {};
                const sd = mt.shadow_dir;
                const sc = mt.shadow_score;
                const shadowStr = sd && sc != null
                  ? sd + (Number(sc) >= 0 ? '+' : '') + Number(sc).toFixed(1)
                  : '—';
                const shadowColor = sd==='CE' ? 'var(--pos)' : sd==='PE' ? 'var(--neg)' : 'var(--fg-4)';
                const firedSignals = new Set(_parseShadowSignals(mt.shadow_basis));
                return (
                  <React.Fragment key={i}>
                    <tr style={{borderTop:'1px solid var(--line-3)',background:rowBg,cursor:'pointer'}}
                        onClick={()=>setSelRow(isSel ? null : i)}>
                      <td style={{padding:'2px 5px',color:'var(--fg-3)',fontFamily:'var(--f-mono)',whiteSpace:'nowrap',fontSize:9.5}}>
                        {isRun
                          ? <span>{d.time}<span style={{color:'var(--fg-4)'}}>–{d.time_end}</span><span style={{color:'var(--accent)',marginLeft:3,fontSize:8.5}}>×{runMins}</span></span>
                          : (d.time || '—')}
                      </td>
                      <td style={{padding:'2px 5px'}}><OutcomePill oc={oc} gate={d.blocker_gate}/></td>
                      <td style={{padding:'2px 5px',maxWidth:130,overflow:'hidden',textOverflow:'ellipsis',whiteSpace:'nowrap'}}>
                        {d.blocker_gate && <span style={{color:'var(--fg-4)'}}>{d.blocker_gate}</span>}
                        {d.reason_code  && <span style={{color:'var(--fg-2)',marginLeft:4}}>{d.reason_code}</span>}
                        {!d.blocker_gate && !d.reason_code && <span style={{color:'var(--fg-4)'}}>—</span>}
                      </td>
                      <td style={{padding:'2px 5px',textAlign:'right',fontFamily:'var(--f-mono)',fontSize:9.5,color:shadowColor,whiteSpace:'nowrap'}}>{shadowStr}</td>
                      <td style={{padding:'2px 5px',textAlign:'right',fontFamily:'var(--f-mono)',fontSize:9.5,color:'var(--fg-3)'}}>{_fmtProb(mt.entry_prob)}</td>
                    </tr>
                    {isSel && (
                      <tr style={{background:'var(--bg-2)'}}>
                        <td colSpan={5} style={{padding:'8px 10px',borderBottom:'1px solid var(--line-3)'}}>
                          <div style={{display:'flex',gap:20,flexWrap:'wrap',fontFamily:'var(--f-mono)',fontSize:9.5}}>
                            {/* probability metrics */}
                            <div>
                              <div style={{color:'var(--fg-4)',fontSize:8.5,textTransform:'uppercase',letterSpacing:'0.08em',marginBottom:5}}>probabilities</div>
                              {[
                                ['entry_prob',    mt.entry_prob,        v=>v!=null&&v>=0.6?'var(--pos)':v!=null&&v>=0.4?'var(--warn)':'var(--neg)'],
                                ['recipe_prob',   mt.recipe_prob,       ()=>'var(--fg-2)'],
                                ['recipe_margin', mt.recipe_margin,     ()=>'var(--fg-2)'],
                                ['dir_up_prob',   mt.direction_up_prob, v=>v!=null&&v>=0.5?'var(--pos)':'var(--neg)'],
                              ].map(([k, v, colorFn]) => v != null && (
                                <div key={k} style={{display:'flex',justifyContent:'space-between',gap:12,marginBottom:2}}>
                                  <span style={{color:'var(--fg-4)'}}>{k}</span>
                                  <span style={{color:colorFn(v),fontWeight:600}}>{_fmtProb(v)}</span>
                                </div>
                              ))}
                              {mt.entry_prob == null && mt.recipe_prob == null && mt.direction_up_prob == null && (
                                <div style={{color:'var(--fg-4)'}}>no probs in trace</div>
                              )}
                            </div>
                            {/* shadow signal grid */}
                            <div>
                              <div style={{color:'var(--fg-4)',fontSize:8.5,textTransform:'uppercase',letterSpacing:'0.08em',marginBottom:5}}>
                                shadow signals
                                {mt.shadow_dir && <span style={{textTransform:'none',marginLeft:6,color:mt.shadow_dir==='CE'?'var(--pos)':'var(--neg)',fontWeight:600}}>{mt.shadow_dir} {shadowStr.replace(mt.shadow_dir,'')}</span>}
                              </div>
                              {_HM_SIGNALS.map(sg => {
                                const ceHit = sg.ce.some(s => firedSignals.has(s));
                                const peHit = sg.pe.some(s => firedSignals.has(s));
                                return (
                                  <div key={sg.label} style={{display:'flex',alignItems:'center',gap:4,marginBottom:2}}>
                                    <span style={{width:50,color:'var(--fg-4)',fontSize:9,textAlign:'right',flexShrink:0}}>{sg.label}</span>
                                    <span style={{
                                      width:24,textAlign:'center',fontSize:8.5,borderRadius:2,padding:'0 2px',fontWeight:ceHit?700:400,
                                      background:ceHit?'rgba(25,195,125,0.22)':'transparent',
                                      color:ceHit?'var(--pos)':'var(--fg-4)',
                                    }}>CE</span>
                                    <span style={{
                                      width:24,textAlign:'center',fontSize:8.5,borderRadius:2,padding:'0 2px',fontWeight:peHit?700:400,
                                      background:peHit?'rgba(220,50,50,0.22)':'transparent',
                                      color:peHit?'var(--neg)':'var(--fg-4)',
                                    }}>PE</span>
                                  </div>
                                );
                              })}
                              {firedSignals.size === 0 && <div style={{color:'var(--fg-4)',fontSize:9}}>no shadow signals (trace predates shadow field)</div>}
                            </div>
                          </div>
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* ── Runtime config + models (collapsed by default) ── */}
      <div style={{borderTop:'1px solid var(--line-3)',paddingTop:8}}>
        <button onClick={()=>setConfigOpen(v=>!v)}
                style={{background:'none',border:'none',cursor:'pointer',color:'var(--fg-4)',fontSize:9.5,fontFamily:'var(--f-mono)',padding:0,letterSpacing:'0.08em',textTransform:'uppercase',display:'flex',alignItems:'center',gap:4}}>
          <span style={{fontSize:9}}>{configOpen?'▾':'▸'}</span> Runtime config &amp; models
        </button>
      </div>
      {configOpen && (() => {
        const arid = diag.activeRunId || '—';
        const isDeterministic = rc.engine === 'deterministic';
        const isOptionPnl = typeof arid === 'string' && arid.startsWith('option_pnl_');
        const producedModel = arid === '—' ? '— (no stored run for this date)'
          : isDeterministic && rc.strategy_profile_id ? `deterministic · ${rc.strategy_profile_id}`
          : isOptionPnl ? 'option_pnl_v1 (bundle)'
          : 'staged_runtime_v1';
        const mismatch = !isDeterministic && arid !== '—' && arid !== (rc.model_run_id || '');
        return (
          <div style={{marginTop:8,lineHeight:1.7}}>
            <div style={{color:'var(--fg-4)',fontSize:9,letterSpacing:'0.08em',textTransform:'uppercase',marginBottom:4}}>Runtime (fires next replay)</div>
            <div><span style={{color:'var(--fg-4)'}}>engine</span> <span style={{color:'var(--accent)'}}>{rc.engine || '—'}</span></div>
            {rc.strategy_profile_id && <div><span style={{color:'var(--fg-4)'}}>profile</span> <span style={{color:'var(--pos)'}}>{rc.strategy_profile_id}</span></div>}
            <div><span style={{color:'var(--fg-4)'}}>model_type</span> <span>{rc.model_type || '—'}</span></div>
            {rc.recipe_id && <div><span style={{color:'var(--fg-4)'}}>recipe</span> <span style={{color:'var(--pos)'}}>{rc.recipe_id}</span> @ thr <span>{rc.decision_threshold}</span></div>}
            <div><span style={{color:'var(--fg-4)'}}>model_run_id</span> <span style={{color:'var(--fg-3)'}}>{rc.model_run_id || '—'}</span></div>
            <div style={{color:'var(--fg-4)',fontSize:9,letterSpacing:'0.08em',textTransform:'uppercase',margin:'10px 0 4px'}}>Trades shown (run_id)</div>
            <div><span style={{color:'var(--fg-4)'}}>run_id</span> <span style={{color:isOptionPnl?'var(--pos)':'var(--warn)'}}>{arid}</span></div>
            <div><span style={{color:'var(--fg-4)'}}>model</span> <span style={{color:isOptionPnl?'var(--pos)':'var(--warn)'}}>{producedModel}</span></div>
            {mismatch && (
              <div style={{marginTop:6,padding:'5px 8px',background:'rgba(245,165,36,0.10)',border:'1px solid rgba(245,165,36,0.35)',borderRadius:2,color:'var(--warn)',fontSize:9.5}}>
                Trades shown != runtime config — trigger a fresh replay to reconcile.
              </div>
            )}
            {!mismatch && arid !== '—' && <div style={{marginTop:4,color:'var(--pos)',fontSize:9.5}}>matched runtime config</div>}
            {models.length > 0 && (
              <>
                <div style={{color:'var(--fg-4)',fontSize:9,letterSpacing:'0.08em',textTransform:'uppercase',margin:'10px 0 4px'}}>Available models ({models.length})</div>
                <div style={{maxHeight:100,overflowY:'auto',border:'1px solid var(--line-3)',borderRadius:2,padding:'3px 5px'}}>
                  {models.slice(0, 20).map((m, i) => {
                    const active = m.run_id && rc.model_run_id && m.run_id === rc.model_run_id;
                    return (
                      <div key={i} style={{padding:'1px 0',color:active?'var(--accent)':'var(--fg-3)',fontSize:9.5}}>
                        {active?'●':'○'} {m.run_id || '?'}{m.model_group && <span style={{color:'var(--fg-4)',marginLeft:6}}>{m.model_group}</span>}
                      </div>
                    );
                  })}
                </div>
              </>
            )}
          </div>
        );
      })()}
    </div>
  );
}

// ── SessionHeatmap ────────────────────────────────────────────────────────
// 375-cell minute strip (9:15–15:29 IST) colored by per-minute engine outcome.
// Each hour is one row; click a cell to see the shadow direction scorecard.
const _HM_HOUR_SLOTS = [
  { h: 9,  mStart: 15, mEnd: 59 },
  { h: 10, mStart: 0,  mEnd: 59 },
  { h: 11, mStart: 0,  mEnd: 59 },
  { h: 12, mStart: 0,  mEnd: 59 },
  { h: 13, mStart: 0,  mEnd: 59 },
  { h: 14, mStart: 0,  mEnd: 59 },
  { h: 15, mStart: 0,  mEnd: 29 },
];
const _HM_SIGNALS = [
  { label:'ORB',      ce:['orh_broken','or_upper_half'], pe:['orl_broken','or_lower_half'] },
  { label:'VWAP',     ce:['above_vwap'],                 pe:['below_vwap'] },
  { label:'ATM Prem', ce:['ce_prem_dominant'],            pe:['pe_prem_dominant'] },
  { label:'PCR',      ce:['pcr_falling'],                 pe:['pcr_rising'] },
  { label:'R15m',     ce:['r15m_up'],                    pe:['r15m_dn'] },
  { label:'R5m',      ce:['r5m_up'],                     pe:['r5m_dn'] },
  { label:'VIX',      ce:['vix_falling'],                pe:['vix_rising'] },
  { label:'IV Skew',  ce:['ce_iv_dom'],                  pe:['pe_iv_dom'] },
  // E5-S1 trap detection signals
  { label:'ORB Trap', ce:['orb_low_rejected'],            pe:['orb_high_rejected'] },
  { label:'VWAP Trap',ce:['vwap_reclaim_bull'],           pe:['vwap_reject_bear'] },
  { label:'IV Fade',  ce:['pe_iv_fading'],                pe:['ce_iv_fading'] },
];
function _hmCellColor(row) {
  if (!row) return 'rgba(255,255,255,0.04)';
  const { oc, gate, rc } = row;
  if (oc === 'entry_taken') return 'var(--pos)';
  if (oc === 'hold')        return 'rgba(245,165,36,0.35)';
  if (oc === 'blocked') {
    if (!gate || gate === 'warmup' || gate === 'entry_phase' || rc === 'invalid_entry_phase')
      return 'rgba(255,255,255,0.08)';
    return 'rgba(220,50,50,0.75)';
  }
  return 'rgba(255,255,255,0.06)';
}
function _parseShadowSignals(sb) {
  if (!sb || sb === 'no_signals') return [];
  const m = sb.match(/\(score=[^:]*:([^)]+)\)/);
  if (m) return m[1].split(',').filter(Boolean);
  const s = sb.match(/[^(]+\(([^)]+)\)/);
  if (s && s[1] !== 'no_signals') return s[1].split(',').filter(Boolean);
  return [];
}
function SessionHeatmap({ data, loading }) {
  const [sel, setSel] = _s(null);
  const [hov, setHov] = _s(null);
  const byTime = _m(() => {
    const map = {};
    if (!data?.rows) return map;
    for (const r of data.rows) { if (r.t) map[r.t] = r; }
    return map;
  }, [data]);

  if (loading) return (
    <div style={{padding:'20px 12px',color:'var(--fg-3)',fontFamily:'var(--f-mono)',fontSize:11}}>
      loading session heatmap…
    </div>
  );
  if (!data) return (
    <div style={{padding:'20px 12px',color:'var(--fg-4)',fontFamily:'var(--f-mono)',fontSize:11}}>
      No heatmap data — select a replay date.
    </div>
  );

  return (
    <div style={{padding:'10px 12px',overflowY:'auto',maxHeight:'100%',fontFamily:'var(--f-mono)',fontSize:10}}>
      {/* minute grid */}
      {_HM_HOUR_SLOTS.map(({h, mStart, mEnd}) => (
        <div key={h} style={{display:'flex',alignItems:'center',gap:1,marginBottom:2}}>
          <span style={{width:22,color:'var(--fg-4)',fontSize:9,flexShrink:0,textAlign:'right',paddingRight:4}}>
            {String(h).padStart(2,'0')}h
          </span>
          <div style={{display:'flex',gap:1}}>
            {Array.from({length: mEnd - mStart + 1}, (_, i) => {
              const m = mStart + i;
              const hhmm = String(h).padStart(2,'0') + ':' + String(m).padStart(2,'0');
              const row  = byTime[hhmm];
              const isSel = sel?.t === hhmm;
              return (
                <div
                  key={hhmm}
                  title={hhmm + (row ? ' · ' + row.oc : '')}
                  onClick={() => setSel(isSel ? null : (row ? row : {t: hhmm}))}
                  onMouseEnter={() => setHov(hhmm)}
                  onMouseLeave={() => setHov(null)}
                  style={{
                    width: 8, height: 16,
                    backgroundColor: _hmCellColor(row),
                    borderRadius: 2,
                    cursor: row ? 'pointer' : 'default',
                    outline: isSel
                      ? '2px solid var(--accent)'
                      : hov === hhmm ? '1px solid rgba(255,255,255,0.25)' : 'none',
                    flexShrink: 0,
                  }}
                />
              );
            })}
          </div>
        </div>
      ))}

      {/* legend */}
      <div style={{display:'flex',gap:14,marginTop:8,fontSize:9,color:'var(--fg-4)'}}>
        {[
          ['var(--pos)',                  'entered'],
          ['rgba(220,50,50,0.75)',        'blocked (logic)'],
          ['rgba(255,255,255,0.08)',      'outside phase'],
          ['rgba(245,165,36,0.35)',       'holding'],
          ['rgba(255,255,255,0.04)',      'no data'],
        ].map(([bg, lbl]) => (
          <span key={lbl}>
            <span style={{display:'inline-block',width:8,height:8,borderRadius:2,background:bg,marginRight:3,verticalAlign:'middle'}}/>
            {lbl}
          </span>
        ))}
      </div>

      {/* detail strip for selected cell */}
      {sel && (
        <div style={{marginTop:10,padding:'8px 10px',background:'var(--bg-2)',borderRadius:3,border:'1px solid var(--line-3)'}}>
          <div style={{display:'flex',alignItems:'baseline',gap:10,marginBottom:6,flexWrap:'wrap'}}>
            <span style={{color:'var(--fg-1)',fontSize:12,fontWeight:700}}>{sel.t}</span>
            <span style={{color: sel.oc==='entry_taken'?'var(--pos)':sel.oc==='blocked'?'var(--neg)':'var(--fg-3)'}}>
              {sel.oc || '—'}
            </span>
            {sel.gate && <span style={{color:'var(--fg-3)',fontSize:9}}>{sel.gate}</span>}
            {sel.rc   && <span style={{color:'var(--fg-4)',fontSize:9}}>{sel.rc}</span>}
            {sel.ep != null && <span style={{color:'var(--fg-3)',fontSize:9}}>ep {(Number(sel.ep)*100).toFixed(1)}%</span>}
          </div>
          {(sel.sc != null || sel.sd) ? (() => {
            const fired = new Set(_parseShadowSignals(sel.sb || ''));
            return (
              <div>
                <div style={{color:'var(--fg-4)',fontSize:9,marginBottom:5}}>
                  shadow · {' '}
                  <span style={{color: (sel.sc||0)>0?'var(--pos)':(sel.sc||0)<0?'var(--neg)':'var(--fg-3)',fontWeight:700}}>
                    {sel.sd || '?'}
                  </span>
                  {' '} score {sel.sc != null ? ((sel.sc>0?'+':'')+Number(sel.sc).toFixed(2)) : '—'}
                </div>
                <div style={{display:'grid',gridTemplateColumns:'repeat(4,1fr)',gap:'3px 10px'}}>
                  {_HM_SIGNALS.map(sg => {
                    const ceFired = sg.ce.find(s => fired.has(s));
                    const peFired = sg.pe.find(s => fired.has(s));
                    const color = ceFired ? 'var(--pos)' : peFired ? 'var(--neg)' : 'var(--fg-4)';
                    const sig   = ceFired || peFired || '·';
                    return (
                      <div key={sg.label} style={{display:'flex',gap:4,alignItems:'center',fontSize:9}}>
                        <span style={{color:'var(--fg-4)',width:52,flexShrink:0}}>{sg.label}</span>
                        <span style={{color,fontSize:9}}>{sig}</span>
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })() : (
            <div style={{color:'var(--fg-4)',fontSize:9}}>No shadow score — trace predates shadow field (run a new replay to populate).</div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Tape: unified trades + signals ────────────────────────────────────────
function Tape({ session, trades, signals, selectedTrade, onSelectTrade, flashId, diag, heatmap }) {
  const [filter, setFilter] = _s('all');
  const rows = _m(() => {
    const out = [];
    if (filter !== 'signals') trades.forEach(t => out.push({ kind: 'trade', sortIdx: t.exitIdx, ...t }));
    if (filter !== 'trades')  signals.forEach(s => out.push({ kind: 'signal', sortIdx: s.idx, ...s }));
    return out.sort((a, b) => b.sortIdx - a.sortIdx);
  }, [trades, signals, filter]);

  return (
    <div className="t-tape-panel">
      <div className="t-panel-head" style={{paddingLeft:0}}>
        <div className="t-tape-tabs">
          <button className={filter==='all'?'active':''} onClick={()=>setFilter('all')}>Tape <span className="count">{trades.length+signals.length}</span></button>
          <button className={filter==='trades'?'active':''} onClick={()=>setFilter('trades')}>Trades <span className="count">{trades.length}</span></button>
          <button className={filter==='signals'?'active':''} onClick={()=>setFilter('signals')}>Signals <span className="count">{signals.length}</span></button>
          {heatmap && <button className={filter==='map'?'active':''} onClick={()=>setFilter('map')}>Map</button>}
          {diag && <button className={filter==='diag'?'active':''} onClick={()=>setFilter('diag')}>Diag</button>}
        </div>
        {(() => {
          const bf = diag?.blockerFunnel;
          if (!bf?.outcomes) return null;
          const o = bf.outcomes;
          const inN      = o.entry_taken || 0;
          const blocked  = o.blocked || 0;
          const held     = o.hold || 0;
          const topGate  = (Array.isArray(bf.primary_blocker_gates) && bf.primary_blocker_gates[0]?.gate) || null;
          return (
            <div style={{marginLeft:'auto',paddingRight:8,display:'flex',gap:8,alignItems:'center',fontFamily:'var(--f-mono)',fontSize:9,color:'var(--fg-4)',whiteSpace:'nowrap'}}>
              {inN > 0 && <span><span style={{color:'var(--pos)',fontWeight:600}}>{inN}</span> in</span>}
              {blocked > 0 && <span><span style={{color:'var(--neg)',fontWeight:600}}>{blocked}</span> blocked</span>}
              {held > 0 && <span><span style={{color:'var(--warn)',fontWeight:600}}>{held}</span> hold</span>}
              {topGate && <span style={{color:'var(--fg-4)'}}>top: {topGate}</span>}
            </div>
          );
        })()}
      </div>
      {filter === 'map' ? (
        <div className="t-panel-body" style={{padding:0}}>
          <SessionHeatmap data={heatmap?.data} loading={heatmap?.loading}/>
        </div>
      ) : filter === 'diag' ? (
        <div className="t-panel-body" style={{padding:0}}><DiagPanel diag={diag}/></div>
      ) : (
      <div className="t-panel-body">
        <table className="t-tape-table">
          <thead>
            <tr>
              <th style={{width:58}}>Time</th>
              <th style={{width:36}}>Type</th>
              <th>Strategy</th>
              <th style={{width:42}}>Dir</th>
              <th style={{width:86}}>Contract</th>
              <th className="r" style={{width:70}}>Entry</th>
              <th className="r" style={{width:70}}>Exit</th>
              <th className="r" style={{width:58}}>P&amp;L</th>
              <th className="r" style={{width:36}}>Hold</th>
              <th style={{width:80}}>Reason</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => {
              if (r.kind === 'trade') {
                const sel = selectedTrade?.id === r.id;
                return (
                  <tr key={'t'+r.id} className={`${sel?'selected':''} ${r.id===flashId?'t-tape-flash':''}`}
                      onClick={() => onSelectTrade(r)}>
                    <td className="muted" style={{whiteSpace:'nowrap',fontFamily:'var(--f-mono)',fontSize:'9px'}}>
                      {_barLabel(session, r.entryIdx)}<span style={{opacity:0.4}}> → </span>{_barLabel(session, r.exitIdx)}
                    </td>
                    <td>
                      <span className={`t-tape-type-dot ${(r.dir||'').toLowerCase()} fired`}/>
                      <span className="muted" style={{fontSize:'9px'}}>FILL</span>
                    </td>
                    <td>{r.strat}</td>
                    <td>
                      <span className={`t-dir ${r.dir}`} title="Chart delta bias">
                        {r.legDir || r.dir}
                        {r.positionSide === 'LONG' ? ' · BUY' : r.positionSide === 'SHORT' ? ' · SELL' : ''}
                      </span>
                    </td>
                    <td style={{fontFamily:'var(--f-mono)',fontSize:'10px',whiteSpace:'nowrap'}}>
                      {r.strike ? (<>
                        <span>{Math.round(r.strike)}</span>
                        {r.optionType && <span className={`t-opt-tag ${r.optionType.toLowerCase()}`} style={{marginLeft:4,padding:'0 4px',borderRadius:2,fontSize:'9px',background:r.optionType==='CE'?'rgba(25,195,125,0.18)':'rgba(242,60,74,0.18)',color:r.optionType==='CE'?'var(--pos)':'var(--neg)'}}>{r.optionType}</span>}
                      </>) : <span className="muted">—</span>}
                    </td>
                    <td className="r">{(r.entryPx||0).toFixed(0)}</td>
                    <td className="r">{(r.exitPx||0).toFixed(0)}</td>
                    <td className={`r ${(r.pnlPct||0)>=0?'t-pos':'t-neg'}`}>{TC.fmtPct(r.pnlPct||0,2)}</td>
                    <td className="r muted">{r.heldBars ?? '—'}b</td>
                    <td className="muted" style={{fontSize:'9px'}}>{(r.exitReason||'').replace('_',' ')}</td>
                  </tr>
                );
              }
              const dir = (r.dir || r.direction || 'LONG').toLowerCase();
              return (
                <tr key={'s'+i} className={r.fired?'':'signal-held'}>
                  <td className="muted">{_barLabel(session, r.idx)}</td>
                  <td>
                    <span className={`t-tape-type-dot ${dir} ${r.fired?'fired':'held'}`}/>
                    <span className="muted" style={{fontSize:'9px',color:r.fired?'var(--fg-2)':'var(--fg-4)'}}>{r.fired?'SIG':'HLD'}</span>
                  </td>
                  <td>{r.strat || r.strategy_name || '—'}</td>
                  <td className="muted" style={{fontSize:'9px'}}>
                    {(r.dir === 'CE' || r.dir === 'PE') ? r.dir : (r.legDir || r.dir || '—')}
                    {r.fired && r.positionSide === 'LONG' ? ' · BUY' : r.fired && r.positionSide === 'SHORT' ? ' · SELL' : ''}
                  </td>
                  <td className="muted">—</td>
                  <td className="r muted">—</td><td className="r muted">—</td><td className="r muted">—</td><td className="r muted">—</td>
                  <td className="muted" style={{fontSize:'9px'}}>{r.reason || r.hold_reason || '—'}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      )}
    </div>
  );
}

// ── Trade Inspector ──────────────────────────────────────────────────────
function TradeInspector({ session, trade }) {
  if (!trade) {
    return (
      <div className="t-inspector">
        <div style={{padding:'14px 12px',color:'var(--fg-3)',fontFamily:'var(--f-mono)',fontSize:11}}>
          Click a trade to inspect.
        </div>
      </div>
    );
  }

  const outCls = trade.exitReason === 'TARGET_HIT' ? 'pos' : trade.exitReason === 'STOP_HIT' ? 'neg' : 'warn';
  // plannedPct is kept as a FRACTION (e.g. 0.05 = +5%) to match trade.pnlPct units;
  // it's rendered via TC.fmtPct which multiplies by 100 at display time.
  const plannedPct = ((trade.targetPx - trade.entryPx) / trade.entryPx) * (trade.dir === 'SHORT' ? -1 : 1);

  const ctx = trade.entryContext || {};
  const traceSummary = ctx.traceSummary || {};
  const selectedVote = ctx.selectedVote || {};
  const selectedVoteMetrics = selectedVote.decision_metrics || {};
  const selectedVoteRaw = selectedVote.raw_signals || {};
  const selectedCandidate = ctx.selectedCandidate || {};

  const _num = (v) => (v != null && Number.isFinite(Number(v)) ? Number(v) : null);

  // Use only stored signal metrics and replay-linked trade context.
  const sm = trade.signal?.metrics || {};
  const entryProb = _num(traceSummary.entry_prob ?? selectedVoteRaw.entry_prob ?? sm.entry_prob);
  const tradeProb = _num(sm.trade_prob);
  const recipeProb = _num(traceSummary.recipe_prob ?? sm.recipe_prob);
  const recipeMargin = _num(traceSummary.recipe_margin ?? sm.recipe_margin);
  const upProb = _num(traceSummary.direction_up_prob ?? sm.up_prob);
  const probs = [
    { v: entryProb, gate: 0.60, label: 'Entry gate', kind: 'gate' },
    { v: tradeProb, gate: 0.55, label: 'Trade gate', kind: 'gate' },
    { v: recipeProb, label: 'Recipe prob', kind: 'neutral' },
    { v: recipeMargin, label: 'Recipe margin', kind: 'neutral' },
    { v: upProb, label: 'Up prob', kind: 'neutral' },
  ].filter(p => p.v != null && Number.isFinite(Number(p.v)));

  // Direction decision — which leg was chosen and why
  const ceProb = _num(selectedVoteRaw.direction_consensus_ce ?? sm.ce_prob);
  const peProb = _num(selectedVoteRaw.direction_consensus_pe ?? sm.pe_prob);
  const consensusMargin = _num(selectedVoteRaw.direction_consensus_margin);
  const chosenLeg = trade.optionType || trade.legDir || (trade.dir === 'SHORT' ? 'PE' : 'CE');
  const dirBasis = trade.signal?.reason || trade.entryReason || '';
  const shadowDir = traceSummary.shadow_dir || null;
  const shadowScore = traceSummary.shadow_score;
  const shadowBasis = traceSummary.shadow_basis || selectedVoteRaw.direction_consensus_shadow_basis || '';
  const firedShadowSignals = new Set(_parseShadowSignals(shadowBasis));
  const directionSource = String(selectedVoteRaw.direction_source || '').trim();
  const policyReason = String(selectedVoteRaw._policy_reason || '').trim();
  const policyChecks = selectedVoteRaw._policy_checks && typeof selectedVoteRaw._policy_checks === 'object'
    ? selectedVoteRaw._policy_checks
    : {};
  const policyCheckRows = Object.entries(policyChecks);
  const selectedStrategy = String(
    selectedVote.strategy
    || ctx.selectedStrategyName
    || selectedCandidate.strategy_name
    || ''
  ).trim();
  // Parse direction decision keywords from reason string
  const dirKeywords = [];
  if (directionSource)             dirKeywords.push(directionSource);
  if (/shadow/i.test(dirBasis))    dirKeywords.push('shadow');
  if (/momentum/i.test(dirBasis))  dirKeywords.push('momentum');
  if (/ml/i.test(dirBasis))        dirKeywords.push('ml');
  if (/vwap/i.test(dirBasis))      dirKeywords.push('vwap');
  if (/orb/i.test(dirBasis))       dirKeywords.push('orb');
  if (/pcr/i.test(dirBasis))       dirKeywords.push('pcr');
  if (/consensus/i.test(dirBasis)) dirKeywords.push('consensus');
  if (/bind/i.test(dirBasis))      dirKeywords.push('bind');
  if (/expir/i.test(dirBasis))     dirKeywords.push('expiry-mode');

  const confluence = trade.confluence || [];
  const counterfactuals = trade.counterfactuals || [
    { label: 'Half size',       delta: -(trade.pnlPct||0)/2, note: 'Linear PnL scaling' },
    { label: 'Skip this trade', delta: -(trade.pnlPct||0),   note: 'Realized PnL forfeited' },
  ];

  // synthetic price path for risk envelope
  const pathPoints = _m(() => {
    const n = 10; let _s2 = 31;
    const rnd = () => { _s2 = (_s2*9301+49297)%233280; return _s2/233280; };
    return Array.from({length: n+1}, (_, i) => {
      const t = i / n;
      const u = t*t*(3-2*t);
      const mid = trade.entryPx + (trade.exitPx - trade.entryPx) * u;
      const dev = (rnd()-0.5) * Math.abs(trade.stopPx - trade.targetPx) * 0.08;
      return { x: t, p: mid + dev };
    });
  }, [trade]);

  return (
    <div className="t-inspector">
      <div className="t-inspector-head">
        <span className={`t-dir ${trade.dir}`} style={{fontSize:'11px',padding:'3px 8px'}}>{trade.dir}</span>
        <div>
          <div className="t-inspector-id">{trade.id} <span style={{color:'var(--fg-3)',fontWeight:400,fontSize:10.5}}>{trade.strat}</span></div>
          <div className="t-inspector-meta">
            <span>{_barLabel(session,trade.entryIdx)} → {_barLabel(session,trade.exitIdx)}</span>
            <span>·</span><span>{trade.heldBars}b</span>
            <span>·</span><span>{trade.regime}</span>
          </div>
        </div>
        <span className={`t-chip ${outCls}`}>● {(trade.exitReason||'').replace('_',' ')}</span>
      </div>
      <div className="t-inspector-body">
        {/* Outcome */}
        <div className="t-outcome-grid">
          <div className="t-outcome-cell big">
            <div className="k">Realized P&amp;L</div>
            <div className={`v ${(trade.pnlPct||0)>=0?'pos':'neg'}`}>{TC.fmtPct(trade.pnlPct||0,2)}</div>
            {plannedPct !== 0 && <div className="sub">vs planned {TC.fmtPct(plannedPct,2)} · realized/planned {(Math.abs(trade.pnlPct||0)/Math.abs(plannedPct)).toFixed(2)}×</div>}
          </div>
          <div className="t-outcome-cell"><div className="k">Entry</div><div className="v">{(trade.entryPx||0).toFixed(2)}</div></div>
          <div className="t-outcome-cell"><div className="k">Exit</div><div className="v">{(trade.exitPx||0).toFixed(2)}</div></div>
          <div className="t-outcome-cell"><div className="k">Conf</div><div className="v">{((trade.conf||0)*100).toFixed(0)}%</div></div>
        </div>

        {/* Probability stack */}
        <div className="t-section-head">Why it fired</div>
        <div className="t-prob-stack">
          {probs.length > 0 ? (
            probs.map((p, i) => <ProbRow key={i} {...p} />)
          ) : (
            <div style={{color:'var(--fg-4)',fontFamily:'var(--f-mono)',fontSize:9.5}}>
              No linked probability metrics for this trade.
            </div>
          )}
        </div>

        {/* Direction decision */}
        <div className="t-section-head">Direction decision</div>
        <div className="t-dir-decision">
          {(ceProb != null || peProb != null) ? (
            <div className="t-dir-leg-row">
              <span className={`t-dir-leg ${chosenLeg === 'CE' ? 'chosen' : ''}`} style={{color: chosenLeg==='CE'?'var(--pos)':'var(--fg-3)'}}>
                CE {ceProb != null ? `${(ceProb*100).toFixed(0)}%` : '—'}
                {chosenLeg === 'CE' && <span style={{marginLeft:4,fontSize:9}}>▲ chosen</span>}
              </span>
              <div style={{flex:1,margin:'0 6px',height:4,borderRadius:2,background:'var(--bg-2)',position:'relative'}}>
                {ceProb != null && <div style={{position:'absolute',left:0,top:0,height:'100%',width:(ceProb*100)+'%',background:'var(--pos)',borderRadius:2,opacity:0.7}}/>}
              </div>
              <span className={`t-dir-leg ${chosenLeg === 'PE' ? 'chosen' : ''}`} style={{color: chosenLeg==='PE'?'var(--neg)':'var(--fg-3)'}}>
                PE {peProb != null ? `${(peProb*100).toFixed(0)}%` : '—'}
                {chosenLeg === 'PE' && <span style={{marginLeft:4,fontSize:9}}>▲ chosen</span>}
              </span>
            </div>
          ) : (
            <div style={{color:'var(--fg-4)',fontFamily:'var(--f-mono)',fontSize:9.5}}>
              Side-probability fields were not stored for this trade; chosen leg was <span style={{color:chosenLeg==='CE'?'var(--pos)':'var(--neg)'}}>{chosenLeg}</span>.
            </div>
          )}
          {upProb != null && (
            <div className="t-dir-up-row">
              <span style={{color:'var(--fg-3)',fontSize:10}}>up_prob</span>
              <div style={{flex:1,margin:'0 6px',height:3,borderRadius:2,background:'var(--bg-2)',position:'relative'}}>
                <div style={{position:'absolute',left:'50%',top:0,height:'100%',width:Math.abs(upProb-0.5)*100+'%',
                  background:upProb>=0.5?'var(--pos)':'var(--neg)',borderRadius:2,opacity:0.65,
                  transform:upProb>=0.5?'none':'translateX(-100%)'}}/>
                <div style={{position:'absolute',left:'50%',top:-1,width:1,height:5,background:'var(--fg-3)',opacity:0.4}}/>
              </div>
              <span style={{color:'var(--fg-3)',fontSize:10}}>{(upProb*100).toFixed(0)}%</span>
            </div>
          )}
          {consensusMargin != null && (
            <div style={{marginTop:5,fontFamily:'var(--f-mono)',fontSize:9.5,color:'var(--fg-3)'}}>
              consensus margin <span style={{color:consensusMargin >= 0.5 ? 'var(--pos)' : consensusMargin >= 0.2 ? 'var(--warn)' : 'var(--neg)'}}>{consensusMargin.toFixed(2)}</span>
            </div>
          )}
          {shadowDir && shadowScore != null && (
            <div style={{marginTop:5,fontFamily:'var(--f-mono)',fontSize:9.5,color:'var(--fg-3)'}}>
              shadow <span style={{color:shadowDir==='CE'?'var(--pos)':'var(--neg)',fontWeight:700}}>{shadowDir}</span> {Number(shadowScore) >= 0 ? '+' : ''}{Number(shadowScore).toFixed(1)}
            </div>
          )}
          {shadowBasis && (
            <>
              <div style={{marginTop:4,display:'flex',flexWrap:'wrap',gap:3}}>
                {_HM_SIGNALS.map(sg => {
                  const ceHit = sg.ce.some(s => firedShadowSignals.has(s));
                  const peHit = sg.pe.some(s => firedShadowSignals.has(s));
                  if (!ceHit && !peHit) return null;
                  return (
                    <span key={sg.label} className="t-confluence-chip" style={{color:ceHit?'var(--pos)':'var(--neg)'}}>
                      {sg.label}:{ceHit ? 'CE' : 'PE'}
                    </span>
                  );
                })}
              </div>
              <div style={{marginTop:4,fontFamily:'var(--f-mono)',fontSize:9,color:'var(--fg-3)',wordBreak:'break-all',lineHeight:1.4}}>{shadowBasis}</div>
            </>
          )}
          {dirKeywords.length > 0 && (
            <div className="t-dir-basis" style={{marginTop:4,display:'flex',flexWrap:'wrap',gap:3}}>
              <span style={{color:'var(--fg-4)',fontSize:9,marginRight:2}}>basis:</span>
              {dirKeywords.map((k,i) => <span key={i} className="t-confluence-chip">{k}</span>)}
            </div>
          )}
          {selectedStrategy && (
            <div style={{marginTop:4,fontFamily:'var(--f-mono)',fontSize:9,color:'var(--fg-3)'}}>
              selected vote: {selectedStrategy}{selectedVote.direction ? ` · ${selectedVote.direction}` : ''}{selectedVote.confidence != null ? ` · conf ${(Number(selectedVote.confidence)*100).toFixed(0)}%` : ''}
            </div>
          )}
          {dirBasis && <div style={{marginTop:4,fontFamily:'var(--f-mono)',fontSize:9,color:'var(--fg-3)',wordBreak:'break-all',lineHeight:1.4}}>{dirBasis}</div>}
          {selectedVote.reason && selectedVote.reason !== dirBasis && (
            <div style={{marginTop:4,fontFamily:'var(--f-mono)',fontSize:9,color:'var(--fg-3)',wordBreak:'break-all',lineHeight:1.4}}>
              {selectedVote.reason}
            </div>
          )}
        </div>

        {(policyReason || policyCheckRows.length > 0 || Object.keys(selectedVoteMetrics).length > 0) && <>
          <div className="t-section-head">Policy · Signals</div>
          <div style={{fontFamily:'var(--f-mono)',fontSize:9.5,color:'var(--fg-3)',lineHeight:1.45}}>
            {policyReason && <div style={{marginBottom:4}}>{policyReason}</div>}
            {policyCheckRows.length > 0 && (
              <div style={{display:'grid',gridTemplateColumns:'auto 1fr',columnGap:8,rowGap:3}}>
                {policyCheckRows.map(([k, v]) => (
                  <React.Fragment key={k}>
                    <span style={{color:'var(--fg-4)'}}>{k}</span>
                    <span style={{color:/^PASS/i.test(String(v))?'var(--pos)':/^WARN/i.test(String(v))?'var(--warn)':/^BLOCK/i.test(String(v))?'var(--neg)':'var(--fg-3)'}}>{String(v)}</span>
                  </React.Fragment>
                ))}
              </div>
            )}
            {selectedVoteMetrics.policy_score != null && (
              <div style={{marginTop:5}}>
                policy_score <span style={{color:'var(--fg-2)'}}>{Number(selectedVoteMetrics.policy_score).toFixed(2)}</span>
              </div>
            )}
          </div>
        </>}

        {/* Confluence */}
        {confluence.length > 0 && <>
          <div className="t-section-head">Confluence</div>
          <div className="t-confluence-list">
            {confluence.map((c, i) => <span key={i} className="t-confluence-chip">{c}</span>)}
          </div>
        </>}

        {/* Risk envelope */}
        <div className="t-section-head">Risk envelope</div>
        <RiskEnvelope trade={trade} path={pathPoints} session={session}/>

        {/* Rationale */}
        <div className="t-section-head">Entry · Exit rationale</div>
        <div className="t-rationale">
          <div className="t-rationale-block entry">
            <div className="heading"><span style={{color:'var(--pos)'}}>● Entry</span><span style={{color:'var(--fg-3)'}}>{_barLabel(session,trade.entryIdx)}</span></div>
            <div className="body">{trade.entryDetail}</div>
          </div>
          <div className={`t-rationale-block exit ${trade.exitReason==='TARGET_HIT'?'target':''}`}>
            <div className="heading"><span style={{color:trade.exitReason==='TARGET_HIT'?'var(--pos)':'var(--neg)'}}>● Exit · {(trade.exitReason||'').replace('_',' ')}</span></div>
            <div className="body">{trade.exitDetail}</div>
          </div>
        </div>

        {/* Counterfactuals */}
        <div className="t-section-head">What-if</div>
        <div className="t-cf-ribbon">
          {counterfactuals.map((cf, i) => {
            const d = typeof cf.delta === 'number' ? cf.delta : 0;
            const pct = Math.min(100, Math.abs(d) / 0.4 * 50);
            const cls = d > 0.001 ? 'pos' : d < -0.001 ? 'neg' : 'zero';
            return (
              <div key={i} className="t-cf-row">
                <div className="scen">{cf.label}<span className="note">{cf.note}</span></div>
                <div className={`delta ${cls}`}>{d===0?'±0.00':TC.fmtPct(d,2)}</div>
                <div className="delta-bar">
                  <span className="zero-mark"/>
                  <span className={`fill ${cls==='neg'?'neg':''}`} style={{left:d>=0?'50%':(50-pct)+'%',width:pct+'%'}}/>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function ProbRow({ v, gate, label, kind }) {
  v = Math.max(0, Math.min(1, v || 0));
  const cleared = gate != null ? v >= gate : null;
  const fillCls = kind === 'gate' ? (cleared ? 'pass' : 'fail') : 'neut';
  const valCls  = fillCls;
  return (
    <div className="t-prob-row">
      <div className="label">
        {label}
        {gate != null && <span className="gate">gate {gate.toFixed(2)} · {cleared ? '✓ pass' : '✗ fail'}</span>}
      </div>
      <div className="t-prob-bar">
        <div className={`fill ${fillCls}`} style={{width:(v*100)+'%'}}/>
        {gate != null && <div className="gate-mark" style={{left:(gate*100)+'%'}}/>}
      </div>
      <div className={`val ${valCls}`}>{v.toFixed(2)}</div>
    </div>
  );
}

function RiskEnvelope({ trade, path, session }) {
  const W = 320, H = 110;
  const PAD = { l: 8, r: 54, t: 12, b: 18 };
  const iW = W - PAD.l - PAD.r, iH = H - PAD.t - PAD.b;
  const prices = [trade.entryPx, trade.stopPx, trade.targetPx, ...path.map(p => p.p)];
  const lo = Math.min(...prices), hi = Math.max(...prices);
  const pad = (hi - lo) * 0.15;
  const pMin = lo - pad, pMax = hi + pad;
  const yOf = p => PAD.t + (pMax - p) / (pMax - pMin) * iH;
  const xOf = t => PAD.l + t * iW;
  const d = path.map((p, i) => `${i===0?'M':'L'} ${xOf(p.x).toFixed(1)} ${yOf(p.p).toFixed(1)}`).join(' ');
  return (
    <div className="t-risk-mini">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
        <rect x={PAD.l} y={Math.min(yOf(trade.stopPx),yOf(trade.entryPx))} width={iW} height={Math.abs(yOf(trade.stopPx)-yOf(trade.entryPx))} fill="var(--neg)" fillOpacity="0.05"/>
        <rect x={PAD.l} y={Math.min(yOf(trade.targetPx),yOf(trade.entryPx))} width={iW} height={Math.abs(yOf(trade.targetPx)-yOf(trade.entryPx))} fill="var(--pos)" fillOpacity="0.05"/>
        <line x1={PAD.l} x2={W-PAD.r} y1={yOf(trade.stopPx)}   y2={yOf(trade.stopPx)}   className="stop-line"/>
        <line x1={PAD.l} x2={W-PAD.r} y1={yOf(trade.targetPx)} y2={yOf(trade.targetPx)} className="target-line"/>
        <line x1={PAD.l} x2={W-PAD.r} y1={yOf(trade.entryPx)}  y2={yOf(trade.entryPx)}  className="entry-line"/>
        <text x={W-PAD.r+3} y={yOf(trade.stopPx)+3}   className="level-label stop"  >{trade.stopPx.toFixed(0)}</text>
        <text x={W-PAD.r+3} y={yOf(trade.targetPx)+3} className="level-label target">{trade.targetPx.toFixed(0)}</text>
        <text x={W-PAD.r+3} y={yOf(trade.entryPx)+3}  className="level-label"       >{trade.entryPx.toFixed(0)}</text>
        <path d={d} className="price-path"/>
        <circle cx={xOf(0)} cy={yOf(trade.entryPx)} r="3.5" className="entry-dot"/>
        <circle cx={xOf(1)} cy={yOf(trade.exitPx)}  r="4"   className="exit-dot"/>
        <text x={xOf(0)}   y={H-4} fontFamily="var(--f-mono)" fontSize="8" fill="var(--fg-3)" textAnchor="start">{_barLabel(session,trade.entryIdx)}</text>
        <text x={xOf(0.99)} y={H-4} fontFamily="var(--f-mono)" fontSize="8" fill="var(--fg-3)" textAnchor="end">{_barLabel(session,trade.exitIdx)}</text>
      </svg>
    </div>
  );
}

// ── Log strip ─────────────────────────────────────────────────────────────
function LogStrip({ session, alerts, wsStatus }) {
  const items = (alerts || []).slice(-4);
  return (
    <div className="t-log-strip">
      <span className="t-chip" style={{fontSize:'8.5px'}}>LOG</span>
      <div className="t-log-feed">
        {items.length === 0
          ? <span style={{color:'var(--fg-4)'}}>No alerts</span>
          : items.map((a, i) => (
            <span key={i} className="t-log-item">
              <span className="t">{_barLabel(session, a.idx ?? 0)}</span>
              <span className={`sev ${a.sev || a.level || 'info'}`}/>
              <span className="msg">{a.msg || a.message}</span>
            </span>
          ))
        }
      </div>
      <div className="t-log-perf">
        <span><span className="k">ws</span><span className={wsStatus==='connected'?'ok':''}>{wsStatus}</span></span>
        <span><span className="k">tbl</span><span className="ok">{(alerts||[]).length}</span></span>
      </div>
    </div>
  );
}

// ── LiveMonitorDark — main component ────────────────────────────────────
function LiveMonitorDark({ onModeSwitch, onKillClick }) {
  const [session,       setSession]       = _s(null);
  const [upToIdx,       setUpToIdx]       = _s(0);
  const [livePrice,     setLivePrice]     = _s(null);
  const [wsStatus,      setWsStatus]      = _s('connecting');
  const [selectedTrade, setSelectedTrade] = _s(null);
  const [flashId,       setFlashId]       = _s(null);
  const [flashIdx,      setFlashIdx]      = _s(null);
  const [runtimeConfig, setRuntimeConfig] = _s(null);
  const [availableModels, setAvailableModels] = _s([]);
  const [brainData, setBrainData] = _s(null);
  const wsRef         = _r(null);
  const sessionRef    = _r(null);
  const prevIdxRef    = _r(null);
  _e(() => { sessionRef.current = session; }, [session]);

  // Diagnostics: in live mode we surface current model + available models.
  // Blocker funnel needs a date and is replay-only.
  _e(() => {
    let alive = true;
    fetch('/api/strategy/current/state?mode=live&latest_n=0')
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`state HTTP ${r.status}`)))
      .then(s => { if (!alive) return; setRuntimeConfig(s?.runtime_config || null); setAvailableModels(Array.isArray(s?.available_models) ? s.available_models : []); })
      .catch(() => {});
    return () => { alive = false; };
  }, []);

  // Live brain status — initial fetch + refresh every 30s.
  _e(() => {
    let alive = true;
    const load = () => fetch('/api/strategy/brain/status?mode=live')
      .then(r => r.ok ? r.json() : Promise.resolve({ available: false, reason: `HTTP ${r.status}` }))
      .catch(() => ({ available: false, reason: 'fetch failed' }))
      .then(b => { if (alive) setBrainData(b); });
    load();
    const id = setInterval(load, 30000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  _e(() => {
    const ws = TC.makeMonitorWS(
      () => ({ action: 'subscribe', mode: 'live' }),
      {
        onStatus: setWsStatus,
        onMessage(msg) {
          if (msg.type === 'snapshot') {
            setSession(msg.session);
            setUpToIdx(msg.up_to_idx);
            if (msg.live_price != null) setLivePrice(msg.live_price);
            prevIdxRef.current = msg.up_to_idx;
          } else if (msg.type === 'waiting') {
            setSession({
              date: new Date().toISOString().slice(0, 10),
              instrument: 'BANKNIFTY',
              candles: [],
              signals: [],
              trades: [],
              alerts: [{ level: 'warn', t: '—', msg: msg.hint || msg.message || 'Waiting for live snapshots…', tms: Date.now() }],
              basePrice: 0,
            });
          } else if (msg.type === 'error') {
            setSession({
              date: new Date().toISOString().slice(0, 10),
              instrument: 'BANKNIFTY',
              candles: [],
              signals: [],
              trades: [],
              alerts: [{ level: 'error', t: '—', msg: msg.message || 'Monitor error', tms: Date.now() }],
              basePrice: 0,
            });
          } else if (msg.type === 'tick') {
            const newIdx = msg.up_to_idx;
            const sess = sessionRef.current;
            if (sess && prevIdxRef.current !== null && newIdx > prevIdxRef.current) {
              const filled = sess.trades.find(t => t.exitIdx > prevIdxRef.current && t.exitIdx <= newIdx);
              if (filled) {
                const bridged = _bridgeTrade(filled);
                setFlashId(bridged.id);
                setFlashIdx(filled.exitIdx);
                setSelectedTrade(bridged);
                setTimeout(() => { setFlashId(null); setFlashIdx(null); }, 1400);
              }
            }
            prevIdxRef.current = newIdx;
            setUpToIdx(newIdx);
            if (msg.live_price != null) setLivePrice(msg.live_price);
          }
        },
      }
    );
    wsRef.current = ws;
    return () => ws.close();
  }, []);

  // keyboard nav
  _e(() => {
    const trades = session ? session.trades.filter(t => t.exitIdx <= upToIdx).map(_bridgeTrade) : [];
    const fn = e => {
      if (e.target.tagName === 'INPUT') return;
      if (e.key === 'j' || e.key === 'J') {
        const i = trades.findIndex(t => t.id === selectedTrade?.id);
        if (i > 0) setSelectedTrade(trades[i - 1]);
      } else if (e.key === 'k' || e.key === 'K') {
        const i = trades.findIndex(t => t.id === selectedTrade?.id);
        if (i >= 0 && i < trades.length - 1) setSelectedTrade(trades[i + 1]);
      } else if ((e.metaKey || e.ctrlKey) && e.key === '.') {
        e.preventDefault(); onKillClick();
      }
    };
    window.addEventListener('keydown', fn);
    return () => window.removeEventListener('keydown', fn);
  }, [session, upToIdx, selectedTrade, onKillClick]);

  if (!session) {
    return (
      <div className="cockpit" style={{gridTemplateRows:'1fr'}}>
        <div className="t-loading">
          <span>{wsStatus === 'connecting' ? 'Connecting…' : wsStatus === 'disconnected' ? 'Reconnecting…' : 'Loading session…'}</span>
        </div>
      </div>
    );
  }

  const candles        = session.candles || [];
  const rawTrades      = (session.trades || []).filter(t => t.exitIdx <= upToIdx);
  const trades         = rawTrades.map(_bridgeTrade);
  const signals        = (session.signals || []).filter(s => s.idx <= upToIdx).slice(-60).reverse();
  const strategies     = _makeStrategies(trades);
  const quote          = _makeQuote(session, upToIdx);
  const sessionPnl     = rawTrades.reduce((a, t) => a + (t.pnlPct || 0), 0);
  const wins           = rawTrades.filter(t => (t.pnlPct || 0) > 0).length;
  const winRate        = rawTrades.length ? Math.round(wins / rawTrades.length * 100) : 0;
  const regime         = session.regime || (trades[0]?.regime) || '—';
  const engine         = session.engine || 'ML_PURE';

  // Default to latest trade in inspector
  const displayTrade   = selectedTrade || (trades.length > 0 ? trades[0] : null);

  return (
    <div className="cockpit">
      <TickerBar quote={quote} instrument={session.instrument}/>
      <StatusBar
        sessionPnl={sessionPnl} tradesCount={trades.length} winRate={winRate}
        regime={regime} engine={engine} ws={wsStatus}
        onModeSwitch={onModeSwitch} onHaltClick={onKillClick}
      />
      <div className="t-workspace">
        <EngineRoster strategies={strategies} dailyRisk={Math.abs(Math.min(0, sessionPnl))}/>

        <div className="t-center">
          <div className="t-chart-panel">
            <div className="t-panel-head">
              <div className="t-panel-title">
                {session.instrument} · 1m <span className="count">{Math.min(upToIdx+1,candles.length)}/{candles.length} bars</span>
              </div>
              <div className="t-panel-actions">
                {displayTrade && <span className="t-chip amber">● {displayTrade.id}</span>}
              </div>
            </div>
            <TermChart
              session={session} candles={candles} trades={trades}
              selectedTrade={displayTrade} onSelectTrade={setSelectedTrade}
              upToIdx={upToIdx} flashIdx={flashIdx}
            />
          </div>
          <Tape
            session={session} trades={[...trades].reverse()} signals={signals}
            selectedTrade={displayTrade} onSelectTrade={setSelectedTrade}
            flashId={flashId}
            diag={{ runtimeConfig, availableModels, brainData, blockerFunnel: null,
                    loading: false, error: '', date: null, onRefresh: null }}
          />
        </div>

        <div className="t-rail right" style={{display:'flex',flexDirection:'column'}}>
          <div className="t-panel-head">
            <div className="t-panel-title">Trade Inspector</div>
            <div className="t-panel-actions">
              <button className="t-btn ghost sm" title="Prev trade (K)"
                onClick={() => { const i=trades.findIndex(t=>t.id===selectedTrade?.id); if(i>0)setSelectedTrade(trades[i-1]); }}>K↑</button>
              <button className="t-btn ghost sm" title="Next trade (J)"
                onClick={() => { const i=trades.findIndex(t=>t.id===selectedTrade?.id); if(i<trades.length-1)setSelectedTrade(trades[i+1]); }}>↓J</button>
            </div>
          </div>
          <div style={{flex:1,overflowY:'auto',minHeight:0}}>
            <TradeInspector session={session} trade={displayTrade}/>
          </div>
        </div>
      </div>
      <LogStrip session={session} alerts={session.alerts} wsStatus={wsStatus}/>
    </div>
  );
}

// ── Replay Ticker Bar ─────────────────────────────────────────────────────
function ReplayTickerBar({ date, vtLabel, instrument, isPlaying }) {
  const [clock, setClock] = _s(TC.fmtClock(new Date()));
  _e(() => { const id = setInterval(() => setClock(TC.fmtClock(new Date())), 1000); return () => clearInterval(id); }, []);
  return (
    <div className="ticker-bar">
      <div className="t-brand">
        <span className="t-brand-mark"/>
        <b>QUANT</b><span>OPS</span>
        <span className="ver">replay</span>
      </div>
      <div className="ticker-pairs">
        {instrument && <div className="pair"><span className="lbl">{instrument}</span></div>}
        <div className="pair">
          <span className="lbl">Date</span>
          <span className="val">{date || '—'}</span>
        </div>
        <div className="pair">
          <span className="lbl">Time</span>
          <span className="val" style={{color:'var(--info)'}}>{vtLabel || '—'}</span>
        </div>
        <div className="pair">
          <span className={`val ${isPlaying ? 'pos' : 'warn'}`} style={{fontSize:'10px'}}>
            {isPlaying ? '▶ PLAYING' : '⏸ PAUSED'}
          </span>
        </div>
      </div>
      <div className="ticker-clock">
        <span style={{color:'var(--fg-4)',fontSize:'9px',letterSpacing:'0.10em',textTransform:'uppercase',marginRight:4}}>IST</span>
        <span>{clock}</span>
      </div>
    </div>
  );
}

// ── Replay Status Bar ─────────────────────────────────────────────────────
//
// C1 model training windows (per ml_pipeline_2/docs/training/MODEL_STATE_20260514.md):
//   train:   2020-08-03 → 2024-04-30  — model SAW these dates during training
//   valid:   2024-05-01 → 2024-07-31  — hyperparameter-tuning data
//   holdout: 2024-08-01 → 2024-10-31  — truly out-of-sample (never seen)
//
// Surfacing these windows in the date picker prevents celebrating in-sample
// numbers as evidence of edge. See PROJECT_PLAN.md §14.
const C1_TRAIN_END   = "2024-04-30";
const C1_VALID_END   = "2024-07-31";
const C1_HOLDOUT_END = "2024-10-31";

function _windowOf(d) {
  if (!d) return "?";
  if (d <= C1_TRAIN_END)   return "train";
  if (d <= C1_VALID_END)   return "valid";
  if (d <= C1_HOLDOUT_END) return "OOS";
  return "post";
}

function _fmtDateOption(d, tradeCounts, modelsByDate) {
  const n = (tradeCounts && tradeCounts[d]) || 0;
  const win = _windowOf(d);
  const tag = win === "train" ? "● train" : win === "valid" ? "◐ valid" : win === "OOS" ? "○ OOS  " : "  post ";
  const m = modelsByDate && modelsByDate[d];
  // Three states: no replay run exists for this date; ran but 0 trades; ran with trades.
  const tradeStr = !m        ? "   no replay"
                 : n > 0     ? `${String(n).padStart(3,' ')} trades`
                 :             "   0 trades ";
  let modelTag = "         ";
  if (m && m.family) {
    if (m.family === "OPT_PNL") {
      modelTag = ` · ${(m.recipe || "OPT_PNL").padEnd(10)}`;
    } else {
      modelTag = ` · ${(m.family || "?").padEnd(10)}`;
    }
  }
  return `${tag} · ${d} · ${tradeStr}${modelTag}`;
}

function ReplayStatusBar({ sessionPnl, tradesCount, winRate, isPlaying, speed, upToIdx,
  total, availableDates, tradeCounts, modelsByDate, replayDate, replayRunId, onPlay, onPause, onSpeed, onScrub, onScrubEnd,
  onReset, onDateChange, onModeSwitch, ws, datesLoading }) {
  const pnlCls = sessionPnl >= 0 ? 'pos' : 'neg';
  const pct = total > 0 ? ((upToIdx + 1) / total * 100).toFixed(0) : 0;
  const selStyle = { fontFamily:'var(--f-mono)', fontSize:'10px', background:'var(--bg-2)',
    color:'var(--fg-1)', border:'1px solid var(--line-2)', borderRadius:'var(--r-2)',
    padding:'0 6px', height:20 };
  return (
    <div className="t-status-bar">
      <div className="t-mode-toggle">
        <button onClick={() => onModeSwitch('live')}><span className="dot live"/>Live</button>
        <button className="active"><span className="dot replay"/>Replay</button>
        <button onClick={() => onModeSwitch('eval')}><span className="dot eval"/>Eval</button>
      </div>
      <div style={{display:'flex',alignItems:'center',gap:5,padding:'0 10px',flex:1,overflow:'hidden',minWidth:0}}>
        {isPlaying
          ? <button className="t-btn sm" onClick={onPause}>⏸</button>
          : <button className="t-btn sm" style={{background:'var(--accent)',color:'#1a1100',borderColor:'var(--accent)'}} onClick={onPlay}>▶</button>
        }
        <button className="t-btn sm ghost" onClick={onReset} title="Reset to start">⟲</button>
        <div style={{display:'flex',gap:2,flexShrink:0}}>
          {[1,2,4,8,16].map(s => (
            <button key={s} className="t-btn sm ghost"
              style={speed===s?{background:'var(--bg-4)',borderColor:'var(--line-3)',color:'var(--fg-1)'}:{}}
              onClick={() => onSpeed(s)}>{s}×</button>
          ))}
        </div>
        <input type="range" min={0} max={Math.max(0, total - 1)} value={upToIdx}
          style={{flex:1,height:4,accentColor:'var(--info)',minWidth:60,cursor:'pointer'}}
          onChange={e => onScrub(Number(e.target.value))}
          onMouseUp={e => onScrubEnd(Number(e.target.value))}
          onTouchEnd={e => onScrubEnd(Number(e.target.value))}
        />
        <span style={{fontFamily:'var(--f-mono)',fontSize:'9.5px',color:'var(--fg-3)',flexShrink:0}}>{pct}%</span>
        <select style={selStyle} value={replayDate}
          disabled={datesLoading || !availableDates.length}
          onChange={e => onDateChange(e.target.value, { runId: modelsByDate[e.target.value]?.run_id || '' })}>
          {availableDates.slice().reverse().map(d => <option key={d} value={d}>{_fmtDateOption(d, tradeCounts, modelsByDate)}</option>)}
        </select>
        {replayRunId && (
          <span style={{fontFamily:'var(--f-mono)',fontSize:'9px',color:'var(--fg-3)',flexShrink:0}}
            title={replayRunId}>run {replayRunId.slice(0, 8)}…</span>
        )}
      </div>
      <div className="t-status-cells" style={{flexShrink:0}}>
        <div className="t-scell big"><span className="k">P&amp;L</span><span className={`v ${pnlCls}`}>{TC.fmtPct(sessionPnl,2)}</span></div>
        <div className="t-scell"><span className="k">Trades</span><span className="v">{tradesCount}<span className="sub"> / {winRate}%wr</span></span></div>
        <div className="t-scell"><span className="k">WS</span><span className={`v ${ws==='connected'?'pos':'warn'}`} style={{fontSize:'10px'}}>{ws}</span></div>
      </div>
    </div>
  );
}

function _replayBootParams() {
  try {
    const p = new URLSearchParams(window.location.search);
    return {
      date: String(p.get('date') || p.get('replay_date') || '').trim(),
      runId: String(p.get('run_id') || '').trim(),
    };
  } catch (_) {
    return { date: '', runId: '' };
  }
}

function _syncReplayUrl({ date, runId }) {
  try {
    const url = new URL(window.location.href);
    url.searchParams.set('mode', 'replay');
    if (date) url.searchParams.set('date', date);
    else url.searchParams.delete('date');
    if (runId) url.searchParams.set('run_id', runId);
    else url.searchParams.delete('run_id');
    window.history.replaceState({}, '', url);
  } catch (_) { /* ignore */ }
}

// ── ReplayMonitorDark ────────────────────────────────────────────────────
function ReplayMonitorDark({ onModeSwitch }) {
  const _boot = _replayBootParams();
  const [session,        setSession]        = _s(null);
  const [upToIdx,        setUpToIdx]        = _s(0);
  const [isPlaying,      setIsPlaying]      = _s(false);
  const [speed,          setSpeed]          = _s(4);
  const [replayDate,     setReplayDate]     = _s(_boot.date || '');
  const [replayRunId,    setReplayRunId]    = _s(_boot.runId || '');
  const [replayError,    setReplayError]    = _s('');
  const [wsStatus,       setWsStatus]       = _s('idle');
  const [availableDates, setAvailableDates] = _s([]);
  const [tradeCounts, setTradeCounts] = _s({});
  const [modelsByDate, setModelsByDate] = _s({});
  const [datesLoading,   setDatesLoading]   = _s(true);
  const [selectedTrade,  setSelectedTrade]  = _s(null);
  const [runtimeConfig,  setRuntimeConfig]  = _s(null);
  const [availableModels,setAvailableModels]= _s([]);
  const [blockerFunnel,  setBlockerFunnel]  = _s(null);
  const [brainData,      setBrainData]      = _s(null);
  const [timeline,       setTimeline]       = _s(null);
  const [tlOutcome,      setTlOutcome]      = _s('blocked');
  const [tlCollapse,     setTlCollapse]     = _s(true);
  const [diagLoading,    setDiagLoading]    = _s(false);
  const [diagError,      setDiagError]      = _s('');
  const [heatmapData,    setHeatmapData]    = _s(null);
  const [heatmapLoading, setHeatmapLoading] = _s(false);
  const wsRef         = _r(null);
  const upToIdxRef    = _r(0);
  const speedRef      = _r(4);
  const replayDateRef = _r(_boot.date || '');
  const replayRunIdRef = _r(_boot.runId || '');
  const fitRef        = _r(null);

  // Diagnostics: load current model + available models, blocker-funnel aggregate
  // for the selected date, AND a per-minute decisions timeline so the operator
  // can scroll snapshot-by-snapshot to see exactly why each minute was blocked.
  // Re-fires whenever replayDate or the timeline outcome filter changes.
  const fetchDiag = _cb(() => {
    const date = replayDateRef.current;
    setDiagLoading(true); setDiagError('');
    const stateP = fetch('/api/strategy/current/state?mode=replay&latest_n=0')
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`state HTTP ${r.status}`)));
    const runQ = replayRunIdRef.current
      ? `&run_id=${encodeURIComponent(replayRunIdRef.current)}`
      : '';
    const funnelP = date
      ? fetch(`/api/strategy/blocker-funnel?mode=replay&date=${encodeURIComponent(date)}${runQ}`)
          .then(r => r.ok ? r.json() : Promise.reject(new Error(`funnel HTTP ${r.status}`)))
      : Promise.resolve(null);
    const tlOutcomeQ = tlOutcome ? `&outcome=${encodeURIComponent(tlOutcome)}` : '';
    const tlCollapseQ = tlCollapse ? `&collapse=true` : '';
    const tlP = date
      ? fetch(`/api/strategy/decisions?mode=replay&date=${encodeURIComponent(date)}&limit=500${runQ}${tlOutcomeQ}${tlCollapseQ}`)
          .then(r => r.ok ? r.json() : Promise.reject(new Error(`decisions HTTP ${r.status}`)))
      : Promise.resolve(null);
    const brainP = fetch('/api/strategy/brain/status?mode=replay')
      .then(r => r.ok ? r.json() : Promise.resolve({ available: false, reason: `HTTP ${r.status}` }))
      .catch(() => ({ available: false, reason: 'fetch failed' }));
    Promise.all([stateP, funnelP, tlP, brainP])
      .then(([state, funnel, tl, brain]) => {
        setRuntimeConfig(state?.runtime_config || null);
        setAvailableModels(Array.isArray(state?.available_models) ? state.available_models : []);
        setBlockerFunnel(funnel);
        setTimeline(tl);
        setBrainData(brain);
        setDiagLoading(false);
      })
      .catch(err => { setDiagError(err.message || String(err)); setDiagLoading(false); });
  }, [tlOutcome, tlCollapse]);

  _e(() => { fetchDiag(); /* refetch on date or filter change */ }, [replayDate, tlOutcome, tlCollapse]);

  _e(() => {
    if (!replayDate) { setHeatmapData(null); return; }
    setHeatmapLoading(true);
    fetch(`/api/strategy/session-heatmap?mode=replay&date=${encodeURIComponent(replayDate)}`)
      .then(r => r.ok ? r.json() : Promise.resolve(null))
      .then(d => { setHeatmapData(d || null); setHeatmapLoading(false); })
      .catch(() => { setHeatmapData(null); setHeatmapLoading(false); });
  }, [replayDate]);

  _e(() => { upToIdxRef.current    = upToIdx;    }, [upToIdx]);
  _e(() => { speedRef.current      = speed;      }, [speed]);
  _e(() => { replayDateRef.current = replayDate; }, [replayDate]);
  _e(() => { replayRunIdRef.current = replayRunId; }, [replayRunId]);

  function openReplaySocket() {
    if (wsRef.current) return;
    setWsStatus('connecting');
    wsRef.current = TC.makeMonitorWS(
      () => {
        const sub = {
          action:'subscribe', mode:'replay', date:replayDateRef.current,
          up_to_idx:upToIdxRef.current, playing:false, speed:speedRef.current,
        };
        if (replayRunIdRef.current) sub.run_id = replayRunIdRef.current;
        return sub;
      },
      {
        onStatus: setWsStatus,
        onMessage(msg) {
          if (msg.type === 'snapshot') {
            const sess = msg.session || {};
            setSession(sess); setUpToIdx(msg.up_to_idx); setIsPlaying(false);
            setReplayDate(sess.date || ''); replayDateRef.current = sess.date || '';
            if (sess.runId) { setReplayRunId(sess.runId); replayRunIdRef.current = sess.runId; }
            setReplayError('');
          } else if (msg.type === 'tick') {
            setUpToIdx(msg.up_to_idx);
          } else if (msg.type === 'state') {
            setUpToIdx(msg.up_to_idx); setIsPlaying(msg.is_playing); setSpeed(msg.speed);
          } else if (msg.type === 'error') {
            setReplayError(msg.message || 'Replay date load failed.'); setIsPlaying(false);
          }
        },
      }
    );
  }

  _e(() => { return () => { if (wsRef.current) wsRef.current.close(); }; }, []);

  _e(() => {
    let alive = true;
    fetch('/api/historical/replay/dates?limit=250')
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
      .then(payload => {
        if (!alive) return;
        setAvailableDates(Array.isArray(payload.dates) ? payload.dates : []);
        setTradeCounts(payload.trade_counts || {});
        setModelsByDate(payload.models_by_date || {});
        setDatesLoading(false);
        const boot = _replayBootParams();
        const pick = boot.date || payload.latest;
        if (pick) handleDateChange(pick, { runId: boot.runId || replayRunIdRef.current });
        else setReplayError('No replay dates found in historical snapshots.');
      })
      .catch(err => { if (!alive) return; setDatesLoading(false); setReplayError('Failed to load dates: ' + err.message); });
    return () => { alive = false; };
  }, []);

  function sendControl(patch) { wsRef.current && wsRef.current.send({ action:'control', ...patch }); }
  function handlePlay()    { setIsPlaying(true);  sendControl({ play: true }); }
  function handlePause()   { setIsPlaying(false); sendControl({ play: false }); }
  function handleSpeed(s)  { setSpeed(s);          sendControl({ speed: s }); }
  function handleScrub(idx){ setUpToIdx(idx); }
  function handleScrubEnd(idx) { setUpToIdx(idx); setIsPlaying(false); sendControl({ seek:idx, play:false }); }
  function handleReset()   { setUpToIdx(0); setIsPlaying(false); sendControl({ seek:0, play:false }); }
  function handleDateChange(newDate, opts = {}) {
    if (!newDate) return;
    const nextRunId = opts.runId !== undefined ? String(opts.runId || '').trim() : replayRunIdRef.current;
    setReplayDate(newDate); replayDateRef.current = newDate;
    setReplayRunId(nextRunId); replayRunIdRef.current = nextRunId;
    _syncReplayUrl({ date: newDate, runId: nextRunId });
    setReplayError(''); setIsPlaying(false);
    setSession(null); setUpToIdx(0);
    openReplaySocket();
    const sub = {
      action:'subscribe', mode:'replay', date:newDate, up_to_idx:0, playing:false, speed:speedRef.current,
    };
    if (nextRunId) sub.run_id = nextRunId;
    const sent = wsRef.current && wsRef.current.send(sub);
    if (!sent) setWsStatus('connecting');
  }
  // keyboard nav
  _e(() => {
    const fn = e => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
      if ((e.key === 'j' || e.key === 'J') && session) {
        const trades = (session.trades||[]).filter(t=>t.exitIdx<=upToIdx).map(_bridgeTrade);
        const i = trades.findIndex(t => t.id === selectedTrade?.id);
        if (i > 0) setSelectedTrade(trades[i-1]);
      } else if ((e.key === 'k' || e.key === 'K') && session) {
        const trades = (session.trades||[]).filter(t=>t.exitIdx<=upToIdx).map(_bridgeTrade);
        const i = trades.findIndex(t => t.id === selectedTrade?.id);
        if (i >= 0 && i < trades.length-1) setSelectedTrade(trades[i+1]);
      } else if (e.key === ' ' && !e.target.closest('button')) {
        e.preventDefault();
        isPlaying ? handlePause() : handlePlay();
      }
    };
    window.addEventListener('keydown', fn);
    return () => window.removeEventListener('keydown', fn);
  }, [session, upToIdx, selectedTrade, isPlaying]);

  // Loading state
  if (!session) {
    const selStyle = { fontFamily:'var(--f-mono)', fontSize:'10px', background:'var(--bg-2)',
      color:'var(--fg-1)', border:'1px solid var(--line-2)', borderRadius:'var(--r-2)', padding:'2px 8px' };
    return (
      <div className="cockpit">
        <div className="ticker-bar">
          <div className="t-brand"><span className="t-brand-mark"/><b>QUANT</b><span>OPS</span><span className="ver">replay</span></div>
          <div/><div/>
        </div>
        <div className="t-status-bar">
          <div className="t-mode-toggle">
            <button onClick={() => onModeSwitch('live')}><span className="dot live"/>Live</button>
            <button className="active"><span className="dot replay"/>Replay</button>
            <button onClick={() => onModeSwitch('eval')}><span className="dot eval"/>Eval</button>
          </div>
          <div style={{display:'flex',alignItems:'center',gap:8,padding:'0 12px'}}>
            <span style={{fontFamily:'var(--f-mono)',fontSize:'10px',color:'var(--fg-3)'}}>Date</span>
            <select style={selStyle} value={replayDate}
              disabled={datesLoading || !availableDates.length}
              onChange={e => handleDateChange(e.target.value, { runId: modelsByDate[e.target.value]?.run_id || '' })}>
              {!replayDate && <option value="">{datesLoading ? 'Loading…' : 'Select date'}</option>}
              {availableDates.slice().reverse().map(d => <option key={d} value={d}>{_fmtDateOption(d, tradeCounts, modelsByDate)}</option>)}
            </select>
          </div>
          <div/>
        </div>
        <div className="t-loading">
          <div style={{textAlign:'center'}}>
            <div style={{color:'var(--fg-2)',marginBottom:8}}>
              {datesLoading          ? 'Loading replay dates…' :
               wsStatus==='connecting' ? 'Connecting to server…' :
               wsStatus==='disconnected' ? 'Reconnecting…' :
               replayError || 'Select a date above to load a session.'}
            </div>
            {replayError && <div style={{fontSize:10,color:'var(--fg-4)'}}>Try a different date or wait for replay data to be generated.</div>}
            {!replayError && !replayRunId && !datesLoading && (
              <div style={{fontSize:10,color:'var(--warn)',marginTop:6,maxWidth:420}}>
                Tip: open Replay from Eval via <strong>Open in Replay</strong> (or add{' '}
                <code style={{fontSize:9}}>run_id=e8ba040a-a8dd-47d1-9bf8-ceffba85e809</code> to the URL).
                Without <code>run_id</code>, the picker may load an older experiment with no votes.
              </div>
            )}
          </div>
        </div>
        <div className="t-log-strip">
          <span className="t-chip" style={{fontSize:'8.5px'}}>LOG</span>
          <div className="t-log-feed"><span style={{color:'var(--fg-4)'}}>Awaiting session</span></div>
          <div className="t-log-perf"><span><span className="k">ws</span>{wsStatus}</span></div>
        </div>
      </div>
    );
  }

  const candles     = session.candles || [];
  const rawTrades   = (session.trades || []).filter(t => t.exitIdx <= upToIdx);
  const trades      = rawTrades.map(_bridgeTrade);
  const signals     = (session.signals || []).filter(s => s.idx <= upToIdx).slice(-120).reverse();
  const strategies  = _makeStrategies(trades);
  const sessionPnl  = rawTrades.reduce((a,t) => a + (t.pnlPct||0), 0);
  const wins        = rawTrades.filter(t => (t.pnlPct||0) > 0).length;
  const winRate     = rawTrades.length ? Math.round(wins/rawTrades.length*100) : 0;
  const vtLabel     = candles[upToIdx]?.label ||
    new Date(candles[upToIdx]?.t||0).toLocaleTimeString('en-IN',
      { hour:'2-digit', minute:'2-digit', hour12:false, timeZone:'Asia/Kolkata' });
  const displayTrade = selectedTrade || (trades.length > 0 ? trades[0] : null);
  const noData = candles.length > 0 && session.trades.length === 0
    && (session.alerts||[]).some(a => a.level === 'warn');
  const banner = replayError || (noData ? `No high-confidence ML signals on ${replayDate}.` : '');

  return (
    <div className="cockpit">
      <ReplayTickerBar date={session.date} vtLabel={vtLabel}
        instrument={session.instrument} isPlaying={isPlaying}/>
      <ReplayStatusBar
        sessionPnl={sessionPnl} tradesCount={trades.length} winRate={winRate}
        isPlaying={isPlaying} speed={speed} upToIdx={upToIdx} total={candles.length}
        availableDates={availableDates} tradeCounts={tradeCounts} modelsByDate={modelsByDate} replayDate={replayDate}
        replayRunId={replayRunId || session.runId || ''}
        onPlay={handlePlay} onPause={handlePause} onSpeed={handleSpeed}
        onScrub={handleScrub} onScrubEnd={handleScrubEnd} onReset={handleReset}
        onDateChange={handleDateChange} onModeSwitch={onModeSwitch}
        ws={wsStatus} datesLoading={datesLoading}
      />

      {/* The 1fr workspace row — flex column so optional banner doesn't break the grid */}
      <div style={{display:'flex',flexDirection:'column',minHeight:0,overflow:'hidden'}}>
        {banner && (
          <div style={{background:'var(--warn-wash)',borderBottom:'1px solid rgba(245,165,36,0.25)',
            padding:'5px 14px',fontFamily:'var(--f-mono)',fontSize:10.5,color:'var(--warn)',
            display:'flex',alignItems:'center',gap:10,flexShrink:0}}>
            <span style={{flex:1}}>{banner}</span>
          </div>
        )}
        <div className="t-workspace" style={{flex:1,minHeight:0}}>
          <EngineRoster strategies={strategies} dailyRisk={sessionPnl<0?Math.abs(sessionPnl)/2:0} brainData={brainData}/>

          <div className="t-center">
            <div className="t-chart-panel">
              <div className="t-panel-head">
                <div className="t-panel-title">
                  {session.instrument} · 1m
                  <span className="count">{Math.min(upToIdx+1,candles.length)}/{candles.length} bars</span>
                </div>
                <div className="t-panel-actions">
                  <button className="t-btn ghost sm" title="Fit chart"
                    onClick={() => fitRef.current && fitRef.current()}>fit</button>
                  {displayTrade && <span className="t-chip amber">● {displayTrade.id}</span>}
                </div>
              </div>
              <div style={{flex:1,minHeight:0}}>
                <LWChart
                  candles={candles} upToIdx={upToIdx}
                  trades={trades} signals={signals}
                  selectedTrade={displayTrade} selectedSignal={null}
                  onSelectTrade={setSelectedTrade} onSelectSignal={() => {}}
                  height="100%" isPlaying={isPlaying} fitRef={fitRef}
                />
              </div>
            </div>
            <Tape
              session={session} trades={[...trades].reverse()} signals={signals}
              selectedTrade={displayTrade} onSelectTrade={setSelectedTrade}
              flashId={null}
              heatmap={{ data: heatmapData, loading: heatmapLoading }}
              diag={{ runtimeConfig, availableModels, blockerFunnel,
                      timeline, brainData,
                      loading: diagLoading, error: diagError,
                      date: replayDate, onRefresh: fetchDiag,
                      onTimelineFilterChange: setTlOutcome,
                      onTimelineCollapseChange: setTlCollapse,
                      activeRunId: replayRunId || (session && session.runId ? session.runId : null) }}
            />
          </div>

          <div className="t-rail right" style={{display:'flex',flexDirection:'column'}}>
            <div className="t-panel-head">
              <div className="t-panel-title">Trade Inspector</div>
              <div className="t-panel-actions">
                <button className="t-btn ghost sm" title="Prev (K)"
                  onClick={() => { const i=trades.findIndex(t=>t.id===selectedTrade?.id); if(i>0)setSelectedTrade(trades[i-1]); }}>K↑</button>
                <button className="t-btn ghost sm" title="Next (J)"
                  onClick={() => { const i=trades.findIndex(t=>t.id===selectedTrade?.id); if(i<trades.length-1)setSelectedTrade(trades[i+1]); }}>↓J</button>
              </div>
            </div>
            <div style={{flex:1,overflowY:'auto',minHeight:0}}>
              <TradeInspector session={session} trade={displayTrade}/>
            </div>
          </div>
        </div>
      </div>

      <LogStrip session={session} alerts={session.alerts} wsStatus={wsStatus}/>
    </div>
  );
}

Object.assign(window, { LiveMonitorDark, ReplayMonitorDark });
