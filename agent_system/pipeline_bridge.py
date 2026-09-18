"""
pipeline_bridge.py — Next.js Web UI ile Python Pipeline Engine arasında canlı köprü.

Stdout'a tek satırlık JSON (NDJSON) basarak aşamaları, ajanları ve üretilen dosyaları Web UI'a iletir:
{"event": "status", "data": "..."}
{"event": "pipeline_event", "data": {...}}
{"event": "done", "data": {...}}
"""

from __future__ import annotations
import sys
import os
import json
import argparse
import traceback
from pathlib import Path

# Windows UTF-8 stdout
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Modül arama yolları
_HERE = Path(__file__).parent.resolve()
_SYS_ROOT = _HERE
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))
for _sub in [_SYS_ROOT / "core", _SYS_ROOT / "engines", _SYS_ROOT / "agents", _SYS_ROOT / "llm", _SYS_ROOT / "storage", _SYS_ROOT]:
    _p = str(_sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)



def emit_event(event_name: str, data: any):
    """Web UI SSE tüketicisine tek satır JSON bas."""
    try:
        payload = json.dumps({"event": event_name, "data": data}, ensure_ascii=False)
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="MYF-Agent Python Pipeline Bridge")
    parser.add_argument("--brief", required=True, help="Proje isteği / hedef gereksinim")
    parser.add_argument("--project-dir", required=True, help="Hedef proje klasörü")
    parser.add_argument("--session-id", default=None, help="Aktif oturum ID'si")
    parser.add_argument("--max-retries", type=int, default=3, help="Hata durumunda maksimum deneme")
    args = parser.parse_args()

    from settings import settings
    from config import set_output_dir
    from permission_manager import permission_manager
    from session_manager import session_manager

    # Web UI'dan tetiklenen pipeline için tam izinli Sandbox modu
    permission_manager._session_grants.add("all")
    try:
        permission_manager._save_project_policy()
    except Exception:
        pass

    target_dir = os.path.abspath(args.project_dir)
    os.makedirs(target_dir, exist_ok=True)
    set_output_dir(target_dir)

    if args.session_id:
        try:
            session_manager.load_session(args.session_id)
            if session_manager.current_session:
                session_manager.current_session.set_custom_project_dir(Path(target_dir))
        except Exception:
            pass

    emit_event("status", f"🚀 Python Pipeline Motoru aktif: '{Path(target_dir).name}' klasöründe başlatılıyor...")

    def on_progress(step: int, total: int, agent: any, status: str, data: dict):
        stage_name = getattr(agent, "display_name", getattr(agent, "name", "Agent"))
        stage_icon = getattr(agent, "icon", "🤖")
        role_type = getattr(agent, "role_type", "worker")
        agent_model = getattr(agent, "model", "")

        if status == "start":
            emit_event("pipeline_event", {
                "stage": step,
                "totalStages": total,
                "stageName": stage_name,
                "stageIcon": stage_icon,
                "status": "start",
                "message": f"[{role_type.upper()}] {stage_name} göreve başladı ({agent_model})...",
                "details": data or {},
            })
            emit_event("status", f"{stage_icon} [{stage_name}] Göreve başladı ({agent_model})...")

        elif status == "completed":
            written_files = (data or {}).get("written", [])
            for wf in written_files:
                emit_event("pipeline_event", {
                    "stage": step,
                    "totalStages": total,
                    "stageName": stage_name,
                    "stageIcon": "💻",
                    "status": "file_written",
                    "file": wf,
                    "message": f"Dosya diske kaydedildi: {wf}",
                })

            emit_event("pipeline_event", {
                "stage": step,
                "totalStages": total,
                "stageName": stage_name,
                "stageIcon": stage_icon,
                "status": "done",
                "message": f"{stage_name} aşaması tamamlandı! ({len(written_files)} dosya üretildi)",
                "details": data or {},
            })
            emit_event("status", f"{stage_icon} [{stage_name}] Başarıyla tamamlandı.")

        elif status == "error":
            err_msg = (data or {}).get("error", "Bilinmeyen hata")
            emit_event("pipeline_event", {
                "stage": step,
                "totalStages": total,
                "stageName": stage_name,
                "stageIcon": "⚠️",
                "status": "error",
                "message": f"{stage_name} adımında hata: {err_msg}",
                "details": data or {},
            })

    try:
        # 1. Mod kontrolü: Subagent vs Waterfall
        if settings.execution_mode == "subagent":
            emit_event("status", "⚡ Dinamik Subagent Orkestrasyon Modu devrede...")
            from subagent_engine import subagent_orchestrator
            result = subagent_orchestrator.run(
                project_brief=args.brief,
                project_dir=target_dir,
                progress_callback=on_progress,
            )
        else:
            emit_event("status", "⚡ 5 Aşamalı Sıralı Çoklu-Ajan (Waterfall) Pipeline devrede...")
            from main import run_pipeline
            result = run_pipeline(
                project_brief=args.brief,
                progress_callback=on_progress,
                project_dir=target_dir,
                max_retries=args.max_retries,
            )

        files_count = len(result.get("files_written", []))
        elapsed_sec = result.get("elapsed", 0)
        emit_event("status", f"✅ Pipeline tüm aşamalarıyla tamamlandı! Toplam {files_count} dosya üretildi ({elapsed_sec:.1f}s).")
        emit_event("done", {
            "files": result.get("files_written", []),
            "elapsed": elapsed_sec,
            "project_dir": target_dir,
        })
    except Exception as exc:
        err_detail = traceback.format_exc()
        emit_event("error", f"Pipeline Hatası: {exc}")
        emit_event("status", f"❌ Pipeline durduruldu: {exc}")
        emit_event("done", {"error": str(exc), "traceback": err_detail})
        sys.exit(1)


if __name__ == "__main__":
    main()
