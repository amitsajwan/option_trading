// Historical replay monitor.
(function () {
  var PAGE = 'historical_replay';
  var cache = null;
  var pending = null;
  var currentTrades = [];
  var currentVotes = [];
  var selectedAnalysis = null;
  var currentDayPage = 0;
  var DAY_PAGE_SIZE = 8;
  var pageData = getFallbackData(); // Initialize with fallback data

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

  function renderRunsTable(runs, activeRunId) {
    var items = Array.isArray(runs) ? runs : [];
    if (!items.length) {
      return '<div class="muted">No completed evaluation runs found. Run a replay to populate the registry.</div>';
    }
    var rows = items.map(function (run) {
      var rid = String(run.run_id || '').trim() || '--';
      var isActive = activeRunId && rid.startsWith(activeRunId);
      var dateFrom = String(run.date_from || '').slice(0, 10);
      var dateTo = String(run.date_to || '').slice(0, 10);
      var statusTag = String(run.status || '').toLowerCase();
      var statusCls = statusTag === 'completed' ? 'pos' : (statusTag === 'running' || statusTag === 'queued' ? 'warn' : 'neg');
      var trades = Number(run.trade_count != null ? run.trade_count : 0);
      var signals = Number(run.signal_count != null ? run.signal_count : 0);
      var badge = isActive ? '<span class="chip pos" style="margin-left:6px">Active</span>' : '';
      var rowClick = 'onclick="window.__selectRun && window.__selectRun(' + C.esc(JSON.stringify(rid)) + ', ' + C.esc(JSON.stringify(dateFrom)) + ', ' + C.esc(JSON.stringify(dateTo)) + ')"';
      return '<tr style="cursor:pointer" ' + rowClick + '>' +
        '<td class="mono">' + C.esc(rid.slice(0, 14)) + '…' + badge + '</td>' +
        '<td>' + C.esc(dateFrom) + ' → ' + C.esc(dateTo) + '</td>' +
        '<td><span class="tag ' + statusCls + '">' + C.esc(statusTag.toUpperCase()) + '</span></td>' +
        '<td class="mono">' + Number(trades).toLocaleString() + ' trades / ' + Number(signals).toLocaleString() + ' signals</td>' +
      '</tr>';
    });
    return '<table class="tbl">' +
      '<thead>' +
        '<tr>' +
          '<th>Run ID</th>' +
          '<th>Date Range</th>' +
          '<th>Status</th>' +
          '<th>Counts</th>' +
        '</tr>' +
      '</thead>' +
      '<tbody>' + rows.join('') + '</tbody>' +
    '</table>';
  }

  function formatRunShortId(runId) {
    if (!runId) return '--';
    return String(runId).slice(0, 18) + '…';
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

  function isoDateParts(value) {
    var text = String(value || '').trim();
    if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) return null;
    var parts = text.split('-');
    return { y: Number(parts[0]), m: Number(parts[1]), d: Number(parts[2]) };
  }

  function addDays(value, delta) {
    var parts = isoDateParts(value);
    if (!parts) return '';
    var dt = new Date(Date.UTC(parts.y, parts.m - 1, parts.d));
    dt.setUTCDate(dt.getUTCDate() + Number(delta || 0));
    return dt.toISOString().slice(0, 10);
  }

  function enumerateDates(start, end) {
    if (!start || !end || start > end) return [];
    var out = [];
    var day = start;
    while (day && day <= end) {
      out.push(day);
      if (day === end) break;
      day = addDays(day, 1);
    }
    return out;
  }

  function mergeEvalDaysWithRange(rangeFrom, rangeTo, days) {
    var rows = Array.isArray(days) ? days : [];
    var byDate = {};
    rows.forEach(function (day) {
      if (day && day.d) byDate[String(day.d)] = day;
    });
    var allDates = enumerateDates(rangeFrom, rangeTo);
    if (!allDates.length) return rows;
    return allDates.map(function (date) {
      return byDate[date] || { d: date, trades: 0, net: 0, status: 'completed', meta: {} };
    });
  }

  function pageForDate(days, date) {
    var items = Array.isArray(days) ? days : [];
    for (var i = 0; i < items.length; i += 1) {
      if (items[i] && items[i].d === date) return Math.floor(i / DAY_PAGE_SIZE);
    }
    return 0;
  }

  function visibleDays(days) {
    var items = Array.isArray(days) ? days : [];
    var totalPages = Math.max(1, Math.ceil(items.length / DAY_PAGE_SIZE));
    if (currentDayPage < 0) currentDayPage = 0;
    if (currentDayPage > totalPages - 1) currentDayPage = totalPages - 1;
    var start = currentDayPage * DAY_PAGE_SIZE;
    return {
      rows: items.slice(start, start + DAY_PAGE_SIZE),
      totalPages: totalPages,
      page: currentDayPage,
    };
  }

  function tradeKey(trade) {
    var meta = (trade && trade.meta) || {};
    return String(
      (trade && trade.signalId)
      || meta.signal_id
      || meta.position_id
      || [trade && trade.t, trade && trade.entry, trade && trade.exit].join('|')
    );
  }

  function voteKey(vote) {
    var meta = (vote && vote.meta) || {};
    return String(
      meta.signal_id
      || meta.snapshot_id
      || [meta.timestamp || vote.t, vote.strat, vote.dir, meta.signal_type || '', meta.decision_reason_code || ''].join('|')
    );
  }

  function findTradeByKey(trades, key) {
    var items = Array.isArray(trades) ? trades : [];
    for (var i = 0; i < items.length; i += 1) {
      if (tradeKey(items[i]) === key) return items[i];
    }
    return null;
  }

  function findVoteByKey(votes, key) {
    var items = Array.isArray(votes) ? votes : [];
    for (var i = 0; i < items.length; i += 1) {
      if (voteKey(items[i]) === key) return items[i];
    }
    return null;
  }

  function resolveSelectedAnalysis() {
    if (selectedAnalysis && selectedAnalysis.kind === 'trade') {
      var selectedTrade = findTradeByKey(currentTrades, selectedAnalysis.key);
      if (selectedTrade) return { kind: 'trade', key: tradeKey(selectedTrade), item: selectedTrade };
    }
    if (selectedAnalysis && selectedAnalysis.kind === 'vote') {
      var selectedVote = findVoteByKey(currentVotes, selectedAnalysis.key);
      if (selectedVote) return { kind: 'vote', key: voteKey(selectedVote), item: selectedVote };
    }
    if (currentTrades.length) return { kind: 'trade', key: tradeKey(currentTrades[0]), item: currentTrades[0] };
    if (currentVotes.length) return { kind: 'vote', key: voteKey(currentVotes[0]), item: currentVotes[0] };
    return null;
  }

  function fmtProb(value, digits) {
    var num = Number(value);
    if (!Number.isFinite(num)) return '--';
    return (num * 100).toFixed(digits == null ? 1 : digits) + '%';
  }

  function toEpochMillis(value) {
    if (!value) return null;
    var millis = new Date(String(value)).getTime();
    return Number.isFinite(millis) ? millis : null;
  }

  function nearestVoteForTimestamp(isoTs) {
    var targetMillis = toEpochMillis(isoTs);
    if (targetMillis == null || !currentVotes.length) return null;
    var nearest = null;
    var nearestDelta = Infinity;
    for (var i = 0; i < currentVotes.length; i += 1) {
      var vote = currentVotes[i];
      var voteTs = vote && vote.meta ? vote.meta.timestamp : null;
      var voteMillis = toEpochMillis(voteTs);
      if (voteMillis == null) continue;
      var delta = Math.abs(voteMillis - targetMillis);
      if (delta < nearestDelta) {
        nearest = vote;
        nearestDelta = delta;
      }
    }
    // Only show a decision when it's truly close to the hovered candle.
    return nearestDelta <= 90 * 1000 ? nearest : null;
  }

  function renderDecisionKv(label, value) {
    return '<div class="kv"><span class="k">' + C.esc(label) + '</span><span class="v">' + value + '</span></div>';
  }

  function renderTradeDecisionPanel(trade) {
    var meta = trade.meta || {};
    var metrics = trade.decisionMetrics && typeof trade.decisionMetrics === 'object'
      ? trade.decisionMetrics
      : (meta.signal_decision_metrics && typeof meta.signal_decision_metrics === 'object' ? meta.signal_decision_metrics : {});
    var confidence = Number.isFinite(Number(metrics.confidence)) ? Number(metrics.confidence) : Number(trade.signalConfidence);
    var summary = [
      trade.t || '--',
      trade.dir || '--',
      Number.isFinite(Number(trade.entry)) ? Number(trade.entry).toFixed(2) : '--',
      Number.isFinite(Number(trade.exit)) ? Number(trade.exit).toFixed(2) : '--'
    ];
    var items = [
      renderDecisionKv('Signal ID', '<span class="mono">' + C.esc(trade.signalId || meta.signal_id || '--') + '</span>'),
      renderDecisionKv('Reason', C.esc(trade.decisionReasonCode || meta.signal_decision_reason_code || '--')),
      renderDecisionKv('Entry prob', '<span class="mono">' + C.esc(fmtProb(metrics.entry_prob, 1)) + '</span>'),
      renderDecisionKv('Trade prob', '<span class="mono">' + C.esc(fmtProb(metrics.direction_trade_prob, 1)) + '</span>'),
      renderDecisionKv('Up prob', '<span class="mono">' + C.esc(fmtProb(metrics.direction_up_prob, 1)) + '</span>'),
      renderDecisionKv('CE prob', '<span class="mono">' + C.esc(fmtProb(metrics.ce_prob, 1)) + '</span>'),
      renderDecisionKv('PE prob', '<span class="mono">' + C.esc(fmtProb(metrics.pe_prob, 1)) + '</span>'),
      renderDecisionKv('Recipe prob', '<span class="mono">' + C.esc(fmtProb(metrics.recipe_prob, 1)) + '</span>'),
      renderDecisionKv('Recipe margin', '<span class="mono">' + C.esc(fmtProb(metrics.recipe_margin, 1)) + '</span>'),
      renderDecisionKv('Confidence', '<span class="mono">' + C.esc(fmtProb(confidence, 1)) + '</span>'),
    ];
    var hasDetailedMetrics = Object.keys(metrics).length > 0 || Number.isFinite(confidence);
    return '<div class="mono tiny" style="margin-bottom:10px;color:var(--ink-3)">' +
      C.esc(summary[0] + '  ' + summary[1] + '  ' + summary[2] + ' -> ' + summary[3]) +
    '</div>' +
    (hasDetailedMetrics
      ? '<div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px 16px">' + items.join('') + '</div>'
      : '<div class="muted">No staged decision metrics were stored on the linked signal for this trade.</div>');
  }

  function renderVoteDecisionPanel(vote) {
    var meta = vote.meta || {};
    var metrics = meta.decision_metrics && typeof meta.decision_metrics === 'object' ? meta.decision_metrics : {};
    var action = String(meta.signal_type || (vote.fired ? 'ENTRY' : 'HOLD') || '').toUpperCase() || '--';
    var state = vote.fired ? 'fired' : (meta.acted_on === false ? 'rejected' : 'held');
    var blockReason = meta.entry_warmup_reason || meta.policy_reason || '--';
    var items = [
      renderDecisionKv('Action', C.esc(action)),
      renderDecisionKv('State', C.esc(state)),
      renderDecisionKv('Strategy', C.esc(vote.strat || '--')),
      renderDecisionKv('Direction', C.esc(vote.dir || '--')),
      renderDecisionKv('Regime', C.esc(meta.regime || '--')),
      renderDecisionKv('Reason', C.esc(meta.decision_reason_code || meta.policy_reason || '--')),
      renderDecisionKv('Block reason', C.esc(blockReason)),
      renderDecisionKv('Confidence', '<span class="mono">' + C.esc(fmtProb(vote.conf, 1)) + '</span>'),
      renderDecisionKv('Policy', C.esc(meta.policy_allowed === true ? 'allowed' : (meta.policy_allowed === false ? 'blocked' : '--'))),
      renderDecisionKv('Entry prob', '<span class="mono">' + C.esc(fmtProb(metrics.entry_prob, 1)) + '</span>'),
      renderDecisionKv('Trade prob', '<span class="mono">' + C.esc(fmtProb(metrics.direction_trade_prob, 1)) + '</span>'),
      renderDecisionKv('Up prob', '<span class="mono">' + C.esc(fmtProb(metrics.direction_up_prob, 1)) + '</span>'),
      renderDecisionKv('CE prob', '<span class="mono">' + C.esc(fmtProb(metrics.ce_prob, 1)) + '</span>'),
      renderDecisionKv('PE prob', '<span class="mono">' + C.esc(fmtProb(metrics.pe_prob, 1)) + '</span>'),
      renderDecisionKv('Recipe prob', '<span class="mono">' + C.esc(fmtProb(metrics.recipe_prob, 1)) + '</span>'),
    ];
    return '<div class="mono tiny" style="margin-bottom:10px;color:var(--ink-3)">' +
      C.esc((vote.t || '--') + '  ' + action + '  ' + (vote.dir || '--')) +
    '</div>' +
    '<div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px 16px">' + items.join('') + '</div>';
  }

  function renderDecisionPanel(selected) {
    if (!selected || !selected.item) {
      return '<div class="muted">Click a trade row or chart marker to inspect why a trade was taken or rejected.</div>';
    }
    return selected.kind === 'vote'
      ? renderVoteDecisionPanel(selected.item)
      : renderTradeDecisionPanel(selected.item);
  }

  function buildTradeMarkers(trades, candles) {
    var items = Array.isArray(trades) ? trades : [];
    if (!candles || !candles.length || !items.length) return [];
    var rows = [];
    items.forEach(function (trade) {
      var meta = trade.meta || {};
      if (meta.entry_time) {
        rows.push({
          timestamp: meta.entry_time,
          type: 'entry',
          side: 'buy',
          label: trade.dir || '',
          analysisKind: 'trade',
          analysisRole: 'entry',
          analysisItem: trade,
          analysisLabel: 'Trade entry',
        });
      }
      if (meta.exit_time) {
        rows.push({
          timestamp: meta.exit_time,
          type: 'exit',
          side: 'sell',
          label: '',
          analysisKind: 'trade',
          analysisRole: 'exit',
          analysisItem: trade,
          analysisLabel: 'Trade exit',
        });
      }
    });
    return window.DashAPI.buildChartMarkers(rows, candles);
  }

  function buildVoteMarkers(votes, candles) {
    var items = Array.isArray(votes) ? votes : [];
    if (!candles || !candles.length || !items.length) return [];
    var rows = items
      .filter(function (vote) { return vote && vote.meta && vote.meta.timestamp; })
      .map(function (vote) {
        var meta = vote.meta || {};
        var action = String(meta.signal_type || (vote.fired ? 'ENTRY' : 'HOLD') || '').toUpperCase() || 'VOTE';
        return {
          timestamp: meta.timestamp,
          type: 'vote',
          side: 'neutral',
          shape: 'circle',
          label: '',
          analysisKind: 'vote',
          analysisRole: action.toLowerCase(),
          analysisItem: vote,
          analysisLabel: action + ' ' + (vote.dir || ''),
        };
      });
    return window.DashAPI.buildChartMarkers(rows, candles);
  }

  function buildReplayChartMarkers(trades, votes, candles, fallbackMarkers) {
    var enriched = [];
    if (Array.isArray(trades) && trades.length) enriched = enriched.concat(buildTradeMarkers(trades, candles));
    else if (Array.isArray(fallbackMarkers) && fallbackMarkers.length) enriched = enriched.concat(fallbackMarkers);
    if (Array.isArray(votes) && votes.length) enriched = enriched.concat(buildVoteMarkers(votes, candles));
    return enriched;
  }

  function chartMarkerTooltip(marker) {
    var meta = marker && marker.meta ? marker.meta : {};
    var item = meta.analysisItem;
    if (!item) return '';
    if (meta.analysisKind === 'vote') {
      var voteMeta = item.meta || {};
      var action = String(voteMeta.signal_type || (item.fired ? 'ENTRY' : 'HOLD') || '').toUpperCase() || 'VOTE';
      return '<div style="font-weight:600; color:#fff">' + C.esc(action + ' ' + (item.dir || '')) + '</div>' +
        '<div style="display:grid; grid-template-columns:auto auto; gap:2px 10px; margin-top:4px; color:#B9C2CC">' +
          '<span>Time</span><span style="color:#fff; text-align:right">' + C.esc(item.t || '--') + '</span>' +
          '<span>Conf</span><span style="color:#fff; text-align:right">' + C.esc(fmtProb(item.conf, 1)) + '</span>' +
          '<span>Why</span><span style="color:#fff; text-align:right">' + C.esc(voteMeta.decision_reason_code || voteMeta.policy_reason || '--') + '</span>' +
        '</div>';
    }
    var tradeMeta = item.meta || {};
    var tradeMetrics = item.decisionMetrics && typeof item.decisionMetrics === 'object' ? item.decisionMetrics : {};
    return '<div style="font-weight:600; color:#fff">' + C.esc((meta.analysisRole === 'exit' ? 'EXIT' : 'ENTRY') + ' ' + (item.dir || '')) + '</div>' +
      '<div style="display:grid; grid-template-columns:auto auto; gap:2px 10px; margin-top:4px; color:#B9C2CC">' +
        '<span>Time</span><span style="color:#fff; text-align:right">' + C.esc(item.t || '--') + '</span>' +
        '<span>Conf</span><span style="color:#fff; text-align:right">' + C.esc(fmtProb(item.signalConfidence != null ? item.signalConfidence : tradeMetrics.confidence, 1)) + '</span>' +
        '<span>Why</span><span style="color:#fff; text-align:right">' + C.esc(item.decisionReasonCode || tradeMeta.signal_decision_reason_code || '--') + '</span>' +
      '</div>';
  }

  function chartHoverTooltip(ctx) {
    var candle = ctx && ctx.candle ? ctx.candle : null;
    if (!candle) return '';
    var nearestVote = nearestVoteForTimestamp(candle.timestamp);
    var decisionBlock = '';
    if (nearestVote) {
      var meta = nearestVote.meta || {};
      var metrics = meta.decision_metrics && typeof meta.decision_metrics === 'object' ? meta.decision_metrics : {};
      var action = String(meta.signal_type || (nearestVote.fired ? 'ENTRY' : 'HOLD') || '').toUpperCase() || '--';
      var state = nearestVote.fired ? 'fired' : (meta.acted_on === false ? 'rejected' : 'held');
      decisionBlock =
        '<div style="margin-top:6px;padding-top:6px;border-top:1px solid rgba(185,194,204,0.25)">' +
          '<div style="font-weight:600;color:#fff">Decision @ ' + C.esc(nearestVote.t || '--') + '</div>' +
          '<div style="display:grid; grid-template-columns:auto auto; gap:2px 10px; margin-top:4px; color:#B9C2CC">' +
            '<span>Action</span><span style="color:#fff; text-align:right">' + C.esc(action + ' ' + (nearestVote.dir || '')) + '</span>' +
            '<span>State</span><span style="color:#fff; text-align:right">' + C.esc(state) + '</span>' +
            '<span>Reason</span><span style="color:#fff; text-align:right">' + C.esc(meta.decision_reason_code || meta.policy_reason || '--') + '</span>' +
            '<span>Regime</span><span style="color:#fff; text-align:right">' + C.esc(meta.regime || '--') + '</span>' +
            '<span>Entry prob</span><span style="color:#fff; text-align:right">' + C.esc(fmtProb(metrics.entry_prob, 1)) + '</span>' +
            '<span>Trade prob</span><span style="color:#fff; text-align:right">' + C.esc(fmtProb(metrics.direction_trade_prob, 1)) + '</span>' +
          '</div>' +
        '</div>';
    }
    return '<div style="font-weight:600; color:#fff">' + C.esc(candle.label || '--') + '</div>' +
      '<div style="display:grid; grid-template-columns:auto auto; gap:2px 10px; margin-top:4px; color:#B9C2CC">' +
        '<span>fut_open</span><span style="color:#fff; text-align:right">' + C.esc(Number(candle.o).toFixed(2)) + '</span>' +
        '<span>fut_high</span><span style="color:#fff; text-align:right">' + C.esc(Number(candle.h).toFixed(2)) + '</span>' +
        '<span>fut_low</span><span style="color:#fff; text-align:right">' + C.esc(Number(candle.l).toFixed(2)) + '</span>' +
        '<span>fut_close</span><span style="color:#fff; text-align:right">' + C.esc(Number(candle.c).toFixed(2)) + '</span>' +
        '<span>fut_volume</span><span style="color:#fff; text-align:right">' + C.esc(String(Number(candle.v).toLocaleString())) + '</span>' +
      '</div>' + decisionBlock;
  }

  function selectAnalysisFromMarker(marker) {
    var meta = marker && marker.meta ? marker.meta : {};
    var item = meta.analysisItem;
    if (!item) return;
    if (meta.analysisKind === 'vote') selectedAnalysis = { kind: 'vote', key: voteKey(item) };
    else selectedAnalysis = { kind: 'trade', key: tradeKey(item) };
    paintDecisionSelection();
  }

  function paintDecisionSelection() {
    var selected = resolveSelectedAnalysis();
    selectedAnalysis = selected ? { kind: selected.kind, key: selected.key } : null;
    document.querySelectorAll('#hr-trades-body tr').forEach(function (row, index) {
      var trade = currentTrades[index];
      var active = trade && selected && selected.kind === 'trade' && tradeKey(trade) === selected.key;
      row.style.cursor = trade ? 'pointer' : '';
      row.style.background = active ? 'rgba(17,24,39,0.04)' : '';
    });
    document.querySelectorAll('#hr-decisions-body tr').forEach(function (row, index) {
      var vote = currentVotes[index];
      var active = vote && selected && selected.kind === 'vote' && voteKey(vote) === selected.key;
      row.style.cursor = vote ? 'pointer' : '';
      row.style.background = active ? 'rgba(17,24,39,0.04)' : '';
    });
    var panel = document.getElementById('hr-trade-decision-body');
    if (panel) panel.innerHTML = renderDecisionPanel(selected);
  }

  function bindTradeHandlers(trades, votes) {
    currentTrades = Array.isArray(trades) ? trades : [];
    currentVotes = Array.isArray(votes) ? votes : [];
    document.querySelectorAll('#hr-trades-body tr').forEach(function (row, index) {
      var trade = currentTrades[index];
      row.onclick = trade ? function () {
        selectedAnalysis = { kind: 'trade', key: tradeKey(trade) };
        paintDecisionSelection();
      } : null;
    });
    document.querySelectorAll('#hr-decisions-body tr').forEach(function (row, index) {
      var vote = currentVotes[index];
      row.onclick = vote ? function () {
        selectedAnalysis = { kind: 'vote', key: voteKey(vote) };
        paintDecisionSelection();
      } : null;
    });
    paintDecisionSelection();
  }

  function renderDayChips(days, activeDateArg) {
    if (!days.length) return { html: '<div class="muted">No replay days available for the resolved run.</div>', totalPages: 1, page: 0 };
    var activeDate = activeDateArg || (days[0] && days[0].d);
    var pageInfo = visibleDays(days);
    var html = '<div style="display:grid;grid-template-columns:repeat(8,minmax(0,1fr));gap:8px">' + pageInfo.rows.map(function (day) {
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
    return { html: html, totalPages: pageInfo.totalPages, page: pageInfo.page };
  }

  function render(model) {
    var data = model || getFallbackData();
    var replay = data.replayStatus || {};
    var activeDate = data.activeDate || data.session.date_ist || '--';
    var rangeFrom = data.rangeFrom || replay.start_date || data.session.date_ist || '--';
    var rangeTo = data.rangeTo || replay.end_date || data.session.date_ist || '--';
    var rangeText = C.esc(rangeFrom) + ' -> ' + C.esc(rangeTo);
    var runId = data.currentRunId || (data.latestCompletedRun && data.latestCompletedRun.run_id) || '--';
    var engBadge = C.engineModeBadge(data.kpis && data.kpis.engineMode, runId);
    var dayInfo = renderDayChips(data.days || [], activeDate);

    return '<div id="historical-replay-view" data-source="' + C.esc(data.source || 'mock') + '">' +
      '<div class="page-head">' +
        '<div>' +
          '<div class="page-crumbs">Operator - Research</div>' +
          '<h1 class="page-title">Historical Replay Monitor</h1>' +
          '<p class="page-sub">Inspect replayed sessions and compare signal/trade alignment.</p>' +
        '</div>' +
        '<div style="display:flex;align-items:center;gap:10px;">' +
          C.dataSourceBadge({ source: data.source || 'mock', updatedAt: data._fetchedAt }) +
          engBadge +
        '</div>' +
      '</div>' +
      '<div class="page-actions">' +
          '<div class="field" style="flex-direction:row;align-items:center;gap:6px">' +
            '<span class="field-label">From</span>' +
            '<input id="replay-from" class="inp" type="date" value="' + C.esc(rangeFrom || '') + '" style="width:130px">' +
            '<span class="field-label">To</span>' +
            '<input id="replay-to" class="inp" type="date" value="' + C.esc(rangeTo || '') + '" style="width:130px">' +
            '<span class="field-label">Speed</span>' +
            '<input id="replay-speed" class="inp" type="number" value="' + C.esc(replay.speed != null ? replay.speed : 0) + '" style="width:60px">' +
          '</div>' +
          '<button id="btn-load-range" class="btn">Load range</button>' +
          '<button id="btn-run-replay" class="btn primary">Run replay</button>' +
          '<button id="btn-share-view" class="btn">Share view</button>' +
          '<span id="replay-run-status" style="font-size:12px;color:var(--ink-3);align-self:center"></span>' +
        '</div>' +
      '</div>' +

      C.kpiStrip(buildKpiItems(data), 6) +

      '<div class="panel">' +
        '<div class="panel-head">' +
          '<div class="panel-title">Session days <span class="count">evaluation</span></div>' +
          '<div class="row gap-s">' +
            '<span class="mono tiny muted" style="align-self:center">Page ' + String(dayInfo.page + 1) + '/' + String(dayInfo.totalPages) + '</span>' +
            '<button id="btn-day-prev" class="btn sm ghost"' + (dayInfo.page <= 0 ? ' disabled' : '') + '>Prev</button>' +
            '<button id="btn-day-next" class="btn sm ghost"' + (dayInfo.page >= dayInfo.totalPages - 1 ? ' disabled' : '') + '>Next</button>' +
          '</div>' +
        '</div>' +
        '<div class="panel-body" style="padding:12px">' + dayInfo.html + '</div>' +
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
            '<div class="panel-head"><div class="panel-title">Recent Runs</div><span class="count">select to inspect</span></div>' +
            '<div class="panel-body" id="hr-runs-table-body">' + renderRunsTable(data.runs, runId) + '</div>' +
            '<div class="panel-foot" style="display:flex;justify-content:space-between;align-items:center;padding:10px 12px;border-top:1px solid var(--line-1)">' +
              '<span class="muted tiny">Click a row to pin that run and load its trades</span>' +
              '<button class="btn sm" id="btn-refresh-runs">Refresh list</button>' +
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
        '<div class="panel" style="grid-column:span 3">' +
          '<div class="panel-head"><div class="panel-title">Trades - <span id="hr-trades-label">' + C.esc(activeDate) + '</span> <span id="hr-trades-count" class="count">' + data.trades.length + '</span></div></div>' +
          '<div class="panel-body flush">' +
            '<table class="tbl">' +
              C.TRADE_TABLE_HEADER +
              '<tbody id="hr-trades-body">' + C.tradeTableRows(data.trades) + '</tbody>' +
            '</table>' +
          '</div>' +
        '</div>' +
        '<div class="panel" style="grid-column:span 3">' +
          '<div class="panel-head"><div class="panel-title">Decision Grid - <span id="hr-decisions-label">' + C.esc(activeDate) + '</span> <span id="hr-decisions-count" class="count">' + data.votes.length + '</span></div></div>' +
          '<div class="panel-body flush" style="max-height:340px;overflow:auto">' +
            '<table class="tbl">' +
              C.DECISION_GRID_HEADER +
              '<tbody id="hr-decisions-body">' + C.decisionGridRows(data.votes) + '</tbody>' +
            '</table>' +
          '</div>' +
        '</div>' +
      '</div>' +

      '<div class="panel">' +
        '<div class="panel-head"><div class="panel-title">Decision analysis</div><span class="count">table or chart</span></div>' +
        '<div id="hr-trade-decision-body" class="panel-body">' + renderDecisionPanel(
          data.trades.length ? { kind: 'trade', item: data.trades[0] } : (data.votes.length ? { kind: 'vote', item: data.votes[0] } : null)
        ) + '</div>' +
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
    var chartMarkers = buildReplayChartMarkers(d.trades, d.votes, candles, (d.chart && d.chart.markers) || []);
    el.__chart = window.InteractiveChart.mount(el, {
      candles: candles,
      markers: chartMarkers,
      onMarkerSelect: selectAnalysisFromMarker,
      formatMarkerTooltip: chartMarkerTooltip,
      formatHoverTooltip: chartHoverTooltip,
    });
  }

  async function loadData(dateFrom, dateTo, runId) {
    if (!window.DashAPI) throw new Error('DashAPI is not loaded');

    // Get date and run_id from URL params if available
    var urlParams = new URLSearchParams(window.location.search);
    var urlDate = urlParams.get('date');
    var urlRunId = urlParams.get('run_id');

    var replayStatus = await window.DashAPI.fetchHistoricalStatus({});
    var latestRunId = replayStatus.latest_completed_run_id;

    // Fetch available runs to validate the URL run_id
    var runsRes = await window.DashAPI.fetchEvalRuns({ dataset: 'historical', status: 'completed', limit: 20 }).catch(function () { return { rows: [] }; });
    var availableRunIds = (runsRes.rows || []).map(function (r) { return r.run_id; });

    // Validate run_id: use provided, then URL, then latest - but only if valid
    var requestedRunId = runId || urlRunId;
    var resolvedRunId;
    if (requestedRunId && availableRunIds.indexOf(requestedRunId) >= 0) {
      resolvedRunId = requestedRunId;
    } else if (latestRunId && availableRunIds.indexOf(latestRunId) >= 0) {
      resolvedRunId = latestRunId;
    } else if (availableRunIds.length > 0) {
      resolvedRunId = availableRunIds[0];
    } else {
      resolvedRunId = requestedRunId || latestRunId || undefined;
    }

    // Use URL date, then pageData, then resolved run's start_date, then fallback
    var resolvedRun = (runsRes.rows || []).find(function (r) { return r.run_id === resolvedRunId; }) || {};
    var runDate = resolvedRun.date_from || resolvedRun.start_date;
    var sessionDate = urlDate || pageData.activeDate || runDate || '2024-07-01';
    var rangeFrom = dateFrom || pageData.rangeFrom || runDate || '';
    var rangeTo = dateTo || pageData.rangeTo || runDate || '';

    // Use already-fetched runs for the table
    var runsPromise = Promise.resolve(runsRes);

    var results = await Promise.all([
      window.DashAPI.fetchHistoricalSession({
        date: sessionDate,
        run_id: resolvedRunId,
        limit_votes: 2000,
        limit_signals: 5000,
        // omit limit_trades — fetching from eval API below, session trades overridden
      }),
      window.DashAPI.fetchEvalDays({
        dataset: 'historical',
        date_from: rangeFrom,
        date_to: rangeTo,
        run_id: resolvedRunId,
        page: 1,
        page_size: 500,
      }).catch(function () { return { rows: [], total: 0, no_runs: true }; }),
      window.DashAPI.fetchEvalTrades({
        dataset: 'historical',
        date_from: rangeFrom,
        date_to: rangeTo,
        run_id: resolvedRunId,
        page: 1,
        page_size: 50,
      }).catch(function () { return { rows: [] }; }),
      runsPromise,
    ]);

    var session = results[0] || {};
    var runsRes = results[3] || { rows: [] };
    pageData = window.DashAPI.sessionToPageData(session);
    pageData.runs = Array.isArray(runsRes.rows) ? runsRes.rows : [];
    pageData._runsTotal = runsRes.total || 0;
    pageData.replayStatus = replayStatus;
    pageData.rangeFrom = rangeFrom;
    pageData.rangeTo = rangeTo;
    pageData.replayStatus = Object.assign({}, replayStatus, {
      start_date: rangeFrom,
      end_date: rangeTo,
    });
    pageData.days = mergeEvalDaysWithRange(rangeFrom, rangeTo, (results[1].rows || []).map(window.DashAPI.mapSessionDay));
    pageData.activeDate = sessionDate;
    pageData.session = Object.assign({}, pageData.session || {}, {
      date_ist: sessionDate,
    });
    currentDayPage = pageForDate(pageData.days, pageData.activeDate);

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
    pageData._fetchedAt = new Date().toISOString();
    return pageData;
  }

  function onLoadError(err) {
    C.showToast({ type: 'error', message: 'Failed to load historical data: ' + String(err && err.message ? err.message : err).slice(0, 60), action: { label: 'Retry', onClick: function () { cache = null; pending = null; loadData(pageData.rangeFrom, pageData.rangeTo).then(function (d) { cache = d; var r = document.getElementById('page'); if (r) { r.innerHTML = render(d); attachHandlers(d); mountChart(d); } }); } } });
  }

  function loadDay(date, runId) {
    // Update chart + trades in-place for a single clicked day — no full re-render.
    var tradesBody = document.getElementById('hr-trades-body');
    var tradesLabel = document.getElementById('hr-trades-label');
    var tradesCount = document.getElementById('hr-trades-count');
    var decisionsBody = document.getElementById('hr-decisions-body');
    var decisionsCount = document.getElementById('hr-decisions-count');
    var decisionsLabel = document.getElementById('hr-decisions-label');
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
        limit_votes: 2000,
        limit_signals: 5000,
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
      if (decisionsBody) decisionsBody.innerHTML  = C.decisionGridRows(sessionData.votes);
      if (decisionsCount) decisionsCount.textContent = sessionData.votes.length;
      if (decisionsLabel) decisionsLabel.textContent = date;
      if (sessionDate) sessionDate.textContent = date;
      bindTradeHandlers(trades, sessionData.votes);

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
      if (cache) {
        cache.activeDate = date;
        cache.trades = trades;
        cache.votes = sessionData.votes;
        cache.chart = { candles: candles || [], markers: markers };
        cache.session = Object.assign({}, cache.session || {}, sessionData.session || {}, { date_ist: date });
      }
      mountChart({ trades: trades, votes: sessionData.votes, chart: { candles: candles || [], markers: markers } });
    }).catch(function (err) {
      console.error('Load day failed:', err);
      if (tradesBody) tradesBody.innerHTML = C.emptyRow(8, 'Failed to load day data.');
      bindTradeHandlers([], []);
    });
  }

  function attachHandlers(data) {
    var root = document.getElementById('page');
    var btnRun = document.getElementById('btn-run-replay');
    var btnLoad = document.getElementById('btn-load-range');
    var btnDayPrev = document.getElementById('btn-day-prev');
    var btnDayNext = document.getElementById('btn-day-next');
    var statusEl = document.getElementById('replay-run-status');
    var runId = data && (data.currentRunId || (data.replayStatus && data.replayStatus.latest_completed_run_id));

    // Day chip clicks — filter table + chart to that specific day.
    document.querySelectorAll('.day-chip').forEach(function (chip) {
      chip.addEventListener('click', function () {
        var date = chip.getAttribute('data-date');
        if (date) loadDay(date, runId);
      });
    });

    function rerenderCachedPage() {
      if (!cache || !root) return;
      root.innerHTML = render(cache);
      attachHandlers(cache);
      mountChart(cache);
    }

    if (btnDayPrev) {
      btnDayPrev.addEventListener('click', function () {
        if (currentDayPage <= 0) return;
        currentDayPage -= 1;
        rerenderCachedPage();
      });
    }

    if (btnDayNext) {
      btnDayNext.addEventListener('click', function () {
        currentDayPage += 1;
        rerenderCachedPage();
      });
    }

    function getInputs() {
      return {
        dateFrom: (document.getElementById('replay-from') || {}).value || '',
        dateTo: (document.getElementById('replay-to') || {}).value || '',
        speed: parseFloat((document.getElementById('replay-speed') || {}).value || '0') || 0,
      };
    }

    function validateDateRange(dateFrom, dateTo) {
      var errors = [];
      if (!dateFrom || !dateTo) { errors.push('Both From and To dates are required'); }
      if (dateFrom && dateTo && dateFrom > dateTo) { errors.push('From date must be before To date'); }
      var today = new Date().toISOString().slice(0, 10);
      if (dateFrom > today) { errors.push('From date cannot be in the future'); }
      if (dateTo > today) { errors.push('To date cannot be in the future'); }
      return errors;
    }

    function showValidationErrors(errors) {
      C.showToast({ type: 'warn', message: errors.join(' • ') });
    }

    if (btnLoad) {
      btnLoad.addEventListener('click', function () {
        var inp = getInputs();
        var validationErrors = validateDateRange(inp.dateFrom, inp.dateTo);
        if (validationErrors.length) { showValidationErrors(validationErrors); return; }
        cache = null;
        pending = loadData(inp.dateFrom, inp.dateTo)
          .then(function (data) {
            cache = data;
            if (root) { root.innerHTML = render(data); attachHandlers(data); mountChart(data); }
          })
          .catch(function (err) { console.error('Load range failed:', err); })
          .finally(function () { pending = null; });
      });
    }

    if (btnRun) {
      btnRun.addEventListener('click', function () {
        var inp = getInputs();
        var validationErrors = validateDateRange(inp.dateFrom, inp.dateTo);
        if (validationErrors.length) { showValidationErrors(validationErrors); return; }
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

    var btnRefreshRuns = document.getElementById('btn-refresh-runs');
    if (btnRefreshRuns) {
      btnRefreshRuns.addEventListener('click', function () {
        var inp = getInputs();
        window.DashAPI.fetchEvalRuns({ dataset: 'historical', status: 'completed', limit: 20, include_counts: '1' }).then(function (res) {
          var rows = res && res.rows ? res.rows : [];
          pageData.runs = rows;
          pageData._runsTotal = res && res.total ? res.total : 0;
          var tbody = document.getElementById('hr-runs-table-body');
          var currentRunId = (data && (data.currentRunId || (data.replayStatus && data.replayStatus.latest_completed_run_id))) || '';
          if (tbody) { tbody.innerHTML = renderRunsTable(rows, currentRunId); }
        }).catch(function (err) { console.error('Refresh runs failed:', err); });
      });
    }

    var btnShare = document.getElementById('btn-share-view');
    if (btnShare && !btnShare.__wired) {
      btnShare.__wired = true;
      btnShare.addEventListener('click', function () {
        var inp = getInputs();
        var runId = data && (data.currentRunId || (data.replayStatus && data.replayStatus.latest_completed_run_id)) || '';
        var qs = new URLSearchParams();
        qs.set('date_from', inp.dateFrom || '');
        qs.set('date_to', inp.dateTo || '');
        qs.set('run_id', runId);
        qs.set('date', pageData.activeDate || inp.dateFrom || '');
        var shareUrl = window.location.origin + window.location.pathname + '?' + qs.toString();
        navigator.clipboard.writeText(shareUrl).then(function () {
          C.showToast({ type: 'success', message: 'URL copied to clipboard' });
        }).catch(function () {
          // Fallback for browsers without clipboard API
          window.prompt('Copy this URL:', shareUrl);
        });
      });
    }

    bindTradeHandlers((data && data.trades) || [], (data && data.votes) || []);
  }

  function selectRunAndReload(runId, dateFrom, dateTo) {
    // Update URL and reload to switch to selected run
    var qs = new URLSearchParams(window.location.search);
    qs.set('run_id', runId);
    qs.set('date_from', dateFrom || '');
    qs.set('date_to', dateTo || '');
    qs.set('date', pageData && pageData.activeDate ? pageData.activeDate : (dateFrom || ''));
    window.history.replaceState({}, '', window.location.pathname + '?' + qs.toString());
    cache = null;
    pending = loadData(dateFrom, dateTo, runId).then(function (data) {
      cache = data;
      if (window.__opCurrentPage !== PAGE) return;
      var root = document.getElementById('page');
      if (root) { root.innerHTML = render(data); attachHandlers(data); mountChart(data); }
    }).catch(function (err) { console.error('Select run failed:', err); })
    .finally(function () { pending = null; });
  }
  window.__selectRun = selectRunAndReload;

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
            var root = document.getElementById('page');
            cache = null;
            loadData(dateFrom, dateTo, runId).then(function (data) {
              cache = data;
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
        onLoadError(err);
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
