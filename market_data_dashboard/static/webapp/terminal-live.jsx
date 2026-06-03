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
  // Chart bias (LONG/SHORT triangles) must include position side.
  // Example: CE + SHORT (short call) is bearish, not LONG.
  const dirMap = { PE: 'SHORT', CE: 'LONG' };
  const rawLegBias = dirMap[rawDir] || dirMap[String(rawDir).toUpperCase()] || rawDir;
  let chartBias = rawLegBias;
  if (optionType === 'CE' && positionSide === 'SHORT') chartBias = 'SHORT';
  else if (optionType === 'PE' && positionSide === 'SHORT') chartBias = 'LONG';
  else if (optionType === 'PE' && positionSide === 'LONG') chartBias = 'SHORT';
  else if (optionType === 'CE' && positionSide === 'LONG') chartBias = 'LONG';
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

// ── Lightweight Charts Candlestick Chart ─────────────────────────────────
// Replaces the fixed SVG. Gives native zoom (scroll wheel), pan (drag),
// crosshair tooltip, and auto-resize — without affecting page font size.
function TermChart({ session, candles, trades, selectedTrade, onSelectTrade, upToIdx }) {
  const LWC = window.LightweightCharts;
  // LightweightCharts renders the time axis in UTC and has no timezone support.
  // Our candle .t is true UTC ms. Shift by IST (+5:30) so the axis prints IST
  // wall-clock (09:15 not 03:45). All series + markers + click use the same shift.
  const IST = 19800; // 5.5h in seconds
  const _tsec = (ms) => Math.floor(ms / 1000) + IST;
  const containerRef = _r(null);
  const chartRef     = _r(null);
  const candleRef    = _r(null);
  const vwapRef      = _r(null);
  const priceLineRef = _r(null);
  const stopLineRef  = _r(null);
  const tgtLineRef   = _r(null);

  // ── Init chart once ──────────────────────────────────────────────────
  _e(() => {
    if (!LWC || !containerRef.current) return;
    const cs = getComputedStyle(document.documentElement);
    const get = v => cs.getPropertyValue(v).trim();

    const chart = LWC.createChart(containerRef.current, {
      layout: { background: { type: 'solid', color: 'transparent' }, textColor: get('--fg-3') || '#71717a', fontSize: 10 },
      grid:   { vertLines: { color: get('--line-1') || '#27272a', style: 1 }, horzLines: { color: get('--line-1') || '#27272a', style: 1 } },
      crosshair: { mode: LWC.CrosshairMode.Normal },
      rightPriceScale: { borderColor: get('--line-2') || '#3f3f46', scaleMargins: { top: 0.05, bottom: 0.05 } },
      timeScale: { borderColor: get('--line-2') || '#3f3f46', timeVisible: true, secondsVisible: false, fixLeftEdge: false, fixRightEdge: false },
      handleWheelMove: true,
      handleScroll: true,
    });
    chartRef.current = chart;

    candleRef.current = chart.addCandlestickSeries({
      upColor: '#22c55e', downColor: '#ef4444',
      borderUpColor: '#22c55e', borderDownColor: '#ef4444',
      wickUpColor: '#22c55e', wickDownColor: '#ef4444',
    });

    vwapRef.current = chart.addLineSeries({
      color: '#3b82f6', lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
    });

    // Click on chart → find nearest trade marker and select it
    chart.subscribeClick(param => {
      if (!param.time || !param.point) return;
      const clickedTime = Number(param.time);
      let best = null, bestDist = 999999;
      trades.forEach(t => {
        const c = candles[t.entryIdx];
        if (!c) return;
        const tSec = _tsec(c.t);
        const d = Math.abs(tSec - clickedTime);
        if (d < bestDist) { bestDist = d; best = t; }
      });
      if (best && bestDist <= 120) onSelectTrade(best); // within 2 bars
    });

    // Auto-resize when container size changes
    const ro = new ResizeObserver(() => {
      if (!containerRef.current || !chartRef.current) return;
      const { width, height } = containerRef.current.getBoundingClientRect();
      chartRef.current.resize(width, Math.max(200, height));
    });
    ro.observe(containerRef.current);

    return () => { ro.disconnect(); chart.remove(); chartRef.current = null; };
  }, []); // eslint-disable-line

  // ── Update candles + VWAP whenever data changes ───────────────────────
  _e(() => {
    if (!candleRef.current || !candles.length) return;
    const visible = candles.slice(0, upToIdx + 1);

    const lwcData = visible.map(c => ({
      time: _tsec(c.t),
      open: c.o, high: c.h, low: c.l, close: c.c,
    }));
    candleRef.current.setData(lwcData);

    // VWAP
    let cv = 0, cc = 0;
    const vwapData = visible.map(c => {
      const tp = (c.h + c.l + c.c) / 3;
      cv += tp * (c.v || 1); cc += (c.v || 1);
      return { time: _tsec(c.t), value: Math.round(cv / cc * 100) / 100 };
    });
    vwapRef.current.setData(vwapData);

    // Trade markers: entry arrow + exit dot label
    const markers = [];
    trades.filter(t => t.entryIdx < visible.length).forEach(t => {
      const ec = candles[t.entryIdx];
      if (!ec) return;
      const pnl = Number(t.pnlPct || 0);
      const isSel = selectedTrade?.id === t.id;
      const color = isSel ? '#f59e0b' : (pnl > 0 ? '#22c55e' : pnl < 0 ? '#ef4444' : '#71717a');
      const isLong = t.dir === 'LONG';
      markers.push({
        time: _tsec(ec.t),
        position: isLong ? 'belowBar' : 'aboveBar',
        color,
        shape: isLong ? 'arrowUp' : 'arrowDown',
        text: `${t.legDir || t.dir}${isSel ? ' ●' : ''}`,
        size: isSel ? 2 : 1,
      });
      // Exit marker if trade is closed and exitIdx is valid
      if (t.exitReason && t.exitIdx != null && t.exitIdx < visible.length) {
        const xc = candles[t.exitIdx];
        if (xc) {
          markers.push({
            time: _tsec(xc.t),
            position: isLong ? 'aboveBar' : 'belowBar',
            color,
            shape: 'circle',
            text: pnl >= 0 ? `+${(pnl*100).toFixed(1)}%` : `${(pnl*100).toFixed(1)}%`,
            size: 1,
          });
        }
      }
    });
    markers.sort((a, b) => a.time - b.time);
    candleRef.current.setMarkers(markers);

    // Current price dashed line
    const lastClose = visible[visible.length - 1]?.c;
    if (lastClose) {
      if (priceLineRef.current) { try { candleRef.current.removePriceLine(priceLineRef.current); } catch(_){} }
      priceLineRef.current = candleRef.current.createPriceLine({ price: lastClose, color: '#f59e0b', lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: true, title: '' });
    }

    // Scroll to latest bar
    chartRef.current?.timeScale().scrollToPosition(3, false);
  }, [candles, upToIdx, trades, selectedTrade]);

  // ── Stop / target lines for selected trade ────────────────────────────
  _e(() => {
    if (!candleRef.current) return;
    if (stopLineRef.current) { try { candleRef.current.removePriceLine(stopLineRef.current); } catch(_){} stopLineRef.current = null; }
    if (tgtLineRef.current)  { try { candleRef.current.removePriceLine(tgtLineRef.current);  } catch(_){} tgtLineRef.current  = null; }
    if (!selectedTrade) return;
    const { stopPx, targetPx } = selectedTrade;
    if (stopPx && stopPx > 0)   stopLineRef.current = candleRef.current.createPriceLine({ price: stopPx,   color: '#ef4444', lineWidth: 1, lineStyle: LWC.LineStyle.Dotted, axisLabelVisible: true, title: 'STP' });
    if (targetPx && targetPx > 0) tgtLineRef.current = candleRef.current.createPriceLine({ price: targetPx, color: '#22c55e', lineWidth: 1, lineStyle: LWC.LineStyle.Dotted, axisLabelVisible: true, title: 'TGT' });
  }, [selectedTrade]);

  if (!LWC) {
    return <div className="t-chart-area" style={{display:'flex',alignItems:'center',justifyContent:'center',color:'var(--fg-4)',fontFamily:'var(--f-mono)',fontSize:11}}>lightweight-charts not loaded</div>;
  }
  return <div className="t-chart-area" ref={containerRef} style={{position:'relative',width:'100%',height:'100%',minHeight:260}} />;
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

// ── Collapsible section header ────────────────────────────────────────────
function CollapseSection({ title, summary, defaultOpen = false, children }) {
  const [open, setOpen] = React.useState(defaultOpen);
  return (
    <div>
      <div
        className="t-section-head"
        onClick={() => setOpen(o => !o)}
        style={{cursor:'pointer', userSelect:'none', display:'flex', justifyContent:'space-between', alignItems:'center'}}
      >
        <span>{title}</span>
        <span style={{display:'flex', alignItems:'center', gap:6}}>
          {!open && summary && <span style={{color:'var(--fg-4)', fontFamily:'var(--f-mono)', fontSize:9, fontWeight:400}}>{summary}</span>}
          <span style={{color:'var(--fg-4)', fontSize:9}}>{open ? '▲' : '▼'}</span>
        </span>
      </div>
      {open && children}
    </div>
  );
}

// ── Decision chain — gate cascade from the decision trace ─────────────────
// Friendly short labels for the engine's gate_ids. Unknown ids fall back to the
// id itself, so this stays correct even as the engine adds gates.
const GATE_LABELS = {
  regime_classification: 'Regime', avoid_veto: 'Avoid', risk_halt: 'Risk halt',
  router_regime_block: 'Router', warmup: 'Warmup', entry_time_windows: 'Time window',
  entry_regime_tag: 'Regime tag', direction_conflict: 'Dir conflict',
  regime_confidence: 'Regime conf', sideways_returns_mixed: 'Sideways mixed',
  stop_loss_cooldown: 'Stop cooldown', direction_flip_cooldown: 'Flip cooldown',
  zero_mfe_cooldown: 'Zero-MFE cool', direction_evidence: 'Dir evidence',
  entry_phase: 'Phase', risk_pause: 'Risk pause', direction_consensus: 'Consensus',
  confidence_gate: 'Confidence', policy_checks: 'Policy', candidate_ranking: 'Ranking',
  strike_depth: 'Strike', execution: 'Execute',
};
function gateLabel(id) {
  return GATE_LABELS[id] || String(id || '?').replace(/_/g, ' ');
}

function DecisionChain({ orderedGates, flowGates, regimeEvidence, proposedDir }) {
  const [openIdx, setOpenIdx] = React.useState(null);

  // Prefer the selected candidate's full ordered_gates; fall back to flow_gates.
  let gates = Array.isArray(orderedGates) && orderedGates.length ? orderedGates
            : Array.isArray(flowGates) ? flowGates : [];
  const hasGates = gates.length > 0;

  // First blocked gate is the decisive one; gates after it were never reached.
  const blockerIdx = gates.findIndex(g => String(g.status || '').toLowerCase() === 'blocked');

  // Regime evidence bull/bear
  const bull = regimeEvidence?.bull_score != null ? Number(regimeEvidence.bull_score) : null;
  const bear = regimeEvidence?.bear_score != null ? Number(regimeEvidence.bear_score) : null;
  const r5m  = regimeEvidence?.r5m  != null ? Number(regimeEvidence.r5m)  : null;
  const r15m = regimeEvidence?.r15m != null ? Number(regimeEvidence.r15m) : null;
  const dir  = String(proposedDir || '').toUpperCase();
  const evidenceOk = dir === 'PE'
    ? (bear != null && bull != null && (bear >= 0.2 || bull <= 0.6))
    : dir === 'CE'
    ? (bull != null && bear != null && (bull >= 0.2 || bear <= 0.6))
    : true;
  const evidenceMismatch = !evidenceOk && bull != null && bear != null;

  const statusColor = (s, reached) => {
    s = String(s || '').toLowerCase();
    if (s === 'pass') return 'var(--pos)';
    if (s === 'blocked') return 'var(--neg)';
    if (s === 'skipped') return 'var(--warn)';
    return reached ? 'var(--fg-3)' : 'var(--fg-4)';
  };
  const statusSym = (s, reached) => {
    s = String(s || '').toLowerCase();
    if (s === 'pass') return '●';
    if (s === 'blocked') return '✗';
    if (s === 'skipped') return '◐';
    return reached ? '○' : '·';
  };

  return (
    <div style={{padding:'0 12px 8px'}}>
      {/* Gate dot row — only gates up to & including the blocker are "reached" */}
      {hasGates && (
        <div style={{display:'flex', gap:3, alignItems:'center', marginBottom:6, flexWrap:'wrap'}}>
          {gates.map((g, i) => {
            const reached = blockerIdx < 0 || i <= blockerIdx;
            const isOpen = openIdx === i;
            const status = reached ? g.status : 'unreached';
            return (
              <span
                key={i}
                title={`${g.gate_id || g.gate_group} · ${g.status}`}
                onClick={() => setOpenIdx(isOpen ? null : i)}
                style={{
                  fontFamily:'var(--f-mono)', fontSize:9.5, color: statusColor(status, reached),
                  cursor:'pointer', padding:'1px 3px', borderRadius:2,
                  background: isOpen ? 'var(--bg-2)' : 'transparent', whiteSpace:'nowrap',
                  opacity: reached ? 1 : 0.5,
                }}
              >
                {statusSym(status, reached)}<span style={{fontSize:8, color: isOpen ? 'var(--fg-2)' : 'var(--fg-4)', marginLeft:2}}>{gateLabel(g.gate_id)}</span>
              </span>
            );
          })}
        </div>
      )}

      {/* Blocker line */}
      {blockerIdx >= 0 && (
        <div style={{fontFamily:'var(--f-mono)', fontSize:9.5, color:'var(--neg)', marginBottom:5, lineHeight:1.4}}>
          ✗ blocked at <strong>{gateLabel(gates[blockerIdx].gate_id)}</strong>
          {gates[blockerIdx].reason_code ? ` · ${gates[blockerIdx].reason_code}` : ''}
          {gates[blockerIdx].message ? <span style={{color:'var(--fg-4)'}}> · {gates[blockerIdx].message}</span> : null}
        </div>
      )}

      {/* Expanded gate detail */}
      {openIdx != null && gates[openIdx] && (() => {
        const g = gates[openIdx];
        const metrics = g.metrics && typeof g.metrics === 'object' ? Object.entries(g.metrics) : [];
        return (
          <div style={{background:'var(--bg-2)', border:'1px solid var(--line-1)', borderRadius:2, padding:'6px 8px', marginBottom:6}}>
            <div style={{fontFamily:'var(--f-mono)', fontSize:9, color:'var(--fg-3)', marginBottom:4, letterSpacing:'0.07em'}}>
              {g.gate_id} · <span style={{color: statusColor(g.status, true)}}>{String(g.status||'').toUpperCase()}</span>
            </div>
            {g.reason_code && <div style={{fontFamily:'var(--f-mono)', fontSize:9.5, color:'var(--fg-2)', marginBottom:3}}>{g.reason_code}</div>}
            {g.message && <div style={{fontFamily:'var(--f-mono)', fontSize:9, color:'var(--fg-3)', marginBottom:4, lineHeight:1.4}}>{g.message}</div>}
            {metrics.length > 0 && (
              <div style={{display:'grid', gridTemplateColumns:'auto 1fr', columnGap:8, rowGap:2}}>
                {metrics.map(([k,v]) => (
                  <React.Fragment key={k}>
                    <span style={{fontFamily:'var(--f-mono)', fontSize:9, color:'var(--fg-4)'}}>{k}</span>
                    <span style={{fontFamily:'var(--f-mono)', fontSize:9, color:'var(--fg-2)'}}>{typeof v === 'number' ? v.toFixed(3) : String(v)}</span>
                  </React.Fragment>
                ))}
              </div>
            )}
          </div>
        );
      })()}

      {/* Regime evidence strip */}
      {(bull != null || bear != null) && (
        <div style={{borderTop:'1px solid var(--line-1)', paddingTop:6, marginTop:2}}>
          <div style={{fontFamily:'var(--f-mono)', fontSize:9, color:'var(--fg-4)', letterSpacing:'0.07em', marginBottom:4}}>REGIME EVIDENCE</div>
          {bull != null && (
            <div style={{marginBottom:3}}>
              <div style={{display:'flex', justifyContent:'space-between', fontFamily:'var(--f-mono)', fontSize:9, color: dir==='CE' ? 'var(--pos)' : 'var(--fg-4)', marginBottom:1}}>
                <span>bull</span><span>{bull.toFixed(2)}</span>
              </div>
              <div style={{height:4, background:'var(--bg-3)', borderRadius:2, position:'relative'}}>
                <div style={{position:'absolute', left:0, top:0, height:'100%', width:Math.min(100, bull/3*100)+'%', background:'var(--pos)', borderRadius:2, opacity:0.7}}/>
              </div>
            </div>
          )}
          {bear != null && (
            <div style={{marginBottom:4}}>
              <div style={{display:'flex', justifyContent:'space-between', fontFamily:'var(--f-mono)', fontSize:9, color: dir==='PE' ? 'var(--neg)' : 'var(--fg-4)', marginBottom:1}}>
                <span>bear</span><span>{bear.toFixed(2)}</span>
              </div>
              <div style={{height:4, background:'var(--bg-3)', borderRadius:2, position:'relative'}}>
                <div style={{position:'absolute', left:0, top:0, height:'100%', width:Math.min(100, bear/3*100)+'%', background:'var(--neg)', borderRadius:2, opacity:0.7}}/>
              </div>
            </div>
          )}
          {dir && (
            <div style={{fontFamily:'var(--f-mono)', fontSize:9, color: evidenceMismatch ? 'var(--neg)' : 'var(--pos)'}}>
              {dir} {evidenceMismatch ? '← ⚠ against evidence' : '← ✓ agrees with evidence'}
            </div>
          )}
          {(r5m != null || r15m != null) && (
            <div style={{marginTop:3, fontFamily:'var(--f-mono)', fontSize:9, color:'var(--fg-4)'}}>
              {r5m  != null && <span style={{marginRight:8}}>r5m {r5m>=0?'+':''}{(r5m*100).toFixed(2)}%</span>}
              {r15m != null && <span>r15m {r15m>=0?'+':''}{(r15m*100).toFixed(2)}%</span>}
            </div>
          )}
        </div>
      )}

      {!hasGates && (bull == null && bear == null) && (
        <div style={{fontFamily:'var(--f-mono)', fontSize:9, color:'var(--fg-4)', marginTop:2}}>
          No decision trace linked for this trade.
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
  // trade_prob is a placeholder (0.5) for deterministic/bypass mode — hide the gate to avoid
  // showing a false FAIL when the engine never evaluated an ML trade gate.
  const _policyMode = String(selectedVoteRaw._entry_policy_mode || '').trim();
  const tradeProb = _policyMode === 'bypass' ? null : _num(sm.trade_prob);
  // In bypass/deterministic mode, recipe_prob and up_prob come only from ML metrics defaults (0.5).
  // Only show them when a real trace value exists — otherwise they're meaningless placeholders.
  const recipeProb   = _num(traceSummary.recipe_prob   ?? (_policyMode === 'bypass' ? null : sm.recipe_prob));
  const recipeMargin = _num(traceSummary.recipe_margin ?? (_policyMode === 'bypass' ? null : sm.recipe_margin));
  const upProb       = _num(traceSummary.direction_up_prob ?? (_policyMode === 'bypass' ? null : sm.up_prob));
  const probs = [
    { v: entryProb,    gate: 0.60, label: 'Entry gate',    kind: 'gate' },
    { v: tradeProb,    gate: 0.55, label: 'Trade gate',    kind: 'gate' },
    { v: recipeProb,               label: 'Recipe prob',   kind: 'neutral' },
    { v: recipeMargin,             label: 'Recipe margin', kind: 'neutral' },
    { v: upProb,                   label: 'Up prob',       kind: 'neutral' },
  ].filter(p => p.v != null && Number.isFinite(Number(p.v)));

  // Direction decision — which leg was chosen and why
  // direction_consensus values are SCORES (summed signal weights, can exceed 1.0), not probabilities.
  // Normalize when total > 1 so the bars display as 0-100% instead of 0-393%.
  const _rawCeScore = _num(selectedVoteRaw.direction_consensus_ce ?? sm.ce_prob);
  const _rawPeScore = _num(selectedVoteRaw.direction_consensus_pe ?? sm.pe_prob);
  const _dirTotal = (_rawCeScore || 0) + (_rawPeScore || 0);
  const ceProb = _dirTotal > 1 ? (_rawCeScore || 0) / _dirTotal : _rawCeScore;
  const peProb = _dirTotal > 1 ? (_rawPeScore || 0) / _dirTotal : _rawPeScore;
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
            <span>·</span><span>{trade.regime || (trade.entryContext?.regimeContext?.regime) || '—'}</span>
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

        {/* Decision chain — gate cascade + regime evidence (always visible) */}
        <div className="t-section-head">Decision chain</div>
        <DecisionChain
          orderedGates={selectedCandidate.ordered_gates}
          flowGates={ctx.flowGates}
          regimeEvidence={(ctx.regimeContext && ctx.regimeContext.evidence) || {}}
          proposedDir={chosenLeg}
        />

        {/* ML probabilities (collapsed) */}
        <CollapseSection
          title="ML probabilities"
          summary={entryProb != null ? `entry ${entryProb.toFixed(2)}` : '—'}
        >
          <div className="t-prob-stack">
            {probs.length > 0 ? (
              probs.map((p, i) => <ProbRow key={i} {...p} />)
            ) : (
              <div style={{color:'var(--fg-4)',fontFamily:'var(--f-mono)',fontSize:9.5,padding:'0 12px 8px'}}>
                No linked probability metrics for this trade.
              </div>
            )}
          </div>
        </CollapseSection>

        {/* Direction decision (collapsed) */}
        <CollapseSection
          title="Direction decision"
          summary={`${chosenLeg}${consensusMargin != null ? ` · margin ${consensusMargin.toFixed(2)}` : ''}`}
        >
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
        </CollapseSection>

        {(policyReason || policyCheckRows.length > 0 || Object.keys(selectedVoteMetrics).length > 0) && (
          <CollapseSection
            title="Policy · Signals"
            summary={selectedVoteMetrics.policy_score != null ? `score ${Number(selectedVoteMetrics.policy_score).toFixed(2)}` : (policyReason ? 'allowed' : '')}
          >
          <div style={{fontFamily:'var(--f-mono)',fontSize:9.5,color:'var(--fg-3)',lineHeight:1.45,padding:'0 12px 8px'}}>
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
          </CollapseSection>
        )}

        {/* Confluence */}
        {confluence.length > 0 && (
          <CollapseSection title="Confluence" summary={`${confluence.length} signals`}>
            <div className="t-confluence-list" style={{padding:'0 12px 8px'}}>
              {confluence.map((c, i) => <span key={i} className="t-confluence-chip">{c}</span>)}
            </div>
          </CollapseSection>
        )}

        {/* Risk envelope */}
        <CollapseSection title="Risk envelope" summary={`stop ${(trade.stopPx||0).toFixed(0)} · tgt ${(trade.targetPx||0).toFixed(0)}`}>
          <RiskEnvelope trade={trade} path={pathPoints} session={session}/>
        </CollapseSection>

        {/* Rationale */}
        <CollapseSection title="Entry · Exit rationale" defaultOpen={true} summary="">
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
        </CollapseSection>

        {/* Counterfactuals */}
        <CollapseSection title="What-if" summary="">
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
        </CollapseSection>
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

// ── useIsMobile — single source of truth for the mobile breakpoint ───────
function _useIsMobile() {
  const mq = '(max-width: 640px)';
  const [isMobile, setIsMobile] = _s(() =>
    typeof window !== 'undefined' && window.matchMedia
      ? window.matchMedia(mq).matches : false);
  _e(() => {
    if (!window.matchMedia) return;
    const m = window.matchMedia(mq);
    const fn = e => setIsMobile(e.matches);
    if (m.addEventListener) m.addEventListener('change', fn);
    else m.addListener(fn);
    return () => {
      if (m.removeEventListener) m.removeEventListener('change', fn);
      else m.removeListener(fn);
    };
  }, []);
  return isMobile;
}

// ── _fmtRelTime — "2s ago" / "12s ago" / "1m ago" ────────────────────────
function _fmtRelTime(ms) {
  if (!ms) return '—';
  const dt = Math.max(0, Date.now() - ms);
  if (dt < 1000) return 'now';
  const s = Math.floor(dt / 1000);
  if (s < 60) return `${s}s ago`;
  const mn = Math.floor(s / 60);
  if (mn < 60) return `${mn}m ago`;
  return `${Math.floor(mn / 60)}h ago`;
}

// ── MobileTradeCard — replaces a Tape row on mobile ──────────────────────
function _parseEntrySignals(rawReason) {
  const r = String(rawReason || '');
  const regimeM = r.match(/\[([A-Z_]+)\]/);
  const regime  = regimeM ? regimeM[1] : null;
  const probM   = r.match(/prob[=\s]*([\d.]+)/i);
  const prob    = probM ? Math.round(Number(probM[1]) * 100) : null;
  const keywords = [];
  if (/\bORB\b/i.test(r))       keywords.push('ORB');
  if (/\bVWAP\b/i.test(r))      keywords.push('VWAP');
  if (/\bBREAKOUT\b/i.test(r))  keywords.push('BRKOUT');
  if (/\bTRAP\b/i.test(r))      keywords.push('TRAP');
  if (/\bIV\b/i.test(r))        keywords.push('IV');
  if (/\bMOMENTUM\b/i.test(r))  keywords.push('MOM');
  return { regime, prob, keywords };
}

function MobileTradeCard({ trade, selected, onSelect, session }) {
  const t = trade;
  const pnl = t.pnlPct || 0;
  const pnlCls = pnl >= 0 ? 'pos' : 'neg';
  const glyph = pnl >= 0 ? '▲' : '▼';
  const exitReason = (t.exitReason || '').toUpperCase();
  const exitCls =
    exitReason === 'TARGET_HIT' || exitReason === 'TARGET'   ? 'target' :
    exitReason === 'STOP_HIT'   || exitReason === 'STOP'     ? 'stop'   :
    exitReason === 'HARD_STOP'                                ? 'stop'   :
    exitReason.includes('TIME')                               ? 'time'   : '';
  const dir = t.dir || 'LONG';
  const legDir = t.legDir || dir;
  const entryLabel = _barLabel(session, t.entryIdx);
  const exitLabel  = _barLabel(session, t.exitIdx);
  const { regime, prob, keywords } = _parseEntrySignals(t.reason || t.entryDetail || '');
  const regimeColor =
    regime === 'BREAKOUT' ? 'var(--pos)' :
    regime === 'VOLATILE' ? 'var(--warn)' :
    regime === 'SIDEWAYS' ? 'var(--fg-3)' :
    regime === 'BEARISH'  ? 'var(--neg)'  : 'var(--fg-4)';
  return (
    <button
      type="button"
      className="m-card"
      aria-pressed={selected ? 'true' : 'false'}
      onClick={() => onSelect(t)}>
      <div className="m-card-head">
        <span className={`m-card-dir ${dir}`}>{legDir}{t.optionType && legDir !== t.optionType ? ` ${t.optionType}` : ''}</span>
        <span className="strat">{t.strat || '—'}</span>
        {prob != null && <span style={{fontFamily:'var(--f-mono)',fontSize:9,color:'var(--fg-3)',marginLeft:'auto'}}>conf {prob}%</span>}
      </div>
      {/* signal pills row */}
      {(regime || keywords.length > 0) && (
        <div style={{display:'flex',gap:4,flexWrap:'wrap',marginBottom:3}}>
          {regime && <span style={{fontSize:8.5,fontFamily:'var(--f-mono)',padding:'1px 5px',borderRadius:3,border:`1px solid ${regimeColor}`,color:regimeColor,letterSpacing:'0.05em'}}>{regime}</span>}
          {keywords.map(k => (
            <span key={k} style={{fontSize:8.5,fontFamily:'var(--f-mono)',padding:'1px 5px',borderRadius:3,background:'rgba(255,255,255,0.06)',color:'var(--fg-3)'}}>{k}</span>
          ))}
        </div>
      )}
      <div className={`m-card-pnl ${pnlCls}`}>
        <span className="glyph">{glyph}</span>
        <span>{TC.fmtPct(pnl, 2)}</span>
      </div>
      <div className="m-card-prices">
        <span><span className="lbl">in</span>{(t.entryPx || 0).toFixed(2)}</span>
        <span className="arr">→</span>
        <span><span className="lbl">out</span>{(t.exitPx || 0).toFixed(2)}</span>
        {t.strike && <span><span className="lbl">strike</span>{Math.round(t.strike)}{t.optionType ? ` ${t.optionType}` : ''}</span>}
      </div>
      <div className="m-card-foot">
        <span className="time">{entryLabel} → {exitLabel}</span>
        {exitReason && <span className={`reason ${exitCls}`}>{exitReason.replace(/_/g, ' ')}</span>}
      </div>
    </button>
  );
}

// ── MobileBottomSheet — generic bottom-anchored sheet ────────────────────
function MobileBottomSheet({ open, title, onClose, children }) {
  _e(() => {
    if (!open) return;
    const fn = e => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', fn);
    return () => window.removeEventListener('keydown', fn);
  }, [open, onClose]);
  return (
    <div className={`m-sheet-overlay ${open ? 'open' : ''}`}
         onClick={e => { if (e.target.classList.contains('m-sheet-overlay')) onClose(); }}>
      <div className={`m-sheet ${open ? 'open' : ''}`}>
        <div className="m-sheet-grab"/>
        <div className="m-sheet-head">
          <span className="m-sheet-title">{title}</span>
          <button className="m-sheet-close" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <div className="m-sheet-body">{children}</div>
      </div>
    </div>
  );
}

// ── MobileLiveShell — phone-first layout for LiveMonitorDark ─────────────
function MobileLiveShell({
  session, candles, upToIdx, flashIdx, flashId,
  trades, signals, strategies,
  quote, sessionPnl, winRate, regime, engine,
  brainData, wsStatus, watchMode, watchRunId, simRuns,
  runtimeConfig, availableModels, blockerFunnel, timeline, heatmap, openPosition,
  selectedTrade, onSelectTrade,
  onHaltClick, onModeSwitch, onBackToLive, onWatchChange,
}) {
  const [tab, setTab] = _s('tape');   // tape | chart | map | more
  const [sheet, setSheet] = _s(null); // null | 'ops'
  const [lastTickAt, setLastTickAt] = _s(Date.now());
  const [now, setNow] = _s(Date.now());
  const inspectorTrade = selectedTrade || (trades.length ? trades[0] : null);
  const [showInspector, setShowInspector] = _s(false);
  const isMobile = _useIsMobile();

  _e(() => { setLastTickAt(Date.now()); }, [upToIdx]);
  _e(() => { const id = setInterval(() => setNow(Date.now()), 2000); return () => clearInterval(id); }, []);

  const ageMs = now - lastTickAt;
  let orbCls = 'ok', orbLbl = 'LIVE';
  if (wsStatus !== 'connected') { orbCls = 'crit'; orbLbl = wsStatus.toUpperCase(); }
  else if (ageMs > 90000) { orbCls = 'crit'; orbLbl = 'STALE'; }
  else if (ageMs > 70000) { orbCls = 'warn'; orbLbl = 'SLOW'; }

  const absPnl = Math.abs(sessionPnl);
  const stress = absPnl >= 0.02 ? 'extreme' : absPnl >= 0.01 ? 'high' : 'normal';
  const pnlCls = sessionPnl > 0.0005 ? 'pos' : sessionPnl < -0.0005 ? 'neg' : 'flat';
  const pnlGlyph = sessionPnl > 0.0005 ? '▲' : sessionPnl < -0.0005 ? '▼' : '◆';

  // Pull-to-refresh
  const ptrRef = _r({ startY: 0, dragging: false, refreshing: false });
  const [ptrState, setPtrState] = _s('idle');
  const onTouchStart = _cb(e => {
    if (window.scrollY > 0) return;
    ptrRef.current.startY = e.touches[0].clientY;
    ptrRef.current.dragging = true;
  }, []);
  const onTouchMove = _cb(e => {
    if (!ptrRef.current.dragging) return;
    const dy = e.touches[0].clientY - ptrRef.current.startY;
    if (dy > 60 && ptrState !== 'pulling') setPtrState('pulling');
    else if (dy <= 0 && ptrState !== 'idle') setPtrState('idle');
  }, [ptrState]);
  const onTouchEnd = _cb(() => {
    if (!ptrRef.current.dragging) return;
    ptrRef.current.dragging = false;
    if (ptrState === 'pulling' && !ptrRef.current.refreshing) {
      ptrRef.current.refreshing = true;
      setPtrState('refreshing');
      setTimeout(() => { ptrRef.current.refreshing = false; setPtrState('idle'); }, 700);
    } else { setPtrState('idle'); }
  }, [ptrState]);

  const tradeCount  = trades.length;
  const signalCount = signals.length;
  const rc  = runtimeConfig || {};
  const bf  = blockerFunnel || {};
  const tradesDone = (bf.outcomes?.entry_taken || 0) + (bf.outcomes?.executed || 0);
  const maxTrades  = rc.risk_max_session_trades ?? null;
  // Last entry prob from decision timeline (most recent minute)
  const lastProb = (() => {
    const rows = timeline?.decisions;
    if (!Array.isArray(rows) || !rows.length) return null;
    const ep = rows[0]?.metrics?.entry_prob;
    return ep != null ? Math.round(Number(ep) * 100) : null;
  })();

  return (
    <div className="m-shell">
      {/* ── Sticky header ────────────────────────────────────────────────── */}
      <header className="m-header" role="banner">
        <div className="m-header-row1">
          <div className="m-brand"><span className="m-brand-mark"/>QUANT</div>
          <div className="m-symbol">
            <span className="sym">{quote?.symbol || session.instrument}</span>
            <span className="px">{quote ? quote.spot.toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2}) : '—'}</span>
            {quote && (
              <span className={`chg ${quote.spotChg >= 0 ? 'pos' : 'neg'}`}>
                {quote.spotChg >= 0 ? '+' : ''}{quote.spotChgPct.toFixed(2)}%
              </span>
            )}
          </div>
          <div className="m-orb" title={`feed ${orbLbl} · ${_fmtRelTime(lastTickAt)}`}>
            <span className={`m-orb-dot ${orbCls}`}/>
            <span className="m-orb-fresh">{_fmtRelTime(lastTickAt)}</span>
          </div>
        </div>
        <div className="m-header-row2">
          <div className="m-pnl" data-stress={stress}>
            <span className={`val ${pnlCls}`}>
              <span className="glyph">{pnlGlyph}</span> {TC.fmtPct(sessionPnl, 2)}
            </span>
            <div className="meta">
              <span className="k">trades</span>
              <span className="v">{tradeCount}{maxTrades != null ? ` / ${maxTrades}` : ''} · {winRate}%</span>
            </div>
          </div>
          <div className="m-header-desk-cells">
            <div className="cell"><span className="k">Regime</span><span className="v">{regime||'—'}</span></div>
            <div className="cell"><span className="k">Engine</span><span className="v">{engine||'—'}</span></div>
            {lastProb != null
              ? <div className="cell"><span className="k">ep</span>
                  <span className={`v ${lastProb >= 80 ? 'pos' : lastProb >= 65 ? 'warn' : 'neg'}`}>{lastProb}%</span></div>
              : <div className="cell"><span className="k">Feed</span>
                  <span className={`v ${orbCls==='ok'?'pos':orbCls==='warn'?'warn':'neg'}`}>{orbLbl}</span></div>
            }
          </div>
          <button className="m-iconbtn" aria-label="Settings" onClick={() => setSheet('ops')}>⚙</button>
          <button className="m-iconbtn danger" aria-label="Halt" onClick={onHaltClick}>⏻</button>
        </div>
        {/* Open position banner — only shown when a position is live */}
        {openPosition && (() => {
          const op = openPosition;
          const opPnl = op.pnl_pct != null ? Number(op.pnl_pct) : null;
          const opMfe = op.mfe_pct != null ? Number(op.mfe_pct) : null;
          const opCls = opPnl != null && opPnl > 0 ? 'pos' : opPnl != null && opPnl < 0 ? 'neg' : 'flat';
          return (
            <div style={{
              background: 'rgba(255,160,0,0.10)', borderBottom: '1px solid rgba(255,160,0,0.25)',
              padding: '6px 14px', display: 'flex', alignItems: 'center', gap: 10,
              fontFamily: 'var(--f-mono)', fontSize: 10,
            }}>
              <span style={{width:7,height:7,borderRadius:'50%',background:'var(--warn)',display:'inline-block',flexShrink:0,animation:'pulse 1.2s infinite'}}/>
              <span style={{color:'var(--warn)',fontWeight:700,letterSpacing:'0.06em'}}>OPEN</span>
              <span style={{color:'var(--fg-2)',fontWeight:600}}>
                {op.option_type || op.direction} {op.strike ? Math.round(op.strike) : ''}
              </span>
              {opPnl != null && <span className={opCls} style={{fontWeight:700}}>{opPnl >= 0 ? '+' : ''}{(opPnl*100).toFixed(2)}%</span>}
              {opMfe != null && <span style={{color:'var(--fg-4)'}}>mfe {opMfe >= 0 ? '+' : ''}{(opMfe*100).toFixed(2)}%</span>}
              {op.bars_held != null && <span style={{color:'var(--fg-4)'}}>{op.bars_held}b</span>}
              <span style={{color:'var(--fg-4)',marginLeft:'auto'}}>{(op.entry_time||'').slice(11,16)}</span>
            </div>
          );
        })()}
      </header>

      {/* ── Body ─────────────────────────────────────────────────────────── */}
      <div className="m-split">
        <div className="m-left">
          <nav className="m-tabs" role="tablist">
            <button className="m-tab" data-tab="tape" aria-pressed={tab==='tape'} onClick={() => setTab('tape')}>
              Tape <span className="count">{tradeCount}</span>
            </button>
            <button className="m-tab" data-tab="chart" aria-pressed={tab==='chart'} onClick={() => setTab('chart')}>Chart</button>
            <button className="m-tab" data-tab="map"   aria-pressed={tab==='map'}   onClick={() => setTab('map')}>Map</button>
            <button className="m-tab" data-tab="more"  aria-pressed={tab==='more'}  onClick={() => setTab('more')}>More</button>
          </nav>
          <main className="m-body" onTouchStart={onTouchStart} onTouchMove={onTouchMove} onTouchEnd={onTouchEnd}>
            <div className={`m-ptr ${ptrState !== 'idle' ? ptrState : ''}`}>
              {ptrState === 'pulling' ? 'Release to refresh' : ptrState === 'refreshing' ? 'Refreshing…' : ''}
            </div>

          {/* ── Tape tab ──────────────────────────────────────────────────── */}
          <div className={tab === 'tape' ? '' : 'm-tab-hidden'}>
            {/* Gate funnel strip — visible when no trades yet or always */}
            {bf.outcomes && (() => {
              const o = bf.outcomes;
              const topGate = bf.primary_blocker_gates?.[0]?.gate;
              const taken = o.entry_taken || 0;
              const blocked = o.blocked || 0;
              const hold = o.hold || 0;
              return (
                <div style={{
                  display:'flex', alignItems:'center', gap:10, padding:'6px 12px 4px',
                  fontFamily:'var(--f-mono)', fontSize:9.5, borderBottom:'1px solid rgba(255,255,255,0.06)',
                  flexWrap:'wrap',
                }}>
                  {taken > 0  && <span><span style={{color:'var(--pos)',fontWeight:700}}>{taken}</span> <span style={{color:'var(--fg-4)'}}>in</span></span>}
                  {blocked > 0 && <span><span style={{color:'var(--neg)',fontWeight:700}}>{blocked}</span> <span style={{color:'var(--fg-4)'}}>blocked</span></span>}
                  {hold > 0   && <span><span style={{color:'var(--warn)',fontWeight:700}}>{hold}</span> <span style={{color:'var(--fg-4)'}}>hold</span></span>}
                  {topGate && <span style={{color:'var(--fg-4)',marginLeft:'auto',fontSize:9}}>⊘ {topGate}</span>}
                </div>
              );
            })()}
            {tradeCount === 0
              ? <div className="m-empty">
                  No trades yet today.
                  <div className="hint">{signalCount > 0 ? `${signalCount} signal${signalCount===1?'':'s'} considered` : 'Waiting for signals…'}</div>
                </div>
              : <div className="m-cards">
                  {trades.map(t => (
                    <MobileTradeCard key={t.id} trade={t} session={session}
                      selected={inspectorTrade?.id === t.id && showInspector}
                      onSelect={tr => { onSelectTrade(tr); setShowInspector(true); }}/>
                  ))}
                </div>
            }
          </div>

          {/* ── Chart tab — phone only ─────────────────────────────────────── */}
          {isMobile && (
            <div className={`m-chart-tab-pane ${tab === 'chart' ? '' : 'm-tab-hidden'}`}>
              <div className="m-chart-wrap">
                <TermChart session={session} candles={candles} trades={trades}
                  selectedTrade={inspectorTrade} onSelectTrade={tr => { onSelectTrade(tr); setShowInspector(true); }}
                  upToIdx={upToIdx} flashIdx={flashIdx}/>
              </div>
            </div>
          )}

          {/* ── Map tab ───────────────────────────────────────────────────── */}
          <div className={tab === 'map' ? '' : 'm-tab-hidden'} style={{height:'100%',overflowY:'auto'}}>
            <SessionHeatmap data={heatmap?.data} loading={!heatmap}/>
          </div>

          {/* ── More tab ──────────────────────────────────────────────────── */}
          <div className={tab === 'more' ? '' : 'm-tab-hidden'}>

            {/* Brain context — first thing a trader checks in the morning */}
            <div className="m-section">
              <div className="m-section-head">Brain</div>
              <div className="m-section-body">
                {brainData?.available
                  ? <div className="m-kv-list">
                      <div className="m-kv"><span className="k">day_score</span>
                        <span className={`v ${
                          String(brainData.day_score||'').toUpperCase()==='CALM'?'pos':
                          String(brainData.day_score||'').toUpperCase()==='AVOID'?'neg':
                          String(brainData.day_score||'').toUpperCase()==='VOLATILE'?'warn':''
                        }`}>{String(brainData.day_score||'—').toUpperCase()}</span></div>
                      <div className="m-kv"><span className="k">confidence</span>
                        <span className="v">{brainData.day_score_confidence!=null?(Number(brainData.day_score_confidence)*100).toFixed(0)+'%':'—'}</span></div>
                      <div className="m-kv"><span className="k">size mult</span>
                        <span className={`v ${brainData.size_multiplier!=null&&Number(brainData.size_multiplier)<1?'warn':'pos'}`}>
                          {brainData.size_multiplier!=null?Number(brainData.size_multiplier).toFixed(2)+'×':'—'}</span></div>
                      <div className="m-kv"><span className="k">carry</span>
                        <span className="v">{brainData.carry_consecutive_losses||0} L · {brainData.losing_streak_days||0}d</span></div>
                    </div>
                  : <div className="m-empty" style={{padding:'10px 4px'}}>Brain unavailable.</div>
                }
              </div>
            </div>

            {/* Session snapshot */}
            <div className="m-section">
              <div className="m-section-head">Session</div>
              <div className="m-section-body">
                <div className="m-kv-list">
                  <div className="m-kv"><span className="k">P&amp;L</span><span className={`v ${pnlCls}`}>{TC.fmtPct(sessionPnl,2)}</span></div>
                  <div className="m-kv"><span className="k">trades</span>
                    <span className="v">{tradeCount}{maxTrades!=null?` / ${maxTrades}`:''} · {winRate}%</span></div>
                  <div className="m-kv"><span className="k">regime</span><span className="v">{regime||'—'}</span></div>
                  <div className="m-kv"><span className="k">feed</span>
                    <span className={`v ${orbCls==='ok'?'pos':orbCls==='warn'?'warn':'neg'}`}>{orbLbl} · {_fmtRelTime(lastTickAt)}</span></div>
                  {lastProb != null && <div className="m-kv"><span className="k">last ep</span>
                    <span className={`v ${lastProb>=80?'pos':lastProb>=65?'warn':'neg'}`}>{lastProb}%</span></div>}
                </div>
              </div>
            </div>

            {/* System config */}
            {(() => {
              const mdl = Array.isArray(availableModels) ? availableModels.find(m => m.is_current) : null;
              const exitMode = rc.exit_strategy_mode || '—';
              const conf     = rc.min_confidence != null ? `${(rc.min_confidence*100).toFixed(0)}%` : '—';
              const profile  = rc.strategy_profile_id || '—';
              const stage    = rc.rollout_stage || '—';
              const dot = ok => <span style={{display:'inline-block',width:7,height:7,borderRadius:'50%',background:ok?'var(--pos)':'var(--neg)',marginRight:5,verticalAlign:'middle'}}/>;
              const row = (label, val, ok) => (
                <div className="m-kv" key={label}>
                  <span className="k" style={{color:'var(--fg-3)'}}>{ok!=null&&dot(ok)}{label}</span>
                  <span className="v" style={{fontFamily:'var(--f-mono)',fontSize:11,color:'var(--fg-1)'}}>{val}</span>
                </div>
              );
              const exitColor = exitMode==='lottery'?'var(--warn)':exitMode==='scalper'?'var(--fg-2)':'var(--fg-4)';
              return (
                <div className="m-section">
                  <div className="m-section-head">System</div>
                  <div className="m-section-body"><div className="m-kv-list">
                    {row('Engine',    rc.engine || engine || '—',  !!(rc.engine||engine))}
                    {row('Profile',   <span style={{fontSize:10,color:'var(--fg-3)'}}>{profile}</span>, profile!=='—')}
                    {row('Stage',     stage,  stage==='live')}
                    {row('Exit mode', <span style={{color:exitColor,fontWeight:600,textTransform:'uppercase'}}>{exitMode}</span>, null)}
                    {row('Confidence', conf, conf!=='—')}
                    {rc.smart_strike_enabled!=null && row('Smart strike', rc.smart_strike_enabled?'on':'off', rc.smart_strike_enabled)}
                    {mdl && row('Model AUC', `${mdl.holdout_auc?.toFixed(3)||'?'} · ${mdl.feature_count??'?'}f`, true)}
                  </div></div>
                </div>
              );
            })()}

            {/* Gate funnel detail */}
            {bf.outcomes && (() => {
              const o = bf.outcomes;
              const gates = Array.isArray(bf.primary_blocker_gates) ? bf.primary_blocker_gates.slice(0,3) : [];
              return (
                <div className="m-section">
                  <div className="m-section-head">Gate Funnel</div>
                  <div className="m-section-body"><div className="m-kv-list">
                    {(o.entry_taken||0)>0 && <div className="m-kv"><span className="k">entries taken</span><span className="v pos">{o.entry_taken}</span></div>}
                    {(o.blocked||0)>0    && <div className="m-kv"><span className="k">blocked</span><span className="v neg">{o.blocked}</span></div>}
                    {(o.hold||0)>0       && <div className="m-kv"><span className="k">hold</span><span className="v warn">{o.hold}</span></div>}
                    {gates.map((g,i) => (
                      <div className="m-kv" key={i}>
                        <span className="k" style={{fontSize:9}}>{i===0?'top gate':`gate ${i+1}`}</span>
                        <span className="v" style={{fontSize:9,color:'var(--neg)'}}>{g.gate} <span style={{color:'var(--fg-4)'}}>×{g.count}</span></span>
                      </div>
                    ))}
                  </div></div>
                </div>
              );
            })()}

            {/* Strategy roster */}
            {strategies.length > 0 && (
              <div className="m-section">
                <div className="m-section-head">Strategy Roster</div>
                <div className="m-section-body"><div className="m-kv-list">
                  {strategies.map(s => (
                    <div key={s.id} className="m-kv">
                      <span className="k">{s.name} · {s.trades}t · {s.wr}%</span>
                      <span className={`v ${s.pnl>=0?'pos':'neg'}`}>{TC.fmtPct(s.pnl,2)}</span>
                    </div>
                  ))}
                </div></div>
              </div>
            )}

            <div className="m-actions">
              <button className="m-action" onClick={() => setSheet('ops')}>
                <span className="label">Mode &amp; Ops</span>
                <span className="sub">Replay · Eval · Pipeline</span>
              </button>
              <button className="m-action danger" onClick={onHaltClick}>
                <span className="label">⏻  Halt engine</span>
                <span className="sub">requires confirmation</span>
              </button>
            </div>
          </div>

        </main>
        </div>{/* end m-left */}

        {/* Right pane: chart on desktop only — phone chart tab handles mobile (never both) */}
        {!isMobile && (
          <div className="m-right-pane">
            <div className="m-right-chart">
              <TermChart
                session={session} candles={candles} trades={trades}
                selectedTrade={inspectorTrade} onSelectTrade={tr => { onSelectTrade(tr); setShowInspector(true); }}
                upToIdx={upToIdx} flashIdx={flashIdx}
              />
            </div>
            {/* Inspector panel — slides in when a trade is selected */}
            {showInspector && inspectorTrade && (
              <div className="m-right-inspector">
                <div className="m-right-inspector-head">
                  <span style={{fontFamily:'var(--f-mono)',fontSize:10,color:'var(--fg-3)',letterSpacing:'0.12em',textTransform:'uppercase'}}>
                    Trade Inspector
                  </span>
                  <button className="m-sheet-close" onClick={() => setShowInspector(false)}>✕</button>
                </div>
                <div style={{flex:1,overflowY:'auto',minHeight:0}}>
                  <TradeInspector session={session} trade={inspectorTrade}/>
                </div>
              </div>
            )}
          </div>
        )}

      </div>{/* end m-split */}

      {/* ── Inspector bottom sheet — phone only; desktop uses right-pane panel */}
      {isMobile && (
        <MobileBottomSheet
          open={showInspector && !!inspectorTrade}
          title={`Trade · ${inspectorTrade?.id || ''}`}
          onClose={() => setShowInspector(false)}>
          {inspectorTrade && (
            <TradeInspector session={session} trade={inspectorTrade}/>
          )}
        </MobileBottomSheet>
      )}

      {/* ── Ops / mode-switch bottom sheet ───────────────────────────────── */}
      <MobileBottomSheet
        open={sheet === 'ops'}
        title="Settings"
        onClose={() => setSheet(null)}>
        <div className="m-sheet-row" style={{display:'block'}}>
          <div className="m-sheet-label">Mode</div>
          <div className="m-sheet-modes">
            <button className="m-sheet-mode active">● Live</button>
            <button className="m-sheet-mode" onClick={() => { setSheet(null); onModeSwitch('replay'); }}>◷ Replay</button>
            <button className="m-sheet-mode" onClick={() => { setSheet(null); onModeSwitch('eval'); }}>
              ⚗ Eval <span className="sub">desktop</span>
            </button>
            <button className="m-sheet-mode" onClick={() => { setSheet(null); onModeSwitch('pipeline'); }}>
              ⬡ Pipeline <span className="sub">desktop</span>
            </button>
          </div>
        </div>
        {(simRuns && simRuns.length > 0) && (
          <div className="m-sheet-row" style={{display:'block'}}>
            <div className="m-sheet-label">Watching</div>
            <select
              className="inp"
              style={{width:'100%', height:44, marginTop:6, fontSize:13}}
              value={watchMode === 'sim' ? `sim:${watchRunId || ''}` : 'live'}
              onChange={e => {
                const raw = String(e.target.value || '');
                if (raw === 'live') { onBackToLive(); return; }
                if (raw.startsWith('sim:')) onWatchChange(raw.slice(4));
              }}>
              <option value="live">LIVE</option>
              {simRuns.map(run => (
                <option key={run.run_id} value={`sim:${run.run_id}`}>
                  SIM · {String(run.label || 'run').slice(0, 16)} · {String(run.run_id || '').slice(0, 8)}…
                </option>
              ))}
            </select>
          </div>
        )}
        <div className="m-sheet-row" style={{display:'block', marginTop:8}}>
          <div className="m-sheet-label">Diagnostics</div>
          <div style={{display:'grid', gap:4, marginTop:6, fontFamily:'var(--f-mono)', fontSize:11, color:'var(--fg-3)'}}>
            <div>WebSocket: <span style={{color: wsStatus === 'connected' ? 'var(--pos)' : 'var(--warn)'}}>{wsStatus}</span></div>
            <div>Last tick: {_fmtRelTime(lastTickAt)}</div>
            <div>Engine: {engine || '—'}</div>
          </div>
        </div>
      </MobileBottomSheet>
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
  const [openPosition, setOpenPosition] = _s(null);
  const [brainData, setBrainData] = _s(null);
  const [blockerFunnel, setBlockerFunnel] = _s(null);
  const [timeline, setTimeline] = _s(null);
  const [heatmapData, setHeatmapData] = _s(null);
  const [simRunsToday, setSimRunsToday] = _s([]);
  const [watchMode, setWatchMode] = _s('live');
  const [watchRunId, setWatchRunId] = _s('');
  const [watchDate, setWatchDate] = _s('');
  const wsRef         = _r(null);
  const sessionRef    = _r(null);
  const prevIdxRef    = _r(null);
  _e(() => { sessionRef.current = session; }, [session]);
  const _todayIST = () => {
    const now = new Date();
    const ist = new Date(now.getTime() + (now.getTimezoneOffset() + 330) * 60000);
    return ist.toISOString().slice(0, 10);
  };

  // State: runtime config + available models + open position. Refresh every 30s.
  _e(() => {
    let alive = true;
    const kindQ = watchMode === 'sim' ? `&kind=sim${watchRunId ? `&run_id=${encodeURIComponent(watchRunId)}` : ''}` : '';
    const modeQ = watchMode === 'sim' ? 'replay' : 'live';
    const load = () => fetch(`/api/strategy/current/state?mode=${modeQ}&latest_n=0${kindQ}`)
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`state HTTP ${r.status}`)))
      .then(s => {
        if (!alive) return;
        setRuntimeConfig(s?.runtime_config || null);
        setAvailableModels(Array.isArray(s?.available_models) ? s.available_models : []);
        setOpenPosition(s?.open_position || null);
      })
      .catch(() => {});
    load();
    const id = setInterval(load, 30000);
    return () => { alive = false; clearInterval(id); };
  }, [watchMode, watchRunId]);

  // Live brain status — initial fetch + refresh every 30s.
  _e(() => {
    let alive = true;
    const modeQ = watchMode === 'sim' ? 'replay' : 'live';
    const kindQ = watchMode === 'sim' ? `&kind=sim${watchRunId ? `&run_id=${encodeURIComponent(watchRunId)}` : ''}` : '';
    const load = () => fetch(`/api/strategy/brain/status?mode=${modeQ}${kindQ}`)
      .then(r => r.ok ? r.json() : Promise.resolve({ available: false, reason: `HTTP ${r.status}` }))
      .catch(() => ({ available: false, reason: 'fetch failed' }))
      .then(b => { if (alive) setBrainData(b); });
    load();
    const id = setInterval(load, 30000);
    return () => { alive = false; clearInterval(id); };
  }, [watchMode, watchRunId]);

  // Live blocker funnel + decision timeline for today (IST). Refresh every 30s.
  _e(() => {
    let alive = true;
    const load = () => {
      const date = watchMode === 'sim' ? (watchDate || _todayIST()) : _todayIST();
      const modeQ = watchMode === 'sim' ? 'replay' : 'live';
      const kindQ = watchMode === 'sim' ? `&kind=sim${watchRunId ? `&run_id=${encodeURIComponent(watchRunId)}` : ''}` : '';
      const funnelP = fetch(`/api/strategy/blocker-funnel?mode=${modeQ}&date=${date}${kindQ}`)
        .then(r => r.ok ? r.json() : null).catch(() => null);
      const tlP = fetch(`/api/strategy/decisions?mode=${modeQ}&date=${date}&limit=500${kindQ}`)
        .then(r => r.ok ? r.json() : null).catch(() => null);
      Promise.all([funnelP, tlP]).then(([f, t]) => {
        if (!alive) return;
        setBlockerFunnel(f);
        setTimeline(t);
      });
    };
    load();
    const id = setInterval(load, 30000);
    return () => { alive = false; clearInterval(id); };
  }, [watchMode, watchRunId, watchDate]);

  // Session heatmap for today. Refresh every 60s.
  _e(() => {
    let alive = true;
    const load = () => {
      const date = watchMode === 'sim' ? (watchDate || _todayIST()) : _todayIST();
      const modeQ = watchMode === 'sim' ? 'replay' : 'live';
      fetch(`/api/strategy/session-heatmap?mode=${modeQ}&date=${date}`)
        .then(r => r.ok ? r.json() : null).catch(() => null)
        .then(d => { if (alive) setHeatmapData(d); });
    };
    load();
    const id = setInterval(load, 60000);
    return () => { alive = false; clearInterval(id); };
  }, [watchMode, watchRunId, watchDate]);

  _e(() => {
    let alive = true;
    const load = () => fetch(`/api/sim/runs?date=${encodeURIComponent(_todayIST())}&limit=50`)
      .then(r => (r.ok ? r.json() : Promise.resolve({ rows: [] })))
      .catch(() => ({ rows: [] }))
      .then(payload => {
        if (!alive) return;
        const rows = Array.isArray(payload?.rows) ? payload.rows : (Array.isArray(payload?.runs) ? payload.runs : []);
        setSimRunsToday(rows);
      });
    load();
    const id = setInterval(load, 30000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  _e(() => {
    const ws = TC.makeMonitorWS(
      () => {
        const payload = { action: 'subscribe', mode: watchMode === 'sim' ? 'sim' : 'live' };
        if (watchMode === 'sim') {
          if (watchRunId) payload.run_id = watchRunId;
          if (watchDate) payload.date = watchDate;
        }
        return payload;
      },
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
  }, [watchMode, watchRunId, watchDate]);

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
  const _latestSignal  = (session.signals || []).reduce((a, s) => (!a || s.idx > a.idx ? s : a), null);
  const regime         = session.regime || _latestSignal?.regime || (trades[0]?.regime) || '—';
  const engine         = runtimeConfig?.engine || runtimeConfig?.model_type || session.engine || '—';

  // Default to latest trade in inspector
  const displayTrade   = selectedTrade || (trades.length > 0 ? trades[0] : null);

  return (
    <MobileLiveShell
      session={session} candles={candles} upToIdx={upToIdx}
      flashIdx={flashIdx} flashId={flashId}
      trades={trades} signals={signals} strategies={strategies}
      quote={quote} sessionPnl={sessionPnl} winRate={winRate}
      regime={regime} engine={engine}
      brainData={brainData} wsStatus={wsStatus}
      runtimeConfig={runtimeConfig} availableModels={availableModels}
      openPosition={openPosition}
      blockerFunnel={blockerFunnel} timeline={timeline}
      heatmap={heatmapData ? { data: heatmapData } : null}
      watchMode={watchMode} watchRunId={watchRunId} simRuns={simRunsToday}
      selectedTrade={displayTrade} onSelectTrade={setSelectedTrade}
      onHaltClick={onKillClick} onModeSwitch={onModeSwitch}
      onBackToLive={() => { setWatchMode('live'); setWatchRunId(''); setWatchDate(''); }}
      onWatchChange={(runId) => {
        const row = (simRunsToday || []).find(r => String(r?.run_id || '') === String(runId || ''));
        setWatchMode('sim');
        setWatchRunId(String(runId || '').trim());
        setWatchDate(String(row?.source_date || row?.trade_date || row?.date || '').slice(0, 10));
      }}
    />
  );
}

// ── MobileReplayShell ─────────────────────────────────────────────────────
// Uses the same m-shell layout as live. Replay-specific header: date/run picker
// in row-1 and play/pause + scrub bar in row-2. Body (tape/chart/map/more) is
// shared infrastructure — same MobileTradeCard, TermChart, TradeInspector.
// Handles both loading (session=null) and loaded states in one component.
function MobileReplayShell({
  session, candles, upToIdx, trades, signals,
  sessionPnl, winRate,
  selectedTrade, onSelectTrade,
  blockerFunnel, heatmap,
  isPlaying, speed, total, vtLabel,
  replayDate, replayRunId, replayKind,
  replayOptions, tradeCounts, modelsByDate, datesLoading, wsStatus, replayError,
  onPlay, onPause, onSpeed, onScrub, onScrubEnd, onReset, onDateChange, onModeSwitch, onRefreshRuns,
}) {
  const [tab, setTab] = _s('tape');
  const isMobile = _useIsMobile();
  const [showInspector, setShowInspector] = _s(false);
  const inspectorTrade = selectedTrade || ((trades||[]).length ? trades[0] : null);
  const pct      = total > 0 ? ((upToIdx + 1) / total * 100).toFixed(0) : 0;
  const pnlCls   = (sessionPnl||0) > 0.0005 ? 'pos' : (sessionPnl||0) < -0.0005 ? 'neg' : 'flat';
  const pnlGlyph = (sessionPnl||0) > 0.0005 ? '▲' : (sessionPnl||0) < -0.0005 ? '▼' : '◆';
  const tradeCount = (trades||[]).length;
  const selStyle = { fontFamily:'var(--f-mono)', fontSize:10, background:'var(--bg-2)',
    color:'var(--fg-1)', border:'1px solid var(--line-2)', borderRadius:'var(--r-2)',
    padding:'0 6px', height:24, flex:1, minWidth:0 };

  // ── Two-level run picker: filter by date, then choose the run on that date.
  // With many sim runs accumulating, a single flat dropdown is hectic — group
  // the options by date (newest first) so the operator picks a date, then the
  // specific run. Run dropdown is only shown when a date has more than one run.
  // Default the kind filter to the loaded run's kind (usually 'sim' from the
  // boot pick) so the picker lands on sim runs without manual toggling.
  const [kindFilter, setKindFilter] = _s(replayKind === 'oos' ? 'oos' : 'sim');
  const filteredOptions = (replayOptions||[]).filter(o => kindFilter==='all' || o.kind===kindFilter);
  const dateGroups = (() => {
    const m = new Map();
    filteredOptions.forEach(o => {
      if (!o.date) return;
      if (!m.has(o.date)) m.set(o.date, { date:o.date, runs:[], kinds:new Set() });
      const g = m.get(o.date); g.runs.push(o); g.kinds.add(o.kind);
    });
    return [...m.values()].sort((a,b) => String(b.date).localeCompare(String(a.date)));
  })();
  const [filterDate, setFilterDate] = _s(replayDate || '');
  _e(() => { if (replayDate) setFilterDate(replayDate); }, [replayDate]);
  const activeDate = filterDate || replayDate || '';
  const runsForDate = filteredOptions.filter(o => o.date === activeDate);
  const _dateLabel = g => {
    const kinds = [...g.kinds];
    const tag = kinds.length === 1 ? kinds[0] : kinds.join('/');
    return `${g.date} · ${g.runs.length} ${tag}`;
  };
  const _loadDate = d => {
    setFilterDate(d);
    const runs = filteredOptions.filter(o => o.date === d);
    // Prefer a sim run that has data, else the first run on that date.
    const pick = runs.find(r => r.kind==='sim') || runs[0];
    if (pick) onDateChange(pick.date, { runId: pick.runId||'', kind: pick.kind||'oos' });
  };
  const _setKind = k => {
    setKindFilter(k);
    const opts = (replayOptions||[]).filter(o => k==='all' || o.kind===k);
    // Keep the current date if it still has runs under the new kind; else jump
    // to the newest date available for that kind and load it.
    if (opts.some(o => o.date === activeDate)) return;
    const newest = opts.map(o => o.date).filter(Boolean)
      .sort((a,b) => String(b).localeCompare(String(a)))[0];
    if (newest) {
      setFilterDate(newest);
      const runs = opts.filter(o => o.date === newest);
      const pick = runs.find(r => r.kind==='sim') || runs[0];
      if (pick) onDateChange(pick.date, { runId: pick.runId||'', kind: pick.kind||'oos' });
    }
  };

  return (
    <div className="m-shell">
      {/* ── Header ─────────────────────────────────────────────────────── */}
      <header className="m-header">
        {/* Row 1: brand + run picker + orb + back-to-Live */}
        <div className="m-header-row1">
          <div className="m-brand">
            <span className="m-brand-mark"/>QUANT
            <span style={{fontSize:9,opacity:0.55,marginLeft:4,color:'var(--info)'}}>◷ REPLAY</span>
          </div>
          <div style={{display:'flex',gap:6,flex:1,minWidth:0,alignItems:'center'}}>
            {/* Kind filter — Sim / OOS / All (segmented) */}
            <div style={{display:'flex',flexShrink:0,border:'1px solid var(--line-2)',borderRadius:'var(--r-2)',overflow:'hidden'}}>
              {['sim','oos','all'].map(k => (
                <button key={k} onClick={() => _setKind(k)}
                  style={{border:0,background:kindFilter===k?'var(--accent)':'transparent',
                    color:kindFilter===k?'#1a1100':'var(--fg-3)',fontFamily:'var(--f-mono)',
                    fontSize:9.5,letterSpacing:'0.04em',padding:'0 7px',height:24,cursor:'pointer',
                    textTransform:'uppercase'}}>
                  {k}
                </button>
              ))}
            </div>
            {/* Date dropdown — distinct dates newest-first */}
            <select style={{...selStyle, flex:'0 0 auto', maxWidth:150}}
              value={activeDate}
              disabled={datesLoading || !dateGroups.length}
              onChange={e => _loadDate(e.target.value)}>
              {!activeDate && <option value="">{datesLoading ? 'Loading…' : 'Date'}</option>}
              {dateGroups.map(g => <option key={g.date} value={g.date}>{_dateLabel(g)}</option>)}
            </select>
            {/* Run dropdown — only when the chosen date has more than one run */}
            {runsForDate.length > 1 && (
              <select style={selStyle}
                value={`${replayKind}:${replayDate}:${replayRunId||''}`}
                onChange={e => {
                  const next = runsForDate.find(r => r.key === e.target.value);
                  if (next) onDateChange(next.date, { runId: next.runId||'', kind: next.kind||'oos' });
                }}>
                {runsForDate.map(opt => (
                  <option key={opt.key} value={opt.key}>{_fmtReplayRunOption(opt, tradeCounts, modelsByDate)}</option>
                ))}
              </select>
            )}
            {/* Refresh — re-fetch the run list so sims run after opening Replay appear */}
            {onRefreshRuns && (
              <button title="Refresh run list" onClick={onRefreshRuns}
                style={{flexShrink:0,width:24,height:24,borderRadius:'var(--r-2)',
                  border:'1px solid var(--line-2)',background:'transparent',color:'var(--fg-3)',
                  cursor:'pointer',fontSize:12,lineHeight:1,display:'grid',placeItems:'center'}}>⟳</button>
            )}
          </div>
          <div style={{display:'flex',alignItems:'center',gap:8,flexShrink:0}}>
            <div className="m-orb" title={`ws: ${wsStatus}`}>
              <span className={`m-orb-dot ${wsStatus==='connected'?'ok':wsStatus==='connecting'?'warn':'crit'}`}/>
            </div>
            <button style={{height:28,padding:'0 12px',borderRadius:'var(--r-2)',border:'1px solid var(--line-2)',
              background:'transparent',color:'var(--fg-2)',fontFamily:'var(--f-mono)',fontSize:10.5,
              cursor:'pointer',display:'flex',alignItems:'center',gap:5}}
              onClick={() => onModeSwitch('live')}>
              <span style={{width:7,height:7,borderRadius:'50%',background:'var(--pos)',display:'inline-block'}}/>Live
            </button>
          </div>
        </div>

        {/* Row 2: loading status (no session) or scrub + play controls */}
        {!session ? (
          <div className="m-header-row2" style={{justifyContent:'center'}}>
            <span style={{fontFamily:'var(--f-mono)',fontSize:10.5,color:'var(--fg-3)'}}>
              {datesLoading ? '⏳ Loading dates…'
                : wsStatus==='connecting' ? '⟳ Connecting…'
                : wsStatus==='disconnected' ? '⟳ Reconnecting…'
                : replayError ? '✗ ' + replayError
                : 'Select a run above to start replay.'}
            </span>
          </div>
        ) : (
          <div className="m-header-row2" style={{display:'flex',alignItems:'center',gap:8}}>
            <div className="m-pnl" data-stress="normal">
              <span className={`val ${pnlCls}`}><span className="glyph">{pnlGlyph}</span>{TC.fmtPct(sessionPnl||0,2)}</span>
              <div className="meta"><span className="k">trades</span><span className="v">{tradeCount} · {winRate||0}%</span></div>
            </div>
            {isPlaying
              ? <button className="m-iconbtn" style={{flexShrink:0}} onClick={onPause} title="Pause (Space)">⏸</button>
              : <button className="m-iconbtn" style={{flexShrink:0,color:'var(--pos)'}} onClick={onPlay} title="Play (Space)">▶</button>
            }
            <input type="range" min={0} max={Math.max(0,total-1)} value={upToIdx}
              style={{flex:1,minWidth:0,accentColor:'var(--accent)',cursor:'pointer'}}
              onChange={e => onScrub(+e.target.value)}
              onMouseUp={e => onScrubEnd(+e.target.value)}
              onTouchEnd={() => onScrubEnd(upToIdx)}
            />
            <span style={{fontFamily:'var(--f-mono)',fontSize:10,color:'var(--fg-3)',whiteSpace:'nowrap',minWidth:62,flexShrink:0}}>
              {vtLabel||'—'} · {pct}%
            </span>
            <select value={speed} onChange={e => onSpeed(+e.target.value)}
              style={{fontFamily:'var(--f-mono)',fontSize:10,background:'var(--bg-2)',color:'var(--fg-1)',
                border:'1px solid var(--line-2)',borderRadius:'var(--r-2)',padding:'0 4px',height:24,width:42,flexShrink:0}}>
              {[1,2,4,8,16].map(s => <option key={s} value={s}>{s}×</option>)}
            </select>
            <button className="m-iconbtn" style={{flexShrink:0}} onClick={onReset} title="Reset to start">⟲</button>
          </div>
        )}
      </header>

      {/* ── Body ───────────────────────────────────────────────────────── */}
      {!session ? (
        <div style={{flex:1,display:'flex',alignItems:'center',justifyContent:'center',
          flexDirection:'column',gap:8,color:'var(--fg-4)',fontFamily:'var(--f-mono)',fontSize:11}}>
          {replayError
            ? <><span style={{color:'var(--neg)'}}>✗ {replayError}</span>
                <span style={{fontSize:9,marginTop:4,color:'var(--fg-4)'}}>Try a different run or trigger a sim from the ⚙ OPS drawer first.</span></>
            : <span>{datesLoading ? 'Loading…' : wsStatus}</span>}
        </div>
      ) : (
        <div className="m-split">
          <div className="m-left">
            <nav className="m-tabs" role="tablist">
              <button className="m-tab" data-tab="tape" aria-pressed={tab==='tape'} onClick={() => setTab('tape')}>
                Tape <span className="count">{tradeCount}</span>
              </button>
              <button className="m-tab" data-tab="chart" aria-pressed={tab==='chart'} onClick={() => setTab('chart')}>Chart</button>
              <button className="m-tab" data-tab="map"   aria-pressed={tab==='map'}   onClick={() => setTab('map')}>Map</button>
              <button className="m-tab" data-tab="more"  aria-pressed={tab==='more'}  onClick={() => setTab('more')}>More</button>
            </nav>
            <main className="m-body">
              <div className={tab==='tape' ? '' : 'm-tab-hidden'}>
                {tradeCount === 0
                  ? <div className="m-empty">No trades at this point.<div className="hint">Scrub forward or press ▶ to play</div></div>
                  : <div className="m-cards">
                      {(trades||[]).map(t => (
                        <MobileTradeCard key={t.id} trade={t} session={session}
                          selected={inspectorTrade?.id===t.id && showInspector}
                          onSelect={tr => { onSelectTrade(tr); setShowInspector(true); }}/>
                      ))}
                    </div>}
              </div>
              {isMobile && (
                <div className={`m-chart-tab-pane ${tab==='chart' ? '' : 'm-tab-hidden'}`}>
                  <div className="m-chart-wrap">
                    <TermChart session={session} candles={candles||[]} trades={trades||[]}
                      selectedTrade={inspectorTrade}
                      onSelectTrade={tr => { onSelectTrade(tr); setShowInspector(true); }}
                      upToIdx={upToIdx} flashIdx={null}/>
                  </div>
                </div>
              )}
              <div className={tab==='map' ? '' : 'm-tab-hidden'} style={{height:'100%',overflowY:'auto'}}>
                <SessionHeatmap data={heatmap?.data} loading={!heatmap}/>
              </div>
              <div className={tab==='more' ? '' : 'm-tab-hidden'}>
                <div className="m-section">
                  <div className="m-section-head">Session</div>
                  <div className="m-section-body">
                    <div className="m-kv-list">
                      <div className="m-kv"><span className="k">date</span><span className="v">{replayDate||session?.date||'—'}</span></div>
                      <div className="m-kv"><span className="k">P&amp;L</span><span className={`v ${pnlCls}`}>{TC.fmtPct(sessionPnl||0,2)}</span></div>
                      <div className="m-kv"><span className="k">trades</span><span className="v">{tradeCount} · {winRate||0}%</span></div>
                      <div className="m-kv"><span className="k">progress</span><span className="v">{upToIdx+1}/{total} bars · {pct}%</span></div>
                      {session?.instrument && <div className="m-kv"><span className="k">instrument</span><span className="v">{session.instrument}</span></div>}
                    </div>
                  </div>
                </div>
                {blockerFunnel?.outcomes && (() => {
                  const o = blockerFunnel.outcomes;
                  const top = blockerFunnel.primary_blocker_gates?.[0]?.gate;
                  return (
                    <div className="m-section">
                      <div className="m-section-head">Decision Funnel</div>
                      <div className="m-section-body">
                        <div className="m-kv-list">
                          <div className="m-kv"><span className="k">evaluated</span><span className="v">{o.evaluated||0}</span></div>
                          <div className="m-kv"><span className="k">taken</span><span className="v pos">{o.entry_taken||0}</span></div>
                          <div className="m-kv"><span className="k">blocked</span><span className="v neg">{o.blocked||0}</span></div>
                          {top && <div className="m-kv"><span className="k">top gate</span><span className="v" style={{fontSize:9}}>{top}</span></div>}
                        </div>
                      </div>
                    </div>
                  );
                })()}
              </div>
            </main>
          </div>
          {!isMobile && (
            <div className="m-right-pane">
              <div className="m-right-chart">
                <TermChart session={session} candles={candles||[]} trades={trades||[]}
                  selectedTrade={inspectorTrade}
                  onSelectTrade={tr => { onSelectTrade(tr); setShowInspector(true); }}
                  upToIdx={upToIdx} flashIdx={null}/>
              </div>
              {showInspector && inspectorTrade && (
                <div className="m-right-inspector">
                  <TradeInspector session={session} trade={inspectorTrade}/>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {isMobile && showInspector && inspectorTrade && (
        <div className="m-sheet-overlay" onClick={() => setShowInspector(false)}>
          <div className="m-sheet" onClick={e => e.stopPropagation()}>
            <button style={{position:'absolute',top:8,right:12,background:'none',border:'none',
              color:'var(--fg-3)',fontSize:18,cursor:'pointer',lineHeight:1}}
              onClick={() => setShowInspector(false)}>×</button>
            <TradeInspector session={session} trade={inspectorTrade}/>
          </div>
        </div>
      )}
    </div>
  );
}

// Legacy helpers — kept so diff is minimal; no longer referenced after ReplayMonitorDark migration
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

function _fmtReplayRunOption(opt, tradeCounts, modelsByDate) {
  if (!opt) return '';
  const kind = String(opt.kind || 'oos').toLowerCase() === 'sim' ? 'sim' : 'oos';
  const date = String(opt.date || '').slice(0, 10);
  const runId = String(opt.runId || '').trim();
  const label = String(opt.label || '').trim();
  if (kind === 'sim') {
    const t   = opt.time ? `${opt.time}` : (runId ? runId.slice(0, 8) : '');
    const trd = (opt.nTrades != null && opt.nTrades !== '') ? ` · ${opt.nTrades}t` : '';
    const lbl = label ? ` · ${label}` : '';
    // date is already shown by the date dropdown; lead with time so the newest run is obvious
    return `${t}${trd}${lbl}`;
  }
  return `[OOS] ${_fmtDateOption(date, tradeCounts, modelsByDate)}`;
}

function ReplayStatusBar({ sessionPnl, tradesCount, winRate, isPlaying, speed, upToIdx,
  total, replayOptions, tradeCounts, modelsByDate, replayDate, replayRunId, replayKind, onPlay, onPause, onSpeed, onScrub, onScrubEnd,
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
        <button onClick={() => onModeSwitch('pipeline')} style={{opacity:0.85}}>⬡ Pipeline</button>
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
        <select
          style={selStyle}
          value={`${replayKind}:${replayDate}:${replayRunId || ''}`}
          disabled={datesLoading || !replayOptions.length}
          onChange={e => {
            const next = (replayOptions || []).find(r => r.key === e.target.value);
            if (!next) return;
            onDateChange(next.date, { runId: next.runId || '', kind: next.kind || 'oos' });
          }}
        >
          {(replayOptions || []).map(opt => (
            <option key={opt.key} value={opt.key}>
              {_fmtReplayRunOption(opt, tradeCounts, modelsByDate)}
            </option>
          ))}
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
      kind: String(p.get('kind') || '').trim().toLowerCase(),
    };
  } catch (_) {
    return { date: '', runId: '', kind: '' };
  }
}

function _syncReplayUrl({ date, runId, kind }) {
  try {
    const url = new URL(window.location.href);
    url.searchParams.set('mode', 'replay');
    if (date) url.searchParams.set('date', date);
    else url.searchParams.delete('date');
    if (runId) url.searchParams.set('run_id', runId);
    else url.searchParams.delete('run_id');
    if (kind) url.searchParams.set('kind', kind);
    else url.searchParams.delete('kind');
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
  const [replayKind,     setReplayKind]     = _s((_boot.kind === 'sim' ? 'sim' : 'oos'));
  const [replayError,    setReplayError]    = _s('');
  const [wsStatus,       setWsStatus]       = _s('idle');
  const [availableDates, setAvailableDates] = _s([]);
  const [oosRuns, setOosRuns] = _s([]);
  const [simRuns, setSimRuns] = _s([]);
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
  const replayKindRef = _r(_boot.kind === 'sim' ? 'sim' : 'oos');
  const fitRef        = _r(null);

  // Diagnostics: load current model + available models, blocker-funnel aggregate
  // for the selected date, AND a per-minute decisions timeline so the operator
  // can scroll snapshot-by-snapshot to see exactly why each minute was blocked.
  // Re-fires whenever replayDate or the timeline outcome filter changes.
  const fetchDiag = _cb(() => {
    const date = replayDateRef.current;
    setDiagLoading(true); setDiagError('');
    const kindQ = replayKindRef.current === 'sim' ? '&kind=sim' : '&kind=oos';
    const stateP = fetch(`/api/strategy/current/state?mode=replay&latest_n=0${kindQ}`)
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`state HTTP ${r.status}`)));
    const runQ = replayRunIdRef.current
      ? `&run_id=${encodeURIComponent(replayRunIdRef.current)}`
      : '';
    const funnelP = date
      ? fetch(`/api/strategy/blocker-funnel?mode=replay${kindQ}&date=${encodeURIComponent(date)}${runQ}`)
          .then(r => r.ok ? r.json() : Promise.reject(new Error(`funnel HTTP ${r.status}`)))
      : Promise.resolve(null);
    const tlOutcomeQ = tlOutcome ? `&outcome=${encodeURIComponent(tlOutcome)}` : '';
    const tlCollapseQ = tlCollapse ? `&collapse=true` : '';
    const tlP = date
      ? fetch(`/api/strategy/decisions?mode=replay${kindQ}&date=${encodeURIComponent(date)}&limit=500${runQ}${tlOutcomeQ}${tlCollapseQ}`)
          .then(r => r.ok ? r.json() : Promise.reject(new Error(`decisions HTTP ${r.status}`)))
      : Promise.resolve(null);
    const brainP = fetch(`/api/strategy/brain/status?mode=replay${kindQ}`)
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
    const kindQ = replayKind === 'sim' ? '&kind=sim' : '&kind=oos';
    fetch(`/api/strategy/session-heatmap?mode=replay${kindQ}&date=${encodeURIComponent(replayDate)}`)
      .then(r => r.ok ? r.json() : Promise.resolve(null))
      .then(d => { setHeatmapData(d || null); setHeatmapLoading(false); })
      .catch(() => { setHeatmapData(null); setHeatmapLoading(false); });
  }, [replayDate]);

  _e(() => { upToIdxRef.current    = upToIdx;    }, [upToIdx]);
  _e(() => { speedRef.current      = speed;      }, [speed]);
  _e(() => { replayDateRef.current = replayDate; }, [replayDate]);
  _e(() => { replayRunIdRef.current = replayRunId; }, [replayRunId]);
  _e(() => { replayKindRef.current = replayKind; }, [replayKind]);

  function openReplaySocket() {
    if (wsRef.current) return;
    setWsStatus('connecting');
    wsRef.current = TC.makeMonitorWS(
      () => {
        const sub = {
          action:'subscribe', mode:'replay', date:replayDateRef.current,
          up_to_idx:upToIdxRef.current, playing:false, speed:speedRef.current,
        };
        sub.kind = replayKindRef.current === 'sim' ? 'sim' : 'oos';
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

  // Fetch the run lists (OOS dates, eval runs, sim runs). autoPick=true on first
  // load picks the newest sim run; the ⟳ refresh button calls it with false so
  // sims run after Replay was opened show up without disturbing the loaded run.
  const loadRunLists = _cb((autoPick) => {
    setDatesLoading(true);
    Promise.all([
      fetch('/api/historical/replay/dates?limit=250')
        .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))),
      fetch('/api/strategy/evaluation/runs?dataset=historical&limit=250')
        .then(r => (r.ok ? r.json() : Promise.resolve({ rows: [] })))
        .catch(() => ({ rows: [] })),
      fetch('/api/sim/runs?limit=120')
        .then(r => (r.ok ? r.json() : Promise.resolve({ rows: [] })))
        .catch(() => ({ rows: [] })),
    ])
      .then(([payload, oosPayload, simPayload]) => {
        setAvailableDates(Array.isArray(payload.dates) ? payload.dates : []);
        setTradeCounts(payload.trade_counts || {});
        setModelsByDate(payload.models_by_date || {});
        setOosRuns(Array.isArray(oosPayload?.rows) ? oosPayload.rows : (Array.isArray(oosPayload?.runs) ? oosPayload.runs : []));
        setSimRuns(Array.isArray(simPayload?.rows) ? simPayload.rows : (Array.isArray(simPayload?.runs) ? simPayload.runs : []));
        setDatesLoading(false);
        if (!autoPick) return;   // refresh-only: leave the loaded run as-is
        const boot = _replayBootParams();
        // Respect an explicit date in the URL (deep-links). Otherwise prefer the
        // newest SIM run with data over the stale OOS holdout date (2024-10-31).
        let pick = boot.date;
        let pickKind = boot.kind === 'sim' ? 'sim' : (boot.kind === 'oos' ? 'oos' : '');
        let pickRunId = boot.runId || replayRunIdRef.current;
        if (!pick) {
          const simRows = Array.isArray(simPayload?.rows) ? simPayload.rows : [];
          const latestSim = simRows.find(r => ((r?.metadata?.collection_counts?.snapshots) || 0) > 0) || simRows[0];
          if (latestSim && (latestSim.source_date || latestSim.date)) {
            pick = latestSim.source_date || latestSim.date;
            pickKind = 'sim';
            pickRunId = latestSim.run_id || '';
          } else {
            pick = payload.latest;
            pickKind = pickKind || 'oos';
          }
        }
        if (pick) handleDateChange(pick, { runId: pickRunId, kind: pickKind || 'oos' });
        else setReplayError('No replay dates found in historical snapshots.');
      })
      .catch(err => { setDatesLoading(false); setReplayError('Failed to load dates: ' + err.message); });
  }, []);

  _e(() => { loadRunLists(true); }, []);

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
    const nextKind = opts.kind === 'sim' ? 'sim' : (opts.kind === 'oos' ? 'oos' : replayKindRef.current);
    setReplayDate(newDate); replayDateRef.current = newDate;
    setReplayRunId(nextRunId); replayRunIdRef.current = nextRunId;
    setReplayKind(nextKind); replayKindRef.current = nextKind;
    _syncReplayUrl({ date: newDate, runId: nextRunId, kind: nextKind });
    setReplayError(''); setIsPlaying(false);
    setSession(null); setUpToIdx(0);
    openReplaySocket();
    const sub = {
      action:'subscribe', mode:'replay', date:newDate, up_to_idx:0, playing:false, speed:speedRef.current,
    };
    sub.kind = nextKind;
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

  const replayOptions = (() => {
    const out = [];
    const seen = new Set();
    (oosRuns || []).forEach(r => {
      const rid = String(r?.run_id || '').trim();
      if (!rid) return;
      const d = String(r?.date_from || r?.date || r?.ended_at || r?.submitted_at || '').slice(0, 10);
      const key = `oos:${d}:${rid}`;
      if (seen.has(key)) return;
      seen.add(key);
      out.push({ key, kind: 'oos', date: d, runId: rid, label: String(r?.message || '').trim() });
    });
    (availableDates || []).slice().reverse().forEach(d => {
      const rid = modelsByDate && modelsByDate[d] ? String(modelsByDate[d].run_id || '').trim() : '';
      const key = `oos:${d}:${rid}`;
      if (seen.has(key)) return;
      seen.add(key);
      out.push({ key, kind: 'oos', date: d, runId: rid, label: '' });
    });
    (simRuns || []).forEach(r => {
      const rid = String(r?.run_id || '').trim();
      if (!rid) return;
      const d = String(r?.source_date || r?.trade_date || r?.date || r?.created_at || '').slice(0, 10);
      const key = `sim:${d}:${rid}`;
      if (seen.has(key)) return;
      seen.add(key);
      // Carry submitted time + trade count so multiple same-date runs are
      // distinguishable (newest first) in the run dropdown.
      const sub = String(r?.submitted_at || r?.created_at || '');
      const time = sub.length >= 16 ? sub.slice(11, 16) : '';
      const cc = r?.metadata?.collection_counts || {};
      const nTrades = Math.floor((cc.positions || 0) / 2) || cc.signals || 0;
      out.push({ key, kind: 'sim', date: d, runId: rid, label: String(r?.label || '').trim(),
        time, nTrades });
    });
    return out;
  })();

  // Compute visible data (works whether session is null or loaded)
  const candles    = session ? (session.candles || []) : [];
  const rawTrades  = session ? (session.trades || []).filter(t => t.exitIdx <= upToIdx) : [];
  const trades     = rawTrades.map(_bridgeTrade);
  const signals    = session ? (session.signals || []).filter(s => s.idx <= upToIdx).slice(-120).reverse() : [];
  const sessionPnl = rawTrades.reduce((a,t) => a + (t.pnlPct||0), 0);
  const wins       = rawTrades.filter(t => (t.pnlPct||0) > 0).length;
  const winRate    = rawTrades.length ? Math.round(wins/rawTrades.length*100) : 0;
  const vtLabel    = candles[upToIdx]?.label ||
    new Date(candles[upToIdx]?.t||0).toLocaleTimeString('en-IN',
      { hour:'2-digit', minute:'2-digit', hour12:false, timeZone:'Asia/Kolkata' });

  return (
    <MobileReplayShell
      session={session} candles={candles} upToIdx={upToIdx}
      trades={trades} signals={signals}
      sessionPnl={sessionPnl} winRate={winRate}
      selectedTrade={selectedTrade} onSelectTrade={setSelectedTrade}
      blockerFunnel={blockerFunnel}
      heatmap={{ data: heatmapData, loading: heatmapLoading }}
      isPlaying={isPlaying} speed={speed} total={candles.length} vtLabel={vtLabel}
      replayDate={replayDate} replayRunId={replayRunId || (session?.runId||'')} replayKind={replayKind}
      replayOptions={replayOptions} tradeCounts={tradeCounts} modelsByDate={modelsByDate}
      datesLoading={datesLoading} wsStatus={wsStatus} replayError={replayError}
      onPlay={handlePlay} onPause={handlePause} onSpeed={handleSpeed}
      onScrub={handleScrub} onScrubEnd={handleScrubEnd} onReset={handleReset}
      onDateChange={handleDateChange} onModeSwitch={onModeSwitch}
      onRefreshRuns={() => loadRunLists(false)}
    />
  );
}

Object.assign(window, { LiveMonitorDark, ReplayMonitorDark });
