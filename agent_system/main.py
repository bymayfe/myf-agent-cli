"""
main.py â€” Pipeline motoru (dinamik, agents_config.json'dan yÃ¼kler).

Ã–zellikler:
  - 5 AÅŸamalÄ± Pipeline: Planlama â†’ Kod Ãœretimi â†’ Micro-Fix â†’ Escalation â†’ DokÃ¼mantasyon
  - Micro-Fix DÃ¶ngÃ¼sÃ¼: qwen2.5-coder:7b ile hÄ±zlÄ± syntax onarÄ±mÄ± (maks 3 deneme)
  - Escalation: 3 denemede Ã§Ã¶zÃ¼lemeyen hatalar qwen3.8:27b'ye iletilir
  - Stuck-Loop Tespiti: AynÄ± hata 3x â†’ farklÄ± yaklaÅŸÄ±m ipucu enjekte edilir
  - Merkezi Log: log_store.py ile her adÄ±m (planner dahil) SQLite'a kaydedilir
  - Fiziksel Test: Ãœretilen kodlar derlenir ve Ã¶rnek verilerle Ã§alÄ±ÅŸtÄ±rÄ±lÄ±r
"""

from __future__ import annotations
import os
import sys
import json
import time
import tempfile
import logging
import subprocess
from pathlib import Path
from datetime import datetime

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = Path(__file__).parent.resolve()
for _sub in [_HERE, _HERE / "core", _HERE / "engines", _HERE / "agents", _HERE / "llm", _HERE / "storage", _HERE / "tests"]:
    _s = str(_sub)
    if _sub.is_dir() and _s not in sys.path:
        sys.path.insert(0, _s)

from config import (
    get_output_dir,
    MICRO_FIX_MODEL,
    ESCALATION_MODEL,
    MICRO_FIX_MAX_TRIES,
)
from settings import settings
from llm_client import call_llm
from brain import (
    write_brain_section,
    append_changelog,
    log_conversation,
    write_output_file,
    list_output_files,
)
from codebase_graph import build_repomap, codebase_graph
from code_parser import UniversalCodeParser, extract_code_blocks, extract_planned_files_from_architecture
from test_runner import CodeVerifier, run_code_verification_tests
from fix_engine import Fix, fix_engine, MicroFixEngine, EscalationEngine
from profiling_engine import ProfilingEngine, StuckLoopDetector
from agents import (
    load_agents,
    build_agent_prompt,
    extract_changelog,
    AgentDefinition,
)
from log_store import log_store
from git_guard import GitGuard, git_guard

logger = logging.getLogger("pipeline")


# ————————————————————————————————————————————————————————————————————————
# Fiziksel Kod Çalıştırma & Örnek Veri Test Motoru
# ————————————————————————————————————————————————————————————————————————

def _save_code_blocks(blocks: list[dict], agent_name: str = "developer") -> list[str]:
    """Kod bloklarını aktif proje klasörü altına yaz."""
    written = []
    for block in blocks:
        fname   = block.get("filename")
        content = block.get("content", "")
        if fname and content.strip():
            ok = write_output_file(fname, content, agent_name=agent_name)
            if ok:
                written.append(fname)
    return written


_run_code_verification_tests = run_code_verification_tests


# ————————————————————————————————————————————————————————————————————————
# ————————————————————————————————————————————————————————————————————————
# FixEngine — Modüler Hata Onarım Yöneticisi
# ————————————————————————————————————————————————————————————————————————

def _run_micro_fix(
    error_log: str,
    file_path: str,
    output_dir: str,
    model: str,
    max_tries: int,
    run_id: str,
    step_id: str,
) -> bool:
    """Tek bir dosyadaki sentaks/runtime hatasını hızlıca düzeltmeye çalışır."""
    return MicroFixEngine.run(error_log, file_path, output_dir, model, max_tries, run_id, step_id)


def _run_escalation(
    error_log: str,
    output_dir: str,
    model: str,
    context: dict,
    run_id: str,
    step_id: str,
) -> tuple[str, list[str]]:
    """Geniş bağlamı ve codebase grafiğini okur, refactoring yapar, kökten düzeltir."""
    return EscalationEngine.run(error_log, output_dir, model, context, run_id, step_id)


# ————————————————————————————————————————————————————————————————————————
# Optimizasyon & Stuck-Loop Tespiti
# ————————————————————————————————————————————————————————————————————————

_FULL_AUTONOMY_CAP = 50

_detect_optimize_request = ProfilingEngine.detect_optimize_request
_check_stuck = StuckLoopDetector.check_stuck
_STUCK_HINTS = StuckLoopDetector.HINTS
_run_profiling_capture = ProfilingEngine.run_profiling


# Dil tespiti icin anahtar kelime haritasi (siralama onemli: daha spesifik
# framework/ekosistem isimleri, genel dil isimlerinden ONCE kontrol edilir).
_LANGUAGE_KEYWORD_MAP: list[tuple[str, tuple[str, ...]]] = [
    ("typescript", ("next.js", "nextjs", "typescript", ".tsx", ".ts ", "tailwindcss", "react")),
    ("javascript", ("javascript", "node.js", "nodejs", "express", "vue.js", "vuejs")),
    ("go", ("golang", " go ", "gin framework", "go modules")),
    ("rust", ("rust", "cargo", "actix", "tokio")),
    ("java", ("spring boot", "java ", "maven", "gradle")),
    ("csharp", ("c#", ".net", "asp.net", "csharp")),
    ("python", ("python", "django", "flask", "fastapi", "pytest")),
]


def _detect_project_language(text: str) -> str:
    """
    Verilen metinden (proje istegi veya mimari belge) basit anahtar kelime
    eslesmesiyle proje dilini tahmin eder. Hicbir eslesme yoksa 'python'
    (sistemin varsayilan/en cok test edilen dili) donulur.
    """
    if not text:
        return "python"
    lowered = f" {text.lower()} "
    for lang, keywords in _LANGUAGE_KEYWORD_MAP:
        if any(kw in lowered for kw in keywords):
            return lang
    return "python"


# Her dil icin OPTIMIZE MODU talimati — sadece o dilde ANLAMLI olan
# profiling/performans izleme mekanizmalarini onerir. Python'a ozgu
# @profile_timer/tracemalloc gibi kavramlar ARTIK diger dillere sizmiyor.
_OPTIMIZE_MODE_TEMPLATES: dict[str, str] = {
    "python": (
        "OPTIMIZE MODU AKTIF: Kod yazarken tum kritik fonksiyonlara @profile_timer "
        "decorator ve tracemalloc bellek izleme ekle. Sureleri ve bellek kullanimi "
        "profiling_output.log dosyasina yaz. os.environ.get('PROFILING_MODE') == '1' "
        "ise calistirma sirasinda ayrintili profiling ciktisi ver."
    ),
    "typescript": (
        "OPTIMIZE MODU AKTIF: Kod yazarken kritik fonksiyonlara console.time()/"
        "console.timeEnd() veya performance.now() ile sure olcumu ekle. Next.js/"
        "React projelerinde agir bilesenleri React.memo, useMemo, useCallback ile "
        "optimize et. process.env.PROFILING_MODE === '1' ise ayrintili performans "
        "loglari yaz. ASLA Python'a ozgu @profile_timer veya tracemalloc kullanma."
    ),
    "javascript": (
        "OPTIMIZE MODU AKTIF: Kod yazarken kritik fonksiyonlara console.time()/"
        "console.timeEnd() ile sure olcumu ekle. process.env.PROFILING_MODE === '1' "
        "ise ayrintili performans loglari yaz. ASLA Python'a ozgu @profile_timer "
        "veya tracemalloc kullanma."
    ),
    "go": (
        "OPTIMIZE MODU AKTIF: Kritik fonksiyonlara time.Now()/time.Since() ile sure "
        "olcumu ekle; gerekirse net/http/pprof paketiyle profiling endpoint'i ekle. "
        "ASLA Python'a ozgu @profile_timer veya tracemalloc kullanma."
    ),
    "rust": (
        "OPTIMIZE MODU AKTIF: Kritik fonksiyonlara std::time::Instant ile sure "
        "olcumu ekle. ASLA Python'a ozgu @profile_timer veya tracemalloc kullanma."
    ),
    "java": (
        "OPTIMIZE MODU AKTIF: Kritik fonksiyonlara System.nanoTime() ile sure "
        "olcumu ekle. ASLA Python'a ozgu @profile_timer veya tracemalloc kullanma."
    ),
    "csharp": (
        "OPTIMIZE MODU AKTIF: Kritik fonksiyonlara System.Diagnostics.Stopwatch ile "
        "sure olcumu ekle. ASLA Python'a ozgu @profile_timer veya tracemalloc kullanma."
    ),
}


def _build_optimize_mode_instruction(language: str) -> str:
    """Verilen dile uygun OPTIMIZE MODU talimatini dondurur (bilinmeyen dil -> python)."""
    return _OPTIMIZE_MODE_TEMPLATES.get(language, _OPTIMIZE_MODE_TEMPLATES["python"])



# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Checkpoint (Devam) Sistemi
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

CHECKPOINT_FILE = ".myfcli/checkpoint.json"


def get_checkpoint_path(project_dir: str) -> Path:
    return Path(project_dir) / CHECKPOINT_FILE


def save_checkpoint(project_dir: str, data: dict) -> bool:
    """
    Checkpoint'i diske ATOMİK olarak yazar.
    tempfile.NamedTemporaryFile + os.replace kullanır.
    Yazım başarısız olursa orijinal checkpoint korunur ve False döner.
    """
    path = get_checkpoint_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=str(path.parent),
            prefix="chk_",
            suffix=".tmp",
            delete=False,
            encoding="utf-8"
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(json.dumps(data, ensure_ascii=False, indent=2))
            tmp.flush()
            os.fsync(tmp.fileno())

        os.replace(tmp_path, path)
        return True
    except Exception as exc:
        logger.error("Atomik checkpoint yazımı başarısız oldu (%s): %s", path, exc)
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        return False


def load_checkpoint(project_dir: str) -> dict | None:
    """
    Mevcut checkpoint'i oku.
    file_write_status kontrolü:
      - Eğer file_write_status == 'pending' ise, yarım kalmış adım tespit edilir.
      - current_role belirtilmiş ve completed_roles içindeyse çıkarılır.
      - current_role belirtilmemişse veya tespit edilemiyorsa, KÖR TAHMİNLE rol silinmez;
        kullanıcıya net hata uyarısı verilir.
      - Kurtarma sonrası düzeltilmiş durum atomik olarak diske yazılır (persist edilir).
      - Alan yoksa geriye dönük uyumluluk için 'committed' varsayılır.
    """
    path = get_checkpoint_path(project_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if "file_write_status" not in data:
            data["file_write_status"] = "committed"

        if data.get("file_write_status") == "pending":
            print(f"\n  ⚠️  [CHECKPOINT-RECOVERY] Yarım kalmış bir dosya yazımı (pending) tespit edildi!")
            completed = data.get("completed_roles", [])
            pending_role = data.get("current_role")

            if pending_role:
                if pending_role in completed:
                    completed.remove(pending_role)
                    print(f"     Yarım kalan adım '{pending_role}' güvenli şekilde baştan çalıştırılacak.")
                else:
                    print(f"     Yarım kalan adım '{pending_role}' henüz tamamlananlara eklenmemişti, baştan çalıştırılacak.")
                data["completed_roles"] = completed
                data["file_write_status"] = "pending_recovered"
                # Kurtarılmış durumu hemen diske yaz (persist et)
                save_checkpoint(project_dir, data)
            else:
                logger.error("Kurtarma yapılamadı: Yarım kalan adım (current_role) tespit edilemedi.")
                print(f"  ❌ [CHECKPOINT-ERROR] Kurtarma yapılamadı: Yarım kalan adım tespit edilemedi. Checkpoint'i manuel kontrol edin.")
                data["file_write_status"] = "unrecoverable_pending"

        return data
    except Exception as exc:
        logger.error("Checkpoint okunurken hata: %s", exc)
        return None


def clear_checkpoint(project_dir: str) -> None:
    """Tamamlanan pipeline'in checkpoint'ini sil."""
    path = get_checkpoint_path(project_dir)
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Ana Pipeline
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def run_pipeline(
    project_brief: str,
    progress_callback=None,
    project_dir: str = None,
    max_retries: int = 3,
    resume_checkpoint: dict = None,
) -> dict:
    """
    5 Aşamalı Pipeline:
      1. Planlama
      2. Kod Üretimi
      3. Micro-Fix Döngüsü
      4. Escalation
      5. Dokümantasyon/Review

    Tüm adımlar log_store.py ile SQLite'a kaydedilir.
    resume_checkpoint: dict ise tamamlanan ajanlar atlanır, bağlam geri yüklenir.
    """
    output_dir = project_dir or get_output_dir()
    os.makedirs(output_dir, exist_ok=True)
    start_time = datetime.now()

    # Yeni veya devam eden koşu için fix_engine state'ini sıfırla
    fix_engine.reset_state()

    # Git Guard başlat ve kirli çalışma ağacı kontrolü yap
    guard = GitGuard(project_dir=output_dir)
    guard.check_initial_dirty()

    agents = load_agents(enabled_only=True)
    if not agents:
        raise RuntimeError("Hic aktif agent yok! manage_agents.py ile agent ekleyin.")

    # Oturum bilgisi
    try:
        from session_manager import session_manager
        session_id = session_manager.current_session.session_id
        project_name = session_manager.current_session.title
    except Exception:
        session_id   = "unknown"
        project_name = project_brief[:30]

    # Log: pipeline baÅŸlat
    run_id = log_store.start_run(
        session_id=session_id,
        project_name=project_name,
        brief=project_brief,
        project_dir=output_dir,
    )

    # Full Otonomi
    is_full_autonomy = (max_retries < 0)
    if is_full_autonomy:
        max_retries = _FULL_AUTONOMY_CAP

    is_optimize_mode = _detect_optimize_request(project_brief)
    if is_optimize_mode:
        logger.info("[OPTIMIZE MODU] Aktif â€” Profiling + Optimizer akisi etkinlesti.")

    mode_str = (f"Full Otonomi + {'Optimize' if is_optimize_mode else 'Standart'}"
                if is_full_autonomy
                else f"Maks {max_retries} Deneme")
    logger.info("Pipeline basliyor (%s). istek: %s", mode_str, project_brief[:80])

    context: dict[str, str] = {"project_brief": project_brief}
    if is_optimize_mode:
        # Baslangicta sadece proje istegi metninden (brief) tahmini bir dil tespiti
        # yapilir. Mimari belge uretildikten sonra (asagida) daha guvenilir bir
        # tespitle bu talimat GUNCELLENIR (bkz. _build_optimize_mode_instruction).
        context["OPTIMIZE_MODE"] = _build_optimize_mode_instruction(
            _detect_project_language(project_brief)
        )

    all_files_written: list[str] = []
    queue = agents.copy()
    if not is_optimize_mode:
        # Optimizasyon modu açık değilse optimizer ajanını normal akıştan çıkar
        queue = [a for a in queue if a.role_type != "optimizer"]

    step  = 0
    retry_count = 0

    error_history:   list[str] = []
    stuck_hint_idx:  int = 0
    qa_fail_streak:  int = 0

    _optimizer_agent = next((a for a in agents if a.role_type == "optimizer"), None)

    # â”€â”€ Resume: checkpoint'ten kaldÄ±ÄŸÄ± yerden devam â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    completed_roles: list[str] = []
    if resume_checkpoint:
        completed_roles = resume_checkpoint.get("completed_roles", [])
        retry_count     = resume_checkpoint.get("retry_count", 0)
        # KaydedilmiÅŸ baÄŸlamÄ± geri yÃ¼kle (prd, architecture, vb.)
        saved_ctx = resume_checkpoint.get("context", {})
        context.update(saved_ctx)
        run_id = resume_checkpoint.get("run_id", run_id)
        # Tamamlanan ajanlarÄ± kuyruktan Ã§Ä±kar
        queue = [a for a in queue if a.role_type not in completed_roles]
        logger.info("[RESUME] Tamamlanan ajanlar atlaniyor: %s", completed_roles)
        logger.info("[RESUME] Devam: %s", [a.role_type for a in queue])

    # ── Başlangıç checkpoint ──────────────────────────────────────────
    _checkpoint_base = {
        "run_id":            run_id,
        "session_id":        session_id,
        "project_dir":       output_dir,
        "brief":             project_brief,
        "max_retries":       max_retries,
        "completed_roles":   completed_roles,
        "retry_count":       retry_count,
        "status":            "running",
        "file_write_status": "committed",
        "started_at":        start_time.isoformat(),
    }
    save_checkpoint(output_dir, {**_checkpoint_base, "context": {}})

    while queue:
        try:
            agent = queue.pop(0)
            step += 1
            total = step + len(queue)

            if progress_callback:
                progress_callback(step, total, agent, "start",
                                  {"role": agent.role_type, "model": agent.model, "retry_count": retry_count})

            # Adım başı: dosya yazımı öncesi 'pending' olarak kaydet
            _ctx_to_save = {k: v for k, v in context.items()
                            if k not in ("repomap",) and len(str(v)) < 30000}
            save_checkpoint(output_dir, {
                **_checkpoint_base,
                "completed_roles":   completed_roles,
                "current_role":      agent.role_type,
                "retry_count":       retry_count,
                "status":            "running",
                "file_write_status": "pending",
                "context":           _ctx_to_save,
            })

            t_prep_start = time.monotonic()
            current_files = list_output_files()
            repomap       = build_repomap(output_dir)
            context["repomap"] = repomap

            user_prompt = build_agent_prompt(agent, context, current_files)
            prep_sec = round(time.monotonic() - t_prep_start, 2)

            if progress_callback:
                progress_callback(step, total, agent, "generating", {"prompt_len": len(user_prompt), "prep_sec": prep_sec})

            # Log: agent adımı başlat
            step_id = log_store.start_step(
                run_id=run_id,
                step_number=step,
                agent_id=agent.id,
                agent_name=agent.display_name,
                agent_role=agent.role_type,
                model=agent.model,
                prompt_chars=len(user_prompt),
                prompt_text=user_prompt,
                project_dir=output_dir,
            )

            step_start = time.monotonic()
            try:
                llm_retries = 20 if max_retries < 0 else 3
                raw = call_llm(
                    agent_name=agent.id,
                    system_prompt=agent.system_prompt,
                    user_prompt=user_prompt,
                    model=agent.model,
                    max_retries=llm_retries,
                )
            except RuntimeError as exc:
                elapsed = time.monotonic() - step_start
                log_store.finish_step(step_id, "llm_error",
                                      response_chars=0, files_written=[],
                                      elapsed_sec=elapsed, full_output=str(exc),
                                      project_dir=output_dir)
                log_store.log_error(
                    run_id=run_id, step_id=step_id,
                    agent_name=agent.display_name, agent_model=agent.model,
                    error_type="llm", error_msg=str(exc),
                    project_dir=output_dir,
                )
                logger.error("[%s] LLM hatasi: %s", agent.display_name, exc)
                if progress_callback:
                    progress_callback(step, total, agent, "error", {"error": str(exc)})
                continue

            # Kod Ã§Ä±ktÄ±larÄ±nÄ± kaydet
            blocks  = extract_code_blocks(raw)
            written = _save_code_blocks(blocks, agent_name=agent.id)
            all_files_written.extend(written)

            # ── DEV ajani: fiziksel test + Eksik Dosya Tespiti + Micro-Fix + Escalation ──
            test_results     = None
            has_physical_error = False

            if agent.role_type == "developer":
                if not written:
                    # Dosya yazılmadıysa (hem ilk çalışmada hem retry'da):
                    no_file_err = context.get(
                        "last_test_error",
                        "KRITIK HATA: Hicbir dosya kaydedilemedi! "
                        "Model, kod bloklarinin ILK satirina '# filepath: dosya.py' "
                        "veya '// filepath: dosya.js' gibi FILEPATH YORUMU eklemedigi icin "
                        "sistem dosyalari tanimlayamadi. "
                        "Bir sonraki denemede TUM kod bloklarinin BIRINCI satirina "
                        "filepath yorumunu KESINLIKLE ekle."
                    )
                    context["last_test_error"] = no_file_err
                    error_history.append(no_file_err)
                    logger.warning(
                        "[%s] Dosya yazilmadi (deneme=%d). filepath yorumu eksik olmali.",
                        agent.display_name, retry_count,
                    )
                    if _check_stuck(error_history):
                        hint = _STUCK_HINTS[stuck_hint_idx % len(_STUCK_HINTS)]
                        stuck_hint_idx += 1
                        context["STUCK_ALERT"] = hint
                        error_history.clear()

                # Mimaride planlanan ancak henüz yazılmamış dosyaları kontrol et
                #
                # ÖNEMLİ: pf_clean bir dizin yolu içeriyorsa (örn. "app/snippets/page.tsx")
                # SADECE tam yol eşleşmesi veya o TAM YOLUN sonek olarak eşleşmesi kabul
                # edilir. Ters yönlü ("pf_clean.endswith('/' + cf)") eşleşme KASITLI
                # OLARAK KULLANILMAZ — aksi halde kökte alakasız bir "page.tsx" varken
                # sistem "app/snippets/page.tsx" de yazılmış zannedebilir (yanlış pozitif).
                # pf_clean'in hiç dizin bilgisi yoksa (bare filename), mimari metninde
                # zaten dizin bağlamı verilmemiş demektir; bu durumda tek bir gevşek
                # (sadece basename) eşleşmeye izin verilir.
                arch_text = context.get("architecture", "")
                planned_files = extract_planned_files_from_architecture(arch_text)
                current_on_disk = list_output_files()
                missing_files = []
                for pf in planned_files:
                    pf_clean = pf.replace("\\", "/").lstrip("./")
                    has_dir = "/" in pf_clean
                    if has_dir:
                        exists = any(
                            cf.replace("\\", "/").lstrip("./") == pf_clean or
                            cf.replace("\\", "/").lstrip("./").endswith("/" + pf_clean)
                            for cf in current_on_disk
                        )
                    else:
                        exists = any(
                            cf.replace("\\", "/").lstrip("./") == pf_clean or
                            cf.replace("\\", "/").split("/")[-1] == pf_clean
                            for cf in current_on_disk
                        )
                    if not exists:
                        missing_files.append(pf_clean)

                dev_loop_count = context.get("_dev_turn_count", 0) + 1
                context["_dev_turn_count"] = dev_loop_count
                MAX_DEV_TURNS = 4

                if missing_files and dev_loop_count < MAX_DEV_TURNS:
                    logger.info(
                        "[%s] Mimarideki %d eksik dosya tespit edildi, tamamlanıyor: %s",
                        agent.display_name, len(missing_files), missing_files
                    )
                    print(f"\n  📝 [DEV OTOMATIK TAMAMLAMA] Mimaride planlanan {len(missing_files)} eksik dosya yazılıyor: {missing_files[:3]}...")
                    missing_msg = (
                        f"MİMARİDE PLANLANAN ANCAK HENÜZ YAZILMAMIŞ {len(missing_files)} EKSİK DOSYA TESPİT EDİLDİ:\n"
                        + "\n".join(f"- {f}" for f in missing_files)
                        + "\n\n🚨 GÖREV: Şimdi SADECE bu eksik dosyaları eksiksiz kodla. "
                        + "Her dosya kod bloğunun birinci satırına, DOSYANIN GERÇEK UZANTISINA VE DİLİNE UYGUN "
                        + "yorum sözdizimiyle filepath yorumu ekle (örn. '# filepath: klasor/dosya.py' sadece "
                        + ".py dosyaları için, '// filepath: klasor/dosya.ts' .ts/.tsx/.js için, "
                        + "'<!-- filepath: dosya.html -->' HTML için, '/* filepath: dosya.css */' CSS için). "
                        + "ASLA otomatik olarak .py veya python varsayma — yukarıdaki eksik dosya listesindeki "
                        + "gerçek uzantıyı kullan."
                    )
                    context["MISSING_FILES"] = missing_msg
                    # Developer'ı sıranın başına tekrar ekle
                    queue.insert(0, agent)
                    if progress_callback:
                        progress_callback(step, total, agent, "missing_files", {"missing": missing_files})
                else:
                    context.pop("MISSING_FILES", None)

                if written:
                    test_results = _run_code_verification_tests(output_dir)

                    if test_results.get("error"):
                        err_msg = test_results["error"]
                        logger.warning("[%s] Test Hatasi: %s", agent.display_name, err_msg[:200])

                        # Log: test hatası — kesin error_type kullan
                        err_id = log_store.log_error(
                            run_id=run_id, step_id=step_id,
                            agent_name=agent.display_name, agent_model=agent.model,
                            error_type=test_results.get("error_type") or "runtime",
                            error_msg=err_msg,
                            file_path=test_results.get("file", ""),
                            project_dir=output_dir,
                        )

                        # ── FIX ENGINE (Hata Onarımı) ──
                        target_fix_file = test_results.get("file") or (written[0] if written else "")
                        fixed, fix_written = fix_engine.repair(
                            error_log=err_msg,
                            target_file=target_fix_file,
                            output_dir=output_dir,
                            context=context,
                            run_id=run_id,
                            step_id=step_id,
                        )
                        all_files_written.extend(fix_written)

                        if fixed:
                            post_test = _run_code_verification_tests(output_dir)
                            if not post_test.get("error"):
                                print(f"  ✓ [FIX ENGINE] Hata basariyla cozuldu!")
                                test_results["resolved"] = True
                                log_store.resolve_error(err_id, resolver="fix_engine")
                                error_history.clear()
                                qa_fail_streak = 0
                                context.pop("last_test_error", None)
                                context.pop("STUCK_ALERT", None)
                            else:
                                context["last_test_error"] = post_test["error"]
                                has_physical_error = True
                                error_history.append(post_test["error"])
                                if _check_stuck(error_history):
                                    hint = _STUCK_HINTS[stuck_hint_idx % len(_STUCK_HINTS)]
                                    stuck_hint_idx += 1
                                    context["STUCK_ALERT"] = hint
                                    error_history.clear()
                        else:
                            context["last_test_error"] = err_msg
                            has_physical_error = True
                    else:
                        # Test başarılı
                        error_history.clear()
                        qa_fail_streak = 0
                        context.pop("last_test_error", None)
                        context.pop("STUCK_ALERT", None)

                        if is_optimize_mode:
                            prof_log = _run_profiling_capture(output_dir)
                            if prof_log:
                                context["profiling_log"] = prof_log

            # Brain bÃ¶lÃ¼mÃ¼ gÃ¼ncelle
            if agent.output_brain_section:
                write_brain_section(agent.output_brain_section, raw[:4000])

            context[agent.produces_output] = raw

            # Mimari belge yeni uretildiyse, OPTIMIZE_MODE talimatini artik cok
            # daha guvenilir olan mimari metnine (package.json, .tsx uzantilari,
            # framework isimleri vb. acikca gecer) gore YENIDEN tespit et.
            if agent.role_type == "software_architect" and is_optimize_mode:
                detected_lang = _detect_project_language(raw)
                context["OPTIMIZE_MODE"] = _build_optimize_mode_instruction(detected_lang)
                logger.info("[OPTIMIZE MODU] Mimariye gore dil tespiti: %s", detected_lang)

            # Log: adım tamamla
            elapsed_step = time.monotonic() - step_start
            log_store.finish_step(
                step_id=step_id,
                status="success" if not has_physical_error else "partial",
                response_chars=len(raw),
                files_written=written,
                elapsed_sec=elapsed_step,
                full_output=raw,
                project_dir=output_dir,
            )

            # Her adım tamamlandığında AUDIT_LOG.md dosyasını otomatik tazele (ayar açıksa)
            if getattr(settings, "auto_audit_log", True):
                try:
                    log_store.export_full_logs_md(project_dir=output_dir)
                except Exception:
                    pass

            # ── Checkpoint güncelle (her ajan sonrası atomik olarak committed yaz) ──
            if agent.role_type not in completed_roles:
                completed_roles.append(agent.role_type)
            _ctx_to_save = {k: v for k, v in context.items()
                            if k not in ("repomap",) and len(str(v)) < 30000}
            save_ok = save_checkpoint(output_dir, {
                **_checkpoint_base,
                "completed_roles":   completed_roles,
                "current_role":      None,
                "retry_count":       retry_count,
                "status":            "running",
                "file_write_status": "committed",
                "context":           _ctx_to_save,
            })
            if not save_ok:
                logger.error("Checkpoint 'committed' olarak kaydedilemedi: %s", output_dir)
            else:
                # Adım başarılı olduğunda otomatik git checkpoint'i al
                guard.checkpoint(step_name=agent.display_name, agent_role=agent.role_type)

            # â”€â”€ Optimizer ajani â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            if agent.role_type == "optimizer":
                is_optimized = "STATUS: OPTIMIZED" in raw.upper()
                can_retry    = (max_retries < 0) or (retry_count < max_retries)
                if is_optimized:
                    changelog_text = extract_changelog(raw)
                    elapsed_now    = (datetime.now() - start_time).total_seconds()
                    append_changelog(
                        title=f"Optimizasyon Tamamlandi: {project_brief[:50]}",
                        body=f"{changelog_text}\n\n_Sure: {elapsed_now:.1f}s_",
                    )
                    log_conversation(agent.display_name, agent.system_prompt, user_prompt, raw)
                    if progress_callback:
                        progress_callback(step, total, agent, "completed", {
                            "written": written, "raw_len": len(raw),
                            "test_results": test_results, "is_loop_triggered": False,
                            "retry_count": retry_count,
                            "elapsed_sec": round(elapsed_step, 1),
                        })
                    break
                elif can_retry and _optimizer_agent:
                    retry_count += 1
                    dev_agent = next((a for a in agents if a.role_type == "developer"), None)
                    if dev_agent:
                        queue.insert(0, _optimizer_agent)
                        queue.insert(0, dev_agent)
                        context["OPTIMIZER_FEEDBACK"] = (
                            f"## OPTIMIZER RAPORU (Tur {retry_count})\n{raw[:2000]}\n\n"
                            "Yukaridaki optimizasyon raporunu dikkate alarak kodu yeniden yaz."
                        )

            # ── QA ajani ─────────────────────────────────────────────────────────────
            is_loop_triggered = False
            if agent.role_type == "qa_tester":
                raw_upper = raw.upper()
                has_explicit_fail = "## STATUS: FAILED" in raw_upper
                has_real_crash = ("last_test_error" in context) or ("IMPORT" in raw_upper and "ERROR" in raw_upper) or ("SYNTAX" in raw_upper and "ERROR" in raw_upper) or ("TRACEBACK" in raw_upper)

                # Mimari eksik dosya kontrolü
                arch_text = context.get("architecture", "")
                planned_files = extract_planned_files_from_architecture(arch_text)
                current_on_disk = list_output_files()
                missing_files = []
                for pf in planned_files:
                    pf_clean = pf.replace("\\", "/").lstrip("./")
                    exists = any(
                        cf.replace("\\", "/").lstrip("./") == pf_clean or
                        cf.replace("\\", "/").endswith("/" + pf_clean) or
                        pf_clean.endswith("/" + cf.replace("\\", "/"))
                        for cf in current_on_disk
                    )
                    if not exists:
                        missing_files.append(pf_clean)

                is_qa_failed = has_explicit_fail or has_real_crash or (len(missing_files) > 0 and retry_count < 2)
                MAX_QA_RETRIES = 3
                can_retry = (retry_count < MAX_QA_RETRIES)

                if (is_qa_failed or "last_test_error" in context) and can_retry:
                    retry_count += 1
                    is_loop_triggered = True
                    qa_fail_streak += 1

                    dev_agent = next((a for a in agents if a.role_type == "developer"), None)
                    qa_agent  = next((a for a in agents if a.role_type == "qa_tester"),  None)

                    # 2. denemede doğrudan FixEngine devreye girsin (eğer fiziksel hata varsa ve eksik dosya yoksa)
                    if retry_count >= 2 and ("last_test_error" in context or test_results) and not missing_files:
                        err_to_fix = context.get("last_test_error", raw)
                        fix_file = (test_results.get("file") if test_results else "") or (written[0] if written else "")
                        print(f"\n  🚨 [LOOP BREAKER] QA başarısız oldu (Deneme {retry_count}). FixEngine ile kökten onarılıyor...")
                        fixed, fix_written = fix_engine.repair(
                            error_log=err_to_fix,
                            target_file=fix_file,
                            output_dir=output_dir,
                            context=context,
                            run_id=run_id,
                            step_id=step_id,
                        )
                        if fixed:
                            context.pop("last_test_error", None)
                            context.pop("QA_FEEDBACK", None)
                            fix_engine.reset_on_success(target_file=fix_file)
                            is_loop_triggered = False
                            continue

                    if dev_agent and qa_agent and is_loop_triggered:
                        if is_optimize_mode and _optimizer_agent:
                            queue.insert(0, _optimizer_agent)
                        queue.insert(0, qa_agent)
                        queue.insert(0, dev_agent)
                        feedback_msg = f"## QA TEST RAPORU (Deneme {retry_count}/{MAX_QA_RETRIES})\n{raw[:3000]}\n"
                        if missing_files:
                            feedback_msg += f"\n🚨 MİMARİDE PLANLANAN ANCAK HENÜZ YAZILMAMIŞ EKSİK DOSYALAR:\n" + "\n".join(f"- {f}" for f in missing_files) + "\n"
                        if "last_test_error" in context:
                            feedback_msg += f"\n## FIZIKSEL TEST HATASI\n{context['last_test_error'][:1500]}\n"

                        if qa_fail_streak >= 2 and "STUCK_ALERT" not in context:
                            hint = _STUCK_HINTS[stuck_hint_idx % len(_STUCK_HINTS)]
                            stuck_hint_idx += 1
                            context["STUCK_ALERT"] = hint
                            qa_fail_streak = 0

                        if "STUCK_ALERT" in context:
                            feedback_msg += f"\n{context['STUCK_ALERT']}\n"

                        context["QA_FEEDBACK"] = feedback_msg

                        # Log: QA hatası
                        log_store.log_error(
                            run_id=run_id, step_id=step_id,
                            agent_name=agent.display_name, agent_model=agent.model,
                            error_type="qa_failed", error_msg=raw[:500],
                            retry_attempt=retry_count,
                            project_dir=output_dir,
                        )

                elif not can_retry and (is_qa_failed or "last_test_error" in context):
                    print(f"\n  ⚠️  [DÖNGÜ SINIRI] Maksimum QA deneme sınırına ({MAX_QA_RETRIES}) ulaşıldı. Kalan sorunlar Reviewer'a aktarılıyor.")
                    post_v = _run_code_verification_tests(output_dir)
                    if post_v.get("error"):
                        context["QA_FEEDBACK"] = (
                            f"## DİKKAT (REVIEWER İÇİN KRİTİK TEST HATASI):\n"
                            f"{post_v['error']}\n\n"
                            f"Lütfen yukarıdaki hatayı SEARCH/REPLACE blokları ile kesinlikle düzelt."
                        )
                    else:
                        context.pop("QA_FEEDBACK", None)
                    context.pop("last_test_error", None)

                elif "QA_FEEDBACK" in context:
                    del context["QA_FEEDBACK"]
                    context.pop("last_test_error", None)
                    context.pop("STUCK_ALERT", None)

            # Changelog (son adÄ±m veya reviewer)
            is_last = (len(queue) == 0)
            if is_last or agent.role_type == "reviewer":
                changelog_text = extract_changelog(raw)
                elapsed = (datetime.now() - start_time).total_seconds()
                append_changelog(
                    title=f"Proje: {project_brief[:60]}",
                    body=f"{changelog_text}\n\n_Sure: {elapsed:.1f}s, {len(all_files_written)} dosya_",
                )

            log_conversation(agent.display_name, agent.system_prompt, user_prompt, raw)

            if progress_callback:
                progress_callback(step, total, agent, "completed", {
                    "written":           written,
                    "raw_len":           len(raw),
                    "test_results":      test_results,
                    "is_loop_triggered": is_loop_triggered,
                    "retry_count":       retry_count,
                    "elapsed_sec":       round(elapsed_step, 1),
                })

        except KeyboardInterrupt:
            # Checkpoint'i "interrupted" ve "pending" olarak kaydet ve çık
            _ctx_to_save = {k: v for k, v in context.items()
                            if k not in ("repomap",) and len(str(v)) < 30000}
            save_checkpoint(output_dir, {
                **_checkpoint_base,
                "completed_roles":   completed_roles,
                "current_role":      agent.role_type if 'agent' in locals() else None,
                "retry_count":       retry_count,
                "status":            "interrupted",
                "file_write_status": "pending",
                "interrupted_at":    datetime.now().isoformat(),
                "context":           _ctx_to_save,
            })
            log_store.finish_run(
                run_id=run_id, status="interrupted",
                total_agents=step, files_written=len(list_output_files()),
                elapsed_sec=(datetime.now() - start_time).total_seconds(),
                project_dir=output_dir,
            )
            raise  # chat.py'deki handler'a ilet

    # Pipeline tamamlandÄ± â€” checkpoint temizle
    elapsed     = (datetime.now() - start_time).total_seconds()
    final_files = list_output_files()

    final_status = "success"
    if any("last_test_error" in context for _ in [None]):
        final_status = "partial"

    log_store.finish_run(
        run_id=run_id,
        status=final_status,
        total_agents=step,
        files_written=len(final_files),
        elapsed_sec=elapsed,
        project_dir=output_dir,
    )
    clear_checkpoint(output_dir)

    return {
        "run_id":       run_id,
        "files_written": final_files,
        "elapsed":       elapsed,
        "agents_run":    step,
        "project_dir":   output_dir,
    }

