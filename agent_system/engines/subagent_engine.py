"""
subagent_engine.py — Dinamik Subagent ve Ajan Orkestrasyon Motoru (v3.3)

Lider koordinatör ajanın projeye özel alt uzmanları (frontend_worker, backend_worker,
db_architect, researcher, debugger) çalışma anında dinamik olarak tanımlamasını,
izole bağlamda çalıştırmasını, mesajlaşmasını ve sonuçları sentezlemesini sağlar.

Temel Yetenekler:
  - define_subagent   : İhtiyaca özel yeni uzman ajan tipi tanımlama
  - invoke_subagent   : Alt ajanı başlatma (izole context & bağımsız LLM turu)
  - send_message      : Ajanlar arası çift yönlü mesajlaşma veri yolu
  - manage_subagents  : Alt ajan durumlarını izleme, listeleme ve sonlandırma
  - SubagentOrchestrator: Dinamik görev ayrıştırma ve çoklu ajan orkestrasyonu
"""

from __future__ import annotations
import json
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

# Paket içi importlar
_HERE = Path(__file__).resolve().parent
_SYSTEM_ROOT = _HERE.parent
for _sub in [_SYSTEM_ROOT, _SYSTEM_ROOT / "core", _SYSTEM_ROOT / "engines", _SYSTEM_ROOT / "agents", _SYSTEM_ROOT / "llm", _SYSTEM_ROOT / "storage"]:
    if _sub.is_dir() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

from code_parser import UniversalCodeParser, extract_code_blocks
from diff_engine import apply_surgical_edit, has_diff_blocks
from log_store import log_store
from permission_manager import permission_manager
from reach_engine import reach_engine
from settings import settings
from test_runner import CodeVerifier
from fix_engine import fix_engine

logger = logging.getLogger("subagent_engine")


# Lider ajanın kendi uydurduğu rol isimlerini ("web_developer", "qa_tester",
# "bugfix_specialist" vb.) önceden tanımlı, uzmanlaşmış şablonlara eşleştirir.
# Eşleşme olmazsa tamamen jenerik (ve zayıf) bir tanıma düşülüyordu — bu da
# örneğin "tester" adını almayan bir test ajanının hiçbir gerçek doğrulama
# talimatı almadan çalışmasına yol açıyordu.
_ROLE_ALIASES: list[tuple[str, tuple[str, ...]]] = [
    ("tester", ("test", "qa", "kalite", "dogrula", "verify", "qc")),
    ("debugger", ("debug", "bugfix", "repair", "onar", "fix")),
    ("architect", ("architect", "mimar", "design", "tasarim")),
    ("researcher", ("research", "arastir", "analiz", "analysis")),
    ("developer", (
        "dev", "coder", "engineer", "backend", "frontend", "fullstack",
        "web", "programmer", "software", "mobile", "api", "ui",
    )),
]


def _resolve_role_alias(name: str) -> Optional[str]:
    """name lider ajan tarafından serbestçe verilmiş bir rol adıysa, en yakın
    önceden tanımlı subagent şablonunun anahtarını döndürür (yoksa None)."""
    lname = name.lower()
    for canonical, keywords in _ROLE_ALIASES:
        if any(kw in lname for kw in keywords):
            return canonical
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Veri Yapıları
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SubagentDefinition:
    """Dinamik olarak tanımlanmış bir alt ajan şablonu."""
    name: str
    description: str
    system_prompt: str
    model: Optional[str] = None
    enable_write_tools: bool = True
    enable_reach_tools: bool = True


@dataclass
class SubagentInstance:
    """Çalışan veya tamamlanmış bir alt ajan örneği."""
    conversation_id: str
    name: str
    role: str
    prompt: str
    model: str
    state: str = "running"  # "running", "idle", "completed", "errored", "waiting_for_message"
    state_detail: str = ""
    history: list[dict[str, str]] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    output_text: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    completed_at: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# SubagentManager (Yönetim & Mesajlaşma Hub'ı)
# ─────────────────────────────────────────────────────────────────────────────

class SubagentManager:
    """Tüm subagent'ların kaydını, yaşam döngüsünü ve mesajlaşmasını yöneten motor."""

    def __init__(self):
        self._definitions: dict[str, SubagentDefinition] = {}
        self._instances: dict[str, SubagentInstance] = {}
        self._register_default_definitions()

    def _register_default_definitions(self) -> None:
        """Varsayılan uzman subagent şablonlarını kaydet."""
        defaults = [
            SubagentDefinition(
                name="researcher",
                description="Kod tabanını, dökümantasyonu ve web kaynaklarını araştıran uzman.",
                system_prompt=(
                    "Sen uzman bir Yazılım ve Kod Tabanı Araştırmacısısın (Codebase & Web Researcher).\n"
                    "Görevin: Verilen konuyu, kod tabanındaki ilişkileri ve harici API dökümanlarını derinlemesine inceleyip "
                    "net, yapılandırılmış bir araştırma sentezi sunmak."
                ),
            ),
            SubagentDefinition(
                name="architect",
                description="Veri modellerini, API interface'lerini ve modül yapısını tasarlayan mimar.",
                system_prompt=(
                    "Sen kıdemli bir Yazılım Mimarısın (Software Architect).\n"
                    "Görevin: Gereksinimleri alıp temiz, minimal, modüler ve eksiksiz bir mimari tasarım ve dosya yapısı çıkarmak."
                ),
            ),
            SubagentDefinition(
                name="developer",
                description="Temiz, hatasız ve doğrudan çalışan kaynak kod üreten geliştirici.",
                system_prompt=(
                    "Sen uzman bir Yazılım Geliştiricisin (Senior Developer).\n"
                    "Görevin: Mimari plana uygun olarak tüm kaynak dosyaları eksiksiz üretmek.\n"
                    "KURAL: Her kod bloğunun birinci satırına KESİNLİKLE `# filepath: dosya.py` veya `// filepath: dosya.js` ekle."
                ),
            ),
            SubagentDefinition(
                name="debugger",
                description="Hataları inceleyen, kök sebebi tespit eden ve cerrahi yama üreten hata ayıklayıcı.",
                system_prompt=(
                    "Sen uzman bir Hata Ayıklayıcısın (Senior Debugger & Repair Specialist).\n"
                    "Görevin: Verilen hata logunu ve kaynak dosyaları analiz edip sorunu en az değişiklikle cerrahi olarak çözmek."
                ),
            ),
            SubagentDefinition(
                name="tester",
                description="Birim testleri ve doğrulama senaryoları yazan test uzmanı.",
                system_prompt=(
                    "Sen uzman bir Test Mühendisisin (QA / Test Specialist).\n"
                    "Görevin: Üretilen kodların uçtan uca doğrulanması için otomatik testler ve çalıştırma senaryoları hazırlamak."
                ),
            ),
        ]
        for d in defaults:
            self._definitions[d.name] = d

    def define_subagent(
        self,
        name: str,
        description: str,
        system_prompt: str,
        model: Optional[str] = None,
        enable_write_tools: bool = True,
        enable_reach_tools: bool = True,
    ) -> SubagentDefinition:
        """Yeni bir özel subagent şablonu tanımla."""
        clean_name = re.sub(r"[^a-zA-Z0-9_\-.]", "_", name).lower()
        defn = SubagentDefinition(
            name=clean_name,
            description=description,
            system_prompt=system_prompt,
            model=model or settings.code_model,
            enable_write_tools=enable_write_tools,
            enable_reach_tools=enable_reach_tools,
        )
        self._definitions[clean_name] = defn
        logger.info("Subagent tanimlandi: %s (%s)", clean_name, description)
        return defn

    def list_definitions(self) -> list[SubagentDefinition]:
        return list(self._definitions.values())

    def invoke_subagent(
        self,
        name: str,
        prompt: str,
        role: str = "",
        model: Optional[str] = None,
        project_dir: Optional[str] = None,
        on_token: Optional[Callable[[str, str], None]] = None,
    ) -> SubagentInstance:
        """Belirtilen subagent'ı izole bir oturum ve prompt ile çalıştır."""
        defn = self._definitions.get(name)
        if not defn:
            # Lider ajanın uydurduğu isim, bilinen bir role benziyor mu? (ör. "qa_tester" -> "tester")
            canonical = _resolve_role_alias(name)
            if canonical:
                defn = self._definitions.get(canonical)
                logger.info("Subagent rol eslesmesi: '%s' -> '%s' sablonu kullanilacak.", name, canonical)
        if not defn:
            # Hiçbir bilinen role benzemiyorsa: son çare olarak jenerik dinamik tanım oluştur
            defn = self.define_subagent(
                name=name,
                description=f"Dinamik oluşturulan {name} uzmanı",
                system_prompt=f"Sen {role or name} konusunda uzmanlaşmış bir yapay zeka ajanısın.",
                model=model,
            )

        conv_id = f"subagent-{uuid.uuid4().hex[:8]}"
        target_model = model or defn.model or settings.code_model
        role_label = role or defn.description

        instance = SubagentInstance(
            conversation_id=conv_id,
            name=name,
            role=role_label,
            prompt=prompt,
            model=target_model,
            state="running",
            state_detail=f"Çalışıyor: {prompt[:60]}...",
        )
        self._instances[conv_id] = instance

        # LLM Çağrısı
        from llm_client import call_llm

        sys_prompt = (
            f"{defn.system_prompt}\n\n"
            f"ÇALIŞMA DİZİNİ: {project_dir or os.getcwd()}\n\n"
            "🔴 EN KRİTİK KURAL — HER KOD BLOĞUNUN İLK SATIRINA DOSYA YOLU YORUMU EKLE:\n"
            "  HTML         : <!-- filepath: index.html -->\n"
            "  CSS          : /* filepath: styles.css */\n"
            "  JS/TS        : // filepath: dosya.js\n"
            "  JSON/YAML    : // filepath: manifest.json\n"
            "  Python/Shell : # filepath: script.py\n"
            "Bu yorum KOD BLOĞUNUN İÇİNDE, BİRİNCİ SATIRDA olmalıdır.\n"
            "Bu yorum OLMADAN dosya diske kaydedilmez ve görevin başarısız sayılır!\n"
            "Lütfen gereksiz sohbet/açıklama yapma; mimarideki tüm dosyaları eksiksiz kod blokları olarak üret."
        )

        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": prompt},
        ]
        instance.history = list(messages)

        try:
            raw_response = call_llm(
                agent_name=name,
                system_prompt=sys_prompt,
                user_prompt=prompt,
                model=target_model,
            )
            instance.output_text = raw_response
            instance.history.append({"role": "assistant", "content": raw_response})
            instance.state = "completed"
            instance.state_detail = "Görev tamamlandı"
            instance.completed_at = datetime.now().isoformat()

            # Kod bloklarını ayrıştır ve yaz (eğer izinliyse)
            if defn.enable_write_tools and project_dir:
                blocks = extract_code_blocks(raw_response)
                written = self._write_blocks_to_disk(blocks, project_dir)

                # Eğer kodlayıcı ajan dosya üretemediyse (filepath eksikse) otomatik tek tık kurtarma
                if not written and len(raw_response) > 500 and defn.enable_write_tools:
                    logger.warning("[%s] Dosya ayrıştırılamadı, filepath hatırlatma turu yapılıyor...", name)
                    retry_prompt = (
                        f"{prompt}\n\n"
                        "🔴 KRİTİK HATA VE ZORUNLU KURAL:\n"
                        "Yukarıdaki görev için ürettiğin kod bloklarının İLK satırında `<!-- filepath: index.html -->`, "
                        "`/* filepath: styles.css */` veya `// filepath: dosya.js` gibi dosya yolu yorumu bulunmadığı için "
                        "sistem dosyaları kaydedemedi!\n"
                        "Lütfen yukarıda istenen TÜM dosyaları, her kod bloğunun BİRİNCİ satırına "
                        "ilgili filepath yorumunu ekleyerek EKSİKSİZ VE ÇALIŞIR ŞEKİLDE YENİDEN YAZ."
                    )
                    raw_retry = call_llm(
                        agent_name=f"{name}-fix",
                        system_prompt=sys_prompt,
                        user_prompt=retry_prompt,
                        model=target_model,
                    )
                    retry_blocks = extract_code_blocks(raw_retry)
                    written = self._write_blocks_to_disk(retry_blocks, project_dir)
                    if written:
                        instance.output_text += f"\n\n{raw_retry}"
                        instance.history.append({"role": "assistant", "content": raw_retry})

                instance.files_written = written

        except Exception as exc:
            instance.state = "errored"
            instance.state_detail = str(exc)
            logger.error("Subagent [%s] hatasi: %s", name, exc)
            print(f"  [!!] Subagent [{name}] hatasi: {exc}")

        return instance

    def send_message(
        self,
        conversation_id: str,
        message: str,
        project_dir: Optional[str] = None,
        on_token: Optional[Callable[[str, str], None]] = None,
    ) -> str:
        """Mevcut bir subagent örneğine yeni talimat gönder ve devam ettir."""
        instance = self._instances.get(conversation_id)
        if not instance:
            raise ValueError(f"Subagent bulunamadı: {conversation_id}")

        from llm_client import call_llm
        from context_budgeter import context_budgeter

        instance.history.append({"role": "user", "content": message})
        instance.state = "running"
        instance.state_detail = f"Yeni mesaj işleniyor: {message[:50]}..."

        sys_prompt = instance.history[0]["content"] if instance.history else "Sen uzman bir yapay zeka ajanısın."
        # Subagent hafızası context limitine yaklaştığında öngörülü özetle
        if context_budgeter.should_summarize(sys_prompt, instance.history, instance.model, threshold=0.75):
            instance.history = context_budgeter.summarize_history(instance.history, keep_recent=4)

        try:
            raw_response = call_llm(
                agent_name=instance.name,
                system_prompt=sys_prompt,
                user_prompt=message,
                model=instance.model,
            )
            instance.history.append({"role": "assistant", "content": raw_response})
            instance.output_text = raw_response
            instance.state = "completed"
            instance.state_detail = "Yanıt verildi"

            # Kod bloklarını yaz
            if project_dir:
                blocks = extract_code_blocks(raw_response)
                written = self._write_blocks_to_disk(blocks, project_dir)
                instance.files_written.extend(written)

            return raw_response
        except Exception as exc:
            instance.state = "errored"
            instance.state_detail = str(exc)
            raise exc

    def manage_subagents(self, action: str = "list", conversation_id: Optional[str] = None) -> Any:
        """Subagent havuzunu yönet."""
        if action == "list":
            return [
                {
                    "conversation_id": inst.conversation_id,
                    "name": inst.name,
                    "role": inst.role,
                    "model": inst.model,
                    "state": inst.state,
                    "state_detail": inst.state_detail,
                    "files_count": len(inst.files_written),
                    "created_at": inst.created_at,
                }
                for inst in self._instances.values()
            ]
        elif action == "kill" and conversation_id:
            if conversation_id in self._instances:
                self._instances[conversation_id].state = "completed"
                self._instances[conversation_id].state_detail = "Kullanıcı tarafından durduruldu"
                return True
            return False
        elif action == "kill_all":
            for inst in self._instances.values():
                inst.state = "completed"
                inst.state_detail = "Tüm ajanlar durduruldu"
            return True
        return None

    def _write_blocks_to_disk(self, blocks: list[dict], project_dir: str) -> list[str]:
        """Ayrıştırılan kod bloklarını proje klasörüne güvenli kaydet."""
        written = []
        p_dir = Path(project_dir).resolve()
        p_dir.mkdir(parents=True, exist_ok=True)

        for block in blocks:
            filename = block.get("filename")
            content = block.get("content", "")
            if not filename or not content.strip():
                continue

            target_file = (p_dir / filename).resolve()
            # Dizin dışına çıkış engeli (path traversal protection)
            try:
                target_file.relative_to(p_dir)
            except ValueError:
                logger.warning("Güvenlik engeli: Dosya proje dizini dışında: %s", filename)
                continue

            target_file.parent.mkdir(parents=True, exist_ok=True)

            # Cerrahi Diff veya Tam Dosya
            if target_file.exists() and has_diff_blocks(content):
                orig_text = target_file.read_text(encoding="utf-8", errors="replace")
                new_text, ok, msg = apply_surgical_edit(orig_text, content)
                if ok:
                    target_file.write_text(new_text, encoding="utf-8")
                    written.append(filename)
                else:
                    target_file.write_text(content, encoding="utf-8")
                    written.append(filename)
            else:
                target_file.write_text(content, encoding="utf-8")
                written.append(filename)

        return list(set(written))


# ─────────────────────────────────────────────────────────────────────────────
# SubagentOrchestrator (Lider Ajan & Dinamik Görev Dağıtıcı)
# ─────────────────────────────────────────────────────────────────────────────

class SubagentOrchestrator:
    """
    Kullanıcı projesini dinamik alt görevlere bölen, uzman subagent'ları
    sırayla ve birbirleriyle haberleştirerek çalıştıran lider orkestratör.
    """

    def __init__(self, manager: Optional[SubagentManager] = None):
        self.manager = manager or SubagentManager()

    def _verify_and_fix(
        self,
        project_dir: str,
        project_brief: str,
        run_id: str,
        step_number_base: int,
        max_attempts: int = 3,
    ) -> tuple[bool, int, str, list[str]]:
        """Üretilen kodu fiilen çalıştırır (syntax + bağımlılık kurulumu + çalıştırma/pytest);
        hata bulursa fix_engine (Micro-Fix -> Deep-Refactor -> Loop-Breaker) ile onarmayı dener.
        Bu adım, adım-adım bir subagent görevi kod ürettiği HER SEFERİNDE çağrılır — böylece
        bir sonraki ajan, henüz çalışmayan/bozuk bir çıktının üzerine inşa etmek zorunda kalmaz.

        Dönüş: (basarili_mi, deneme_sayisi, kalan_hata_metni, fix_engine_tarafindan_yazilan_dosyalar)
        """
        fix_attempts = 0
        newly_written: list[str] = []
        verify_result = CodeVerifier.run_tests(project_dir)

        while verify_result.get("error") and fix_attempts < max_attempts:
            fix_attempts += 1
            err_msg = verify_result["error"]
            target_fix_file = verify_result.get("file") or ""

            step_id = log_store.start_step(
                run_id=run_id, step_number=step_number_base * 100 + fix_attempts,
                agent_id="debugger", agent_name=f"Debugger #{fix_attempts}",
                agent_role="debugger", model=settings.code_model,
                prompt_chars=len(err_msg), project_dir=project_dir,
            )
            err_id = log_store.log_error(
                run_id=run_id, step_id=step_id,
                agent_name="Test & Onarim", agent_model=settings.code_model,
                error_type=verify_result.get("error_type") or "runtime",
                error_msg=err_msg, file_path=target_fix_file, project_dir=project_dir,
            )
            fixed, fix_written = fix_engine.repair(
                error_log=err_msg,
                target_file=target_fix_file,
                output_dir=project_dir,
                context={"project_brief": project_brief},
                run_id=run_id,
                step_id=step_id,
            )
            log_store.finish_step(
                step_id=step_id, status="success" if fixed else "failed",
                files_written=fix_written, project_dir=project_dir,
            )
            if fix_written:
                newly_written.extend(fix_written)

            if fixed:
                log_store.resolve_error(err_id, resolver="fix_engine")
                verify_result = CodeVerifier.run_tests(project_dir)
            else:
                break

        return (not verify_result.get("error")), fix_attempts, verify_result.get("error", ""), newly_written

    def run(
        self,
        project_brief: str,
        project_dir: str,
        progress_callback: Optional[Callable[[int, int, Any, str, dict], None]] = None,
    ) -> dict[str, Any]:
        """Dinamik Subagent modunda projeyi inşa et."""
        start_time = time.time()
        p_path = Path(project_dir).resolve()
        p_path.mkdir(parents=True, exist_ok=True)

        all_files_written: list[str] = []
        task_log: list[dict] = []
        total_fix_attempts = 0
        any_unresolved_error = ""

        # ── 1. Planlama & Görev Ayrıştırma ─────────────────────────────────
        if progress_callback:
            progress_callback(1, 4, None, "start", {"agent_name": "Lider Ajan", "role": "Görev Ayrıştırma"})

        leader_prompt = (
            f"PROJE İSTEĞİ VE TEKNİK ŞARTLAR:\n{project_brief}\n\n"
            "GÖREV:\n"
            "Sen uzman bir Proje Mimarı ve Lider Orkestratörsün. Bu projeyi EN ÇEVİK, EN DOĞRU ve EKSİKSİZ şekilde inşa etmek için "
            "tam olarak İHTİYAÇ KADAR alt uzman ajan belirle.\n\n"
            "STRATEJİ VE PRENSİPLER:\n"
            "1. Gereksiz yapay adımlar ve şişirme roller YARATMA. İhtiyaç ne kadarsa o kadar alt ajan belirle "
            "(Basit/orta işlerde 1 veya 2 uzman, karmaşık/çok katmanlı sistemlerde gerektiği kadar uzman).\n"
            "2. Proje saf Vanilla JS/HTML/CSS gibi bir arayüz veya frontend işi ise: Doğrudan tüm dosyaları eksiksiz yazacak "
            "bir `frontend_developer` veya `web_engineer` görevlendir. İstenmeyen backend, npm veya TypeScript ekleme.\n"
            "3. Her ajanın görev tanımında, üreteceği dosyaları (örn: `index.html`, `styles.css`, `app.js`, `storage.js`, `notify.js`) açıkça belirt.\n\n"
            "Çıktını SADECE aşağıdaki JSON formatında ver:\n"
            "```json\n"
            "[\n"
            "  {\"name\": \"web_developer\", \"role\": \"Web Arayüz & Çekirdek Kod Uzmanı\", \"task\": \"index.html, styles.css, app.js, storage.js ve notify.js dosyalarını eksiksiz kod blokları olarak üret.\"},\n"
            "  {\"name\": \"qa_tester\", \"role\": \"Test ve Doğrulama Uzmanı\", \"task\": \"Kullanım ve doğrulama senaryolarını tamamla.\"}\n"
            "]\n"
            "```"
        )

        from llm_client import call_llm
        raw_plan = ""
        try:
            raw_plan = call_llm(
                agent_name="orchestrator",
                system_prompt="Sen uzman bir yazılım proje mimarı ve orkestratörüsün. Görevin projeyi tam ihtiyaç kadar temiz alt uzmanlıklara bölmektir.",
                user_prompt=leader_prompt,
                model=settings.planning_model,
            )
            m_json = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw_plan, re.DOTALL)
            plan_json_str = m_json.group(1) if m_json else raw_plan
            tasks = json.loads(plan_json_str)
        except Exception as exc:
            logger.warning("Lider plan ayrıştırma hatası, yalın görev zincirine geçiliyor: %s", exc)
            tasks = [
                {"name": "developer", "role": "Yazılım Geliştirici", "task": f"Proje isteklerindeki tüm kaynak kod dosyalarını eksiksiz üret:\n{project_brief}"},
                {"name": "tester", "role": "Doğrulama Uzmanı", "task": "Kullanım ve test doğrulamasını tamamla."},
            ]

        if progress_callback:
            progress_callback(1, len(tasks) + 2, None, "completed", {"written": [], "raw_len": len(raw_plan)})

        # Tüm adımlar boyunca kullanılacak tek bir log_store "run" kaydı
        verify_run_id = log_store.start_run(
            session_id=f"subagent-{uuid.uuid4().hex[:6]}",
            project_name=p_path.name,
            brief=project_brief,
            project_dir=str(p_path),
        )

        # ── 2. Görevleri Subagent'lara Dağıt, HER ADIMDAN SONRA Çalıştır/Onar ──
        total_steps = len(tasks) + 2
        accumulated_context = f"=== PROJE İSTEĞİ ===\n{project_brief}\n\n"

        for idx, task_info in enumerate(tasks, 2):
            sub_name = task_info.get("name", "developer")
            sub_role = task_info.get("role", "Uzman")
            sub_task = task_info.get("task", "")

            if progress_callback:
                progress_callback(
                    idx, total_steps, None, "start",
                    {"agent_name": f"{sub_name.upper()}", "role": sub_role, "task": sub_task}
                )

            current_prompt = (
                f"{accumulated_context}\n"
                f"=== SENİN ÖZEL GÖREVİN ({sub_role}) ===\n"
                f"{sub_task}\n\n"
                "Lütfen görevini eksiksiz yerine getir ve gereken tüm dosyaları kod blokları içinde `# filepath: ...` veya `// filepath: ...` ile üret."
            )

            instance = self.manager.invoke_subagent(
                name=sub_name,
                prompt=current_prompt,
                role=sub_role,
                project_dir=project_dir,
            )

            all_files_written.extend(instance.files_written)
            accumulated_context += f"\n=== [{sub_name.upper()} ÇIKTISI] ===\n{instance.output_text[:2000]}\n"

            # ── Bu adım kod ürettiyse: HEMEN fiilen çalıştır ve gerekirse onar ──
            # (bir sonraki subagent, önceki adımın bozuk çıktısının üzerine inşa etmesin diye)
            step_test_ok: Optional[bool] = None
            step_fix_attempts = 0
            step_remaining_error = ""
            if instance.files_written:
                if progress_callback:
                    progress_callback(idx, total_steps, None, "testing", {"agent_name": sub_name, "role": "Fiziksel Doğrulama"})

                step_test_ok, step_fix_attempts, step_remaining_error, fix_written = self._verify_and_fix(
                    project_dir=project_dir,
                    project_brief=project_brief,
                    run_id=verify_run_id,
                    step_number_base=idx,
                )
                if fix_written:
                    all_files_written.extend(fix_written)
                    instance.files_written = list(set(instance.files_written) | set(fix_written))
                total_fix_attempts += step_fix_attempts
                if not step_test_ok:
                    any_unresolved_error = step_remaining_error

            task_log.append({
                "step": idx,
                "subagent": sub_name,
                "role": sub_role,
                "task": sub_task,
                "files_written": instance.files_written,
                "state": instance.state,
                "test_ok": step_test_ok,
                "fix_attempts": step_fix_attempts,
            })

            if progress_callback:
                progress_callback(
                    idx, total_steps, None, "completed",
                    {
                        "written": instance.files_written, "state": instance.state, "raw_len": len(instance.output_text),
                        "test_ok": step_test_ok, "fix_attempts": step_fix_attempts,
                    }
                )

        # ── 2.5 Son Bütünleşme Kontrolü ─────────────────────────────────────
        # Adım adım testler her dosyayı tek tek doğrular; ama örn. frontend'in
        # backend'e bağlandığı bir adım henüz backend yazılmadan test edilmiş
        # olabilir. Tüm adımlar bittikten sonra tek bir bütünleşik (integration)
        # kontrolüyle bunu da yakalıyoruz.
        if progress_callback:
            progress_callback(total_steps, total_steps, None, "start", {"agent_name": "Test & Onarım", "role": "Bütünleşik Son Kontrol"})

        final_test_ok, final_fix_attempts, final_remaining_error, final_fix_written = self._verify_and_fix(
            project_dir=project_dir,
            project_brief=project_brief,
            run_id=verify_run_id,
            step_number_base=99,
        )
        if final_fix_written:
            all_files_written.extend(final_fix_written)
        total_fix_attempts += final_fix_attempts
        test_ok = final_test_ok
        if not test_ok:
            any_unresolved_error = final_remaining_error

        log_store.finish_run(
            run_id=verify_run_id,
            status="success" if test_ok else "failed",
            total_agents=len(tasks),
            files_written=len(set(all_files_written)),
            project_dir=str(p_path),
        )

        if progress_callback:
            progress_callback(
                total_steps, total_steps, None, "completed",
                {"test_ok": test_ok, "fix_attempts": total_fix_attempts, "remaining_error": any_unresolved_error[:200]},
            )

        # ── 3. Final Sentez & Dokümantasyon ────────────────────────────────
        if progress_callback:
            progress_callback(total_steps, total_steps, None, "start", {"agent_name": "Lider Sentezleyici", "role": "Finalleştirme"})

        elapsed = time.time() - start_time
        unique_files = sorted(list(set(all_files_written)))

        # Canlı AUDIT_LOG.md yazımı
        audit_file = p_path / "AUDIT_LOG.md"
        audit_content = (
            f"# 🤖 Subagent Orkestrasyon Raporu\n\n"
            f"- **Tarih:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"- **Mod:** Dinamik Subagent & Swarm Orkestrasyonu\n"
            f"- **Toplam Süre:** {elapsed:.1f} saniye\n"
            f"- **Üretilen Dosyalar ({len(unique_files)}):** {', '.join(unique_files)}\n\n"
            f"## 🧪 Fiziksel Test & Onarım Sonucu (Her Adımdan Sonra + Bütünleşik Son Kontrol)\n\n"
            f"- **Durum:** {'✅ Başarılı — kod çalıştırılıp doğrulandı' if test_ok else '⚠️ Çözülemeyen hata kaldı (elle kontrol gerekebilir)'}\n"
            f"- **Toplam Otomatik Onarım Denemesi:** {total_fix_attempts}\n"
            + (f"- **Kalan Hata:**\n```\n{any_unresolved_error[:1000]}\n```\n" if not test_ok else "")
            + "\n"
            f"## 📋 Ajan Görev Dağılımı ve Çıktılar\n\n"
        )
        for t in task_log:
            audit_content += f"### Adım {t['step']}: {t['subagent']} ({t['role']})\n"
            audit_content += f"- **Görev:** {t['task']}\n"
            audit_content += f"- **Üretilen Dosyalar:** {', '.join(t['files_written']) or 'Yok (Rapor)'}\n"
            audit_content += f"- **Durum:** {t['state']}\n"
            if t.get("test_ok") is not None:
                test_label = "✅ Çalıştırıldı, hata yok" if t["test_ok"] else f"⚠️ Onarılamadı ({t.get('fix_attempts', 0)} deneme sonrası)"
                audit_content += f"- **Fiziksel Test:** {test_label}\n"
            audit_content += "\n"

        audit_file.write_text(audit_content, encoding="utf-8")
        if "AUDIT_LOG.md" not in unique_files:
            unique_files.append("AUDIT_LOG.md")

        if progress_callback:
            progress_callback(total_steps, total_steps, None, "completed", {"written": ["AUDIT_LOG.md"], "elapsed_sec": round(elapsed, 1)})

        return {
            "success": True,
            "mode": "subagent",
            "files_written": unique_files,
            "tasks": task_log,
            "elapsed": elapsed,
            "test_ok": test_ok,
            "fix_attempts": total_fix_attempts,
            "remaining_error": any_unresolved_error,
        }


# Modül seviyesinde singleton
subagent_manager = SubagentManager()
subagent_orchestrator = SubagentOrchestrator(subagent_manager)
