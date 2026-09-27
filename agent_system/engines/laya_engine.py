"""
laya_engine.py — Laya System 1 Karar ve Refleks Motoru

Convai Innovations'ın açık kaynaklı non-autoregressive 'Laya' karar modeli
üzerine inşa edilmiştir.

Temel Özellikler:
  - Generatif LLM (token token metin üretimi) DEĞİLDİR; tek bir forward-pass ile
    karar, skor ve boolean (True/False) olasılıkları döndürür (~33ms gecikme).
  - CPU Üzerinde Eşzamanlı Çalışma: GPU VRAM'ini ana üretken kodlama modellerine
    (Qwen, DeepSeek vb.) bırakır; CPU çekirdeklerinde bağımsız olarak çalışır.
  - Graceful Degradation (Zarif Fallback): Laya kütüphanesi veya ağırlıkları
    yüklü değilse akıllı kural tabanlı (heuristic) analiz ile kesintisiz devam eder.
"""

from __future__ import annotations
import logging
import re
import time
import threading
from typing import List, Optional, Tuple, Dict, Any
from pathlib import Path

logger = logging.getLogger("laya_engine")


class LayaDecisionEngine:
    """
    Laya System 1 Karar Motoru Singleton Sınıfı.
    Pipeline yönlendirme, test/hata sınıflandırma ve döngü tespiti kararlarını yönetir.
    """

    _instance: Optional["LayaDecisionEngine"] = None
    _lock: threading.Lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        model_name: str = "convaiinnovations/laya",
        device: str = "cpu",
        enabled: bool = True,
    ):
        if getattr(self, "_initialized", False):
            return

        self.model_name = model_name
        self.device = device
        self.enabled = enabled
        self._router = None
        self._is_ready = False
        self._init_attempted = False
        self._init_lock = threading.Lock()
        self._initialized = True

    def _ensure_loaded(self) -> bool:
        """Laya modelini güvenli ve tembel (lazy) şekilde yükler."""
        if not self.enabled:
            return False
        if self._is_ready:
            return True
        if self._init_attempted:
            return False

        with self._init_lock:
            if self._is_ready or self._init_attempted:
                return self._is_ready
            self._init_attempted = True

            try:
                t0 = time.monotonic()
                # laya paketini dinamik import et
                import importlib
                from pathlib import Path
                laya_mod = importlib.import_module("laya")
                RouterCls = getattr(laya_mod, "Router", None)
                if RouterCls:
                    # Yerel model yollarını öncelikle kontrol et
                    candidate_paths = [
                        Path(__file__).resolve().parent.parent.parent / "llama_server" / "models" / "laya",
                        Path("/home/seyfettin/Desktop/Projects/private/llama_server/models/laya"),
                    ]
                    for cand in candidate_paths:
                        if (cand / "rl_agent_config.json").exists() and (cand / "model.safetensors").exists():
                            self.model_name = str(cand)
                            break

                    models_dict = {"english": (self.model_name, None)} if self.model_name else None
                    self._router = RouterCls(models=models_dict, device=self.device)
                    self._is_ready = True
                    load_ms = (time.monotonic() - t0) * 1000
                    logger.info("[LAYA] Model başarıyla yüklendi (%s, device=%s) [%.1f ms]", self.model_name, self.device, load_ms)
                    print(f"  ⚡ [LAYA] System 1 Karar Motoru aktif (Device: {self.device.upper()}, ~30ms).")
                    return True
            except ImportError:
                logger.info("[LAYA] 'laya' paketi sistemde bulunamadı. Heuristic fallback devrede.")
            except Exception as exc:
                logger.warning("[LAYA] Laya modeli yüklenirken hata oluştu: %s. Heuristic fallback devrede.", exc)

            return False

    @property
    def is_available(self) -> bool:
        """Gerçek Laya modelinin aktif olup olmadığını döner."""
        return self._ensure_loaded()

    # ─────────────────────────────────────────────────────────────
    # Temel Primitifler (Choice, Score, Noul/Boolean)
    # ─────────────────────────────────────────────────────────────

    def classify(
        self,
        context: str,
        choices: List[str],
        instruction: str = "Select the best matching category",
    ) -> Tuple[str, float, float]:
        """
        Girdi metnini (state) verilen seçenekler arasından birine sınıflandırır.
        Dönüş: (seçilen_kategori, güven_skoru, geçen_süre_ms)
        """
        t0 = time.monotonic()
        if not choices:
            return "", 0.0, 0.0

        if self._ensure_loaded() and self._router:
            try:
                questions = {
                    "selection": {
                        "type": "choice",
                        "instructions": instruction,
                        "criteria": choices,
                    }
                }
                res = self._router.predict(context, questions)
                answers = res.get("answers", {})
                sel_ans = answers.get("selection", res.get("selection", {}))
                selected = sel_ans.get("choice", sel_ans.get("answer", choices[0]))
                confidence = float(sel_ans.get("answer_confidence", sel_ans.get("confidence", 0.95)))
                ms = (time.monotonic() - t0) * 1000
                return selected, confidence, ms
            except Exception as exc:
                logger.warning("[LAYA] classify sırasında hata: %s", exc)

        # Fallback: Kural tabanlı eşleşme
        ms = (time.monotonic() - t0) * 1000
        ctx_lower = context.lower()
        for choice in choices:
            if choice.lower() in ctx_lower:
                return choice, 0.85, ms

        return choices[0], 0.50, ms

    def score(
        self,
        context: str,
        instruction: str = "Rate from 1 to 10",
        scale: Tuple[int, int] = (1, 10),
    ) -> Tuple[float, float]:
        """
        Girdi metnini verilen ölçekte skorlar.
        Dönüş: (skor, geçen_süre_ms)
        """
        t0 = time.monotonic()
        min_v, max_v = scale

        if self._ensure_loaded() and self._router:
            try:
                questions = {
                    "rating": {
                        "type": "score",
                        "instructions": instruction,
                        "criteria": [str(i) for i in range(min_v, max_v + 1)],
                    }
                }
                res = self._router.predict(context, questions)
                answers = res.get("answers", {})
                rat_ans = answers.get("rating", res.get("rating", {}))
                sc = float(rat_ans.get("score", rat_ans.get("answer", (min_v + max_v) / 2)))
                ms = (time.monotonic() - t0) * 1000
                return sc, ms
            except Exception as exc:
                logger.warning("[LAYA] score sırasında hata: %s", exc)

        # Fallback: Metin uzunluğu ve anahtar kelime heuristiği
        ms = (time.monotonic() - t0) * 1000
        length_penalty = min(len(context) / 2000, 1.0)
        has_error = 1.0 if "error" in context.lower() or "exception" in context.lower() else 0.2
        heuristic_score = min_v + (max_v - min_v) * (length_penalty * 0.5 + has_error * 0.5)
        return round(heuristic_score, 2), ms

    def decide_boolean(
        self,
        context: str,
        question: str,
    ) -> Tuple[bool, float, float]:
        """
        Verilen ifadenin True/False olma olasılığını (Noul) hesaplar.
        Dönüş: (karar, güven_skoru, geçen_süre_ms)
        """
        t0 = time.monotonic()

        if self._ensure_loaded() and self._router:
            try:
                questions = {
                    "decision": {
                        "type": "noul",
                        "instructions": question,
                    }
                }
                res = self._router.predict(context, questions)
                answers = res.get("answers", {})
                dec_ans = answers.get("decision", res.get("decision", {}))
                prob = float(dec_ans.get("noul", dec_ans.get("probability", 0.5)))
                is_true = prob >= 0.5
                ms = (time.monotonic() - t0) * 1000
                return is_true, prob, ms
            except Exception as exc:
                logger.warning("[LAYA] decide_boolean sırasında hata: %s", exc)

        # Fallback: Heuristic
        ms = (time.monotonic() - t0) * 1000
        q_lower = question.lower()
        ctx_lower = context.lower()

        # Döngü veya hata benzerliği kontrolü
        if "loop" in q_lower or "stuck" in q_lower:
            repeat_words = len(re.findall(r"(again|repeat|failed|same)", ctx_lower))
            prob = min(0.3 + (repeat_words * 0.2), 0.95)
            return (prob >= 0.6), prob, ms

        return False, 0.50, ms

    # ─────────────────────────────────────────────────────────────
    # Özelleşmiş Domain Fonksiyonları (Hata Analizi, Döngü Kırma)
    # ─────────────────────────────────────────────────────────────

    def classify_error(self, error_log: str) -> Dict[str, Any]:
        """
        Hata logunu ~30ms içinde cerrahi olarak analiz eder:
        Kategoriler:
          - syntax_error: Yazım/girinti hataları (Micro-Fix için mükemmel)
          - missing_dependency: ModuleNotFoundError/ImportError
          - logic_bug: Assert/AssertionError veya iş mantığı sapması
          - environment: Dosya yolu bulunamadı, izin hatası
          - other: Bilinmeyen genel hata
        """
        choices = [
            "syntax_error",
            "missing_dependency",
            "logic_bug",
            "environment_failure",
            "other",
        ]
        instruction = "Classify this software test/runtime error into the most accurate technical category."
        category, confidence, elapsed_ms = self.classify(error_log[:2000], choices, instruction)

        # Heuristic ve Güven Kalibrasyonu:
        # Traceback içindeki açık sentaks, eksik paket veya assertion belirteçlerini kontrol et
        err_lower = error_log.lower()
        has_explicit_import_error = any(term in err_lower for term in ("modulenotfounderror", "importerror", "cannot find module", "no module named"))
        has_explicit_syntax_error = any(term in err_lower for term in ("syntaxerror", "indentationerror", "taberror"))
        has_explicit_assert_error = any(term in err_lower for term in ("assertionerror", "assert ", "failed assert"))
        has_explicit_env_error = any(term in err_lower for term in ("filenotfounderror", "permissionerror", "connectionrefused"))

        # Eğer model yüklenmemişse VEYA modelin güveni düşükse (< 0.60) VEYA açık belirteç sınıflandırmayla çelişiyorsa:
        if (not self._is_ready) or (confidence < 0.60) or (has_explicit_import_error and category != "missing_dependency") or (has_explicit_syntax_error and category != "syntax_error"):
            if has_explicit_syntax_error:
                category = "syntax_error"
                confidence = max(confidence, 0.99)
            elif has_explicit_import_error:
                category = "missing_dependency"
                confidence = max(confidence, 0.98)
            elif has_explicit_assert_error:
                category = "logic_bug"
                confidence = max(confidence, 0.95)
            elif has_explicit_env_error:
                category = "environment_failure"
                confidence = max(confidence, 0.95)

        # Önerilen onarım stratejisi
        suggested_action = "micro_fix"
        if category in ("missing_dependency", "environment_failure"):
            suggested_action = "package_or_env_fix"
        elif category == "logic_bug":
            suggested_action = "targeted_refactor"

        return {
            "category": category,
            "confidence": confidence,
            "suggested_action": suggested_action,
            "elapsed_ms": round(elapsed_ms, 2),
            "engine": "laya" if self._is_ready else "heuristic_fallback",
        }

    def evaluate_loop_risk(
        self,
        current_error: str,
        recent_signatures: List[str],
        current_signature: str,
    ) -> Tuple[bool, float]:
        """
        Ajanın bir kısır döngüde (infinite loop / circular fix) olup olmadığını değerlendirir.
        Dönüş: (is_loop_detected, confidence)
        """
        # Son 3 imza arasında mevcut imza kaç kez var?
        same_count = recent_signatures.count(current_signature)
        if same_count >= 2:
            # Kesin kural: Aynı imza 3. kez geliyorsa %100 döngü
            return True, 1.0

        # Laya Noul kararı ile anlamsal sıkışma kontrolü
        context = f"Current error:\n{current_error[:1000]}\nRecent error signatures: {recent_signatures[-3:]}"
        question = "Is the agent stuck repeating the same failing cycle or fix attempt?"
        is_loop, conf, _ = self.decide_boolean(context, question)

        return is_loop, conf

    def evaluate_qa_report(self, report_text: str) -> Dict[str, Any]:
        """
        QA Test Raporunu analiz eder:
        - '## STATUS: PASSED' varsa ve fatal traceback yoksa doğrudan True döner.
        - '## STATUS: FAILED' varsa False döner.
        - Standart kütüphane halüsinasyonlarını (time, sqlite3, argparse vb.) filtreler.
        - Laya Noul boolean çıkarımı ile gerçek bir çökme/kod arızası olup olmadığını 30ms'de belirler.
        """
        t0 = time.monotonic()
        if not report_text:
            return {"passed": True, "reason": "empty_report", "elapsed_ms": 0.0}

        report_upper = report_text.upper()

        # 1. Açık STATUS bildirimleri
        if "## STATUS: PASSED" in report_upper or "STATUS: PASSED" in report_upper:
            has_fatal_traceback = bool(re.search(r"Traceback \(most recent call last\):", report_text, re.I))
            if not has_fatal_traceback:
                elapsed_ms = (time.monotonic() - t0) * 1000
                return {
                    "passed": True,
                    "reason": "explicit_status_passed",
                    "elapsed_ms": round(elapsed_ms, 2),
                    "engine": "laya_status_evaluator",
                }

        if "## STATUS: FAILED" in report_upper:
            elapsed_ms = (time.monotonic() - t0) * 1000
            return {
                "passed": False,
                "reason": "explicit_status_failed",
                "elapsed_ms": round(elapsed_ms, 2),
                "engine": "laya_status_evaluator",
            }

        # 2. Standart Python kütüphanesi yanlış alarm filtresi (built-in modüller)
        builtins = {"time", "sqlite3", "argparse", "sys", "os", "json", "math", "re", "datetime", "pathlib", "typing", "dataclasses"}
        report_lower = report_text.lower()
        has_real_bug = False
        for line in report_text.splitlines():
            line_l = line.lower()
            if any(term in line_l for term in ("syntaxerror", "assertionerror", "typeerror", "nameerror", "indexerror")):
                has_real_bug = True
                break
            if "import" in line_l and "error" in line_l:
                tokens = set(re.findall(r"\b[a-zA-Z0-9_]+\b", line_l))
                complained_builtins = tokens & builtins
                if not complained_builtins:
                    has_real_bug = True
                    break

        if not has_real_bug and ("passed" in report_lower or "başarılı" in report_lower):
            elapsed_ms = (time.monotonic() - t0) * 1000
            return {
                "passed": True,
                "reason": "builtins_warning_only",
                "elapsed_ms": round(elapsed_ms, 2),
                "engine": "laya_status_evaluator",
            }

        # 3. Laya Noul çıkarımı
        is_failing, prob, elapsed_ms = self.decide_boolean(
            report_text[:2000],
            "Does this QA report confirm that the application code has critical unhandled bugs or failed tests?",
        )
        return {
            "passed": not is_failing,
            "reason": f"laya_noul_decision (fail_prob={prob:.2f})",
            "elapsed_ms": round(elapsed_ms, 2),
            "engine": "laya" if self._is_ready else "heuristic_fallback",
        }

    def extract_pinpoint_diagnostic(
        self,
        error_log: str,
        project_dir: str = "",
        history_note: str = "",
    ) -> Dict[str, Any]:
        """
        Traceback'ten hata yapılan tam dosya ve satır numarasını ayrıştırır.
        Varsa diske gidip o satırın 3 satır öncesini ve sonrasını çeker.
        Nokta atışı cerrahi teşhis kartı oluşturur.
        """
        from pathlib import Path
        file_path = ""
        line_num = 0
        error_msg = ""
        failing_line = ""

        # 1. Traceback içindeki tüm Python ve Pytest çağrı çerçevelerini topla:
        #    Python: File "...", line 123
        #    Pytest: path/to/file.py:123: in func veya path/to/file.py:123:
        all_py = re.findall(r'File\s+[\'"]([^\'"]+)[\'"],\s+line\s+(\d+)', error_log)
        all_pytest = re.findall(r'(?:^|\s|\b)([a-zA-Z0-9_\-\./\\]+\.py):(\d+):', error_log)
        all_matches = all_py + all_pytest

        # Çağrı yığınını en içten (en alttaki hatayı asıl üreten çerçeveden) yukarı doğru tara:
        for cand_f, cand_l in reversed(all_matches):
            cand_clean = cand_f.replace("\\", "/").strip()
            if any(sys_dir in cand_clean for sys_dir in ("/usr/", "site-packages", ".venv", "lib/python")):
                continue
            file_path = cand_clean
            try:
                line_num = int(cand_l)
            except Exception:
                line_num = 0
            if project_dir and isinstance(project_dir, (str, Path)):
                p = Path(project_dir)
                if (p / cand_clean).exists() or list(p.rglob(Path(cand_clean).name)):
                    break
            else:
                break

        if not file_path:
            m_collect = re.search(r'ERROR collecting\s+([a-zA-Z0-9_\-\./\\]+\.py)', error_log)
            if m_collect:
                file_path = m_collect.group(1).replace("\\", "/")

        # Hata mesajı satırı (Traceback sonundaki TypeError, ImportError, SyntaxError vs.)
        err_lines = [l.strip() for l in error_log.splitlines() if l.strip()]
        for l in reversed(err_lines):
            if any(l.startswith(p) for p in ("E   ", "Error:", "Exception:")) or re.match(r"^[A-Z][a-zA-Z0-9_]*Error:", l):
                error_msg = l.lstrip("E ").strip()
                break
        if not error_msg and err_lines:
            error_msg = err_lines[-1]

        # Diskteki dosya bağlamını veya hafızadaki kod sözlüğünü oku (Context Window)
        surrounding_code = []
        if file_path and project_dir:
            lines = []
            if isinstance(project_dir, dict):
                for k in project_dir:
                    if k == file_path or k.endswith("/" + file_path) or file_path.endswith("/" + k) or Path(k).name == Path(file_path).name:
                        lines = str(project_dir[k]).splitlines()
                        break
            elif isinstance(project_dir, (str, Path)):
                full_p = Path(project_dir) / file_path
                if not full_p.exists():
                    matches = list(Path(project_dir).rglob(Path(file_path).name))
                    if matches:
                        full_p = matches[0]

                if full_p.exists():
                    try:
                        lines = full_p.read_text(encoding="utf-8", errors="ignore").splitlines()
                    except Exception:
                        lines = []

            if lines and line_num > 0 and line_num <= len(lines):
                failing_line = lines[line_num - 1]
                start = max(0, line_num - 4)
                end = min(len(lines), line_num + 3)
                for idx in range(start, end):
                    prefix = " ➔ " if idx == (line_num - 1) else "    "
                    surrounding_code.append(f"{prefix}Satır {idx+1:02d}: {lines[idx]}")

        # Laya hata analizi
        diag = self.classify_error(error_log)
        cat = diag.get("category", "unknown")

        # Formatlı Teşhis Kartı
        code_ctx_str = "\n".join(surrounding_code) if surrounding_code else (failing_line or "(Kod satırı doğrudan okunamadı)")
        card = (
            f"=== 🎯 LAYA SYSTEM 1 NOKTA ATIŞI HATA TEŞHİSİ ===\n"
            f"📍 Hata Dosyası : {file_path or 'Bilinmiyor'} (Satır: {line_num if line_num else 'N/A'})\n"
            f"🏷 Hata Sınıfı  : {cat.upper()} (Teşhis: {diag.get('suggested_action', 'repair')})\n"
            f"❌ Hata Mesajı  : {error_msg}\n"
            f"🔍 Hatalı Kod Çevresi:\n{code_ctx_str}\n"
        )
        if history_note:
            card += f"⏳ Döngü Geçmişi: {history_note}\n"
        card += "📌 TALİMAT: Sadece bu dosyadaki hatayı SEARCH/REPLACE ile cerrahi olarak düzeltin. Çalışan diğer dosyalara dokunmayın!"

        return {
            "file": file_path,
            "line": line_num,
            "error_msg": error_msg,
            "category": cat,
            "diagnostic_card": card,
        }

    def determine_repair_strategy(
        self,
        error_log: str,
        file_path: str = "",
        project_dir: str = "",
    ) -> Dict[str, Any]:
        """
        Laya System 1 Karar Motoru:
        Hatanın yapısına ve dosyanın boyutuna göre en uygun onarım stratejisini seçer:
        1. 'full_file' : Dosyayı baştan sona tam ve tutarlı olarak üretir (SEARCH/REPLACE diff yapmaz).
        2. 'micro_fix' : Hata noktasal bir sentaks/typo ise cerrahi diff yapar.
        """
        err_lower = error_log.lower()

        # 1. Kural: Bütüncül sınıf/modül yeniden yazımı gerektiren yapısal hatalar -> FULL-FILE
        # @dataclass alan sıralaması: diff yapılırsa diğer alanlar (örn. frequency) unutuluyor
        if "non-default argument" in err_lower and "follows default argument" in err_lower:
            return {
                "strategy": "full_file",
                "reason": "dataclass_argument_order (Bütüncül sınıf yeniden yazımı gerekli)",
            }

        # Module / Import hataları: Modülün import hiyerarşisi yeniden kurulmalı
        if "modulenotfounderror" in err_lower or "importerror" in err_lower:
            return {
                "strategy": "full_file",
                "reason": "import_hierarchy (İçe aktarım yapısı yeniden yapılandırması)",
            }

        # Metod imza veya argüman uyumsuzluğu
        if "unexpected keyword argument" in err_lower or "missing required positional argument" in err_lower:
            return {
                "strategy": "full_file",
                "reason": "signature_contract (Metod/Sınıf argüman sözleşmesi)",
            }

        # 2. Kural: Noktasal sentaks/typo hataları -> MICRO_FIX
        if "syntaxerror" in err_lower or "nameerror" in err_lower or "indentationerror" in err_lower:
            return {
                "strategy": "micro_fix",
                "reason": "localized_syntax_or_name (Noktasal cerrahi düzeltme)",
            }

        # 3. Kural: Dosya boyutu kontrolü
        line_count = 0
        if file_path and project_dir and isinstance(project_dir, (str, Path)):
            full_p = Path(project_dir) / file_path
            if not full_p.exists():
                matches = list(Path(project_dir).rglob(Path(file_path).name))
                if matches:
                    full_p = matches[0]
            if full_p.exists():
                try:
                    content = full_p.read_text(encoding="utf-8", errors="ignore")
                    line_count = len(content.splitlines())
                except Exception:
                    line_count = 0

        if 0 < line_count < 80:
            return {
                "strategy": "full_file",
                "reason": f"small_file ({line_count} satır - tam dosya üretimi daha güvenli)",
            }

        return {
            "strategy": "micro_fix",
            "reason": "default_micro_fix",
        }

    def generate_loop_breaking_intervention(
        self,
        error_log: str,
        retry_count: int,
        project_dir: str = "",
    ) -> str:
        """
        Aynı hatanın tekrarlandığı durumlarda (kısırdöngü riski),
        Laya System 1 analizi ile LLM'e doğrudan, net ve somut bir strateji müdahale kartı üretir.
        """
        pinpoint = self.extract_pinpoint_diagnostic(error_log, project_dir=project_dir)
        target_file = pinpoint.get("file", "")
        line_num = pinpoint.get("line", 0)
        error_msg = pinpoint.get("error_msg", "")

        err_lower = error_log.lower()
        if "non-default argument" in err_lower and "follows default argument" in err_lower:
            specific_action = (
                f"🚨 PYTHON @DATACLASS ALAN SIRALAMA HATASI:\n"
                f"- Hatalı Dosya : `{target_file}` (Satır ~{line_num})\n"
                f"- HATA DETAYI  : Varsayılan değerli alanlar (örn: `id: Optional[int] = None`), varsayılanı olmayan alanlardan (örn: `name: str`) ÖNCE GELEMEZ!\n"
                f"- ÇÖZÜM TALİMATI: `{target_file}` dosyasında zorunlu alanları en üste alın, varsayılan değerli alanları en alta koyun:\n\n"
                f"```python\n"
                f"@dataclass\n"
                f"class Entity:\n"
                f"    name: str                  # 1. Zorunlu alanlar en başta\n"
                f"    description: str\n"
                f"    id: Optional[int] = None   # 2. Varsayılan değerli alanlar en sonda\n"
                f"```\n"
                f"🔴 DİKKAT: Test dosyalarını değiştirmeyin! Hatayı doğrudan `{target_file}` içinde düzeltin!"
            )
        elif "modulenotfounderror" in err_lower or "importerror" in err_lower:
            specific_action = (
                f"🚨 MODÜL / IMPORT HATASI:\n"
                f"- Hatalı Dosya : `{target_file}`\n"
                f"- HATA DETAYI  : Var olmayan veya yanlış bir içe aktarım (import) yolu kullanılıyor.\n"
                f"- ÇÖZÜM TALİMATI: Proje dizininde fiziksel olarak bulunmayan hiçbir hayali paket adı (automlx, myapp vb.) uydurmayın. Doğrudan klasör adıyla (`from habit_tracker.models import ...`) veya göreceli import (`from .models import ...`) kullanın."
            )
        elif "unexpected keyword argument" in err_lower or "missing required positional argument" in err_lower:
            specific_action = (
                f"🚨 FONKSİYON / METOD İMZA UYUMSUZLUĞU:\n"
                f"- Hatalı Dosya : `{target_file}`\n"
                f"- HATA DETAYI  : Çağrılan metodun parametreleri ile sınıf/fonksiyon tanımı uyuşmuyor.\n"
                f"- ÇÖZÜM TALİMATI: Çağıran kod ile tanımlanan sınıfın argüman listelerini tam olarak eşleştirin."
            )
        else:
            specific_action = (
                f"🚨 TEKRARLAYAN HATAYI KÖKTEN DÜZELTME TALİMATI:\n"
                f"- Hatalı Dosya : `{target_file}` (Satır: {line_num})\n"
                f"- Hata Mesajı  : {error_msg}\n"
                f"- ÇÖZÜM TALİMATI: Önceki denemelerdeki başarısız yaklaşımı bırakın. Hedef dosyayı sıfırdan inceleyip kök nedeni cerrahi olarak giderin."
            )

        card = (
            f"╔══════════════════════════════════════════════════════════════════════════════════════╗\n"
            f"║ ⚡ LAYA SYSTEM 1 STRATEJİ MÜDAHALESİ — DÖNGÜ KIRICI (Deneme {retry_count})                ║\n"
            f"╚══════════════════════════════════════════════════════════════════════════════════════╝\n"
            f"⚠️ DİKKAT: Aynı hata ardı ardına tekrarlandı! Önceki yaklaşım kısırdöngü üretiyor.\n"
            f"Aşağıdaki somut çözümü uygulayarak döngüyü kırın:\n\n"
            f"{specific_action}\n"
        )
        return card

    def create_focused_retry_context(
        self,
        error_log: str,
        full_code_files: str,
        project_dir: str = "",
        retry_count: int = 1,
    ) -> str:
        """
        18.000 karakterlik devasa kod torbasını budar:
        Sadece hatanın geçtiği dosyayı ve test dosyasını tutar.
        Gereksiz 14 çalışan dosyayı atarak context'i %80 küçültür (~3.000 karakter).
        """
        pinpoint = self.extract_pinpoint_diagnostic(
            error_log=error_log,
            project_dir=project_dir,
            history_note=f"Bu hata döngüde {retry_count}. kez tekrarlanıyor.",
        )
        target_file = pinpoint.get("file", "")

        # Kod dosyalarından sadece hedef dosya ve test bloğunu ayıkla
        focused_code_blocks = []
        try:
            from engines.code_parser import CodeParser
            parsed = CodeParser.extract_code_blocks(full_code_files)
            if parsed:
                for blk in parsed:
                    p = blk.path.lower()
                    fname = Path(target_file).name.lower() if target_file else ""
                    if fname and (fname in p or p.endswith(fname)):
                        focused_code_blocks.append(f"# filepath: {blk.path}\n{blk.content}")
                    elif "test" in p:
                        focused_code_blocks.append(f"# filepath: {blk.path}\n{blk.content[:1500]}")
        except Exception:
            pass

        if not focused_code_blocks:
            blocks = re.split(r"(#\s*filepath:\s*[^\n]+|//\s*filepath:\s*[^\n]+|```[a-z]*:[^\n]+|---\s*File:\s*[^\n]+)", full_code_files)
            if target_file and len(blocks) > 1:
                for i in range(1, len(blocks), 2):
                    header = blocks[i]
                    body = blocks[i+1] if i+1 < len(blocks) else ""
                    combined = f"{header}\n{body}"
                    fname = Path(target_file).name.lower()
                    if fname in header.lower() or "test" in header.lower():
                        focused_code_blocks.append(combined[:3000])

        pruned_code = "\n\n".join(focused_code_blocks) if focused_code_blocks else full_code_files[:4000]

        intervention = ""
        if retry_count >= 2:
            intervention = self.generate_loop_breaking_intervention(
                error_log=error_log,
                retry_count=retry_count,
                project_dir=project_dir,
            ) + "\n\n"

        return (
            f"{intervention}"
            f"{pinpoint['diagnostic_card']}\n\n"
            f"=== ODAKLANMIŞ KOD BAĞLAMI (Düzeltilecek Dosyalar) ===\n"
            f"{pruned_code}\n"
        )


# Global Singleton
laya_engine = LayaDecisionEngine()


def get_laya_engine() -> LayaDecisionEngine:
    return laya_engine
