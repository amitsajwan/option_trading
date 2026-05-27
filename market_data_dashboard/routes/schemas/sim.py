from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class SimRunCreateRequest(BaseModel):
    source_date: str = Field(..., description="Source trade date YYYY-MM-DD")
    source_coll: str = Field("phase1_market_snapshots")
    label: str = Field("", max_length=120)
    env_overrides: Dict[str, str] = Field(default_factory=dict)
    speed: float = Field(30.0, gt=0.0)


class SimRunCreateResponse(BaseModel):
    run_id: str
    manifest_path: str
    stream_name: str
    dashboard_url: str


class SimRunSummary(BaseModel):
    run_id: str
    kind: str = "sim"
    source_date: Optional[str] = None
    status: str
    terminal_status: Optional[str] = None
    label: str = ""
    submitted_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    stream_name: Optional[str] = None
    manifest_path: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

