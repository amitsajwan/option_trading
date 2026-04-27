# UX Improvements Summary — Trading Dashboard

> Historical record — see README.md for current state.

## Implemented P0 Critical Improvements

### 1. Confirmation Dialogs for Destructive Actions
**Files:** `components.js`, `live_strategy.js`, `strategy_evaluation.js`

**Changes:**
- Added `QComponents.confirm()` modal system with optional "type to confirm" requirement
- "Halt all strategies" button now requires typing "HALT" to confirm
- "Promote to production" button now requires typing "PROMOTE" to confirm
- Modal includes danger styling (red button) for high-risk actions

**Usage:**
```javascript
C.confirm({
  title: 'Halt All Strategies',
  message: 'This will immediately stop all live trading strategies.',
  confirmText: 'Halt strategies',
  danger: true,
  requireType: 'HALT'  // User must type exactly this
}).then(confirmed => { if (confirmed) execute(); });
```

---

### 2. Toast Notification System
**File:** `components.js`

**Features:**
- 4 severity levels: `info`, `success`, `warn`, `error`
- Error toasts persist until manually dismissed
- Success/warn toasts auto-dismiss after 4 seconds
- Optional retry action button on error toasts
- Stacked notifications (top-right corner)

**Usage:**
```javascript
C.showToast({ type: 'error', message: 'Failed to load', action: { label: 'Retry', onClick: fn } });
```

---

### 3. Data Source Badges
**Files:** `components.js`, `historical_replay.js`

**Features:**
- Shows `Live` / `Cached` / `Mock` / `Error` / `Stale` states
- Color-coded badges using design system tokens
- Shows age indicator: "2m ago" for cached data
- Displays error message on fetch failures

**Usage:**
```javascript
C.dataSourceBadge({ source: 'live', updatedAt: data._fetchedAt, error: null })
```

---

### 4. Run Selector Component
**File:** `components.js`

**Features:**
- Dropdown with run metadata display
- Shows: Run ID (truncated), Date range, Status, Trade count
- Returns both HTML and wire function for event binding

**Usage:**
```javascript
var selector = C.runSelector({
  runs: data.runs,
  value: data.currentRunId,
  onChange: function (runId, runObj) { /* handle selection */ }
});
html += selector.html;
// After insertion:
selector.wire();
```

---

### 5. Skeleton Loading States
**Files:** `components.js`, `tokens.css`

**Features:**
- Shimmer animation for loading placeholders
- Configurable width/height
- Uses design system colors

**Usage:**
```javascript
C.skeleton('100%', '200px')  // width, height
```

---

### 6. Error Handling with Retry
**Files:** `historical_replay.js`, `live_strategy.js`, `strategy_evaluation.js`

**Changes:**
- All `loadData()` calls now show toast on error
- Added `_fetchedAt` timestamp tracking
- Retry button on error toasts re-runs the failed load
- Console error logging preserved for debugging

---

### 7. Recent Runs Table (Already Implemented)
**File:** `historical_replay.js`

**Features:**
- Shows recent completed runs with metadata
- Click row to switch to that run
- Refresh button to update list
- Active run highlighted with badge

---

## CSS Additions

**File:** `tokens.css`

```css
/* Shimmer animation for skeletons */
@keyframes shimmer {
  0% { transform: translateX(-100%); }
  100% { transform: translateX(100%); }
}

/* Danger button variant */
.btn.danger {
  background: var(--neg);
  color: #fff;
  border-color: var(--neg);
}
```

---

## Component Exports

**File:** `components.js` — Added to `QComponents`:

- `showToast` — Toast notifications
- `confirm` — Confirmation modals
- `skeleton` — Loading placeholders
- `dataSourceBadge` — Data provenance indicator
- `runSelector` — Run selection dropdown

---

## Pages Updated

| Page | Changes |
|------|---------|
| **Live Strategy** | Halt button confirmation dialog, error toasts, data badge |
| **Historical Replay** | Data badge, error handling with retry, recent runs table |
| **Strategy Evaluation** | Promote/Flag confirmation dialogs, wireButtons refactor |

---

## Remaining P0/P1 Items (Not Yet Implemented)

### P0
- [ ] Date range validation (prevent to < from)
- [ ] Replace free-text run inputs with RunSelector dropdown in Strategy Evaluation
- [ ] "Share this view" button for URL copying

### P1
- [ ] Full-page skeleton loading state
- [ ] Keyboard shortcuts
- [ ] Table sorting/filtering
- [ ] Mobile responsiveness audit

---

## Testing Checklist

- [ ] Click "Halt all strategies" — verify HALT typing required
- [ ] Click "Promote to production" — verify PROMOTE typing required
- [ ] Disconnect network — verify error toast with retry
- [ ] Check data badge updates when switching pages
- [ ] Verify Recent Runs table shows on Historical Replay
- [ ] Click a run in Recent Runs table — verify navigation works
