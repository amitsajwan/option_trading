// Dashboard API adapter for the redesign pages.
(function (global) {
  'use strict';

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>]/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;' }[ch];
    });
  }

  function toNum(value) {
    var out = Number(value);
    return Number.isFinite(out) ? out : null;
  }

  function fmtTime(iso) {
    if (!iso) return '';
    try {
      var dt = new Date(iso);
      if (Number.isNaN(dt.getTime())) return String(iso).slice(11, 19);
      return [dt.getHours(), dt.getMinutes(), dt.getSeconds()]
        .map(function (item) { return String(item).padStart(2, '0'); })
        .join(':');
    } catch (err) {
      return String(iso).slice(11, 19);
    }
  }

  function fmtShortTime(iso) {
    var text = fmtTime(iso);
    return text ? text.slice(0, 5) : '';
  }

  function fmtSigned(value, digits, suffix) {
    var num = toNum(value);
    if (num == null) return '--';
    var places = digits == null ? 2 : digits;
    return (num >= 0 ? '+' : '') + num.toFixed(places) + (suffix || '');
  }

  function fmtPercentFromRatio(value, digits) {
    var num = toNum(value);
    if (num == null) return '--';
    return fmtSigned(num * 100, digits == null ? 2 : digits, '%');
  }

  function fmtAge(seconds) {
    var value = toNum(seconds);
    if (value == null) return '--';
    if (value < 60) return value.toFixed(value < 10 ? 1 : 0) + 's';
    if (value < 3600) return Math.floor(value / 60) + 'm';
    return Math.floor(value / 3600) + 'h';
  }

  function fmtCompactInt(value) {
    var num = toNum(value);
    if (num == null) return '--';
    if (Math.abs(num) >= 1000000) return (num / 1000000).toFixed(1) + 'M';
    if (Math.abs(num) >= 1000) return (num / 1000).toFixed(1) + 'k';
    return String(Math.round(num));
  }

  function fmtHold(entryIso, exitIso) {
    if (!entryIso || !exitIso) return '--';
    var start = new Date(entryIso);
    var end = new Date(exitIso);
    if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return '--';
    var seconds = Math.max(0, Math.round((end.getTime() - start.getTime()) / 1000));
    if (seconds < 60) return seconds + 's';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm';
    return Math.floor(seconds / 3600) + 'h ' + Math.floor((seconds % 3600) / 60) + 'm';
  }

  function normDir(raw) {
    var value = String(raw || '').trim().toUpperCase();
    if (value === 'CE') return 'LONG';
    if (value === 'PE') return 'SHORT';
    if (value === 'BUY') return 'LONG';
    if (value === 'SELL') return 'SHORT';
    if (value === 'AVOID' || value === 'NO_TRADE' || value === 'HOLD' || value === 'FLAT' || value === 'EXIT') return 'FLAT';
    return value || 'FLAT';
  }

  function maxDefined(values) {
    var max = null;
    values.forEach(function (value) {
      var num = toNum(value);
      if (num == null) return;
      max = max == null ? num : Math.max(max, num);
    });
    return max;
  }

  function mapVote(row) {
    return {
      t: fmtTime(row && row.timestamp),
      strat: row && row.strategy ? String(row.strategy) : '',
      dir: normDir(row && row.direction),
      conf: toNum(row && row.confidence) || 0,
      fired: !!(row && (row.policy_allowed === true || (row.signal_type === 'ENTRY' && row.policy_allowed !== false))),
      meta: row || {},
    };
  }

  function mapSignal(row) {
    return {
      t: fmtTime(row && row.timestamp),
      strat: row && row.strategy ? String(row.strategy) : '',
      dir: normDir(row && row.direction),
      conf: toNum(row && row.confidence) || 0,
      fired: !!(row && row.acted_on === true),
      meta: row || {},
    };
  }

  function mapTrade(row) {
    var rawPnlRatio = toNum(row && row.pnl_pct_net);
    if (rawPnlRatio == null) rawPnlRatio = toNum(row && row.pnl_pct);
    var capitalPnlRatio = toNum(row && row.capital_pnl_pct);
    var pnlRatio = capitalPnlRatio != null ? capitalPnlRatio : rawPnlRatio;
    var entry = toNum(row && row.entry_premium);
    var exit = toNum(row && row.exit_premium);
    var decisionMetrics = row && row.signal_decision_metrics;
    if (!decisionMetrics || typeof decisionMetrics !== 'object') decisionMetrics = row && row.decision_metrics;
    return {
      t: fmtTime(row && (row.entry_time || row.exit_time || row.timestamp)),
      strat: row && (row.entry_strategy || row.strategy) ? String(row.entry_strategy || row.strategy) : '',
      dir: normDir(row && row.direction),
      qty: row && row.lots != null ? row.lots : '--',
      entry: entry,
      exit: exit,
      signalId: row && row.signal_id ? String(row.signal_id) : '',
      signalConfidence: toNum(row && row.signal_confidence),
      decisionReasonCode: row && (row.signal_decision_reason_code || row.decision_reason_code)
        ? String(row.signal_decision_reason_code || row.decision_reason_code)
        : '',
      decisionMetrics: decisionMetrics && typeof decisionMetrics === 'object' ? decisionMetrics : {},
      rawPnlPct: rawPnlRatio != null ? rawPnlRatio * 100 : null,
      capitalPnlPct: capitalPnlRatio != null ? capitalPnlRatio * 100 : null,
      pnl: pnlRatio != null ? pnlRatio * 100 : null,
      pnlPct: pnlRatio != null ? pnlRatio * 100 : null,
      hold: fmtHold(row && row.entry_time, row && row.exit_time),
      meta: row || {},
    };
  }

  function mapAlert(row) {
    var severity = String((row && row.severity) || 'info').toLowerCase();
    return {
      t: fmtTime(row && (row.last_seen_ist || row.first_seen_ist)),
      level: severity === 'critical' ? 'crit' : (severity === 'warning' ? 'warn' : severity),
      msg: row && row.title
        ? (row.detail ? ('<strong>' + esc(row.title) + '</strong> - ' + esc(row.detail)) : esc(row.title))
        : '',
      meta: row || {},
    };
  }

  function mapSessionDay(row) {
    var ratio = row && row.day_return_pct != null ? toNum(row.day_return_pct) : toNum(row && row.net_return_pct);
    var fallbackNet = toNum(row && row.net);
    return {
      d: row && (row.date_ist || row.date) ? String(row.date_ist || row.date) : '',
      trades: row && (row.trade_count != null ? row.trade_count : row.trades) || 0,
      net: ratio != null ? ratio * 100 : (fallbackNet != null ? fallbackNet : 0),
      status: row && row.status ? String(row.status) : 'completed',
      meta: row || {},
    };
  }

  function buildSyntheticCandles(sessionChart) {
    var chart = sessionChart || {};
    var opens = Array.isArray(chart.opens) ? chart.opens : [];
    var highs = Array.isArray(chart.highs) ? chart.highs : [];
    var lows = Array.isArray(chart.lows) ? chart.lows : [];
    var closes = Array.isArray(chart.closes) ? chart.closes : [];
    var volumes = Array.isArray(chart.volumes) ? chart.volumes : [];
    var prices = Array.isArray(chart.prices) ? chart.prices : [];
    var labels = Array.isArray(chart.labels) ? chart.labels : [];
    var timestamps = Array.isArray(chart.timestamps) ? chart.timestamps : [];
    var candles = [];
    var hasOhlcv = closes.length || opens.length || highs.length || lows.length || volumes.length;
    var total = Math.max(
      closes.length,
      opens.length,
      highs.length,
      lows.length,
      volumes.length,
      labels.length,
      timestamps.length,
      prices.length
    );

    for (var index = 0; index < total; index += 1) {
      var close = toNum(hasOhlcv ? closes[index] : prices[index]);
      if (close == null) continue;
      var open = toNum(opens[index]);
      if (open == null) open = close;
      var high = toNum(highs[index]);
      if (high == null) high = Math.max(open, close);
      var low = toNum(lows[index]);
      if (low == null) low = Math.min(open, close);
      var volume = toNum(volumes[index]);
      candles.push({
        t: candles.length,
        o: open,
        h: high,
        l: low,
        c: close,
        v: volume == null ? 0 : volume,
        label: labels[index] || fmtShortTime(timestamps[index]) || String(index),
        timestamp: timestamps[index] || null,
      });
    }

    return candles;
  }

  function findNearestIndex(candles, isoTs) {
    if (!isoTs || !candles.length) return 0;
    var target = new Date(isoTs).getTime();
    if (Number.isNaN(target)) return 0;
    var bestIndex = 0;
    var bestDelta = Infinity;
    candles.forEach(function (candle, index) {
      var current = new Date(candle.timestamp || '').getTime();
      if (Number.isNaN(current)) return;
      var delta = Math.abs(current - target);
      if (delta < bestDelta) {
        bestDelta = delta;
        bestIndex = index;
      }
    });
    return bestIndex;
  }

  function buildChartMarkers(rows, candles) {
    var items = Array.isArray(rows) ? rows : [];
    if (!candles.length) return [];
    return items.map(function (row) {
      var index = findNearestIndex(candles, row && row.timestamp);
      var candle = candles[index] || candles[candles.length - 1];
      var type = String((row && row.type) || '').toLowerCase();
      return {
        t: index,
        price: candle ? candle.c : 0,
        side: row && row.side ? String(row.side) : (type === 'exit' ? 'sell' : 'buy'),
        shape: row && row.shape ? String(row.shape) : 'triangle',
        label: row && row.label ? String(row.label) : '',
        meta: row || {},
      };
    });
  }

  function sessionToPageData(payload) {
    var p = payload || {};
    var session = p.session || {};
    var today = p.today_summary || {};
    var overall = today.overall || {};
    var equity = today.equity || {};
    var counts = p.counts || {};
    var ops = p.ops_state || {};
    var engineContext = p.engine_context || {};
    var freshness = session.data_freshness || {};
    var oldestAgeSec = maxDefined([
      freshness.latest_vote_age_sec,
      freshness.latest_signal_age_sec,
      freshness.latest_position_age_sec,
    ]);
    var candles = buildSyntheticCandles(p.session_chart);
    var trades = (p.recent_trades || []).map(mapTrade);
    var votes = (p.recent_votes || []).map(mapVote);
    var signals = (p.recent_signals || []).map(mapSignal);
    var decisions = votes.length ? votes : signals;
    var alerts = (p.active_alerts || []).map(mapAlert);
    var strategyReturns = (today.by_strategy || [])
      .map(function (row) {
        return {
          label: row && row.strategy ? String(row.strategy) : '',
          value: toNum(row && row.net_return_pct) != null ? toNum(row.net_return_pct) * 100 : 0,
        };
      })
      .filter(function (row) { return row.label; })
      .sort(function (a, b) { return b.value - a.value; });

    return {
      source: 'api',
      raw: p,
      session: session,
      overall: overall,
      equity: equity,
      counts: counts,
      ops: ops,
      engineContext: engineContext,
      replayStatus: p.replay_status || null,
      latestCompletedRun: p.latest_completed_run || null,
      currentRunId: p.active_run_id || session.run_id || null,
      freshness: freshness,
      oldestAgeSec: oldestAgeSec,
      chart: {
        candles: candles,
        markers: buildChartMarkers(p.chart_markers || [], candles),
      },
      kpis: {
        runStatus: String(ops.engine_state || p.status || 'unknown').replace(/_/g, ' ').toUpperCase(),
        sessionPnl: fmtPercentFromRatio(equity.net_return_pct, 2),
        sessionPnlCls: toNum(equity.net_return_pct) == null ? '' : (toNum(equity.net_return_pct) >= 0 ? 'pos' : 'neg'),
        openPositions: Array.isArray(p.current_positions) ? p.current_positions.length : 0,
        tradesToday: overall.trade_count != null ? overall.trade_count : (counts.trades || counts.closed_trades || trades.length || 0),
        engineMode: String(engineContext.active_engine_mode || 'unknown').replace(/_/g, ' ').toUpperCase(),
        marketState: String(ops.market_state || 'unknown').toUpperCase(),
        dataFreshness: fmtAge(oldestAgeSec),
        dataFreshnessCls: oldestAgeSec == null ? '' : (oldestAgeSec <= 2.5 ? 'pos' : (oldestAgeSec <= 10 ? 'warn' : 'neg')),
        winRate: toNum(overall.win_rate),
        profitFactor: toNum(overall.profit_factor),
      },
      votes: decisions,
      signals: signals,
      trades: trades,
      alerts: alerts,
      strategyReturns: strategyReturns,
    };
  }

  function evalToPageData(summary, equityResp, tradesResp) {
    var summaryPayload = summary || {};
    var overall = summaryPayload.overall || {};
    var equityCurve = Array.isArray(equityResp && equityResp.equity_curve) ? equityResp.equity_curve : [];
    var tradeRows = Array.isArray(tradesResp && tradesResp.rows) ? tradesResp.rows : [];
    return {
      source: 'api',
      raw: {
        summary: summaryPayload,
        equity: equityResp || {},
        trades: tradesResp || {},
      },
      filters: summaryPayload.filters || {},
      overall: overall,
      counts: summaryPayload.counts || {},
      resolvedRunId: summaryPayload.resolved_run_id || null,
      byStrategy: (summaryPayload.by_strategy || []).map(function (row) {
        return {
          label: row && row.entry_strategy ? String(row.entry_strategy) : (row && row.strategy ? String(row.strategy) : ''),
          trades: row && row.trades != null ? Number(row.trades) : 0,
          value: toNum(row && row.total_capital_pnl_pct) != null
            ? toNum(row.total_capital_pnl_pct) * 100
            : (toNum(row && row.net_return_pct) != null ? toNum(row.net_return_pct) * 100 : 0),
        };
      }).filter(function (row) { return row.label; }),
      equityCurve: equityCurve.map(function (row) {
        var value = toNum(row && row.cumulative_return_pct);
        return value == null ? 0 : value * 100;
      }),
      dailyReturns: Array.isArray(equityResp && equityResp.daily_returns) ? equityResp.daily_returns : [],
      tradePnls: tradeRows.map(function (row) {
        var value = toNum(row && row.capital_pnl_pct);
        if (value == null) value = toNum(row && row.pnl_pct_net);
        return value == null ? 0 : value * 100;
      }),
      trades: tradeRows.map(mapTrade),
      kpis: {
        tradeCount: overall.trade_count != null ? Number(overall.trade_count) : 0,
        netReturn: fmtPercentFromRatio(overall.net_return_pct, 2),
        netReturnCls: toNum(overall.net_return_pct) == null ? '' : (toNum(overall.net_return_pct) >= 0 ? 'pos' : 'neg'),
        winRate: toNum(overall.win_rate),
        maxDrawdown: fmtPercentFromRatio(overall.max_drawdown_pct, 2),
        profitFactor: toNum(overall.profit_factor),
        sharpe: toNum(overall.sharpe),
        avgHoldBars: overall.avg_hold_bars != null ? Number(overall.avg_hold_bars) : null,
      },
    };
  }

  function healthToPageData(payload) {
    var p = payload || {};
    var checks = p.checks || {};
    var deps = p.dependencies || {};
    return {
      source: 'api',
      raw: p,
      overall: String(p.status || 'unknown'),
      ready: !!p.ready,
      timestamp: p.timestamp || '',
      checks: {
        marketDataApi: {
          ok: !!checks.market_data_api,
          detail: deps.market_data_api || {},
        },
        redis: {
          ok: !!checks.redis,
          detail: deps.redis || {},
        },
        liveStrategyService: {
          ok: !!checks.live_strategy_monitor_service,
          detail: deps.live_strategy_monitor_service || {},
        },
        strategyEvalService: {
          ok: !!checks.strategy_evaluation_service,
          detail: deps.strategy_evaluation_service || {},
        },
      },
    };
  }

  function cleanParams(params) {
    var out = {};
    Object.keys(params || {}).forEach(function (key) {
      var value = params[key];
      if (value == null || value === '') return;
      out[key] = value;
    });
    return out;
  }

  async function get(path, params) {
    var query = cleanParams(params || {});
    var qs = Object.keys(query).length ? ('?' + new URLSearchParams(query).toString()) : '';
    var response = await fetch(path + qs);
    if (!response.ok) {
      throw new Error(response.status + ' ' + response.statusText + ' (' + path + ')');
    }
    return response.json();
  }

  async function post(path, body) {
    var response = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    if (!response.ok) {
      var text = await response.text().catch(function () { return response.statusText; });
      throw new Error(response.status + ' ' + text + ' (' + path + ')');
    }
    return response.json();
  }

  global.DashAPI = {
    get: get,
    post: post,
    fetchLiveSession: function (params) { return get('/api/live/strategy/session', params); },
    fetchLiveTraces: function (params) { return get('/api/live/strategy/traces', params); },
    fetchHistoricalSession: function (params) { return get('/api/historical/replay/session', params); },
    fetchHistoricalStatus: function (params) { return get('/api/historical/replay/status', params); },
    fetchEvalSummary: function (params) { return get('/api/strategy/evaluation/summary', params); },
    fetchEvalEquity: function (params) { return get('/api/strategy/evaluation/equity', params); },
    fetchEvalTrades: function (params) { return get('/api/strategy/evaluation/trades', params); },
    fetchEvalDays: function (params) { return get('/api/strategy/evaluation/days', params); },
    fetchLatestEvalRun: function (params) { return get('/api/strategy/evaluation/runs/latest', params); },
    fetchEvalRun: function (runId) { return get('/api/strategy/evaluation/runs/' + encodeURIComponent(runId), {}); },
    fetchEvalRuns: function (params) { return get('/api/strategy/evaluation/runs', params || {}); },
    fetchDashHealth: function () { return get('/api/health', {}); },
    fetchMarketDataHealth: function () { return get('/api/market-data/health', {}); },
    fetchSystemMode: function () { return get('/api/v1/system/mode', {}); },
    buildSyntheticCandles: buildSyntheticCandles,
    buildChartMarkers: buildChartMarkers,
    sessionToPageData: sessionToPageData,
    evalToPageData: evalToPageData,
    healthToPageData: healthToPageData,
    mapVote: mapVote,
    mapSignal: mapSignal,
    mapTrade: mapTrade,
    mapAlert: mapAlert,
    mapSessionDay: mapSessionDay,
    normDir: normDir,
    esc: esc,
    fmtTime: fmtTime,
    fmtShortTime: fmtShortTime,
    fmtSigned: fmtSigned,
    fmtPercentFromRatio: fmtPercentFromRatio,
    fmtAge: fmtAge,
    fmtCompactInt: fmtCompactInt,
    fmtHold: fmtHold,
  };
})(window);
