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
from test_runner import CodeVerifier
from git_guard import GitGuard

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

        system_prompt = (
            "You are an expert code repair specialist.\n"
            "You will be given an error log and the problematic file.\n"
            "Fix the error SURGICALLY with minimal changes.\n"
            "Do NOT rewrite the whole file! Provide only the modified section in SEARCH/REPLACE format:\n"
            "```python\n# FILE: " + (file_path or "file.py") + "\n"
            "<<<<<<< SEARCH\n(erroneous old code)\n=======\n(fixed new code)\n>>>>>>> REPLACE\n```\n"
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
                    test = CodeVerifier.run_tests(output_dir)
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
            "3. When fixing existing files, prefer SEARCH/REPLACE blocks.\n"
            "4. Every file block MUST begin with # FILE: (or // FILE:, <!-- FILE:, etc.).\n"
            "OUTPUT LANGUAGE: Always provide explanations and summaries to the user in fluent Turkish."
        )

        esc_step_id = log_store.start_step(
            run_id=run_id,
            step_number=0,
            agent_id="fix_engine",
            agent_name="Fix Engine (Onarım)",
            agent_role="fix_engine",
            model=model,
            prompt_chars=len(error_log) + len(repomap),
        )

        user_prompt = (
            f"## ERROR / REPAIR REQUEST\n{error_log}\n\n"
            f"## CODEBASE GRAPH & REPO MAP\n{repomap[:4000]}\n\n"
            f"## PROJECT BRIEF\n{context.get('project_brief', '')[:500]}\n\n"
            "Resolve this issue at its root cause. Apply changes using SEARCH/REPLACE blocks or complete files if creating new files."
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
      - Thread-Safe: Singleton state, threading.Lock ile eşzamanlı subagent çağrılarına karşı korunur.
    """

    def __init__(self, model: Optional[str] = None, micro_model: Optional[str] = None):
        self.model = model or ESCALATION_MODEL
        self.micro = MicroFix(parent_fix=self, model=micro_model)
        self._lock = threading.Lock()
        self._error_counts: dict[str, int] = {}
        self._file_stages: dict[str, FixStage] = {}

    def reset_state(self) -> None:
        """Yeni bir pipeline veya test koşusunda durum hafızasını sıfırlar."""
        with self._lock:
            self._error_counts.clear()
            self._file_stages.clear()

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
            if stage_order[prev_stage] > stage_order[calc_stage]:
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
            return False, []

        # 2. Aşama: MICRO_FIX (1. deneme ve dosya daha önce escalate edilmemişse)
        if stage == FixStage.MICRO_FIX and target_file:
            ok = self.micro.run(
                error_log=error_log,
                file_path=target_file,
                output_dir=output_dir,
                run_id=run_id,
                step_id=step_id,
            )
            if ok:
                # Başarılı onarım: stage cezasını ve hata sayacını sıfırla
                self.reset_on_success(target_file=target_file, error_signature=sig)
                return True, [target_file]

            # Micro-Fix başarısız olduysa tek yönlü olarak bir üst aşamaya (DEEP_REFACTOR) geç
            with self._lock:
                self._file_stages[target_file] = FixStage.DEEP_REFACTOR

        # 3. Aşama: DEEP_REFACTOR (Escalation)
        print(f"\n  🚨 [FIX::DeepRefactor] Micro-Fix yetersiz kaldı / Hata tekrarı ({count}. deneme) -> Derin Yeniden Yapılandırma ({self.model.split('/')[-1]})!")
        raw, written = EscalationEngine.run(
            error_log=error_log,
            output_dir=output_dir,
            model=self.model,
            context=context,
            run_id=run_id,
            step_id=step_id,
        )
        if written:
            test = CodeVerifier.run_tests(output_dir)
            if not test.get("error"):
                # Başarılı derin onarım: stage cezasını ve hata sayacını sıfırla
                self.reset_on_success(target_file=target_file, error_signature=sig)
                return True, written
            else:
                return False, written
        return False, []


# Singleton Fix Nesnesi
fix_engine = Fix(model=ESCALATION_MODEL, micro_model=None)
