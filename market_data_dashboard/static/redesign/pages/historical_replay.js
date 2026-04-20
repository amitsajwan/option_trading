// Historical replay monitor.
(function () {
  var PAGE = 'historical_replay';
  var cache = null;
  var pending = null;

  var C = window.QComponents;

  function getFallbackData() {
    var mock = window.MOCK || {};
    return {
      source: 'mock',
      session: {
        date_ist: '2026-04-16',
        instrument: 'NIFTY 50',
      },
      currentRunId: 'mock_historical_run',
      replayStatus: {
        status: 'completed',
        current_replay_timestamp: '2026-04-16T15:30:00+05:30',
        events_emitted: 48200,
        speed: 0,
        start_date: '2026-04-16',
        end_date: '2026-04-16',
        collection_counts: {
          votes: 12,
          signals: 8,
          positions: 16,
        },
      },
      latestCompletedRun: {
        run_id: 'mock_historical_run',
      },
      kpis: {
        engineMode: 'UNKNOWN',
      },
      overall: {
        trade_count: (mock.closedTrades || []).length,
        win_rate: 0.71,
        profit_factor: 2.9,
      },
      equity: {
        net_return_pct: 0.0084,
      },
      votes: (mock.votes || []).slice(0, 6),
      trades: (mock.closedTrades || []).slice(0, 8),
      days: mock.sessionDays || [],
      chart: {
        candles: mock.candles || [],
        markers: mock.trades || [],
      },
    };
  }

  function buildKpiItems(model) {
    var replay = model.replayStatus || {};
    var dash = window.DashAPI;
    var trades = model.overall && model.overall.trade_count != null ? model.overall.trade_count : model.trades.length;
    var netPnl = model.equity && model.equity.net_return_pct != null
      ? dash.fmtPercentFromRatio(model.equity.net_return_pct, 2)
      : '--';
    var netValue = model.equity && model.equity.net_return_pct != null ? Number(model.equity.net_return_pct) : null;
    var netCls = Number.isFinite(netValue) ? (netValue >= 0 ? 'pos' : 'neg') : '';
    return [
      { label: 'REPLAY STATE',   value: C.esc(String(replay.status || 'unknown').toUpperCase()), cls: 'pos', sub: 'Run ' + C.esc(model.currentRunId || '--') },
      { label: 'VIRTUAL TIME',   value: C.esc(dash.fmtTime(replay.current_replay_timestamp || replay.virtual_time_current).slice(0, 8) || '--'), sub: C.esc(replay.current_trade_date || model.session.date_ist || '--') },
      { label: 'EVENTS EMITTED', value: C.esc(dash.fmtCompactInt(replay.events_emitted)), sub: 'votes ' + Number((replay.collection_counts || {}).votes || 0) + ' - signals ' + Number((replay.collection_counts || {}).signals || 0) },
      { label: 'SPEED',          value: C.esc(replay.speed != null ? String(replay.speed) + 'x' : '--'), sub: 'Replay controller' },
      { label: 'SESSION TRADES', value: C.esc(String(trades)), sub: Number.isFinite(Number(model.overall && model.overall.win_rate)) ? ((model.overall.win_rate * 100).toFixed(1) + '% WR') : 'Session summary' },
      { label: 'NET PNL',        value: C.esc(netPnl), cls: netCls, sub: 'Capital weighted' },
    ];
  }

  function tradeDate(trade) {
    var meta = (trade && trade.meta) || {};
    var candidates = [
      meta.trade_date_ist,
      meta.date_ist,
      meta.date,
      meta.entry_time,
      meta.exit_time,
    ];
    for (var i = 0; i < candidates.length; i += 1) {
      var value = candidates[i];
      if (!value) continue;
      var text = String(value).trim();
      if (!text) continue;
      return text.length >= 10 ? text.slice(0, 10) : text;
    }
    return '';
  }

  function filterTradesForDate(trades, date) {
    var items = Array.isArray(trades) ? trades : [];
    if (!date || !items.length) return items;
    var hasAnyTradeDate = false;
    var filtered = items.filter(function (trade) {
      var current = tradeDate(trade);
      if (current) hasAnyTradeDate = true;
      return current === date;
    });
    return filtered.length || hasAnyTradeDate ? filtered : items;
  }

  function renderDayChips(days, activeDateArg) {
    if (!days.length) return '<div class="muted">No replay days available for the resolved run.</div>';
    var activeDate = activeDateArg || (days[0] && days[0].d);
    return '<div style="display:grid;grid-template-columns:repeat(8,minmax(0,1fr));gap:8px">' + days.map(function (day) {
      var active = day.d === activeDate;
      return '<button class="day-chip panel" data-date="' + C.esc(day.d) + '" style="' +
          'padding:10px 12px; text-align:left; cursor:pointer;' +
          'border:1px solid ' + (active ? 'var(--ink)' : 'var(--line-1)') + ';' +
          'background:' + (active ? 'var(--ink)' : 'var(--paper)') + ';' +
          'color:' + (active ? 'var(--paper)' : 'var(--ink)') + '">' +
        '<div class="mono tiny" style="opacity:0.7">' + C.esc(day.d) + '</div>' +
        '<div style="margin-top:4px; font-size:14px; font-weight:600" class="mono">' + C.fmtSigned(day.net, 2, '%') + '</div>' +
        '<div class="mono tiny" style="opacity:0.7; margin-top:2px">' + Number(day.trades || 0) + ' trades</div>' +
      '</button>';
    }).join('') + '</div>';
  }

  function render(model) {
    var data = model || getFallbackData();
    var replay = data.replayStatus || {};
    var activeDate = data.activeDate || data.session.date_ist || '--';
    var rangeText = C.esc(replay.start_date || data.session.date_ist || '--') + ' -> ' + C.esc(replay.end_date || data.session.date_ist || '--');
    var runId = data.currentRunId || (data.latestCompletedRun && data.latestCompletedRun.run_id) || '--';
    var engBadge = C.engineModeBadge(data.kpis && data.kpis.engineMode, runId);

    return '<div id="historical-replay-view" data-source="' + C.esc(data.source || 'mock') + '">' +
      '<div class="page-head">' +
        '<div>' +
          '<div class="page-crumbs">Operator - Historical</div>' +
          '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">' +
            '<h1 class="page-title" style="margin:0">Historical Replay Monitor</h1>' +
            engBadge +
          '</div>' +
          '<p class="page-sub">Replay-first operator view using the dashboard historical session API and evaluation summaries.</p>' +
        '</div>' +
        '<div class="page-actions">' +
          '<div class="field" style="flex-direction:row;align-items:center;gap:6px">' +
            '<span class="field-label">From</span>' +
            '<input id="replay-from" class="inp" type="date" value="' + C.esc(replay.start_date || data.session.date_ist || '') + '" style="width:130px">' +
            '<span class="field-label">To</span>' +
            '<input id="replay-to" class="inp" type="date" value="' + C.esc(replay.end_date || data.session.date_ist || '') + '" style="width:130px">' +
            '<span class="field-label">Speed</span>' +
            '<input id="replay-speed" class="inp" type="number" value="' + C.esc(replay.speed != null ? replay.speed : 0) + '" style="width:60px">' +
          '</div>' +
          '<button id="btn-load-range" class="btn">Load range</button>' +
          '<button id="btn-run-replay" class="btn primary">Run replay</button>' +
          '<span id="replay-run-status" style="font-size:12px;color:var(--ink-3);align-self:center"></span>' +
        '</div>' +
      '</div>' +

      C.kpiStrip(buildKpiItems(data), 6) +

      '<div class="panel">' +
        '<div class="panel-head">' +
          '<div class="panel-title">Session days <span class="count">evaluation</span></div>' +
          '<div class="row gap-s"><button class="btn sm ghost">Prev</button><button class="btn sm ghost">Next</button></div>' +
        '</div>' +
        '<div class="panel-body" style="padding:12px">' + renderDayChips(data.days || [], activeDate) + '</div>' +
      '</div>' +

      '<div class="g-main-side">' +
        '<div class="panel">' +
          '<div class="panel-head">' +
            '<div class="row gap-m">' +
              '<div class="panel-title">' + C.esc(data.session.instrument || 'Underlying') + ' - replay day <span id="historical-replay-session-date">' + C.esc(activeDate) + '</span></div>' +
              '<span class="chip info"><span class="dot"></span>historical</span>' +
              '<span class="chip">Range ' + rangeText + '</span>' +
            '</div>' +
            '<div class="row gap-s"><button class="btn sm ghost">Fit</button><button class="btn sm ghost">Expand</button></div>' +
          '</div>' +
          '<div class="panel-body flush" style="padding:10px 8px 0">' +
            '<div id="historical-price-chart" style="width:100%; height:380px; position:relative"></div>' +
            '<div style="display:flex;gap:16px;padding:8px 12px 12px;font-size:11px;color:var(--ink-3);font-family:var(--f-mono)">' +
              '<span><span style="display:inline-block;width:10px;height:2px;background:var(--ink);vertical-align:middle"></span> Session close</span>' +
              '<span style="color:var(--pos)">Entry marker</span>' +
              '<span style="color:var(--neg)">Exit marker</span>' +
              '<span>' + Number((data.overall && data.overall.trade_count) || data.trades.length || 0) + ' trades - run <span class="mono">' + C.esc(runId) + '</span></span>' +
            '</div>' +
          '</div>' +
        '</div>' +

        '<div style="display:grid;gap:14px">' +
          '<div class="panel">' +
            '<div class="panel-head"><div class="panel-title">Run details</div><span class="chip pos"><span class="dot"></span>' + C.esc(String(replay.status || 'unknown')) + '</span></div>' +
            '<div class="panel-body">' +
              '<div class="kv"><span class="k">Run ID</span><span class="v">' + C.esc(runId) + '</span></div>' +
              '<div class="kv"><span class="k">Dataset</span><span class="v">historical</span></div>' +
              '<div class="kv"><span class="k">Range</span><span class="v">' + rangeText + '</span></div>' +
              '<div class="kv"><span class="k">Events emitted</span><span class="v">' + C.esc(window.DashAPI.fmtCompactInt(replay.events_emitted)) + '</span></div>' +
              '<div class="kv"><span class="k">Votes</span><span class="v">' + Number((replay.collection_counts || {}).votes || 0) + '</span></div>' +
              '<div class="kv"><span class="k">Signals</span><span class="v">' + Number((replay.collection_counts || {}).signals || 0) + '</span></div>' +
            '</div>' +
          '</div>' +
          '<div class="panel">' +
            '<div class="panel-head"><div class="panel-title">Compare</div></div>' +
            '<div class="panel-body" style="display:grid;gap:8px">' +
              '<button class="btn" style="justify-content:space-between">Open compare page <span class="muted mono tiny">-></span></button>' +
              '<button class="btn" style="justify-content:space-between">Latest run JSON <span class="muted mono tiny">-></span></button>' +
              '<button class="btn" style="justify-content:space-between">Trades API <span class="muted mono tiny">-></span></button>' +
            '</div>' +
          '</div>' +
        '</div>' +
      '</div>' +

      '<div class="g3">' +
        '<div class="panel" style="grid-column:span 2">' +
          '<div class="panel-head"><div class="panel-title">Trades - <span id="hr-trades-label">' + C.esc(activeDate) + '</span> <span id="hr-trades-count" class="count">' + data.trades.length + '</span></div></div>' +
          '<div class="panel-body flush">' +
            '<table class="tbl">' +
              C.TRADE_TABLE_HEADER +
              '<tbody id="hr-trades-body">' + C.tradeTableRows(data.trades) + '</tbody>' +
            '</table>' +
          '</div>' +
        '</div>' +
        '<div class="panel">' +
          '<div class="panel-head"><div class="panel-title">Signals - recent <span id="hr-signals-count" class="count">' + data.votes.length + '</span></div></div>' +
          '<div class="panel-body flush">' +
            '<table class="tbl">' +
              C.VOTE_TABLE_HEADER +
              '<tbody id="hr-signals-body">' + C.voteTableRows(data.votes) + '</tbody>' +
            '</table>' +
          '</div>' +
        '</div>' +
      '</div>' +
    '</div>';
  }

  function mountChart(data) {
    var el = document.getElementById('historical-price-chart');
    if (!el) return;
    if (el.__chart) {
      try { el.__chart.destroy(); } catch (err) {}
      el.__chart = null;
    }
    var d = data || getFallbackData();
    var candles = (d.chart && d.chart.candles && d.chart.candles.length) ? d.chart.candles : null;
    if (!candles) {
      el.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;' +
        'color:var(--ink-3);font-size:13px;font-family:var(--f-mono)">' +
        'No session chart data — select a date range and run replay</div>';
      return;
    }
    if (!window.InteractiveChart) return;
    el.__chart = window.InteractiveChart.mount(el, {
      candles: candles,
      markers: (d.chart && d.chart.markers) || [],
    });
  }

  async function loadData(dateFrom, dateTo, runId) {
    if (!window.DashAPI) throw new Error('DashAPI is not loaded');
    var replayStatus = await window.DashAPI.fetchHistoricalStatus({});
    var resolvedRunId = runId || replayStatus.latest_completed_run_id || undefined;
    var rangeFrom = dateFrom || replayStatus.start_date || replayStatus.date_ist;
    var rangeTo   = dateTo   || replayStatus.end_date   || replayStatus.date_ist;

    // Use the run's actual end date for the session chart query.
    // replayStatus.date_ist is the default snapshot date (today or latest snapshot)
    // which would return no candles when querying historical snapshot collection.
    var sessionDate = rangeTo || replayStatus.date_ist;

    var results = await Promise.all([
      window.DashAPI.fetchHistoricalSession({
        date: sessionDate,
        run_id: resolvedRunId,
        limit_votes: 12,
        limit_signals: 12,
        // omit limit_trades — fetching from eval API below, session trades overridden
      }),
      window.DashAPI.fetchEvalDays({
        dataset: 'historical',
        date_from: rangeFrom,
        date_to: rangeTo,
        run_id: resolvedRunId,
        page: 1,
        page_size: 8,
      }).catch(function () { return { rows: [], total: 0, no_runs: true }; }),
      window.DashAPI.fetchEvalTrades({
        dataset: 'historical',
        date_from: rangeFrom,
        date_to: rangeTo,
        run_id: resolvedRunId,
        page: 1,
        page_size: 50,
      }).catch(function () { return { rows: [] }; }),
    ]);

    var pageData = window.DashAPI.sessionToPageData(results[0]);
    pageData.replayStatus = replayStatus;
    pageData.days = (results[1].rows || []).map(window.DashAPI.mapSessionDay);
    pageData.activeDate = pageData.session.date_ist || sessionDate;

    // Use eval trades as the authoritative source for the historical run.
    var evalTradeRows = results[2].rows || [];
    var evalTrades = evalTradeRows.map(window.DashAPI.mapTrade);
    if (evalTrades.length > 0) {
      pageData.trades = filterTradesForDate(evalTrades, pageData.activeDate);
    }

    // Build chart markers from eval trades when session has none.
    var candles = pageData.chart && pageData.chart.candles;
    var hasMarkers = pageData.chart && pageData.chart.markers && pageData.chart.markers.length;
    if (!hasMarkers && evalTradeRows.length && candles && candles.length) {
      var rawMarkers = [];
      evalTradeRows.forEach(function (row) {
        if (row.entry_time) rawMarkers.push({ timestamp: row.entry_time, type: 'entry', label: row.direction || '' });
        if (row.exit_time)  rawMarkers.push({ timestamp: row.exit_time,  type: 'exit',  label: '' });
      });
      pageData.chart.markers = window.DashAPI.buildChartMarkers(rawMarkers, candles);
    }

    pageData.source = 'api';
    return pageData;
  }

  function loadDay(date, runId) {
    // Update chart + trades in-place for a single clicked day — no full re-render.
    var tradesBody = document.getElementById('hr-trades-body');
    var tradesLabel = document.getElementById('hr-trades-label');
    var tradesCount = document.getElementById('hr-trades-count');
    var signalsBody = document.getElementById('hr-signals-body');
    var signalsCount = document.getElementById('hr-signals-count');
    var sessionDate = document.getElementById('historical-replay-session-date');

    // Highlight the clicked chip, dim others.
    document.querySelectorAll('.day-chip').forEach(function (chip) {
      var active = chip.getAttribute('data-date') === date;
      chip.style.background = active ? 'var(--ink)' : 'var(--paper)';
      chip.style.color = active ? 'var(--paper)' : 'var(--ink)';
      chip.style.border = '1px solid ' + (active ? 'var(--ink)' : 'var(--line-1)');
    });

    if (tradesBody) tradesBody.innerHTML = '<tr><td colspan="8" class="muted" style="text-align:center;padding:18px">Loading…</td></tr>';

    Promise.all([
      window.DashAPI.fetchHistoricalSession({
        date: date,
        run_id: runId || undefined,
        limit_votes: 12,
        limit_signals: 12,
      }),
      window.DashAPI.fetchEvalTrades({
        dataset: 'historical',
        date_from: date,
        date_to: date,
        run_id: runId || undefined,
        page: 1,
        page_size: 50,
      }).catch(function () { return { rows: [] }; }),
    ]).then(function (results) {
      var sessionData = window.DashAPI.sessionToPageData(results[0]);
      var evalTradeRows = results[1].rows || [];
      var evalTrades = evalTradeRows.map(window.DashAPI.mapTrade);
      var trades = evalTrades.length ? filterTradesForDate(evalTrades, date) : filterTradesForDate(sessionData.trades, date);

      if (tradesBody)  tradesBody.innerHTML  = C.tradeTableRows(trades);
      if (tradesLabel) tradesLabel.textContent = date;
      if (tradesCount) tradesCount.textContent = trades.length;
      if (signalsBody) signalsBody.innerHTML  = C.voteTableRows(sessionData.votes);
      if (signalsCount) signalsCount.textContent = sessionData.votes.length;
      if (sessionDate) sessionDate.textContent = date;

      // Rebuild chart for this day.
      var candles = sessionData.chart && sessionData.chart.candles;
      var markers = (sessionData.chart && sessionData.chart.markers) || [];
      if (!markers.length && evalTradeRows.length && candles && candles.length) {
        var rawM = [];
        evalTradeRows.forEach(function (row) {
          if (row.entry_time) rawM.push({ timestamp: row.entry_time, type: 'entry', label: row.direction || '' });
          if (row.exit_time)  rawM.push({ timestamp: row.exit_time,  type: 'exit',  label: '' });
        });
        markers = window.DashAPI.buildChartMarkers(rawM, candles);
      }
      mountChart({ chart: { candles: candles || [], markers: markers } });
    }).catch(function (err) {
      console.error('Load day failed:', err);
      if (tradesBody) tradesBody.innerHTML = C.emptyRow(8, 'Failed to load day data.');
    });
  }

  function attachHandlers(data) {
    var btnRun = document.getElementById('btn-run-replay');
    var btnLoad = document.getElementById('btn-load-range');
    var statusEl = document.getElementById('replay-run-status');
    var runId = data && (data.currentRunId || (data.replayStatus && data.replayStatus.latest_completed_run_id));

    // Day chip clicks — filter table + chart to that specific day.
    document.querySelectorAll('.day-chip').forEach(function (chip) {
      chip.addEventListener('click', function () {
        var date = chip.getAttribute('data-date');
        if (date) loadDay(date, runId);
      });
    });

    function getInputs() {
      return {
        dateFrom: (document.getElementById('replay-from') || {}).value || '',
        dateTo: (document.getElementById('replay-to') || {}).value || '',
        speed: parseFloat((document.getElementById('replay-speed') || {}).value || '0') || 0,
      };
    }

    if (btnLoad) {
      btnLoad.addEventListener('click', function () {
        var inp = getInputs();
        if (!inp.dateFrom || !inp.dateTo) { alert('Set From and To dates first.'); return; }
        cache = null;
        pending = loadData(inp.dateFrom, inp.dateTo)
          .then(function (data) {
            cache = data;
            var root = document.getElementById('page');
            if (root) { root.innerHTML = render(data); attachHandlers(data); mountChart(data); }
          })
          .catch(function (err) { console.error('Load range failed:', err); })
          .finally(function () { pending = null; });
      });
    }

    if (btnRun) {
      btnRun.addEventListener('click', function () {
        var inp = getInputs();
        if (!inp.dateFrom || !inp.dateTo) { alert('Set From and To dates first.'); return; }
        btnRun.disabled = true;
        btnRun.textContent = 'Queuing…';
        if (statusEl) statusEl.textContent = '';

        window.DashAPI.post('/api/strategy/evaluation/runs', {
          dataset: 'historical',
          date_from: inp.dateFrom,
          date_to: inp.dateTo,
          speed: inp.speed,
        }).then(function (resp) {
          var runId = resp.run_id;
          if (statusEl) statusEl.textContent = 'Run ' + runId.slice(0, 8) + '… queued';
          btnRun.textContent = 'Running…';
          pollRun(runId, statusEl, btnRun, inp.dateFrom, inp.dateTo);
        }).catch(function (err) {
          btnRun.disabled = false;
          btnRun.textContent = 'Run replay';
          if (statusEl) statusEl.textContent = 'Error: ' + String(err);
        });
      });
    }
  }

  function pollRun(runId, statusEl, btnRun, dateFrom, dateTo) {
    var interval = setInterval(function () {
      window.DashAPI.fetchEvalRun(runId).then(function (run) {
        var st = run.status || 'unknown';
        var pct = Number(run.progress_pct || 0).toFixed(0);
        if (statusEl) statusEl.textContent = st + ' ' + pct + '% — ' + (run.message || '');
        if (st === 'completed' || st === 'failed' || st === 'error') {
          clearInterval(interval);
          btnRun.disabled = false;
          btnRun.textContent = 'Run replay';
          if (st === 'completed') {
            cache = null;
            loadData(dateFrom, dateTo, runId).then(function (data) {
              cache = data;
              var root = document.getElementById('page');
              if (root) { root.innerHTML = render(data); attachHandlers(data); mountChart(data); }
            }).catch(function (err) { console.error('Refresh after run failed:', err); });
          }
        }
      }).catch(function () { /* ignore poll errors */ });
    }, 2000);
  }

  function mount() {
    var page = document.getElementById('page');
    if (!page) return;

    // Wire buttons immediately so Run/Load work even if loadData fails.
    attachHandlers(null);

    if (cache) {
      var currentView = document.getElementById('historical-replay-view');
      if (!currentView || currentView.getAttribute('data-source') !== 'api') {
        page.innerHTML = render(cache);
        attachHandlers(cache);
      }
      mountChart(cache);
      return;
    }

    if (pending) return;
    pending = loadData()
      .then(function (data) {
        cache = data;
        if (window.__opCurrentPage !== PAGE) return;
        var root = document.getElementById('page');
        if (root) { root.innerHTML = render(data); attachHandlers(data); mountChart(data); }
      })
      .catch(function (err) {
        console.error('Failed to hydrate historical replay page:', err);
      })
      .finally(function () {
        pending = null;
      });
  }

  window.PAGES = window.PAGES || {};
  window.PAGES.historical_replay = render;
  window.PAGE_MOUNTS = window.PAGE_MOUNTS || {};
  window.PAGE_MOUNTS.historical_replay = mount;
})();
