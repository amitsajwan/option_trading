// Interactive price chart — crosshair tooltip, pan (drag), zoom (wheel/pinch),
// fullscreen + autoscale controls. Designed to mount into a container element.
//
// Usage:
//   const chart = InteractiveChart.mount(containerEl, { candles, markers });
//   chart.destroy();
//
(function (global) {
  const POS = '#0A8F5C';
  const NEG = '#C23E2F';
  const INK = '#0B0F14';
  const MUTED = '#8A94A1';
  const GRID = 'rgba(11,15,20,0.08)';

  function ns(tag) { return document.createElementNS('http://www.w3.org/2000/svg', tag); }
  function fmt(n, d = 2) { return n.toFixed(d); }
  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>]/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[ch]));
  }

  function mount(container, opts) {
    const candles = opts.candles.slice();
    const markers = (opts.markers || []).slice();
    const onMarkerSelect = typeof opts.onMarkerSelect === 'function' ? opts.onMarkerSelect : null;
    const formatMarkerTooltip = typeof opts.formatMarkerTooltip === 'function' ? opts.formatMarkerTooltip : null;
    const formatHoverTooltip = typeof opts.formatHoverTooltip === 'function' ? opts.formatHoverTooltip : null;
    const state = {
      // visible window as candle-index range [i0, i1]
      i0: 0,
      i1: candles.length - 1,
      hover: null,
      markerHover: null,
      dragging: false,
      dragStartX: 0,
      dragStartI: [0, 0],
      fullscreen: false,
    };

    container.innerHTML = '';
    container.style.position = 'relative';
    container.style.userSelect = 'none';
    container.style.touchAction = 'none';

    // toolbar
    const toolbar = document.createElement('div');
    toolbar.style.cssText = 'position:absolute; top:8px; right:8px; z-index:3; display:flex; gap:4px; background:rgba(246,244,239,0.88); border:1px solid var(--line-1); border-radius:4px; padding:2px;';
    toolbar.innerHTML = `
      <button class="btn sm ghost" data-act="zoomin" title="Zoom in">＋</button>
      <button class="btn sm ghost" data-act="zoomout" title="Zoom out">−</button>
      <button class="btn sm ghost" data-act="fit" title="Fit all">⤢</button>
      <button class="btn sm ghost" data-act="full" title="Fullscreen">⛶</button>
    `;
    toolbar.querySelectorAll('button').forEach(b => {
      b.style.cssText = 'height:24px;width:28px;padding:0;font-size:13px;border:0;background:transparent;cursor:pointer;color:var(--ink);';
      b.onmouseover = () => b.style.background = 'rgba(11,15,20,0.06)';
      b.onmouseout = () => b.style.background = 'transparent';
    });
    container.appendChild(toolbar);

    // svg
    const svg = ns('svg');
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.style.cssText = 'display:block; width:100%; height:100%; cursor:crosshair;';
    container.appendChild(svg);

    // tooltip (HTML overlay)
    const tip = document.createElement('div');
    tip.style.cssText = 'position:absolute; pointer-events:none; z-index:4; background:var(--ink); color:var(--paper); font:11px/1.4 "JetBrains Mono",monospace; padding:6px 8px; border-radius:4px; white-space:nowrap; display:none; box-shadow: 0 4px 12px rgba(0,0,0,0.18); transform: translate(-50%, -100%); margin-top:-8px; font-variant-numeric: tabular-nums;';
    container.appendChild(tip);

    const crosshair = document.createElement('div');
    crosshair.style.cssText = 'position:absolute; pointer-events:none; z-index:2;';
    container.appendChild(crosshair);

    function getSize() {
      const r = container.getBoundingClientRect();
      return { w: r.width, h: r.height };
    }

    function extentY(i0, i1) {
      let mn = Infinity, mx = -Infinity;
      for (let i = i0; i <= i1; i++) {
        const c = candles[i];
        if (c.l < mn) mn = c.l;
        if (c.h > mx) mx = c.h;
      }
      return [mn, mx];
    }

    function draw() {
      const { w, h } = getSize();
      if (!w || !h) return;
      svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
      svg.innerHTML = '';

      const padL = 0, padR = 58, padT = 10, padB = 22;
      const [mn, mx] = extentY(state.i0, state.i1);
      const pad = (mx - mn) * 0.08 || 1;
      const y0 = mn - pad, y1 = mx + pad;

      const x = i => padL + ((i - state.i0) / Math.max(1, state.i1 - state.i0)) * (w - padR - padL);
      const y = v => padT + (1 - (v - y0) / (y1 - y0)) * (h - padT - padB);
      state._x = x; state._y = y;

      // gridlines
      const gLay = ns('g');
      const yticks = 5;
      for (let i = 0; i <= yticks; i++) {
        const yv = y0 + (y1 - y0) * (i / yticks);
        const yy = y(yv);
        const ln = ns('line');
        ln.setAttribute('x1', padL); ln.setAttribute('x2', w - padR);
        ln.setAttribute('y1', yy); ln.setAttribute('y2', yy);
        ln.setAttribute('stroke', GRID); ln.setAttribute('stroke-width', 1);
        gLay.appendChild(ln);
        const tx = ns('text');
        tx.setAttribute('x', w - padR + 6); tx.setAttribute('y', yy + 3);
        tx.setAttribute('fill', MUTED); tx.setAttribute('font-size', 10);
        tx.setAttribute('font-family', 'JetBrains Mono, monospace');
        tx.textContent = yv.toFixed(2);
        gLay.appendChild(tx);
      }
      // x ticks
      const xticks = 6;
      for (let i = 0; i <= xticks; i++) {
        const idx = state.i0 + Math.floor((state.i1 - state.i0) * (i / xticks));
        const c = candles[idx];
        if (!c) continue;
        const xx = x(idx);
        const tx = ns('text');
        tx.setAttribute('x', xx); tx.setAttribute('y', h - 6);
        tx.setAttribute('fill', MUTED); tx.setAttribute('font-size', 10);
        tx.setAttribute('font-family', 'JetBrains Mono, monospace');
        tx.setAttribute('text-anchor', 'middle');
        tx.textContent = c.label || '';
        gLay.appendChild(tx);
      }
      svg.appendChild(gLay);

      // close line + area
      let d = '';
      for (let i = state.i0; i <= state.i1; i++) {
        const c = candles[i];
        d += `${i === state.i0 ? 'M' : 'L'}${x(i).toFixed(2)} ${y(c.c).toFixed(2)} `;
      }
      const area = ns('path');
      area.setAttribute('d', `${d} L ${x(state.i1).toFixed(2)} ${h - padB} L ${x(state.i0).toFixed(2)} ${h - padB} Z`);
      area.setAttribute('fill', INK); area.setAttribute('opacity', 0.04);
      svg.appendChild(area);

      // hi/lo light strokes
      let hiD = '', loD = '';
      for (let i = state.i0; i <= state.i1; i++) {
        const c = candles[i];
        hiD += `${i === state.i0 ? 'M' : 'L'}${x(i).toFixed(2)} ${y(c.h).toFixed(2)} `;
        loD += `${i === state.i0 ? 'M' : 'L'}${x(i).toFixed(2)} ${y(c.l).toFixed(2)} `;
      }
      const hi = ns('path'); hi.setAttribute('d', hiD); hi.setAttribute('fill', 'none');
      hi.setAttribute('stroke', MUTED); hi.setAttribute('stroke-width', 0.8); hi.setAttribute('opacity', 0.4);
      svg.appendChild(hi);
      const lo = ns('path'); lo.setAttribute('d', loD); lo.setAttribute('fill', 'none');
      lo.setAttribute('stroke', MUTED); lo.setAttribute('stroke-width', 0.8); lo.setAttribute('opacity', 0.4);
      svg.appendChild(lo);

      const line = ns('path'); line.setAttribute('d', d); line.setAttribute('fill', 'none');
      line.setAttribute('stroke', INK); line.setAttribute('stroke-width', 1.6);
      line.setAttribute('stroke-linejoin', 'round');
      svg.appendChild(line);

      // pair buy→sell markers into trade segments (entry/exit/pnl)
      const paired = [];
      let openBuy = null;
      const sorted = markers.filter(m => m.side === 'buy' || m.side === 'sell').slice().sort((a, b) => a.t - b.t);
      for (const m of sorted) {
        if (m.side === 'buy') openBuy = m;
        else if (m.side === 'sell' && openBuy) {
          paired.push({ entry: openBuy, exit: m });
          openBuy = null;
        }
      }

      // draw trade segments (connector + shaded box + pnl label)
      for (const p of paired) {
        const e = p.entry, x2 = p.exit;
        if (x2.t < state.i0 || e.t > state.i1) continue;
        const ex = x(Math.max(e.t, state.i0));
        const xx = x(Math.min(x2.t, state.i1));
        const ey = y(e.price), xy = y(x2.price);
        const pnl = x2.price - e.price;
        const pnlPct = (pnl / e.price) * 100;
        const good = pnl >= 0;
        const col = good ? POS : NEG;

        // shaded region between entry/exit prices
        const rect = ns('rect');
        const top = Math.min(ey, xy), bot = Math.max(ey, xy);
        rect.setAttribute('x', ex); rect.setAttribute('y', top);
        rect.setAttribute('width', Math.max(1, xx - ex)); rect.setAttribute('height', Math.max(1, bot - top));
        rect.setAttribute('fill', col); rect.setAttribute('opacity', 0.08);
        svg.appendChild(rect);

        // entry horizontal dashed line across segment
        const l1 = ns('line');
        l1.setAttribute('x1', ex); l1.setAttribute('x2', xx);
        l1.setAttribute('y1', ey); l1.setAttribute('y2', ey);
        l1.setAttribute('stroke', col); l1.setAttribute('stroke-dasharray', '3 2');
        l1.setAttribute('stroke-width', 1); l1.setAttribute('opacity', 0.8);
        svg.appendChild(l1);
        // exit line
        const l2 = ns('line');
        l2.setAttribute('x1', ex); l2.setAttribute('x2', xx);
        l2.setAttribute('y1', xy); l2.setAttribute('y2', xy);
        l2.setAttribute('stroke', col); l2.setAttribute('stroke-dasharray', '3 2');
        l2.setAttribute('stroke-width', 1); l2.setAttribute('opacity', 0.8);
        svg.appendChild(l2);
        // vertical connector at exit
        const l3 = ns('line');
        l3.setAttribute('x1', xx); l3.setAttribute('x2', xx);
        l3.setAttribute('y1', ey); l3.setAttribute('y2', xy);
        l3.setAttribute('stroke', col); l3.setAttribute('stroke-width', 1); l3.setAttribute('opacity', 0.7);
        svg.appendChild(l3);

        // PnL label
        const labelTxt = (good ? '+' : '') + pnl.toFixed(2) + '  ' + (good ? '+' : '') + pnlPct.toFixed(2) + '%';
        const labW = labelTxt.length * 6.2 + 10;
        const labX = Math.min(xx + 6, getSize().w - 58 - labW);
        const labY = xy;
        const lbg = ns('rect');
        lbg.setAttribute('x', labX); lbg.setAttribute('y', labY - 8);
        lbg.setAttribute('width', labW); lbg.setAttribute('height', 16);
        lbg.setAttribute('fill', col); lbg.setAttribute('rx', 2);
        svg.appendChild(lbg);
        const ltx = ns('text');
        ltx.setAttribute('x', labX + labW / 2); ltx.setAttribute('y', labY + 3);
        ltx.setAttribute('fill', 'white'); ltx.setAttribute('font-size', 10);
        ltx.setAttribute('font-family', 'JetBrains Mono, monospace');
        ltx.setAttribute('font-weight', 600); ltx.setAttribute('text-anchor', 'middle');
        ltx.textContent = labelTxt;
        svg.appendChild(ltx);
      }

      // markers in view
      for (const m of markers) {
        if (m.t < state.i0 || m.t > state.i1) continue;
        const mx2 = x(m.t), my = y(m.price);
        const color = m.side === 'buy' ? POS : m.side === 'sell' ? NEG : '#B97405';
        const sz = 5;
        const group = ns('g');
        const shape = String(m.shape || 'triangle').toLowerCase();
        if (shape === 'circle') {
          const dot = ns('circle');
          dot.setAttribute('cx', mx2); dot.setAttribute('cy', my);
          dot.setAttribute('r', 4.5);
          dot.setAttribute('fill', color); dot.setAttribute('stroke', INK); dot.setAttribute('stroke-width', 0.5);
          group.appendChild(dot);
        } else {
          const dir = m.side === 'buy' ? 1 : -1;
          const tri = ns('path');
          tri.setAttribute('d', `M ${mx2} ${my + dir * sz} L ${mx2 - sz} ${my - dir * sz} L ${mx2 + sz} ${my - dir * sz} Z`);
          tri.setAttribute('fill', color); tri.setAttribute('stroke', INK); tri.setAttribute('stroke-width', 0.5);
          group.appendChild(tri);
        }
        const hit = ns('circle');
        hit.setAttribute('cx', mx2); hit.setAttribute('cy', my);
        hit.setAttribute('r', 10);
        hit.setAttribute('fill', 'transparent');
        hit.style.cursor = 'pointer';
        hit.addEventListener('mouseenter', () => { state.markerHover = m; drawHover(); });
        hit.addEventListener('mouseleave', () => { if (state.markerHover === m) { state.markerHover = null; drawHover(); } });
        hit.addEventListener('click', evt => {
          evt.preventDefault();
          evt.stopPropagation();
          if (onMarkerSelect) onMarkerSelect(m);
        });
        group.appendChild(hit);
        svg.appendChild(group);
        if (m.label) {
          const t = ns('text');
          t.setAttribute('x', mx2 + 6); t.setAttribute('y', my - 6);
          t.setAttribute('fill', color); t.setAttribute('font-size', 10);
          t.setAttribute('font-family', 'JetBrains Mono, monospace');
          t.setAttribute('font-weight', 600);
          t.textContent = m.label;
          svg.appendChild(t);
        }
      }

      // last price label
      const last = candles[state.i1];
      const lastY = y(last.c);
      const lastLine = ns('line');
      lastLine.setAttribute('x1', padL); lastLine.setAttribute('x2', w - padR);
      lastLine.setAttribute('y1', lastY); lastLine.setAttribute('y2', lastY);
      lastLine.setAttribute('stroke', INK); lastLine.setAttribute('stroke-dasharray', '3 3');
      lastLine.setAttribute('opacity', 0.45);
      svg.appendChild(lastLine);
      const tag = ns('rect');
      tag.setAttribute('x', w - padR + 2); tag.setAttribute('y', lastY - 8);
      tag.setAttribute('width', 52); tag.setAttribute('height', 16);
      tag.setAttribute('fill', INK);
      svg.appendChild(tag);
      const tagTxt = ns('text');
      tagTxt.setAttribute('x', w - padR + 28); tagTxt.setAttribute('y', lastY + 3);
      tagTxt.setAttribute('fill', 'white'); tagTxt.setAttribute('font-size', 10);
      tagTxt.setAttribute('font-family', 'JetBrains Mono, monospace');
      tagTxt.setAttribute('text-anchor', 'middle'); tagTxt.setAttribute('font-weight', 600);
      tagTxt.textContent = last.c.toFixed(2);
      svg.appendChild(tagTxt);

      // hover state
      drawHover();
    }

    function defaultMarkerTooltip(marker) {
      const meta = marker && marker.meta ? marker.meta : {};
      const timestamp = meta.timestamp || '';
      const title = meta.analysisLabel || meta.label || marker.label || meta.type || 'Marker';
      const subtitle = timestamp ? '<div style="color:#B9C2CC">' + esc(String(timestamp).slice(11, 19)) + '</div>' : '';
      return '<div style="font-weight:600; color:#fff">' + esc(title) + '</div>' + subtitle;
    }

    function drawHover() {
      crosshair.innerHTML = '';
      if (state.markerHover) {
        const marker = state.markerHover;
        const xx = state._x(marker.t);
        const yy = state._y(marker.price);
        const html = formatMarkerTooltip ? formatMarkerTooltip(marker) : defaultMarkerTooltip(marker);
        tip.innerHTML = html || '';
        if (!tip.innerHTML) { tip.style.display = 'none'; return; }
        tip.style.display = 'block';
        const { w } = getSize();
        const tipW = tip.offsetWidth || 180;
        let px = xx;
        if (px < tipW / 2 + 4) px = tipW / 2 + 4;
        if (px > w - tipW / 2 - 4) px = w - tipW / 2 - 4;
        tip.style.left = px + 'px';
        tip.style.top = Math.max(40, yy - 18) + 'px';
        return;
      }
      if (!state.hover) { tip.style.display = 'none'; return; }
      const { w, h } = getSize();
      const padR = 58, padT = 10, padB = 22;
      const i = state.hover.i;
      const c = candles[i];
      if (!c) return;
      const xx = state._x(i), yy = state._y(c.c);

      // vertical + horizontal lines
      crosshair.innerHTML = `
        <div style="position:absolute; left:${xx}px; top:${padT}px; width:1px; height:${h - padB - padT}px; background:rgba(11,15,20,0.45); border-left:1px dashed var(--ink-3); opacity:0.7"></div>
        <div style="position:absolute; left:0; top:${yy}px; width:${w - padR}px; height:1px; border-top:1px dashed var(--ink-3); opacity:0.5"></div>
        <div style="position:absolute; left:${xx - 3}px; top:${yy - 3}px; width:6px; height:6px; border-radius:50%; background:var(--ink); box-shadow:0 0 0 2px var(--paper)"></div>
      `;

      if (formatHoverTooltip) {
        tip.innerHTML = formatHoverTooltip({
          candle: c,
          index: i,
          prev: i > 0 ? candles[i - 1] : null,
          candles: candles,
        }) || '';
        if (!tip.innerHTML) { tip.style.display = 'none'; return; }
        tip.style.display = 'block';
        const tipW2 = tip.offsetWidth || 160;
        let px2 = xx;
        if (px2 < tipW2 / 2 + 4) px2 = tipW2 / 2 + 4;
        if (px2 > w - tipW2 / 2 - 4) px2 = w - tipW2 / 2 - 4;
        tip.style.left = px2 + 'px';
        tip.style.top = Math.max(40, yy - 14) + 'px';
        return;
      }

      const chg = i > 0 ? c.c - candles[i - 1].c : 0;
      const chgPct = i > 0 ? (chg / candles[i - 1].c) * 100 : 0;
      const chgCol = chg >= 0 ? '#7AD4A8' : '#E89389';
      tip.innerHTML = `
        <div style="font-weight:600; color:#fff">${c.label}</div>
        <div style="display:grid; grid-template-columns:auto auto; gap:2px 10px; margin-top:4px; color:#B9C2CC">
          <span>O</span><span style="color:#fff; text-align:right">${fmt(c.o)}</span>
          <span>H</span><span style="color:#fff; text-align:right">${fmt(c.h)}</span>
          <span>L</span><span style="color:#fff; text-align:right">${fmt(c.l)}</span>
          <span>C</span><span style="color:#fff; text-align:right">${fmt(c.c)}</span>
          <span>Vol</span><span style="color:#fff; text-align:right">${c.v.toLocaleString()}</span>
          <span>Δ</span><span style="color:${chgCol}; text-align:right">${chg >= 0 ? '+' : ''}${fmt(chg)} (${chg >= 0 ? '+' : ''}${fmt(chgPct)}%)</span>
        </div>
      `;
      tip.style.display = 'block';
      // clamp position
      const tipW = tip.offsetWidth || 160;
      let px = xx;
      if (px < tipW / 2 + 4) px = tipW / 2 + 4;
      if (px > w - tipW / 2 - 4) px = w - tipW / 2 - 4;
      tip.style.left = px + 'px';
      tip.style.top = Math.max(40, yy - 14) + 'px';
    }

    // interactions
    function ptToCandleIndex(e) {
      const r = container.getBoundingClientRect();
      const x = (e.touches ? e.touches[0].clientX : e.clientX) - r.left;
      const padR = 58;
      const t = x / (r.width - padR);
      const i = Math.round(state.i0 + t * (state.i1 - state.i0));
      return Math.max(state.i0, Math.min(state.i1, i));
    }

    function onMove(e) {
      if (state.dragging) {
        const r = container.getBoundingClientRect();
        const x = (e.touches ? e.touches[0].clientX : e.clientX) - r.left;
        const dx = x - state.dragStartX;
        const padR = 58;
        const span = state.dragStartI[1] - state.dragStartI[0];
        const pxPerCandle = (r.width - padR) / Math.max(1, span);
        const shift = -dx / pxPerCandle;
        let i0 = Math.round(state.dragStartI[0] + shift);
        let i1 = Math.round(state.dragStartI[1] + shift);
        if (i0 < 0) { i1 -= i0; i0 = 0; }
        if (i1 > candles.length - 1) { i0 -= (i1 - (candles.length - 1)); i1 = candles.length - 1; }
        state.i0 = Math.max(0, i0);
        state.i1 = Math.min(candles.length - 1, i1);
        draw();
        return;
      }
      state.hover = { i: ptToCandleIndex(e) };
      drawHover();
    }
    function onLeave() { state.hover = null; state.markerHover = null; drawHover(); }
    function onDown(e) {
      state.markerHover = null;
      state.dragging = true;
      const r = container.getBoundingClientRect();
      state.dragStartX = (e.touches ? e.touches[0].clientX : e.clientX) - r.left;
      state.dragStartI = [state.i0, state.i1];
      svg.style.cursor = 'grabbing';
      e.preventDefault();
    }
    function onUp() { state.dragging = false; svg.style.cursor = 'crosshair'; }
    function onWheel(e) {
      e.preventDefault();
      const r = container.getBoundingClientRect();
      const x = e.clientX - r.left;
      const padR = 58;
      const t = Math.max(0, Math.min(1, x / (r.width - padR)));
      const pivotI = state.i0 + t * (state.i1 - state.i0);
      const scale = e.deltaY > 0 ? 1.2 : 1 / 1.2;
      const span = (state.i1 - state.i0) * scale;
      if (span < 6) return; // min zoom
      if (span > candles.length - 1) { state.i0 = 0; state.i1 = candles.length - 1; draw(); return; }
      let i0 = pivotI - (pivotI - state.i0) * scale;
      let i1 = pivotI + (state.i1 - pivotI) * scale;
      i0 = Math.round(Math.max(0, i0));
      i1 = Math.round(Math.min(candles.length - 1, i1));
      if (i1 - i0 < 6) return;
      state.i0 = i0; state.i1 = i1;
      draw();
    }

    // touch pinch
    let pinchStart = null;
    function onTouchStart(e) {
      if (e.touches.length === 2) {
        const [a, b] = e.touches;
        pinchStart = {
          d: Math.hypot(b.clientX - a.clientX, b.clientY - a.clientY),
          i0: state.i0, i1: state.i1,
        };
      } else {
        onDown(e);
      }
    }
    function onTouchMove(e) {
      if (e.touches.length === 2 && pinchStart) {
        const [a, b] = e.touches;
        const d = Math.hypot(b.clientX - a.clientX, b.clientY - a.clientY);
        const scale = pinchStart.d / d;
        const span = (pinchStart.i1 - pinchStart.i0) * scale;
        if (span < 6 || span > candles.length) return;
        const mid = (pinchStart.i0 + pinchStart.i1) / 2;
        state.i0 = Math.round(Math.max(0, mid - span / 2));
        state.i1 = Math.round(Math.min(candles.length - 1, mid + span / 2));
        draw();
        e.preventDefault();
      } else {
        onMove(e);
      }
    }
    function onTouchEnd(e) { pinchStart = null; onUp(); }

    svg.addEventListener('mousemove', onMove);
    svg.addEventListener('mouseleave', onLeave);
    svg.addEventListener('mousedown', onDown);
    window.addEventListener('mouseup', onUp);
    svg.addEventListener('wheel', onWheel, { passive: false });
    svg.addEventListener('touchstart', onTouchStart, { passive: false });
    svg.addEventListener('touchmove', onTouchMove, { passive: false });
    svg.addEventListener('touchend', onTouchEnd);

    toolbar.addEventListener('click', e => {
      const act = e.target.dataset.act;
      if (!act) return;
      if (act === 'zoomin') {
        const span = state.i1 - state.i0;
        const mid = (state.i0 + state.i1) / 2;
        const ns2 = Math.max(8, span / 1.4);
        state.i0 = Math.round(Math.max(0, mid - ns2 / 2));
        state.i1 = Math.round(Math.min(candles.length - 1, mid + ns2 / 2));
      } else if (act === 'zoomout') {
        const span = state.i1 - state.i0;
        const mid = (state.i0 + state.i1) / 2;
        const ns2 = Math.min(candles.length - 1, span * 1.4);
        state.i0 = Math.round(Math.max(0, mid - ns2 / 2));
        state.i1 = Math.round(Math.min(candles.length - 1, mid + ns2 / 2));
      } else if (act === 'fit') {
        state.i0 = 0; state.i1 = candles.length - 1;
      } else if (act === 'full') {
        toggleFullscreen();
        return;
      }
      draw();
    });

    function toggleFullscreen() {
      state.fullscreen = !state.fullscreen;
      if (state.fullscreen) {
        container.dataset._prev = container.style.cssText;
        container.style.cssText += 'position:fixed!important;inset:0;z-index:1000;background:var(--paper);padding:40px 20px;';
      } else {
        container.style.cssText = container.dataset._prev || '';
        container.style.position = 'relative';
      }
      setTimeout(draw, 50);
    }

    // esc to exit fullscreen
    function onKey(e) { if (e.key === 'Escape' && state.fullscreen) toggleFullscreen(); }
    window.addEventListener('keydown', onKey);

    const ro = new ResizeObserver(() => draw());
    ro.observe(container);

    draw();

    return {
      destroy() {
        ro.disconnect();
        window.removeEventListener('keydown', onKey);
        window.removeEventListener('mouseup', onUp);
        container.innerHTML = '';
      },
      redraw: draw,
    };
  }

  global.InteractiveChart = { mount };
})(window);
