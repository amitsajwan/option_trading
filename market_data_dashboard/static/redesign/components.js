// Shared UI components for all redesign pages.
// Requires: api.js (for DashAPI formatters)
(function (global) {
  'use strict';

  // ── Utilities ─────────────────────────────────────────────────────────────

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"]/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch];
    });
  }

  function fmtSigned(value, digits, suffix) {
    var num = Number(value);
    if (!Number.isFinite(num)) return '--';
    return (num >= 0 ? '+' : '') + num.toFixed(digits == null ? 2 : digits) + (suffix || '');
  }

  function emptyRow(colspan, text) {
    return '<tr><td colspan="' + colspan + '" class="muted" style="text-align:center;padding:18px">' + esc(text) + '</td></tr>';
  }

  function fmtProbCell(value) {
    var num = Number(value);
    if (!Number.isFinite(num)) return '--';
    return (num * 100).toFixed(1) + '%';
  }

  // ── Engine mode badge ─────────────────────────────────────────────────────
  //
  // mode: raw string from API — 'ML PURE', 'ml_pure', 'DETERMINISTIC', 'HYBRID', etc.
  // detail: optional subtitle string (e.g. run_id or model group)
  //
  // Badge color:
  //   ml_pure       → green  (model inference active)
  //   deterministic → blue-gray (rule-based)
  //   hybrid        → amber (ML + rules)
  //   unknown / other → muted

  function engineModeBadge(mode, detail) {
    var normalized = String(mode || 'unknown').toLowerCase().replace(/[\s_]+/g, '_');
    var label = String(mode || 'UNKNOWN').replace(/_/g, ' ').toUpperCase();

    var palette = {
      ml_pure:       { bg: 'var(--pos)',    fg: '#fff' },
      deterministic: { bg: '#4a5568',       fg: '#fff' },
      hybrid:        { bg: '#b45309',       fg: '#fff' },
    };
    var colors = palette[normalized] || { bg: 'var(--line-2)', fg: 'var(--ink-3)' };

    var badge = '<span style="' +
        'display:inline-flex;align-items:center;gap:5px;' +
        'padding:3px 9px;border-radius:4px;' +
        'font-size:11px;font-weight:700;letter-spacing:0.05em;' +
        'background:' + colors.bg + ';color:' + colors.fg + ';' +
        'font-family:var(--f-mono)">' +
      '<span style="width:6px;height:6px;border-radius:50%;background:currentColor;opacity:0.75"></span>' +
      esc(label) +
    '</span>';

    if (detail) {
      badge += '<span class="muted mono tiny" style="margin-left:6px">' + esc(detail) + '</span>';
    }
    return badge;
  }

  // ── KPI strip ─────────────────────────────────────────────────────────────
  //
  // items: [{ label, value, cls?, sub }]
  //   label — plain text
  //   value — plain text or safe HTML string
  //   cls   — optional CSS class on kpi-value (e.g. 'pos', 'neg')
  //   sub   — plain text or safe HTML string (shown below value)
  // cols: --cols CSS var (default items.length)

  function kpiStrip(items, cols) {
    var colCount = cols || items.length || 6;
    return '<div class="kpi-strip" style="--cols:' + colCount + '">' +
      items.map(function (item) {
        return '<div class="kpi">' +
          '<div class="kpi-label">' + esc(item.label) + '</div>' +
          '<div class="kpi-value ' + (item.cls || '') + '">' + (item.value != null ? item.value : '--') + '</div>' +
          '<div class="kpi-sub">' + (item.sub || '') + '</div>' +
        '</div>';
      }).join('') +
    '</div>';
  }

  // ── Trade table rows ──────────────────────────────────────────────────────
  //
  // Unified 8-column layout (same in live and historical):
  //   Time | Strategy | Dir | Qty* | Entry* | Exit* | PnL | Hold*
  //   (* = mobile-hide)
  //
  // Matching <thead>:
  //   <tr><th>Time</th><th>Strategy</th><th>Dir</th>
  //       <th class="r mobile-hide">Qty</th><th class="r mobile-hide">Entry</th>
  //       <th class="r mobile-hide">Exit</th><th class="r">PnL</th>
  //       <th class="r mobile-hide">Hold</th></tr>

  function tradeTableRows(trades) {
    if (!trades || !trades.length) return emptyRow(8, 'No trades in this session.');
    return trades.map(function (trade) {
      var dirCls = trade.dir === 'LONG' ? 'pos' : (trade.dir === 'SHORT' ? 'neg' : '');
      var pnlCls = Number(trade.pnlPct) >= 0 ? 'pos' : 'neg';
      var entry = Number.isFinite(Number(trade.entry)) ? Number(trade.entry).toFixed(2) : '--';
      var exit  = Number.isFinite(Number(trade.exit))  ? Number(trade.exit).toFixed(2)  : '--';
      return '<tr>' +
        '<td class="muted">' + esc(trade.t) + '</td>' +
        '<td>' + (trade.strat ? esc(trade.strat) : '<span class="muted">--</span>') + '</td>' +
        '<td><span class="chip ' + dirCls + '">' + esc(trade.dir) + '</span></td>' +
        '<td class="r mobile-hide">' + esc(String(trade.qty != null ? trade.qty : '--')) + '</td>' +
        '<td class="r mobile-hide">' + entry + '</td>' +
        '<td class="r mobile-hide">' + exit + '</td>' +
        '<td class="r ' + pnlCls + '">' + fmtSigned(trade.pnlPct != null ? trade.pnlPct : 0, 2, '%') + '</td>' +
        '<td class="r mobile-hide">' + esc(trade.hold || '--') + '</td>' +
      '</tr>';
    }).join('');
  }

  // Canonical thead for the 8-col trade table.
  var TRADE_TABLE_HEADER = '<thead><tr>' +
    '<th>Time</th><th>Strategy</th><th>Dir</th>' +
    '<th class="r mobile-hide">Qty</th>' +
    '<th class="r mobile-hide">Entry</th>' +
    '<th class="r mobile-hide">Exit</th>' +
    '<th class="r">PnL</th>' +
    '<th class="r mobile-hide">Hold</th>' +
  '</tr></thead>';

  // ── Toast notifications ───────────────────────────────────────────────────
  //
  // Simple toast stack for errors, warnings, success messages.
  // Usage: QComponents.showToast({ type: 'error', message: 'Failed to load', action: { label: 'Retry', onClick: fn } })

  var toastContainer = null;
  function ensureToastContainer() {
    if (toastContainer) return toastContainer;
    toastContainer = document.createElement('div');
    toastContainer.id = 'toast-container';
    toastContainer.style.cssText = 'position:fixed;top:12px;right:12px;z-index:1000;display:flex;flex-direction:column;gap:8px;';
    document.body.appendChild(toastContainer);
    return toastContainer;
  }

  function showToast(opts) {
    opts = opts || {};
    var type = opts.type || 'info'; // info, success, warn, error
    var message = opts.message || '';
    var duration = opts.duration || (type === 'error' ? 0 : 4000);
    var action = opts.action;

    var el = document.createElement('div');
    var colors = {
      info:  { bg: 'var(--info-wash)',  border: 'var(--info)',  icon: 'ℹ' },
      success:{ bg: 'var(--pos-wash)',   border: 'var(--pos)',   icon: '✓' },
      warn:  { bg: 'var(--warn-wash)',  border: 'var(--warn)',  icon: '!' },
      error: { bg: 'rgba(194,62,47,0.12)', border: 'var(--neg)', icon: '✕' },
    }[type] || colors.info;

    el.style.cssText = 'background:' + colors.bg + ';border-left:3px solid ' + colors.border + ';padding:10px 14px;border-radius:4px;box-shadow:0 4px 12px rgba(0,0,0,0.08);min-width:260px;max-width:400px;font-size:12px;color:var(--ink-2);';

    var html = '<div style="display:flex;align-items:flex-start;gap:8px">' +
      '<span style="font-weight:700;color:' + colors.border + '">' + esc(colors.icon) + '</span>' +
      '<div style="flex:1">' + esc(message) + '</div>';

    if (action && action.label) {
      html += '<button class="btn sm" style="margin-left:8px">' + esc(action.label) + '</button>';
    }
    html += '</div>';
    el.innerHTML = html;

    if (action && action.label && action.onClick) {
      el.querySelector('button').addEventListener('click', function () {
        try { action.onClick(); } catch (e) {}
        if (el.parentNode) el.parentNode.removeChild(el);
      });
    }

    var closeBtn = document.createElement('button');
    closeBtn.textContent = '×';
    closeBtn.style.cssText = 'position:absolute;top:4px;right:6px;background:none;border:none;color:var(--ink-3);cursor:pointer;font-size:14px;line-height:1;';
    closeBtn.onclick = function () { if (el.parentNode) el.parentNode.removeChild(el); };
    el.style.position = 'relative';
    el.appendChild(closeBtn);

    ensureToastContainer().appendChild(el);

    if (duration > 0) {
      setTimeout(function () {
        if (el.parentNode) el.parentNode.removeChild(el);
      }, duration);
    }
    return el;
  }

  // ── Modal dialogs ─────────────────────────────────────────────────────────
  //
  // Confirmation modals for destructive actions.
  // Usage: QComponents.confirm({ title, message, confirmText, danger }).then(ok => { if (ok) doIt(); });

  var activeModal = null;
  function confirmModal(opts) {
    opts = opts || {};
    var title = opts.title || 'Confirm';
    var message = opts.message || '';
    var confirmText = opts.confirmText || 'Confirm';
    var cancelText = opts.cancelText || 'Cancel';
    var danger = !!opts.danger;
    var requireType = opts.requireType || ''; // e.g., "HALT" — user must type this

    return new Promise(function (resolve) {
      if (activeModal) { resolve(false); return; }

      var overlay = document.createElement('div');
      overlay.style.cssText = 'position:fixed;inset:0;background:rgba(11,15,20,0.45);z-index:999;display:flex;align-items:center;justify-content:center;';

      var box = document.createElement('div');
      box.style.cssText = 'background:var(--paper);border:1px solid var(--line-2);border-radius:6px;padding:18px 20px;min-width:320px;max-width:460px;box-shadow:0 10px 40px rgba(0,0,0,0.15);';

      var html = '<div style="font-weight:600;font-size:14px;margin-bottom:8px;color:var(--ink);">' + esc(title) + '</div>' +
        '<div style="font-size:12px;line-height:1.5;color:var(--ink-2);margin-bottom:14px;">' + esc(message) + '</div>';

      if (requireType) {
        html += '<div style="margin-bottom:12px;">' +
          '<div style="font-size:11px;color:var(--ink-3);margin-bottom:4px;">Type <b>' + esc(requireType) + '</b> to confirm</div>' +
          '<input id="modal-confirm-input" class="inp" style="width:100%" placeholder="' + esc(requireType) + '">' +
        '</div>';
      }

      html += '<div style="display:flex;justify-content:flex-end;gap:8px;">' +
        '<button id="modal-cancel" class="btn">' + esc(cancelText) + '</button>' +
        '<button id="modal-confirm" class="btn ' + (danger ? 'danger' : 'primary') + '" ' + (requireType ? 'disabled' : '') + '>' + esc(confirmText) + '</button>' +
      '</div>';

      box.innerHTML = html;
      overlay.appendChild(box);
      document.body.appendChild(overlay);
      activeModal = overlay;

      var input = requireType ? box.querySelector('#modal-confirm-input') : null;
      var confirmBtn = box.querySelector('#modal-confirm');

      function close(result) {
        if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
        activeModal = null;
        resolve(result);
      }

      if (input) {
        input.addEventListener('input', function () {
          confirmBtn.disabled = input.value.trim() !== requireType;
        });
        input.focus();
      }

      box.querySelector('#modal-cancel').addEventListener('click', function () { close(false); });
      confirmBtn.addEventListener('click', function () { close(true); });
      overlay.addEventListener('click', function (e) { if (e.target === overlay) close(false); });
    });
  }

  // ── Skeleton loading ────────────────────────────────────────────────────────
  //
  // Shimmer placeholder for loading states.

  function skeleton(width, height) {
    var w = width || '100%';
    var h = height || '20px';
    return '<div style="width:' + w + ';height:' + h + ';background:var(--paper-3);border-radius:4px;position:relative;overflow:hidden;">' +
      '<div style="position:absolute;inset:0;background:linear-gradient(90deg,transparent 0%,rgba(255,255,255,0.4) 50%,transparent 100%);animation:shimmer 1.5s infinite;"></div>' +
    '</div>';
  }

  // ── Data source badge ──────────────────────────────────────────────────────
  //
  // Indicates mock vs live vs cached vs stale

  function dataSourceBadge(opts) {
    opts = opts || {};
    var source = opts.source || 'mock'; // mock, live, cached, error
    var updatedAt = opts.updatedAt; // ISO timestamp or null
    var error = opts.error;

    var labels = { mock: 'Mock', live: 'Live', cached: 'Cached', error: 'Error', stale: 'Stale' };
    var colors = {
      mock:  { bg: 'var(--line-2)',      fg: 'var(--ink-3)' },
      live:  { bg: 'var(--pos-wash)',    fg: 'var(--pos)' },
      cached:{ bg: 'var(--info-wash)',  fg: 'var(--info)' },
      stale: { bg: 'var(--warn-wash)',   fg: 'var(--warn)' },
      error: { bg: 'rgba(194,62,47,0.15)', fg: 'var(--neg)' },
    };

    var label = error ? (labels.error + ': ' + String(error).slice(0, 30)) : labels[source];
    var c = colors[source] || colors.mock;

    var timeText = '';
    if (updatedAt && source !== 'mock' && source !== 'error') {
      var age = Date.now() - new Date(updatedAt).getTime();
      var mins = Math.floor(age / 60000);
      timeText = mins < 1 ? 'just now' : mins + 'm ago';
    }

    return '<span style="display:inline-flex;align-items:center;gap:5px;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:500;background:' + c.bg + ';color:' + c.fg + ';">' +
      '<span style="width:6px;height:6px;border-radius:50%;background:currentColor;opacity:0.8;"></span>' +
      esc(label) + (timeText ? ' · ' + timeText : '') +
    '</span>';
  }

  // ── Run selector component ─────────────────────────────────────────────────
  //
  // Dropdown for selecting runs with metadata display.

  function runSelector(opts) {
    opts = opts || {};
    var runs = opts.runs || [];
    var value = opts.value || '';
    var onChange = opts.onChange;
    var placeholder = opts.placeholder || 'Select a run…';

    var id = 'run-sel-' + Math.random().toString(36).slice(2, 8);

    var itemsHtml = runs.map(function (run) {
      var rid = String(run.run_id || '').trim();
      var shortId = rid.slice(0, 14) + (rid.length > 14 ? '…' : '');
      var dateFrom = String(run.date_from || '').slice(0, 10);
      var dateTo = String(run.date_to || '').slice(0, 10);
      var status = String(run.status || '').toLowerCase();
      var trades = Number(run.trade_count || 0);
      var selected = rid === value ? 'selected' : '';
      return '<option value="' + esc(rid) + '" ' + selected + '>' +
        esc(shortId) + ' | ' + esc(dateFrom) + '→' + esc(dateTo) + ' | ' + esc(status) + ' | ' + trades + ' trades' +
      '</option>';
    }).join('');

    if (!runs.length) {
      itemsHtml = '<option disabled>No runs available</option>';
    }

    var html = '<select id="' + id + '" class="inp" style="min-width:280px;max-width:400px;">' +
      '<option value="" disabled ' + (value ? '' : 'selected') + '>' + esc(placeholder) + '</option>' +
      itemsHtml +
    '</select>';

    // Return HTML and a wire function to attach events after insertion
    return {
      html: html,
      wire: function () {
        var el = document.getElementById(id);
        if (!el || !onChange) return;
        el.addEventListener('change', function () {
          var selected = runs.find(function (r) { return String(r.run_id) === String(el.value); });
          onChange(el.value, selected);
        });
      }
    };
  }

  // ── Vote / signal table rows ──────────────────────────────────────────────
  //
  // 5-column layout:
  //   Time | Strategy | Dir | Conf | State
  //
  // Matching <thead>:
  //   <tr><th>Time</th><th>Strategy</th><th>Dir</th><th class="r">Conf</th><th>State</th></tr>

  function voteTableRows(votes) {
    if (!votes || !votes.length) return emptyRow(5, 'No signals in this session.');
    return votes.map(function (vote) {
      var dirCls = vote.dir === 'LONG' ? 'pos' : (vote.dir === 'SHORT' ? 'neg' : '');
      var stateHtml = vote.fired
        ? '<span class="chip pos"><span class="dot"></span>fired</span>'
        : ((vote.meta && vote.meta.acted_on === false)
          ? '<span class="chip neg"><span class="dot"></span>rejected</span>'
          : '<span class="chip"><span class="dot"></span>held</span>');
      return '<tr>' +
        '<td class="muted">' + esc(vote.t) + '</td>' +
        '<td>' + esc(vote.strat) + '</td>' +
        '<td><span class="chip ' + dirCls + '">' + esc(vote.dir) + '</span></td>' +
        '<td class="r">' + Number(vote.conf || 0).toFixed(2) + '</td>' +
        '<td>' + stateHtml + '</td>' +
      '</tr>';
    }).join('');
  }

  var VOTE_TABLE_HEADER = '<thead><tr>' +
    '<th>Time</th><th>Strategy</th><th>Dir</th>' +
    '<th class="r">Conf</th><th>State</th>' +
  '</tr></thead>';

  function decisionGridRows(votes) {
    if (!votes || !votes.length) return emptyRow(10, 'No decisions in this session.');
    return votes.map(function (vote) {
      var dirCls = vote.dir === 'LONG' ? 'pos' : (vote.dir === 'SHORT' ? 'neg' : '');
      var meta = vote.meta || {};
      var metrics = meta.decision_metrics && typeof meta.decision_metrics === 'object' ? meta.decision_metrics : {};
      var stateText = vote.fired ? 'fired' : (meta.acted_on === false ? 'rejected' : 'held');
      var stateCls = vote.fired ? 'pos' : (meta.acted_on === false ? 'neg' : '');
      var reason = meta.decision_reason_code || meta.policy_reason || meta.reason || '--';
      return '<tr>' +
        '<td class="muted">' + esc(vote.t) + '</td>' +
        '<td>' + esc(vote.strat || '--') + '</td>' +
        '<td><span class="chip ' + dirCls + '">' + esc(vote.dir) + '</span></td>' +
        '<td class="r">' + Number(vote.conf || 0).toFixed(2) + '</td>' +
        '<td class="r">' + fmtProbCell(metrics.entry_prob) + '</td>' +
        '<td class="r">' + fmtProbCell(metrics.direction_trade_prob) + '</td>' +
        '<td class="r">' + fmtProbCell(metrics.ce_prob) + '</td>' +
        '<td class="r">' + fmtProbCell(metrics.pe_prob) + '</td>' +
        '<td><span class="chip ' + stateCls + '">' + esc(stateText) + '</span></td>' +
        '<td>' + esc(reason) + '</td>' +
      '</tr>';
    }).join('');
  }

  var DECISION_GRID_HEADER = '<thead><tr>' +
    '<th>Time</th><th>Strategy</th><th>Dir</th>' +
    '<th class="r">Conf</th><th class="r">Entry</th><th class="r">Trade</th>' +
    '<th class="r">CE</th><th class="r">PE</th><th>State</th><th>Reason</th>' +
  '</tr></thead>';

  // ── Export ────────────────────────────────────────────────────────────────

  global.QComponents = {
    esc: esc,
    fmtSigned: fmtSigned,
    emptyRow: emptyRow,
    engineModeBadge: engineModeBadge,
    kpiStrip: kpiStrip,
    tradeTableRows: tradeTableRows,
    TRADE_TABLE_HEADER: TRADE_TABLE_HEADER,
    voteTableRows: voteTableRows,
    VOTE_TABLE_HEADER: VOTE_TABLE_HEADER,
    decisionGridRows: decisionGridRows,
    DECISION_GRID_HEADER: DECISION_GRID_HEADER,
    showToast: showToast,
    confirm: confirmModal,
    skeleton: skeleton,
    dataSourceBadge: dataSourceBadge,
    runSelector: runSelector,
  };
})(window);
