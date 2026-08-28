from __future__ import annotations

import time
from typing import Any, Dict, Optional

from .schemas import TraceEvent


def event(
    run_id: str,
    stage: str,
    name: str,
    status: str,
    data: Optional[Dict[str, Any]] = None,
) -> TraceEvent:
    return TraceEvent(
        run_id=run_id,
        stage=stage,
        event=name,
        status=status,
        timestamp=time.time(),
        data=data or {},
    )
