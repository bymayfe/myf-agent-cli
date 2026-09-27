"""
fix_engine.py — Modüler Hata Onarım ve Escalation Motoru

Cerrahi kod düzeltmeleri (Micro-Fix), geniş bağlamlı mimari onarımlar (Escalation) 
ve otomatik onarım döngülerini yönetir.
"""

from __future__ import annotations
import time
import logging
import re
import enum
import hashlib
import threading
from pathlib import Path
from typing import Optional

from config import (
    ESCALATION_MODEL,
    MICRO_FIX_MAX_TRIES,
)
from llm_client import call_llm
from brain import write_output_file
from codebase_graph import build_repomap
from code_parser import extract_code_blocks
from log_store import log_store
from test_runner import CodeVerifier, run_code_verification_tests
from git_guard import GitGuard

try:
    from laya_engine import laya_engine
except ImportError:
    try:
        from engines.laya_engine import laya_engine
    except ImportError:
        laya_engine = None

logger = logging.getLogger("fix_engine")


class FixStage(str, enum.Enum):
    """Hata onarım durum makinesi aşamaları."""
    MICRO_FIX = "MICRO_FIX"
    DEEP_REFACTOR = "DEEP_REFACTOR"
    LOOP_BREAKER = "LOOP_BREAKER"


def compute_error_signature(error_log: str) -> str:
    """
    Hata logunu normalleştirip kararlı bir SHA-256 imzası üretir.
    Geçici adresleri (0x7f...), zaman damgalarını ve geçici dosya yollarını temizler.
    """
    if not error_log:
        return hashlib.sha256(b"").hexdigest()

    # 1. Bellek adreslerini temizle (0x7f8a9b1c -> 0xADDR)
    normalized = re.sub(r"0x[0-9a-fA-F]+", "0xADDR", error_log)
    # 2. Tarih/saat damgalarını temizle
    normalized = re.sub(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?", "TIMESTAMP", normalized)
    # 3. Yollardaki geçici klasör isimlerini (/tmp/pytest-xxx/) normalize et
    normalized = re.sub(r"/tmp/[^/\s]+", "/tmp/DIR", normalized)
    # 4. Fazla boşlukları sadeleştir
    normalized = "\n".join(line.strip() for line in normalized.splitlines() if line.strip())

    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _save_code_blocks(blocks: list[dict], agent_name: str = "fix_engine") -> list[str]:
    """Kod bloklarını aktif proje klasörü altına kaydeder."""
    written = []
    for block in blocks:
        fname = block.get("filename")
        content = block.get("content", "")
        if fname and content.strip():
            ok = write_output_file(fname, content, agent_name=agent_name)
            if ok:
                written.append(fname)
    return written


class MicroFixEngine:
    """Tek bir dosyadaki sentaks/runtime hatasını hızlıca ve cerrahi olarak düzeltir."""

    @classmethod
    def run(
        cls,
        error_log: str,
        file_path: str,
        output_dir: str,
        model: str,
        max_tries: int,
        run_id: str,
        step_id: str,
        laya_diagnosis: Optional[dict] = None,
    ) -> bool:
        full_path = Path(output_dir) / file_path if file_path else None
        if full_path and not full_path.exists() and file_path:
            matches = list(Path(output_dir).rglob(Path(file_path).name))
            if matches:
                full_path = matches[0]
                file_path = str(full_path.relative_to(output_dir)).replace("\\", "/")

        file_content = ""
        if full_path and full_path.exists():
            try:
                file_content = full_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                pass

        diag_hint = ""
        if laya_diagnosis:
            cat = laya_diagnosis.get("category", "")
            act = laya_diagnosis.get("suggested_action", "")
            diag_hint = f"## FAST DIAGNOSTIC (LAYA Karar Motoru)\n- Category: {cat}\n- Suggested Action: {act}\n\n"

        system_prompt = (
            "You are an expert code repair specialist.\n"
            "You will be given an error log and the problematic file.\n"
            "Fix the error SURGICALLY with minimal changes.\n"
            "Do NOT rewrite the whole file! Provide only the modified section in SEARCH/REPLACE format:\n"
            "```python\n# FILE: " + (file_path or "file.py") + "\n"
            "<<<<<<< SEARCH\n(erroneous old code)\n=======\n(fixed new code)\n>>>>>>> REPLACE\n```\n"
            "🚨 REPAIR RULES:\n"
            "1. CONTRACT PRESERVATION: Never delete, empty, or hollow out existing working classes/methods. Preserve existing functionality and append or integrate missing symbols.\n"
            "2. TECH STACK DISCIPLINE: Adhere strictly to the existing libraries and tech stack. Never import unrequested external frameworks or ORMs when standard libraries are used.\n"
            "3. NO PLACEHOLDERS: Never write empty `pass` implementations, TODO comments, or functions containing only comments. Every function must have real executable statements to prevent IndentationError.\n"
            "4. 🔴 SQLALCHEMY SINGLETON RULE: NEVER create a new `db = SQLAlchemy()` inside model files (user.py, task.py, etc.)! "
            "There must be exactly ONE shared `db = SQLAlchemy()` in the project (typically in `app/db.py` or `app/__init__.py`). "
            "Model files must IMPORT db: `from app.db import db` or `from ..db import db`. "
            "Creating a new SQLAlchemy() instance in a model breaks the shared metadata registry and causes `Table already defined` or mapping errors.\n"
            "5. 🔴 FLASK APP CONTEXT: SQLAlchemy operations (`db.session`, `db.create_all()`) require an active Flask application context. "
            "If fixing test files, always ensure they call `create_app()` and push an app_context before any db operation.\n"
            "6. 🔴 FLASK BLUEPRINT CONTRACT: In Flask route modules (app/api.py, app/routes.py), ALWAYS use Blueprint (`from flask import Blueprint; api_bp = Blueprint('api', __name__)`). NEVER do `app = Flask(__name__)` inside route files — passing a Flask instance to register_blueprint() causes `AttributeError: 'Flask' object has no attribute 'register'`.\n"
            "7. 🔴 NO EXTERNAL HTTP REQUESTS IN TESTS: NEVER use `requests.get/post('http://localhost:5000')` in test files. Always use Flask test client (`client.get(...)`, `client.post(...)`).\n"
            "OUTPUT LANGUAGE: If you include explanations, write them in fluent Turkish."
        )


        for attempt in range(1, max_tries + 1):
            micro_step_id = log_store.start_step(
                run_id=run_id,
                step_number=0,
                agent_id="micro-fix",
                agent_name=f"Micro-Fix #{attempt}",
                agent_role="micro_fix",
                model=model,
                prompt_chars=len(error_log) + len(file_content),
            )

            user_prompt = (
                f"## ERROR LOG\n{error_log}\n\n"
                f"{diag_hint}"
                f"## PROBLEMATIC FILE: {file_path}\n```python\n# FILE: {file_path}\n"
                f"{file_content[:8000]}\n```\n\n"
                "Surgically fix the error in this file using the SEARCH/REPLACE format."
            )

            t0 = time.monotonic()
            try:
                raw = call_llm(
                    agent_name="micro_fix",
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    model=model,
                    max_retries=2,
                )
                elapsed = time.monotonic() - t0

                blocks = extract_code_blocks(raw)
                written = _save_code_blocks(blocks, agent_name="micro_fix")

                log_store.finish_step(
                    step_id=micro_step_id,
                    status="success" if written else "no_output",
                    response_chars=len(raw),
                    files_written=written,
                    elapsed_sec=elapsed,
                    full_output=raw,
                    project_dir=output_dir,
                )

                if written:
                    test = run_code_verification_tests(output_dir)
                    if not test.get("error"):
                        logger.info("[MICRO-FIX #%d] Hata cozuldu: %s", attempt, file_path)
                        err_id = log_store.log_error(
                            run_id=run_id,
                            step_id=step_id,
                            agent_name="Micro-Fix",
                            agent_model=model,
                            error_type="micro_fix",
                            error_msg=error_log[:500],
                            file_path=file_path,
                            retry_attempt=attempt,
                        )
                        log_store.resolve_error(err_id, resolver="micro_fix")
                        return True
                    else:
                        error_log = test["error"]
                        logger.warning("[MICRO-FIX #%d] Test gecmedi, sonraki deneme...", attempt)
                else:
                    logger.warning("[MICRO-FIX #%d] Kod blogu uretilmedi.", attempt)

            except Exception as exc:
                elapsed = time.monotonic() - t0
                log_store.finish_step(
                    micro_step_id,
                    "llm_error",
                    response_chars=0,
                    files_written=[],
                    elapsed_sec=elapsed,
                    full_output=str(exc),
                    project_dir=output_dir,
                )
                log_store.log_error(
                    run_id=run_id,
                    step_id=step_id,
                    agent_name="Micro-Fix",
                    agent_model=model,
                    error_type="llm",
                    error_msg=str(exc),
                    file_path=file_path,
                    retry_attempt=attempt,
                )
                logger.error("[MICRO-FIX #%d] LLM hatasi: %s", attempt, exc)

        return False


class EscalationEngine:
    """Geniş bağlamı ve codebase grafiğini okur, refactoring yapar, hatayı kökten düzeltir."""

    @classmethod
    def run(
        cls,
        error_log: str,
        output_dir: str,
        model: str,
        context: dict,
        run_id: str,
        step_id: str,
    ) -> tuple[str, list[str]]:
        repomap = build_repomap(output_dir)

        system_prompt = (
            "You are a senior software architect and debugging specialist.\n"
            "You will be provided with an error log or code defect.\n\n"
            "🚨 STRICT RULES:\n"
            "1. Strictly adhere to the existing project file tree and package structure.\n"
            "2. NEVER invent a different directory hierarchy (e.g. inventing 'src/' or placing files at root when packages exist).\n"
            "3. When fixing a file, provide the COMPLETE, WORKING file implementation under # FILE: path/to/file.py (or surgical SEARCH/REPLACE if the change is a single line). Writing the entire clean file is preferred to guarantee correct syntax, complete classes, and defect-free imports.\n"
            "4. Every file block MUST begin with # FILE: (or // FILE:, <!-- FILE:, etc.).\n"
            "5. CONTRACT PRESERVATION: When adding missing classes, models, or functions to an existing file, NEVER delete or overwrite existing classes/methods. Always retain all previously existing code and integrate the missing symbols.\n"
            "6. TECH STACK DISCIPLINE: Strictly respect the project's chosen technologies. NEVER introduce unrequested third-party libraries or external ORMs when the project uses standard library or built-in modules.\n"
            "7. COMPLETE IMPLEMENTATION: Every defined function or method MUST contain real executable statements and logic. NEVER leave a function body empty or containing ONLY comments (e.g. 'def delete_note(self, id): # comment' without statements), because in Python comments do not create an indented block and will instantly crash with fatal 'IndentationError: expected an indented block'! Never introduce empty stub classes (`pass`) or TODO placeholders. Implement complete, functioning code.\n"
            "8. NO BINARY OR DB FILES: Never output binary or database files (.db, .sqlite). Always create tables purely in code via `CREATE TABLE IF NOT EXISTS`.\n"
            "9. SQLITE CONCURRENCY / TESTING: When testing SQLite, do NOT use `:memory:` across independent connections because `:memory:` databases are connection-isolated and ephemeral; use a temporary file or share a single connection.\n"
            "10. EXISTING MODULE REUSE: NEVER define entity models (e.g. `class Habit`) directly inside service files when a models module already exists. Always import from wherever that model ACTUALLY lives on disk per the Codebase Graph below — this may be a single file (`from app.models import Habit`) or a package submodule (`from models.habit import Habit`); NEVER assume it is a package if the Codebase Graph shows it is a plain file, and vice versa.\n"
            "11. DIRECTORY PLURALITY RESPECT: NEVER invent plural or singular variants of existing directories (e.g. do not create `repositories/` if `repository/` exists, or `service/` if `services/` exists). Check the Codebase Graph.\n"
            "12. DATACLASS FIELD ORDERING: In `@dataclass` classes, non-default arguments (`name: str`, `description: str`) MUST ALWAYS COME BEFORE default arguments (`id: Optional[int] = None`, `completed: bool = False`). NEVER put `id: int = None` before required fields!\n"
            "13-15. 🔴 STACK-CONDITIONAL RULES (Flask/SQLAlchemy): Rules 13-15 below apply ONLY IF the PROJECT BRIEF or the Codebase Graph shows the project actually uses Flask and/or SQLAlchemy. If the project brief says standard-library-only (sqlite3, dataclasses, argparse, etc.) or names a different stack, IGNORE rules 13-15 entirely and NEVER introduce Flask, `create_app()`, Blueprints, SQLAlchemy, or any ORM as part of your fix — introducing them when they were not asked for and not already present IS ITSELF the defect (see rule 6, TECH STACK DISCIPLINE).\n"
            "13. 🔴 SQLALCHEMY SINGLETON RULE (Flask-SQLAlchemy projects only): NEVER create `db = SQLAlchemy()` inside individual model files (user.py, task.py, project.py, etc.)! "
            "There must be exactly ONE shared SQLAlchemy instance in the project, typically in `app/db.py` or `app/__init__.py`. "
            "All model files MUST import the shared db: `from app.db import db` or `from ..db import db`. "
            "Duplicating SQLAlchemy() breaks the shared metadata registry → 'Table already defined' / mapper configuration errors.\n"
            "14. 🔴 FLASK APP CONTEXT IN TESTS (Flask-SQLAlchemy projects only): Flask-SQLAlchemy operations (db.session, db.create_all, db.drop_all) require an active Flask app context. "
            "Test files using Flask+SQLAlchemy MUST: (1) import create_app, (2) call self.app = create_app({'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:'}), "
            "(3) push self.app.app_context() in setUp, (4) pop it in tearDown. NEVER call db.session without a pushed app context.\n"
            "15. 🔴 ERROR TRACEBACK LOCALIZATION (Flask/SQLAlchemy projects only): When a traceback shows flask_sqlalchemy/model.py or sqlalchemy/orm/ in the middle of the stack, "
            "the root cause is almost never in those library files — it is in the CALLER (your model class definition). "
            "Look at the last user-code frame before the library call. For non-Flask/SQLAlchemy projects, disregard this rule and look for the last user-code frame before whatever library actually appears in the traceback.\n"
            "16. 🔴 FLASK BLUEPRINT CONTRACT (Flask projects only): Route modules (`app/api.py`, `app/routes.py`, etc.) MUST define a Blueprint: `from flask import Blueprint; api_bp = Blueprint('api', __name__)` and register with `app.register_blueprint(api_bp)`. NEVER create `app = Flask(__name__)` inside route modules — passing a Flask instance to `register_blueprint()` raises `AttributeError: 'Flask' object has no attribute 'register'`.\n"
            "17. 🔴 NO EXTERNAL HTTP REQUESTS IN TESTS: Tests MUST NOT use `requests.get/post('http://localhost:5000')` because pytest runs in-process without a live web server. ALWAYS use Flask's test client (`client.get('/api/...')`, `client.post('/api/...', json={...})`).\n"
            "OUTPUT LANGUAGE: Always provide explanations and summaries to the user in fluent Turkish."
        )

        # Fix Hafızası: Önceki başarısız denemeleri prompt'a ekle
        # Bu sayede LLM aynı hatalı pattern'ı tekrar üretmez.
        failed_attempts = context.get("failed_fix_attempts", [])
        failed_attempts_block = ""
        if failed_attempts:
            attempts_summary = "\n".join([
                f"  • Deneme {i + 1}: [{a.get('strategy', '?')}] → BAŞARISIZ: {str(a.get('error', ''))[:250]}"
                for i, a in enumerate(failed_attempts[-5:])
            ])
            failed_attempts_block = (
                f"\n\n⚠️ ÖNCEKİ BAŞARISIZ ONARIM DENEMELERİ ({len(failed_attempts)} deneme) — BUNLARI TEKRAR YAPMA:\n"
                f"{attempts_summary}"
            )

        uncertainty_note = (
            "\n\n⚠️ NOTE: The automated error classifier had LOW CONFIDENCE on this error's category. "
            "Do not anchor on any single guessed category — read the raw error log and codebase graph "
            "yourself and diagnose the actual root cause from first principles."
            if context.get("laya_uncertain") else ""
        )

        user_prompt = (
            f"## ERROR / REPAIR REQUEST\n{error_log}\n\n"
            f"## CODEBASE GRAPH & REPO MAP\n{repomap[:4000]}\n\n"
            f"## PROJECT BRIEF\n{context.get('project_brief', '')[:500]}"
            f"{failed_attempts_block}"
            f"{uncertainty_note}\n\n"
            "Resolve this issue at its root cause. Apply changes using SEARCH/REPLACE blocks or complete files if creating new files."
        )

        esc_step_id = log_store.start_step(
            run_id=run_id,
            step_number=0,
            agent_id="fix_engine",
            agent_name="Fix Engine (Onarım)",
            agent_role="fix_engine",
            model=model,
            prompt_chars=len(user_prompt),
            prompt_text=user_prompt,
        )

        t0 = time.monotonic()
        try:
            raw = call_llm(
                agent_name="fix_engine",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=model,
                max_retries=3,
            )
            elapsed = time.monotonic() - t0

            blocks = extract_code_blocks(raw)
            written = _save_code_blocks(blocks, agent_name="fix_engine")

            log_store.finish_step(
                step_id=esc_step_id,
                status="success" if written else "no_output",
                response_chars=len(raw),
                files_written=written,
                elapsed_sec=elapsed,
                full_output=raw,
                project_dir=output_dir,
            )
            return raw, written

        except Exception as exc:
            elapsed = time.monotonic() - t0
            log_store.finish_step(
                esc_step_id,
                "llm_error",
                response_chars=0,
                files_written=[],
                elapsed_sec=elapsed,
                full_output=str(exc),
                project_dir=output_dir,
            )
            log_store.log_error(
                run_id=run_id,
                step_id=step_id,
                agent_name="Fix Engine",
                agent_model=model,
                error_type="llm",
                error_msg=str(exc),
            )
            return "", []


class MicroFix:
    """Fix sınıfının alt bileşeni (Sub-fixer)."""

    def __init__(self, parent_fix: "Fix", model: Optional[str] = None, max_tries: int = MICRO_FIX_MAX_TRIES):
        self.parent = parent_fix
        self._model = model
        self.max_tries = max_tries

    @property
    def model(self) -> str:
        return self._model or self.parent.model

    def run(
        self,
        error_log: str,
        file_path: str,
        output_dir: str,
        run_id: str,
        step_id: str,
        laya_diagnosis: Optional[dict] = None,
    ) -> bool:
        model_name = self.model.split("/")[-1]
        print(f"\n  ⚡ [FIX::Micro] Hata onarımı ({file_path}) -> Model: {model_name}")
        return MicroFixEngine.run(
            error_log=error_log,
            file_path=file_path,
            output_dir=output_dir,
            model=self.model,
            max_tries=self.max_tries,
            run_id=run_id,
            step_id=step_id,
            laya_diagnosis=laya_diagnosis,
        )


class Fix:
    """
    Tek Yönlü Hata Onarım ve State Machine Motoru (One-Way Escalation State Machine).
    Aşamalar: MICRO_FIX -> DEEP_REFACTOR -> LOOP_BREAKER.

    Özellikler:
      - Tek yönlü geçiş kuralı: Bir hata veya dosya için kademe yükseltildiğinde asla geri düşmez.
      - Hata İmzası Takibi (SHA-256):
          - Aynı hata 2. kez görülürse -> Otomatik DEEP_REFACTOR'a yükseltilir (Micro-Fix atlanır).
          - Aynı hata 3. kez görülürse -> LOOP_BREAKER devreye girer (Pipeline durdurulur / döngü kırılır).
      - Laya Karar Desteği: Hata teşhisi (Karar Motoru, ~30ms) ile akıllı mikro-onarım ve döngü riski puanlaması.
      - Thread-Safe: Singleton state, threading.Lock ile eşzamanlı subagent çağrılarına karşı korunur.
    """

    def __init__(self, model: Optional[str] = None, micro_model: Optional[str] = None):
        self.model = model or ESCALATION_MODEL
        self.micro = MicroFix(parent_fix=self, model=micro_model)
        self._lock = threading.Lock()
        self._error_counts: dict[str, int] = {}
        self._file_stages: dict[str, FixStage] = {}
        self._last_loop_broken = False

    @property
    def is_loop_broken(self) -> bool:
        return getattr(self, "_last_loop_broken", False)

    def reset_state(self) -> None:
        """Yeni bir pipeline veya test koşusunda durum hafızasını sıfırlar."""
        with self._lock:
            self._error_counts.clear()
            self._file_stages.clear()
            self._last_loop_broken = False

    def get_stage_for(self, target_file: str, error_signature: str) -> FixStage:
        """
        Dosya ve hata imzasına göre çalıştırılması gereken geçerli onarım aşamasını hesaplar.
        """
        with self._lock:
            count = self._error_counts.get(error_signature, 0) + 1
            self._error_counts[error_signature] = count

            prev_stage = self._file_stages.get(target_file, FixStage.MICRO_FIX)

            if count >= 3:
                calc_stage = FixStage.LOOP_BREAKER
            elif count == 2:
                calc_stage = FixStage.DEEP_REFACTOR
            else:
                calc_stage = FixStage.MICRO_FIX

            stage_order = {FixStage.MICRO_FIX: 1, FixStage.DEEP_REFACTOR: 2, FixStage.LOOP_BREAKER: 3}
            # Dosya seviyesinde tek yönlü tırmanma:
            # SADECE aynı hata veya tırmanmış dosya için geçerli, ancak LOOP_BREAKER SADECE count >= 3 durumunda tetiklenir!
            if prev_stage == FixStage.LOOP_BREAKER and count < 3:
                effective_stage = FixStage.DEEP_REFACTOR
            elif stage_order[prev_stage] > stage_order[calc_stage]:
                effective_stage = prev_stage
            else:
                effective_stage = calc_stage

            self._file_stages[target_file] = effective_stage
            return effective_stage

    def reset_on_success(self, target_file: Optional[str] = None, error_signature: Optional[str] = None) -> None:
        """
        Bir onarım başarılı olduğunda dosyanın stage cezasını ve hata sayacını temizler.
        Böylece gelecekte o dosyada çıkacak yeni/farklı hatalar yeniden MICRO_FIX'ten başlar.
        """
        with self._lock:
            if target_file and target_file in self._file_stages:
                del self._file_stages[target_file]
            if error_signature and error_signature in self._error_counts:
                del self._error_counts[error_signature]

    def repair(
        self,
        error_log: str,
        target_file: str,
        output_dir: str,
        context: dict,
        run_id: str,
        step_id: str,
    ) -> tuple[bool, list[str]]:
        sig = compute_error_signature(error_log)
        stage = self.get_stage_for(target_file, sig)

        with self._lock:
            count = self._error_counts.get(sig, 1)

        # Laya Karar Motoru Hata Analizi ve Teşhisi (~30ms)
        # Güven eşiği: Laya sınıflandırması bu eşiğin altındaysa, kategoriye göre dar bir
        # onarım stratejisi (örn. yalnızca import satırı yaması) seçmek yerine güvenli
        # varsayılana (full_file) düşülür ve belirsizlik escalation prompt'una iletilir —
        # aksi halde düşük güvenli (%29-40) yanlış teşhisler, yüksek güvenliymiş gibi aynı
        # dar stratejiyi tekrar tekrar tetikleyip döngüye sokabilir.
        LAYA_CONFIDENCE_THRESHOLD = 0.5
        laya_diagnosis = None
        repair_strategy = "full_file"
        strategy_reason = ""
        laya_low_confidence = False
        if laya_engine:
            try:
                laya_diagnosis = laya_engine.classify_error(error_log)
                cat = laya_diagnosis.get("category", "unknown")
                conf = laya_diagnosis.get("confidence", 0.0)
                ms = laya_diagnosis.get("elapsed_ms", 0.0)
                logger.info("[FIX::Laya] Hata Sınıfı: %s (Güven: %.2f, Süre: %.1fms)", cat, conf, ms)
                print(f"  ⚡ [LAYA Karar Motoru] Teşhis: {cat} (Güven: %{int(conf*100)}, {ms:.1f}ms)")

                if conf < LAYA_CONFIDENCE_THRESHOLD:
                    laya_low_confidence = True
                    logger.warning(
                        "[FIX::Laya] Güven eşiğinin altında (%.2f < %.2f) — kategoriye özel dar strateji yerine full_file'a düşülüyor.",
                        conf, LAYA_CONFIDENCE_THRESHOLD,
                    )
                    print(f"  ⚠  [LAYA Karar Motoru] Düşük güven (%{int(conf*100)}) — teşhis kategorisine güvenilmeyecek, geniş bağlamlı yaklaşıma düşülüyor.")
                elif hasattr(laya_engine, "determine_repair_strategy"):
                    strat_info = laya_engine.determine_repair_strategy(
                        error_log=error_log,
                        file_path=target_file,
                        project_dir=output_dir,
                    )
                    repair_strategy = strat_info.get("strategy", "full_file")
                    strategy_reason = strat_info.get("reason", "")
            except Exception as exc:
                logger.debug("[FIX::Laya] Analiz sırasında hata: %s", exc)

        if laya_low_confidence:
            context = dict(context)
            context["laya_uncertain"] = True

        # 1. Aşama: LOOP_BREAKER (3. tekrar veya son çare)
        if stage == FixStage.LOOP_BREAKER:
            logger.warning("[FIX::LoopBreaker] Aynı hata imzası %d. kez tekrarlandı (İmza: %s). Döngü kırıcı devrede.", count, sig[:8])
            print(f"\n  🛑 [FIX::LoopBreaker] Aynı hata ({sig[:8]}) {count}. kez tekrarlandı! Pipeline döngü kırıcıyla durduruluyor.")
            # Güvenlik ağı: Loop breaker öncesi anlık checkpoint al
            try:
                guard = GitGuard(project_dir=output_dir)
                guard.checkpoint(step_name=f"before-loop-breaker: {target_file}", agent_role="fix_engine")
            except Exception as exc:
                logger.warning("[FIX::LoopBreaker] Git checkpoint alınamadı: %s", exc)
            self._last_loop_broken = True
            return False, []
        else:
            self._last_loop_broken = False

        # 2. Aşama: MICRO_FIX
        # Laya Karar Motoru: Yalnızca noktasal sentaks ve import hatalarında cerrahi diff (MicroFix) çalıştırılır.
        # Mantık, test veya mimari sorunlarda doğrudan Bütüncül (Full-File) Onarım uygulanır.
        if stage == FixStage.MICRO_FIX and target_file and repair_strategy == "micro_fix":
            ok = self.micro.run(
                error_log=error_log,
                file_path=target_file,
                output_dir=output_dir,
                run_id=run_id,
                step_id=step_id,
                laya_diagnosis=laya_diagnosis,
            )
            if ok:
                # Başarılı onarım: stage cezasını ve hata sayacını sıfırla
                self.reset_on_success(target_file=target_file, error_signature=sig)
                return True, [target_file]

            # Micro-Fix başarısız olduysa tek yönlü olarak bir üst aşamaya (DEEP_REFACTOR) geç
            with self._lock:
                self._file_stages[target_file] = FixStage.DEEP_REFACTOR
        elif stage == FixStage.MICRO_FIX and repair_strategy == "full_file":
            print(f"  ⚡ [LAYA Karar Motoru] Strateji Tavsiyesi: Bütüncül (Full-File) yaklaşım -> {strategy_reason}")

        # 3. Aşama: DEEP_REFACTOR (Escalation)
        active_error_log = error_log
        if (count >= 1 or repair_strategy == "full_file") and laya_engine and hasattr(laya_engine, "generate_loop_breaking_intervention"):
            intervention = laya_engine.generate_loop_breaking_intervention(
                error_log=error_log,
                retry_count=count,
                project_dir=output_dir,
            )
            active_error_log = f"{intervention}\n\n{error_log}"
            print(f"  ⚡ [LAYA Karar Motoru] Strateji Tavsiyesi ({count}. deneme) -> Döngü kırıcı yönlendirme enjekte edildi.")

        print(f"\n  🚨 [FIX::DeepRefactor] Bütüncül Onarım ({count}. deneme) -> {self.model.split('/')[-1]}!")
        raw, written = EscalationEngine.run(
            error_log=active_error_log,
            output_dir=output_dir,
            model=self.model,
            context=context,
            run_id=run_id,
            step_id=step_id,
        )
        if written:
            test = run_code_verification_tests(output_dir)
            if not test.get("error"):
                # Başarılı derin onarım: stage cezasını ve hata sayacını sıfırla
                self.reset_on_success(target_file=target_file, error_signature=sig)
                return True, written
            else:
                # Başarısız — bu denemeyi context'e kaydet (fix hafızası)
                if "failed_fix_attempts" not in context:
                    context["failed_fix_attempts"] = []
                context["failed_fix_attempts"].append({
                    "strategy": "deep_refactor",
                    "error": test.get("error", "")[:300],
                    "files_written": written,
                    "attempt": count,
                })
                return False, written
        # Dosya üretilmedi — yine de kaydet
        if "failed_fix_attempts" not in context:
            context["failed_fix_attempts"] = []
        context["failed_fix_attempts"].append({
            "strategy": "deep_refactor_no_output",
            "error": error_log[:300],
            "files_written": [],
            "attempt": count,
        })
        return False, []



# Singleton Fix Nesnesi
fix_engine = Fix(model=ESCALATION_MODEL, micro_model=None)
