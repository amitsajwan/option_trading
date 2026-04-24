// Strategy Monitor views: shared layout + WS-backed Live and Replay monitors.
/* global React, TradingCore, KpiStrip, PriceChart, TradeTable, DecisionGrid, DecisionDetail, AlertList, StrategyBars */
const { useState: _useState, useEffect: _useEffect, useMemo: _useMemo, useRef: _useRef } = React;

// ── Shared layout ─────────────────────────────────────────────────────────
// Renders the full monitor shell. Data comes in via props; no fetching here.
function StrategyMonitor(props) {
  const {
    mode, session, upToIdx, trades, signals, alerts,
    onSelectTrade, onSelectSignal, selectedTrade, selectedSignal,
    livePrice, liveIdx, flashTradeId, kpiItems,
    topBar, leftControls, statusBit,
  } = props;

  const [detailExpanded, setDetailExpanded] = _useState(true);
  const [tab, setTab] = _useState('trades');
  const strategyRows = _useMemo(() => TradingCore.strategyContribution(trades), [trades]);
  const activeSignal =
    selectedSignal ? selectedSignal :
    selectedTrade  ? selectedTrade.signal :
    signals[signals.length - 1] || null;

  return (
    <div className={`monitor-shell mode-${mode}`}>
      <div className="page-head">
        <div style={{ minWidth: 0 }}>
          <div className="page-crumbs">Operator · {mode === 'live' ? 'Market hours' : 'Research'}</div>
          <h1 className="page-title">
            {mode === 'live' ? 'Live Strategy Monitor' : 'Historical Replay Monitor'}
            {mode === 'live' && <span className="chip pos" style={{ marginLeft: 10, verticalAlign: 'middle' }}><span className="dot"></span>LIVE</span>}
            {mode === 'replay' && <span className="chip info" style={{ marginLeft: 10, verticalAlign: 'middle' }}><span className="dot"></span>REPLAY</span>}
          </h1>
          <p className="page-sub">
            {session.instrument} · <span className="mono">{session.date}</span>
            {statusBit}
          </p>
        </div>
        <div className="head-right">{topBar}</div>
      </div>

      {leftControls && <div className="page-actions">{leftControls}</div>}

      <KpiStrip items={kpiItems} cols={kpiItems.length} />

      <div className="g-chart-side">
        <div className="panel">
          <div className="panel-head">
            <div className="row gap-m" style={{ minWidth: 0 }}>
              <div className="panel-title">
                {session.instrument} <span className="muted" style={{ fontWeight: 400 }}>· 1m futures</span>
              </div>
              <span className="count">{Math.min(upToIdx + 1, session.candles.length)} / {session.candles.length} bars</span>
            </div>
            <div className="row gap-s">
              <span className="muted tiny">Last</span>
              <span className="mono" style={{ fontWeight: 600 }}>
                {(livePrice != null ? livePrice : session.candles[upToIdx]?.c || 0).toFixed(2)}
              </span>
            </div>
          </div>
          <div className="panel-body flush" style={{ padding: '6px 4px 0' }}>
            <PriceChart
              candles={session.candles}
              upToIdx={upToIdx}
              trades={trades}
              selectedTradeId={selectedTrade ? selectedTrade.id : null}
              onSelectTrade={onSelectTrade}
              height={340}
              liveIdx={mode === 'live' ? liveIdx : null}
            />
          </div>
        </div>

        <div className="panel decision-panel">
          <div className="panel-head">
            <div className="panel-title">Decision</div>
            <span className="count">{signals.length} signals</span>
          </div>
          <DecisionDetail sig={activeSignal} expanded={detailExpanded} setExpanded={setDetailExpanded} />
        </div>
      </div>

      <div className="mobile-tabs">
        {[
          { k: 'trades',  label: 'Trades',      n: trades.length },
          { k: 'signals', label: 'Signals',     n: signals.length },
          { k: 'perf',    label: 'Strategy P&L' },
        ].map(t => (
          <button key={t.k} className={`tab ${tab === t.k ? 'active' : ''}`} onClick={() => setTab(t.k)}>
            {t.label} {t.n != null && <span className="n">{t.n}</span>}
          </button>
        ))}
      </div>

      <div className="g-tables">
        <div className={`panel ${tab !== 'trades' ? 'mobile-hide' : ''}`}>
          <div className="panel-head">
            <div className="panel-title">Trades today <span className="count">{trades.length}</span></div>
            <div className="row gap-s mobile-hide"><button className="btn sm ghost">Export CSV</button></div>
          </div>
          <div className="panel-body flush" style={{ maxHeight: 340, overflow: 'auto' }}>
            <TradeTable trades={trades} selectedId={selectedTrade?.id} onSelect={onSelectTrade} flashId={flashTradeId} />
          </div>
        </div>

        <div className={`panel ${tab !== 'signals' ? 'mobile-hide' : ''}`}>
          <div className="panel-head">
            <div className="panel-title">Signal stream <span className="count">{signals.length}</span></div>
            <div className="row gap-s mobile-hide">
              <span className="mono tiny muted">entry_prob · trade_prob · recipe_prob</span>
            </div>
          </div>
          <div className="panel-body flush" style={{ maxHeight: 340, overflow: 'auto' }}>
            <DecisionGrid signals={signals} selectedId={selectedSignal} onSelect={onSelectSignal} />
          </div>
        </div>
      </div>

      <div className="g-foot">
        <div className={`panel ${tab !== 'perf' && tab !== 'trades' ? 'mobile-hide' : ''}`}>
          <div className="panel-head">
            <div className="panel-title">Strategy P&amp;L contribution</div>
            <span className="count">{strategyRows.length} strategies</span>
          </div>
          <div className="panel-body"><StrategyBars rows={strategyRows} /></div>
        </div>
        <div className={`panel ${tab !== 'perf' ? 'mobile-hide' : ''}`}>
          <div className="panel-head">
            <div className="panel-title">Alerts &amp; notes</div>
            <span className="count">{alerts.length}</span>
          </div>
          <div className="panel-body" style={{ padding: '6px 4px' }}>
            <AlertList alerts={alerts} />
          </div>
        </div>
      </div>
    </div>
  );
}

// ── LIVE VIEW ─────────────────────────────────────────────────────────────
function LiveMonitor({ onModeSwitch, onKillClick }) {
  const [session,       setSession]       = _useState(null);
  const [upToIdx,       setUpToIdx]       = _useState(0);
  const [liveIdx,       setLiveIdx]       = _useState(0);
  const [livePrice,     setLivePrice]     = _useState(null);
  const [wsStatus,      setWsStatus]      = _useState('connecting');
  const [isPlaying,     setIsPlaying]     = _useState(true);
  const [selectedTrade, setSelectedTrade] = _useState(null);
  const [selectedSignal,setSelectedSignal]= _useState(null);
  const [flashTradeId,  setFlashTradeId]  = _useState(null);
  const wsRef        = _useRef(null);
  const isPlayingRef = _useRef(true);
  const sessionRef   = _useRef(null);
  const prevIdxRef   = _useRef(null);

  _useEffect(() => { isPlayingRef.current = isPlaying; }, [isPlaying]);
  _useEffect(() => { sessionRef.current   = session;   }, [session]);

  _useEffect(() => {
    const ws = TradingCore.makeMonitorWS(
      () => ({ action: 'subscribe', mode: 'live' }),
      {
        onStatus: setWsStatus,
        onMessage(msg) {
          if (msg.type === 'snapshot') {
            setSession(msg.session);
            setUpToIdx(msg.up_to_idx);
            setLiveIdx(msg.live_idx ?? msg.up_to_idx);
            setLivePrice(msg.live_price ?? null);
            prevIdxRef.current = msg.up_to_idx;
          } else if (msg.type === 'tick') {
            if (!isPlayingRef.current) return;
            const newIdx     = msg.up_to_idx;
            const newLiveIdx = msg.live_idx ?? newIdx;
            const sess       = sessionRef.current;
            if (sess && prevIdxRef.current !== null && newIdx > prevIdxRef.current) {
              const prev   = prevIdxRef.current;
              const filled = sess.trades.find(tr => tr.exitIdx > prev && tr.exitIdx <= newIdx);
              if (filled) {
                setFlashTradeId(filled.id);
                setTimeout(() => setFlashTradeId(null), 1200);
              }
            }
            prevIdxRef.current = newIdx;
            setUpToIdx(newIdx);
            setLiveIdx(newLiveIdx);
            if (msg.live_price != null) setLivePrice(msg.live_price);
          }
        },
      }
    );
    wsRef.current = ws;
    return () => ws.close();
  }, []);

  const visibleTrades = _useMemo(
    () => session ? session.trades.filter(tr => tr.exitIdx <= upToIdx).reverse() : [],
    [session, upToIdx]
  );
  const visibleSignals = _useMemo(
    () => session ? session.signals.filter(s => s.idx <= upToIdx).slice(-60).reverse() : [],
    [session, upToIdx]
  );

  if (!session) {
    return (
      <div className="loading-shell">
        <span className="mono" style={{ fontSize: 12 }}>
          {wsStatus === 'connecting'   ? 'Connecting to server\u2026' :
           wsStatus === 'disconnected' ? 'Reconnecting\u2026'         : 'Loading session\u2026'}
        </span>
      </div>
    );
  }

  const totalPnl  = visibleTrades.reduce((a, t) => a + t.pnlPct, 0);
  const wins      = visibleTrades.filter(t => t.pnlPct > 0).length;
  const winRate   = visibleTrades.length ? wins / visibleTrades.length : 0;

  const statusBit = (
    <> · WS <span className={`mono ${wsStatus === 'connected' ? 'pos' : 'muted'}`}>{wsStatus}</span></>
  );

  const kpiItems = [
    { label: 'ENGINE',      value: 'ML_PURE_V3', sub: 'stage-1 · stage-2 · policy', cls: '' },
    { label: 'MARKET',      value: 'OPEN',        cls: 'pos', sub: 'regime · TREND_UP' },
    { label: 'SESSION P&L', value: TradingCore.fmtSigned(totalPnl, 2, '%'),
      cls: totalPnl >= 0 ? 'pos' : 'neg', sub: `${visibleTrades.length} trades · ${(winRate * 100).toFixed(0)}% WR` },
    { label: 'OPEN',        value: '0', sub: 'positions' },
    { label: 'WS',          value: wsStatus, cls: wsStatus === 'connected' ? 'pos' : 'warn', sub: 'monitor feed' },
    { label: 'CLOCK',       value: <ClockValue />, sub: 'IST · Asia/Kolkata' },
  ];

  const topBar = (
    <div className="row gap-s">
      <button className="btn sm" onClick={onModeSwitch}>&#8634; Replay mode</button>
      <button className="btn sm danger" onClick={onKillClick}>Kill engine</button>
    </div>
  );

  const leftControls = (
    <>
      <button className={`btn ${isPlaying ? 'ghost' : 'primary'}`}
        onClick={() => setIsPlaying(p => !p)}
        aria-label={isPlaying ? 'Pause' : 'Resume'}>
        {isPlaying ? '\u23F8 Pause stream' : '\u25B6 Resume stream'}
      </button>
      <div style={{ flex: 1 }} />
      <button className="btn ghost sm">Export CSV</button>
      <button className="btn ghost sm">Share view</button>
    </>
  );

  return (
    <StrategyMonitor
      mode="live"
      session={session}
      upToIdx={upToIdx}
      liveIdx={liveIdx}
      livePrice={livePrice}
      trades={visibleTrades}
      signals={visibleSignals}
      alerts={session.alerts}
      selectedTrade={selectedTrade}
      selectedSignal={selectedSignal}
      onSelectTrade={t  => { setSelectedTrade(t);  setSelectedSignal(null); }}
      onSelectSignal={s => { setSelectedSignal(s); setSelectedTrade(null);  }}
      flashTradeId={flashTradeId}
      kpiItems={kpiItems}
      topBar={topBar}
      leftControls={leftControls}
      statusBit={statusBit}
    />
  );
}

// ── CLOCK ──────────────────────────────────────────────────────────────────
function ClockValue() {
  const [, set] = _useState(0);
  _useEffect(() => { const id = setInterval(() => set(x => x + 1), 1000); return () => clearInterval(id); }, []);
  return <span className="mono">{TradingCore.fmtClock(new Date())}</span>;
}

// ── REPLAY VIEW ────────────────────────────────────────────────────────────
function ReplayMonitor({ onModeSwitch }) {
  const [session,        setSession]        = _useState(null);
  const [upToIdx,        setUpToIdx]        = _useState(0);
  const [isPlaying,      setIsPlaying]      = _useState(false);
  const [speed,          setSpeed]          = _useState(4);
  const [replayDate,     setReplayDate]     = _useState('2026-04-16');
  const [wsStatus,       setWsStatus]       = _useState('connecting');
  const [selectedTrade,  setSelectedTrade]  = _useState(null);
  const [selectedSignal, setSelectedSignal] = _useState(null);
  const [scrubHover,     setScrubHover]     = _useState(null);
  const wsRef         = _useRef(null);
  const upToIdxRef    = _useRef(0);
  const speedRef      = _useRef(4);
  const replayDateRef = _useRef('2026-04-16');

  _useEffect(() => { upToIdxRef.current    = upToIdx;    }, [upToIdx]);
  _useEffect(() => { speedRef.current      = speed;      }, [speed]);
  _useEffect(() => { replayDateRef.current = replayDate; }, [replayDate]);

  _useEffect(() => {
    const ws = TradingCore.makeMonitorWS(
      () => ({
        action: 'subscribe', mode: 'replay',
        date:       replayDateRef.current,
        up_to_idx:  upToIdxRef.current,
        playing:    false,
        speed:      speedRef.current,
      }),
      {
        onStatus: setWsStatus,
        onMessage(msg) {
          if (msg.type === 'snapshot') {
            setSession(msg.session);
            setUpToIdx(msg.up_to_idx);
            setIsPlaying(false);
          } else if (msg.type === 'tick') {
            setUpToIdx(msg.up_to_idx);
          } else if (msg.type === 'state') {
            setUpToIdx(msg.up_to_idx);
            setIsPlaying(msg.is_playing);
            setSpeed(msg.speed);
          }
        },
      }
    );
    wsRef.current = ws;
    return () => ws.close();
  }, []);

  function sendControl(patch) {
    wsRef.current && wsRef.current.send({ action: 'control', ...patch });
  }

  function handlePlay()    { setIsPlaying(true);  sendControl({ play: true });        }
  function handlePause()   { setIsPlaying(false); sendControl({ play: false });       }
  function handleSpeed(s)  { setSpeed(s);          sendControl({ speed: s });          }
  function handleScrub(idx){ setUpToIdx(idx);                                          }
  function handleScrubEnd(idx) {
    setUpToIdx(idx);
    setIsPlaying(false);
    sendControl({ seek: idx, play: false });
  }
  function handleReset() {
    setUpToIdx(0);
    setIsPlaying(false);
    sendControl({ seek: 0, play: false });
  }
  function handleDateChange(newDate) {
    setReplayDate(newDate);
    replayDateRef.current = newDate;
    setSession(null);
    setUpToIdx(0);
    upToIdxRef.current = 0;
    setIsPlaying(false);
    wsRef.current && wsRef.current.send({
      action: 'subscribe', mode: 'replay',
      date: newDate, up_to_idx: 0, playing: false, speed: speedRef.current,
    });
  }

  const visibleTrades = _useMemo(
    () => session ? session.trades.filter(tr => tr.exitIdx <= upToIdx).reverse() : [],
    [session, upToIdx]
  );
  const visibleSignals = _useMemo(
    () => session ? session.signals.filter(s => s.idx <= upToIdx).slice(-120).reverse() : [],
    [session, upToIdx]
  );

  if (!session) {
    return (
      <div className="loading-shell">
        <span className="mono" style={{ fontSize: 12 }}>
          {wsStatus === 'connecting'   ? 'Connecting to server\u2026' :
           wsStatus === 'disconnected' ? 'Reconnecting\u2026'         : 'Loading session\u2026'}
        </span>
      </div>
    );
  }

  const totalPnl = visibleTrades.reduce((a, t) => a + t.pnlPct, 0);
  const wins     = visibleTrades.filter(t => t.pnlPct > 0).length;
  const winRate  = visibleTrades.length ? wins / visibleTrades.length : 0;
  const pct      = (upToIdx + 1) / session.candles.length;
  const vtLabel  = session.candles[upToIdx]?.label || '09:15';

  const statusBit = (
    <> · WS <span className={`mono ${wsStatus === 'connected' ? '' : 'muted'}`}>{wsStatus}</span></>
  );

  const kpiItems = [
    { label: 'VIRTUAL TIME', value: vtLabel, sub: session.date, cls: '' },
    { label: 'REPLAY',       value: isPlaying ? 'RUNNING' : 'PAUSED',
      cls: isPlaying ? 'pos' : 'warn', sub: `${speed}\u00D7 speed` },
    { label: 'SESSION P&L',  value: TradingCore.fmtSigned(totalPnl, 2, '%'),
      cls: totalPnl >= 0 ? 'pos' : 'neg', sub: `${visibleTrades.length} trades · ${(winRate * 100).toFixed(0)}% WR` },
    { label: 'SIGNALS',      value: String(visibleSignals.length),
      sub: `${visibleSignals.filter(s => s.fired).length} fired` },
    { label: 'PROGRESS',     value: (pct * 100).toFixed(0) + '%',
      sub: `${upToIdx + 1}/${session.candles.length} bars` },
    { label: 'ENGINE',       value: 'ML_PURE_V3', sub: 'run r-2026-0416-ml3' },
  ];

  const topBar = (
    <div className="row gap-s">
      <button className="btn sm" onClick={onModeSwitch}>&#8634; Live mode</button>
      <button className="btn sm ghost">Share view</button>
    </div>
  );

  const leftControls = (
    <>
      {isPlaying
        ? <button className="btn ghost"   onClick={handlePause}>{'\u23F8'} Pause</button>
        : <button className="btn primary" onClick={handlePlay}>{'\u25B6'} Play</button>
      }
      <button className="btn ghost" onClick={handleReset}>{'\u27F2'} Reset</button>
      <div className="row gap-s" style={{ marginLeft: 4 }}>
        {[1, 2, 4, 8, 16].map(s => (
          <button key={s}
            className={`btn sm ${speed === s ? 'primary' : 'ghost'}`}
            onClick={() => handleSpeed(s)}>{s}&times;</button>
        ))}
      </div>
      <div style={{ flex: 1 }} />
      <div className="mobile-hide row gap-s" style={{ alignSelf: 'center' }}>
        <span className="field-label">Date</span>
        <input className="inp mono" type="date" value={replayDate} style={{ width: 140 }}
          onChange={e => handleDateChange(e.target.value)} />
      </div>
    </>
  );

  return (
    <div>
      <StrategyMonitor
        mode="replay"
        session={session}
        upToIdx={upToIdx}
        trades={visibleTrades}
        signals={visibleSignals}
        alerts={session.alerts}
        selectedTrade={selectedTrade}
        selectedSignal={selectedSignal}
        onSelectTrade={t  => { setSelectedTrade(t);  setSelectedSignal(null); }}
        onSelectSignal={s => { setSelectedSignal(s); setSelectedTrade(null);  }}
        kpiItems={kpiItems}
        topBar={topBar}
        leftControls={leftControls}
        statusBit={statusBit}
      />
      <div className="scrubber-wrap">
        <ReplayScrubber
          candles={session.candles}
          trades={session.trades}
          upToIdx={upToIdx}
          onScrub={handleScrub}
          onScrubEnd={handleScrubEnd}
          vtLabel={vtLabel}
          hover={scrubHover}
          onHover={setScrubHover}
        />
      </div>
    </div>
  );
}

// ── REPLAY SCRUBBER ────────────────────────────────────────────────────────
// onScrub: called on every drag move (local visual update only)
// onScrubEnd: called once on mouseup/touchend (sends seek to server)
function ReplayScrubber({ candles, trades, upToIdx, onScrub, onScrubEnd, vtLabel, hover, onHover }) {
  const ref = _useRef(null);
  const [width, setWidth] = _useState(800);
  const H = 64;

  _useEffect(() => {
    function measure() { if (ref.current) setWidth(ref.current.clientWidth); }
    measure();
    window.addEventListener('resize', measure);
    return () => window.removeEventListener('resize', measure);
  }, []);

  const { min, max } = _useMemo(() => {
    let mn = Infinity, mx = -Infinity;
    candles.forEach(c => { if (c.l < mn) mn = c.l; if (c.h > mx) mx = c.h; });
    return { min: mn, max: mx };
  }, [candles]);

  const xOf = i  => (i / Math.max(1, candles.length - 1)) * width;
  const yOf = p  => 6 + (1 - (p - min) / (max - min)) * (H - 12);

  const pathD = _useMemo(() => {
    let d = '';
    for (let i = 0; i < candles.length; i++)
      d += (i === 0 ? 'M' : 'L') + xOf(i).toFixed(1) + ' ' + yOf(candles[i].c).toFixed(1) + ' ';
    return d;
  }, [candles, width]);

  const areaPast = _useMemo(() => {
    let d = 'M0 ' + H + ' ';
    const upto = Math.min(upToIdx, candles.length - 1);
    for (let i = 0; i <= upto; i++)
      d += 'L' + xOf(i).toFixed(1) + ' ' + yOf(candles[i].c).toFixed(1) + ' ';
    d += 'L' + xOf(upto).toFixed(1) + ' ' + H + ' Z';
    return d;
  }, [candles, upToIdx, width]);

  const ticks = _useMemo(() => {
    const out = [];
    for (let i = 0; i < candles.length; i += 30) out.push({ i, label: candles[i].label });
    out.push({ i: candles.length - 1, label: candles[candles.length - 1].label });
    return out;
  }, [candles]);

  const tradeDots = _useMemo(() =>
    trades.map(tr => ({ id: tr.id, x: xOf(tr.entryIdx), y: yOf(tr.entry), pos: tr.pnlPct >= 0 })),
    [trades, width, min, max]
  );

  function startDrag(e) {
    const r = ref.current.getBoundingClientRect();
    let lastIdx = upToIdx;
    function setFromX(clientX) {
      const x   = clientX - r.left;
      const idx = Math.max(0, Math.min(candles.length - 1, Math.round((x / width) * (candles.length - 1))));
      lastIdx = idx;
      onScrub(idx);
    }
    setFromX(e.clientX ?? e.touches?.[0]?.clientX);
    function move(ev) { setFromX(ev.clientX ?? ev.touches?.[0]?.clientX); }
    function up() {
      window.removeEventListener('mousemove',  move);
      window.removeEventListener('touchmove',  move);
      window.removeEventListener('mouseup',    up);
      window.removeEventListener('touchend',   up);
      if (onScrubEnd) onScrubEnd(lastIdx);
    }
    window.addEventListener('mousemove',  move);
    window.addEventListener('touchmove',  move);
    window.addEventListener('mouseup',    up);
    window.addEventListener('touchend',   up);
  }

  function handleHover(e) {
    const r   = ref.current.getBoundingClientRect();
    const x   = e.clientX - r.left;
    const idx = Math.max(0, Math.min(candles.length - 1, Math.round((x / width) * (candles.length - 1))));
    onHover({ idx, x: xOf(idx) });
  }

  return (
    <div className="scrubber">
      <div className="scrubber-head">
        <div className="scrubber-time">
          <span className="muted tiny">Virtual time</span>
          <span className="mono" style={{ fontSize: 15, color: 'var(--ink)', fontWeight: 600 }}>{vtLabel}</span>
          <span className="chip">bar {upToIdx + 1}/{candles.length}</span>
        </div>
        <div className="scrubber-meta mono tiny">
          <span className="muted">09:15</span>
          <span className="muted">·</span>
          <span className="muted">15:29</span>
        </div>
      </div>
      <div className="scrubber-track" ref={ref}
        onMouseDown={startDrag}
        onTouchStart={startDrag}
        onMouseMove={handleHover}
        onMouseLeave={() => onHover(null)}>
        <svg width={width} height={H}>
          <path d={pathD} stroke="var(--ink-3)" strokeWidth="1" fill="none" opacity="0.5" />
          <path d={areaPast} fill="var(--ink)" opacity="0.08" />
          <path d={areaPast.replace(/M0 \d+ /, 'M' + xOf(0) + ' ' + yOf(candles[0].c) + ' ').replace(/ Z$/, '')
            .replace(/L([\d.]+) 64\s*$/, '')} stroke="var(--ink)" strokeWidth="1.4" fill="none" />
          {tradeDots.map(d => (
            <circle key={d.id} cx={d.x} cy={d.y} r="2"
              fill={d.pos ? 'var(--pos)' : 'var(--neg)'}
              opacity={d.x <= xOf(upToIdx) ? 1 : 0.3} />
          ))}
          {ticks.map((t, i) => (
            <g key={i}>
              <line x1={xOf(t.i)} x2={xOf(t.i)} y1={H - 4} y2={H} stroke="var(--ink-3)" opacity="0.4" />
              {i % 2 === 0 && (
                <text x={xOf(t.i)} y={H + 14} fontSize="10" fontFamily="var(--f-mono)"
                  fill="var(--ink-3)" textAnchor="middle">{t.label}</text>
              )}
            </g>
          ))}
          <line x1={xOf(upToIdx)} x2={xOf(upToIdx)} y1={0} y2={H} stroke="var(--ink)" strokeWidth="2" />
          <circle cx={xOf(upToIdx)} cy={yOf(candles[upToIdx]?.c || 0)} r="5"
            fill="var(--paper)" stroke="var(--ink)" strokeWidth="2" />
          {hover && hover.idx !== upToIdx && (
            <line x1={hover.x} x2={hover.x} y1={0} y2={H}
              stroke="var(--ink-3)" strokeDasharray="2 3" opacity="0.6" />
          )}
        </svg>
        {hover && (
          <div className="scrubber-tip mono tiny" style={{ left: Math.min(hover.x + 8, width - 60) }}>
            {candles[hover.idx].label}
          </div>
        )}
      </div>
    </div>
  );
}

Object.assign(window, { StrategyMonitor, LiveMonitor, ReplayMonitor });
