import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


CACHE_ROOT_NAME = "usage-data-zh"


def _package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _digest_files(paths) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.relative_to(_package_root())).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _analyzer_version(name: str, relative_paths) -> str:
    root = _package_root()
    paths = [root / rel for rel in relative_paths]
    return f"insight-zh-{name}-{_digest_files(paths)}"


SESSION_META_ANALYZER_VERSION = _analyzer_version(
    "session-meta",
    [
        "domain/session.py",
        "sources/jsonl_source.py",
        "sources/session_loader.py",
    ],
)
FACET_ANALYZER_VERSION = _analyzer_version(
    "facets",
    [
        "analysis/session_inference.py",
        "domain/session.py",
        "sources/jsonl_source.py",
        "sources/session_loader.py",
    ],
)


def cache_root(claude_dir: Path) -> Path:
    return claude_dir / CACHE_ROOT_NAME


def session_meta_dir(claude_dir: Path) -> Path:
    return cache_root(claude_dir) / "session-meta"


def facets_dir(claude_dir: Path) -> Path:
    return cache_root(claude_dir) / "facets"


def reports_dir(claude_dir: Path) -> Path:
    return cache_root(claude_dir) / "reports"


def index_path(claude_dir: Path) -> Path:
    return cache_root(claude_dir) / "index.json"


def analysis_window(start_date=None, end_date=None) -> Dict[str, str]:
    return {
        "start_date": start_date.isoformat() if hasattr(start_date, "isoformat") else (str(start_date) if start_date else ""),
        "end_date": end_date.isoformat() if hasattr(end_date, "isoformat") else (str(end_date) if end_date else ""),
    }


def source_fingerprint(jsonl_path: Path, claude_dir: Optional[Path] = None) -> Dict[str, Any]:
    stat = jsonl_path.stat()
    fingerprint = {
        "path": str(jsonl_path),
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
    }
    if claude_dir is not None:
        related = {}
        session_id = jsonl_path.stem
        for label, path in {
            "official_session_meta": claude_dir / "usage-data" / "session-meta" / f"{session_id}.json",
            "official_facet": claude_dir / "usage-data" / "facets" / f"{session_id}.json",
        }.items():
            if path.exists():
                related_stat = path.stat()
                related[label] = {
                    "path": str(path),
                    "mtime_ns": related_stat.st_mtime_ns,
                    "size": related_stat.st_size,
                }
        fingerprint["related"] = related
    return fingerprint


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _same_source(payload: Dict[str, Any], fingerprint: Dict[str, Any]) -> bool:
    return payload.get("source_fingerprint") == fingerprint


def _parse_report_date(meta: Dict[str, Any]) -> Optional[str]:
    if meta.get("report_date"):
        return str(meta["report_date"])
    for key in ("end_time", "start_time"):
        value = meta.get(key)
        if not value:
            continue
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone().date().isoformat()
        except Exception:
            continue
    return None


def load_cached_report_item(jsonl_path: Path, claude_dir: Path, start_date=None, end_date=None) -> Optional[Dict[str, Any]]:
    session_id = jsonl_path.stem
    fingerprint = source_fingerprint(jsonl_path, claude_dir)
    window = analysis_window(start_date, end_date)
    meta = _read_json(session_meta_dir(claude_dir) / f"{session_id}.json")
    facet = _read_json(facets_dir(claude_dir) / f"{session_id}.json")

    if not meta or not facet:
        return None
    if meta.get("analyzer_version") != SESSION_META_ANALYZER_VERSION:
        return None
    if facet.get("analyzer_version") != FACET_ANALYZER_VERSION:
        return None
    if meta.get("analysis_window") != window or facet.get("analysis_window") != window:
        return None
    if not _same_source(meta, fingerprint) or not _same_source(facet, fingerprint):
        return None

    report_date = _parse_report_date(meta)
    if not report_date:
        return None

    meta = dict(meta)
    facet = dict(facet)
    meta.pop("analyzer_version", None)
    meta.pop("source_fingerprint", None)
    meta.pop("analysis_window", None)
    meta.pop("generated_at", None)
    facet.pop("analyzer_version", None)
    facet.pop("source_fingerprint", None)
    facet.pop("analysis_window", None)
    facet.pop("generated_at", None)
    return {
        "facet": facet,
        "meta": meta,
        "date": datetime.fromisoformat(report_date).date(),
        "_cache_hit": True,
    }


def write_report_item_cache(item: Dict[str, Any], jsonl_path: Path, claude_dir: Path, start_date=None, end_date=None) -> None:
    session_id = jsonl_path.stem
    fingerprint = source_fingerprint(jsonl_path, claude_dir)
    window = analysis_window(start_date, end_date)
    generated_at = datetime.now().astimezone().isoformat()

    meta = dict(item.get("meta") or {})
    facet = dict(item.get("facet") or {})
    report_date = item.get("date")
    if report_date:
        meta["report_date"] = report_date.isoformat() if hasattr(report_date, "isoformat") else str(report_date)

    meta.update({
        "session_id": session_id,
        "analyzer_version": SESSION_META_ANALYZER_VERSION,
        "analysis_window": window,
        "source_fingerprint": fingerprint,
        "generated_at": generated_at,
    })
    facet.update({
        "session_id": session_id,
        "analyzer_version": FACET_ANALYZER_VERSION,
        "analysis_window": window,
        "source_fingerprint": fingerprint,
        "generated_at": generated_at,
    })

    _write_json(session_meta_dir(claude_dir) / f"{session_id}.json", meta)
    _write_json(facets_dir(claude_dir) / f"{session_id}.json", facet)
    update_index(session_id, item, fingerprint, claude_dir, generated_at, window)


def update_index(session_id: str, item: Dict[str, Any], fingerprint: Dict[str, Any], claude_dir: Path, generated_at: str, window: Dict[str, str]) -> None:
    path = index_path(claude_dir)
    index = _read_json(path)
    sessions = index.setdefault("sessions", {})
    report_date = item.get("date")
    sessions[session_id] = {
        "session_id": session_id,
        "report_date": report_date.isoformat() if hasattr(report_date, "isoformat") else str(report_date or ""),
        "project_path": (item.get("meta") or {}).get("project_path", ""),
        "analysis_window": window,
        "source_fingerprint": fingerprint,
        "session_meta_analyzer_version": SESSION_META_ANALYZER_VERSION,
        "facet_analyzer_version": FACET_ANALYZER_VERSION,
        "generated_at": generated_at,
    }
    index["cache_format"] = "insight-zh-cache-v1"
    index["updated_at"] = generated_at
    _write_json(path, index)
