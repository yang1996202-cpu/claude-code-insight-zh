import json
from pathlib import Path
from typing import Any, Dict


def load_facet(session_id: str, claude_dir: Path) -> Dict[str, Any]:
    path = claude_dir / "usage-data" / "facets" / f"{session_id}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
