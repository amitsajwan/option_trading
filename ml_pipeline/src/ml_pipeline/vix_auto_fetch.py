import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd

from .pipeline_layout import VIX_ROOT


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def auto_fetch_enabled() -> bool:
    return _truthy(os.getenv("ML_PIPELINE_AUTO_FETCH_VIX", "0"))


def _credentials_candidates() -> list[Path]:
    out: list[Path] = []
    configured = str(os.getenv("KITE_CREDENTIALS_PATH") or "").strip()
    if configured:
        out.append(Path(configured))
    out.append(Path.cwd() / "credentials.json")
    out.append(Path(__file__).resolve().parents[3] / "credentials.json")
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in out:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq


def _load_kite_credentials() -> Tuple[Optional[str], Optional[str]]:
    api_key = str(os.getenv("KITE_API_KEY") or "").strip()
    access_token = str(os.getenv("KITE_ACCESS_TOKEN") or "").strip()
    if api_key and access_token:
        return api_key, access_token
    for path in _credentials_candidates():
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        key = str(payload.get("api_key") or "").strip()
        token = str(
            payload.get("access_token")
            or ((payload.get("data") or {}).get("access_token") if isinstance(payload.get("data"), dict) else "")
            or ""
        ).strip()
        if key and token:
            return key, token
    return None, None


def _build_kite_client(api_key: str, access_token: str):
    try:
        from kiteconnect import KiteConnect
    except Exception as exc:
        raise RuntimeError(f"kiteconnect not available: {exc}")
    client = KiteConnect(api_key=api_key, timeout=int(os.getenv("KITE_HTTP_TIMEOUT", "30")))
    client.set_access_token(access_token)
    return client


def _resolve_vix_instrument_token(kite) -> int:
    env_token = str(os.getenv("KITE_VIX_TOKEN") or "").strip()
    if env_token.isdigit():
        return int(env_token)
    rows = kite.instruments("NSE")
    if not isinstance(rows, list) or len(rows) == 0:
        raise RuntimeError("kite.instruments('NSE') returned no rows")
    symbol_candidates = {"INDIA VIX", "INDIAVIX", "INDIA_VIX"}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("tradingsymbol") or "").upper().strip()
        token = row.get("instrument_token")
        if symbol in symbol_candidates and str(token).isdigit():
            return int(token)
    raise RuntimeError("INDIA VIX token not found in NSE instruments dump")


def _target_csv_path() -> Path:
    VIX_ROOT.mkdir(parents=True, exist_ok=True)
    return VIX_ROOT / "kite_india_vix_daily.csv"


def _parse_latest_date_from_csv(path: Path) -> Optional[date]:
    if not path.exists():
        return None
    try:
        frame = pd.read_csv(path)
    except Exception:
        return None
    if "date" not in frame.columns:
        cols = {str(c).strip().lower(): c for c in frame.columns}
        date_col = cols.get("date")
    else:
        date_col = "date"
    if not date_col:
        return None
    series = pd.to_datetime(frame[date_col], errors="coerce")
    series = series.dropna()
    if len(series) == 0:
        return None
    return series.max().date()


def ensure_vix_history_for_trade_day(
    *,
    trade_day: Optional[str] = None,
    force_refresh: bool = False,
) -> Optional[str]:
    if not auto_fetch_enabled():
        return None
    csv_path = _target_csv_path()
    today = datetime.now().date()
    required_day = today - timedelta(days=1)
    if trade_day:
        try:
            required_day = datetime.strptime(str(trade_day), "%Y-%m-%d").date() - timedelta(days=1)
        except Exception:
            required_day = today - timedelta(days=1)

    latest = _parse_latest_date_from_csv(csv_path)
    if (not force_refresh) and latest is not None and latest >= required_day:
        return str(csv_path)

    api_key, access_token = _load_kite_credentials()
    if not api_key or not access_token:
        print("[vix_auto_fetch] Kite credentials not found; skipping auto VIX fetch.")
        return None

    from_date_raw = str(os.getenv("ML_PIPELINE_VIX_FROM_DATE") or "2024-01-01").strip()
    try:
        from_date = datetime.strptime(from_date_raw, "%Y-%m-%d").date()
    except Exception:
        from_date = date(2024, 1, 1)
    to_date = today

    try:
        kite = _build_kite_client(api_key=api_key, access_token=access_token)
        token = _resolve_vix_instrument_token(kite)
        rows = kite.historical_data(token, from_date=from_date, to_date=to_date, interval="day")
    except Exception as exc:
        print(f"[vix_auto_fetch] Failed to fetch VIX from Kite: {exc}")
        return None

    if not isinstance(rows, list) or len(rows) == 0:
        print("[vix_auto_fetch] Kite historical_data returned no rows for VIX.")
        return None

    frame = pd.DataFrame(rows)
    if "date" not in frame.columns:
        print("[vix_auto_fetch] Unexpected Kite VIX payload: missing 'date' column.")
        return None
    for col in ("open", "high", "low", "close"):
        if col not in frame.columns:
            frame[col] = pd.NA
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"]).copy()
    if len(frame) == 0:
        print("[vix_auto_fetch] VIX frame empty after date coercion.")
        return None
    frame["date"] = frame["date"].dt.date.astype(str)
    out = frame.loc[:, ["date", "open", "high", "low", "close"]].copy()
    out.to_csv(csv_path, index=False)
    print(f"[vix_auto_fetch] Refreshed VIX daily file: {csv_path} rows={len(out)}")
    return str(csv_path)

