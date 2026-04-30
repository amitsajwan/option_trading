# Trading Dashboard UX Evaluation

> Historical record — see README.md for current state.

## Executive Summary

The dashboard has a solid design foundation (Quiet Operator design system) but has several UX gaps that create confusion, errors, and operational risk.

**Priority Issues:**
1. Silent failures — errors are swallowed, users see stale/mock data without knowing
2. Inconsistent mock vs real indicators — operators can't trust what they see
3. No confirmation on destructive actions — "Halt all strategies" can accidentally stop production
4. Weak run selection UX — free-text inputs without autocomplete lead to wrong runs being compared
5. Missing loading states — users don't know if data is fetching or stuck

---

## 1. Error Handling & System Feedback

### Current State
- API failures silently fall back to mock data
- No toast notifications, banners, or error states
- `catch(() => ({ rows: [] }))` pattern hides all errors

### Problems
- Operators cannot distinguish between "no data" and "API error"
- Silent failures in live trading context = operational risk
- No retry mechanism exposed to users

### Recommendations

```javascript
// Replace silent catches with:
.catch(err => {
  showToast({ type: 'error', message: 'Failed to load trades', retry: loadData });
  return { rows: [], error: err.message };
})
```

- [ ] Add global toast/notification system
- [ ] Show error banners when critical APIs fail (live session, trades)
- [ ] Display "last successful fetch" timestamp
- [ ] Add retry buttons on failed panels
- [ ] Distinguish "empty data" (valid) from "fetch failed" (error)

---

## 2. Data Trust Indicators

### Current State
- Some panels show "mock" labels, others don't
- No consistent "live" vs "cached" vs "stale" indicators
- Run ID displayed but not easily copyable

### Problems
- User reported: "not sure what run is it showing"
- Can't verify if displayed trades match intended run
- Mock data looks identical to real data

### Recommendations

**Add data provenance badges to every panel:**
```
[Live] [Cached 2m ago] [Mock] [Stale 15m]
```

- [ ] Consistent badge system: `DatasourceBadge` component
- [ ] Timestamp on every data fetch: "Updated 14:32:05 IST"
- [ ] Click-to-copy Run ID (currently truncated with "…")
- [ ] Visual distinction for mock data (watermark or border)

---

## 3. Destructive Action Protection

### Current State
- "Halt all strategies" button — immediate action, no confirmation
- "Promote to production" — one click in evaluation page
- Run replay — can overwrite in-progress runs

### Problems
- Accidental clicks can stop live trading
- No audit trail of who did what
- No safeguards for production-affecting actions

### Recommendations

```javascript
// Add confirmation modal pattern
function confirmDestructive({ title, message, confirmText, danger }) {
  return new Promise(resolve => {
    // Show modal, resolve true/false
  });
}

// Usage:
btnHalt.addEventListener('click', async () => {
  const ok = await confirmDestructive({
    title: 'Halt All Strategies',
    message: 'This will immediately stop all live trading. Are you sure?',
    confirmText: 'Yes, halt strategies',
    danger: true
  });
  if (ok) executeHalt();
});
```

- [ ] Confirmation modals for: halt, promote, delete, replay overwrite
- [ ] Require typing confirmation for critical actions: "Type HALT to confirm"
- [ ] Audit logging visible in UI (who ran what when)

---

## 4. Run Selection & Identification

### Current State
- Free-text inputs for Run IDs
- No autocomplete or validation
- Recent runs table added (good!) but not integrated everywhere

### Problems
- Easy to typo a run ID and compare wrong runs
- No visibility into available runs without navigating away
- Run IDs truncated with "…" making them hard to verify

### Recommendations

**Add `RunSelector` component:**
```javascript
function RunSelector({ dataset, value, onChange, status = 'completed' }) {
  // Dropdown with:
  // - Run ID (shortened with tooltip for full)
  // - Date range
  // - Trade count
  // - Status badge
  // - "Active" indicator
}
```

- [ ] Replace free-text run inputs with searchable dropdowns
- [ ] Show run metadata inline (date range, trade count, status)
- [ ] Add "Copy full Run ID" button on all run displays
- [ ] Filter runs by dataset (historical vs live)
- [ ] Pre-select most recent completed run by default

---

## 5. Loading States & Skeletons

### Current State
- Page renders immediately with mock data
- Then hydrates with real data
- No visual indication of loading

### Problems
- Flash of mock content (confusing)
- Can't tell if page is "ready" or still loading
- Actions can be clicked before data is ready

### Recommendations

**Skeleton loading pattern:**
```css
.skeleton {
  background: linear-gradient(90deg, var(--paper-2) 25%, var(--paper-3) 50%, var(--paper-2) 75%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
}
```

- [ ] Show skeletons while fetching initial data
- [ ] Disable action buttons until data loaded
- [ ] Add loading spinners on async operations (run replay, refresh)
- [ ] Progressive enhancement: load critical data first, charts second

---

## 6. Form Validation & Input

### Current State
- Date inputs accept any text
- No validation of date ranges (to < from allowed)
- Speed input not validated

### Problems
- Invalid dates cause API errors (silently caught)
- Date range can be backwards
- No feedback on invalid input

### Recommendations

```javascript
function validateDateRange(from, to) {
  const errors = [];
  if (!from || !to) errors.push('Both dates required');
  if (from > to) errors.push('From date must be before To date');
  if (new Date(from) > new Date()) errors.push('From date cannot be in future');
  return errors;
}
```

- [ ] Date pickers with min/max constraints
- [ ] Inline validation errors (red text below inputs)
- [ ] Disable submit until inputs valid
- [ ] Preserve last valid date range in localStorage

---

## 7. Navigation & Wayfinding

### Current State
- 4 tabs in header, but 9 HTML templates exist
- Missing pages: Trading Models, Research, Terminal, Velocity Testing
- No breadcrumbs
- No indication of which dataset is being viewed

### Problems
- Users can't access all features (templates exist but not routed)
- No way to share/link to specific views
- Deep linking doesn't work well (URL params not synced)

### Recommendations

- [ ] Complete navigation for all 9 pages OR remove unused templates
- [ ] Add breadcrumbs: `Operator > Historical Replay > Run 2026-04-16`
- [ ] Sync URL params with UI state (bidirectional)
- [ ] Add "Share this view" button (copies URL with all params)

---

## 8. Table UX Improvements

### Current State
- Tables truncate on mobile
- No sorting or filtering
- No pagination controls visible
- Trade tables show all columns always

### Problems
- Mobile experience is poor
- Hard to find specific trades in long lists
- Column overload makes scanning difficult

### Recommendations

- [ ] Responsive tables: horizontal scroll on mobile, not truncation
- [ ] Column visibility toggle (show/hide Qty, Hold bars)
- [ ] Sortable columns (click header to sort)
- [ ] Search/filter within tables
- [ ] Pagination with page size selector
- [ ] Sticky headers on scroll

---

## 9. Chart UX

### Current State
- Charts render but lack context
- No zoom/pan controls
- Limited legend information
- No tooltips on data points

### Problems
- Can't inspect specific trades on chart
- No way to zoom into specific time ranges
- Chart legend doesn't identify which run is which

### Recommendations

- [ ] Hover tooltips showing trade details
- [ ] Zoom controls (+/-, reset)
- [ ] Brush selection for time range
- [ ] Legend with run IDs (not just "current/baseline")
- [ ] Sync chart time range with table filter

---

## 10. Mobile Responsiveness

### Current State
- Some `hide-mobile` classes exist
- Layout breaks on narrow screens
- Tables overflow
- Touch targets may be too small

### Recommendations

- [ ] Audit all pages at 375px, 768px, 1440px widths
- [ ] Convert tables to card lists on mobile
- [ ] Increase touch targets to min 44px
- [ ] Simplify KPI strip on mobile (2x2 grid instead of 6-col)
- [ ] Consider bottom sheet for filters on mobile

---

## Implementation Priority

### P0 (Critical — Fix First)
1. Add confirmation dialogs for destructive actions
2. Show error states (not silent failures)
3. Distinguish mock from real data clearly
4. Fix run selection UX (dropdowns not free-text)

### P1 (High — Next Sprint)
5. Add loading skeletons
6. Form validation with inline errors
7. URL sync for shareable views
8. Table sorting and filtering

### P2 (Medium — Backlog)
9. Mobile responsiveness improvements
10. Chart zoom/pan features
11. Global search across runs
12. Keyboard shortcuts

---

## Design System Improvements

### Add Components
- `Toast` — notifications
- `Modal` — confirmations
- `Skeleton` — loading states
- `RunSelector` — run picker with search
- `DateRange` — validated date inputs
- `DataBadge` — provenance indicator
- `EmptyState` — illustrated empty states

### CSS Additions
```css
/* Error states */
.panel.error { border-left: 3px solid var(--neg); }

/* Loading states */
.skeleton { /* shimmer animation */ }

/* Improved focus */
button:focus-visible { outline: 2px solid var(--info); }
```

---

## Files to Modify

| File | Changes |
|------|---------|
| `api.js` | Add error handling, expose fetch status |
| `components.js` | Add Toast, Modal, RunSelector, Skeleton |
| `tokens.css` | Add animation keyframes, error states |
| `historical_replay.js` | Run dropdown, error states, loading |
| `strategy_evaluation.js` | Run dropdowns, confirmation dialogs |
| `live_strategy.js` | Halt confirmation, error banners |
| `index.js` | Service error states |
| `dashboard.html` | Toast container, modal container |
