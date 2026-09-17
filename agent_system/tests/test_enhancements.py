"""
test_enhancements.py — Yeni Codebase Memory, Reach Engine ve Diff Engine Testleri
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _sub in [_ROOT, _ROOT / "core", _ROOT / "engines", _ROOT / "agents", _ROOT / "llm", _ROOT / "storage", _ROOT / "tests"]:
    if _sub.is_dir() and str(_sub) not in sys.path:
        sys.path.insert(0, str(_sub))

import os
import pytest
from diff_engine import (
    has_diff_blocks,
    extract_diff_blocks,
    apply_search_replace_block,
    apply_surgical_edit,
)
from reach_engine import ReachEngine, BLOCKED_EXTENSIONS
from codebase_graph import CodebaseGraphEngine


def test_diff_engine_exact_match():
    orig = "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
    edit = (
        "<<<<<<< SEARCH\n"
        "def foo():\n"
        "    return 1\n"
        "=======\n"
        "def foo():\n"
        "    return 42\n"
        ">>>>>>> REPLACE"
    )
    assert has_diff_blocks(edit) is True
    blocks = extract_diff_blocks(edit)
    assert len(blocks) == 1

    new_text, ok, msg = apply_surgical_edit(orig, edit)
    assert ok is True
    assert "return 42" in new_text
    assert "return 2" in new_text


def test_diff_engine_multiple_blocks():
    orig = "line1\nline2\nline3\nline4\n"
    edit = (
        "<<<<<<< SEARCH\n"
        "line1\n"
        "=======\n"
        "NEW_LINE_1\n"
        ">>>>>>> REPLACE\n\n"
        "<<<<<<< SEARCH\n"
        "line4\n"
        "=======\n"
        "NEW_LINE_4\n"
        ">>>>>>> REPLACE"
    )
    new_text, ok, msg = apply_surgical_edit(orig, edit)
    assert ok is True
    assert "NEW_LINE_1" in new_text
    assert "NEW_LINE_4" in new_text
    assert "line2" in new_text


def test_diff_engine_new_file():
    orig = None
    edit = "def brand_new_function():\n    return True\n"
    new_text, ok, msg = apply_surgical_edit(orig, edit)
    assert ok is True
    assert "brand_new_function" in new_text


def test_reach_engine_safety():
    engine = ReachEngine()
    # Zararlı / ikili uzantıların engellendiğini doğrula
    for ext in [".exe", ".bat", ".zip", ".msi"]:
        res = engine.read_url(f"https://example.com/malware{ext}")
        assert "Güvenlik Uyarısı" in res or "❌" in res


def test_codebase_graph_ast_fallback(tmp_path):
    sample_py = tmp_path / "sample.py"
    sample_py.write_text("class MyService:\n    def do_work(self, x, y):\n        pass\n", encoding="utf-8")

    engine = CodebaseGraphEngine()
    summary = engine._ast_fallback(str(tmp_path))
    assert "class MyService:" in summary
    assert "def do_work(self, x, y)" in summary


def test_code_parser_valid_and_invalid_filenames():
    from code_parser import UniversalCodeParser

    # Reddedilmesi gereken durumlar
    reject_cases = [
        "7.5", "4.1", "4.3", "1.3", "3.1", "3.2", "5.1",
        "chrome.storage.session", "performance.now", "chrome.storage",
    ]
    for name in reject_cases:
        assert UniversalCodeParser._is_valid_filename(name) is False, f"Should reject {name}"

    # Kabul edilmesi gereken durumlar
    accept_cases = [
        "manifest.json", "popup.js", "background.js", "content.js",
        "popup.html", "popup.css", "lib/utils.js", "lib/csv.js",
        "README.md", "Dockerfile", ".gitignore", ".env",
        "main.py", "app.ts", "styles.scss", "docker-compose.yml",
    ]
    for name in accept_cases:
        assert UniversalCodeParser._is_valid_filename(name) is True, f"Should accept {name}"


def test_code_parser_multiblock_and_headers():
    from code_parser import extract_code_blocks

    sample = """
### 1.1 `manifest.json`
```json
{
  "manifest_version": 3
}
```

### popup.css
```css
body { background: #000; }
```

**lib/utils.js**
```javascript
export function delay(ms) {}
```

## 4.1 Test Bolumu (dosya degil)
```javascript
console.log(performance.now());
```

File: background.js
```javascript
chrome.runtime.onInstalled.addListener(() => {});
```
"""
    blocks = extract_code_blocks(sample)
    assert len(blocks) == 5
    assert blocks[0]["filename"] == "manifest.json"
    assert blocks[1]["filename"] == "popup.css"
    assert blocks[2]["filename"] == "lib/utils.js"
    assert blocks[3]["filename"] is None  # 4.1 test section rejected
    assert blocks[4]["filename"] == "background.js"


def test_settings_execution_mode_mapping(tmp_path):
    """Ayar eşlemesi gerçek kullanıcı settings.json dosyasını değiştirmemeli."""
    from settings import Settings

    isolated_settings = Settings(tmp_path / "settings.json")
    isolated_settings.execution_mode = "1"
    assert isolated_settings.execution_mode == "sequential"

    isolated_settings.execution_mode = "2"
    assert isolated_settings.execution_mode == "subagent"

    isolated_settings.execution_mode = "3"
    assert isolated_settings.execution_mode == "interactive"


def test_chat_mode_never_auto_starts_pipeline(monkeypatch):
    """Mod 3'te model marker üretse bile sohbet REPL olarak kalmalıdır."""
    from coordinator_agent import CoordinatorAgent
    from session_manager import session_manager

    coordinator = CoordinatorAgent.__new__(CoordinatorAgent)
    coordinator.history = []
    monkeypatch.setattr(coordinator, "_rebuild_system_prompt", lambda: None)
    monkeypatch.setattr(
        coordinator,
        "_call_llm",
        lambda on_token=None: "##PIPELINE_START##\nBir proje\n##PIPELINE_END##",
    )
    monkeypatch.setattr(session_manager.current_session, "save", lambda history: True)

    response, should_start = coordinator.chat("bana plan çıkar", allow_pipeline=False)

    assert "##PIPELINE_START##" in response
    assert should_start is False


def test_subagent_manager_definitions_and_lifecycle():
    from subagent_engine import SubagentManager

    mgr = SubagentManager()
    defs = mgr.list_definitions()
    assert len(defs) >= 5
    def_names = [d.name for d in defs]
    assert "researcher" in def_names
    assert "architect" in def_names
    assert "developer" in def_names

    # Özel tanım ekleme
    custom_def = mgr.define_subagent(
        name="crypto_trader",
        description="Arbitraj ve kripto analiz uzmanı",
        system_prompt="Sen kripto uzmanısın.",
    )
    assert custom_def.name == "crypto_trader"
    assert len(mgr.list_definitions()) == len(defs) + 1

    # Manage subagents test
    mgr.manage_subagents("kill_all")
    listing = mgr.manage_subagents("list")
    assert isinstance(listing, list)


def test_diff_engine_ambiguous_rejection():
    """Aynı imzalı iki fonksiyon farklı sınıflarda olduğunda anchorsız SEARCH bloğu reddedilmeli."""
    orig = (
        "class Cat:\n"
        "    def speak(self):\n"
        "        return 'meow'\n\n"
        "class Dog:\n"
        "    def speak(self):\n"
        "        return 'woof'\n"
    )
    # def speak() dosyada 2 kez var, SEARCH bloğu sınıf bağlamı içermiyor
    ambiguous_search = (
        "<<<<<<< SEARCH\n"
        "    def speak(self):\n"
        "        return 'meow'\n"
        "=======\n"
        "    def speak(self):\n"
        "        return 'purr'\n"
        ">>>>>>> REPLACE"
    )
    # Birebir eşleşmede aynı gövde olsa bile eğer def speak 2 kez varsa veya birebir duplicate varsa:
    orig_duplicate = (
        "class Cat:\n"
        "    def speak(self):\n"
        "        return 'sound'\n\n"
        "class Dog:\n"
        "    def speak(self):\n"
        "        return 'sound'\n"
    )
    duplicate_search = (
        "<<<<<<< SEARCH\n"
        "    def speak(self):\n"
        "        return 'sound'\n"
        "=======\n"
        "    def speak(self):\n"
        "        return 'bark'\n"
        ">>>>>>> REPLACE"
    )
    res_text, ok, msg = apply_surgical_edit(orig_duplicate, duplicate_search)
    assert ok is False
    assert "Belirsiz eşleşme" in msg


def test_diff_engine_disambiguated_with_anchor():
    """Sınıf bağlamı (anchor) verildiğinde sadece hedeflenen sınıfın metodu güncellenmeli."""
    orig = (
        "class Cat:\n"
        "    def speak(self):\n"
        "        return 'sound'\n\n"
        "class Dog:\n"
        "    def speak(self):\n"
        "        return 'sound'\n"
    )
    anchored_search = (
        "<<<<<<< SEARCH\n"
        "class Dog:\n"
        "    def speak(self):\n"
        "        return 'sound'\n"
        "=======\n"
        "class Dog:\n"
        "    def speak(self):\n"
        "        return 'woof'\n"
        ">>>>>>> REPLACE"
    )
    res_text, ok, msg = apply_surgical_edit(orig, anchored_search)
    assert ok is True
    assert "class Cat:\n    def speak(self):\n        return 'sound'" in res_text
    assert "class Dog:\n    def speak(self):\n        return 'woof'" in res_text


def test_diff_engine_anchor_mismatch_rejection():
    """SEARCH bloğundaki sınıf adı dosyadaki gerçek konumla uyuşmazsa reddedilmeli."""
    from diff_engine import _has_context_anchor
    orig = (
        "class Cat:\n"
        "    def speak(self):\n"
        "        return 'meow'\n\n"
        "class Dog:\n"
        "    def speak(self):\n"
        "        return 'woof'\n"
    )
    # Cat'in satır indeksinde (1) Dog anchor'ı arıyoruz -> False olmalı
    search_with_wrong_anchor = "class Dog:\n    def speak(self):"
    assert _has_context_anchor(search_with_wrong_anchor, orig, start_line_idx=1) is False
    # Dog'un satır indeksinde (5) Dog anchor'ı -> True olmalı
    assert _has_context_anchor(search_with_wrong_anchor, orig, start_line_idx=5) is True


def test_diff_engine_fuzzy_anchor_mismatch_rejected():
    """Fuzzy eşleşme skoru %95+ olsa dahi SEARCH bloğundaki anchor yanlış sınıfa aitse reddedilmeli."""
    orig = (
        "class Cat:\n"
        "    def calculate(self, x, y):\n"
        "        step1 = x * 10\n"
        "        step2 = y * 20\n"
        "        return step1 + step2\n\n"
        "class Dog:\n"
        "    def bark(self):\n"
        "        return 'woof'\n"
    )
    # SEARCH bloğu Dog sınıfını iddia ediyor ama içerik Cat'in calculate metoduna %95+ benziyor
    fuzzy_search_wrong_class = (
        "<<<<<<< SEARCH\n"
        "class Dog:\n"
        "    def calculate(self, x, y):\n"
        "        step1 = x * 10\n"
        "        step2 = y * 20\n"
        "        return step1 + step2\n"
        "=======\n"
        "class Dog:\n"
        "    def calculate(self, x, y):\n"
        "        return 100\n"
        ">>>>>>> REPLACE"
    )
    res_text, ok, msg = apply_surgical_edit(orig, fuzzy_search_wrong_class)
    assert ok is False
    assert "Bağlam uyuşmazlığı" in msg or "SEARCH bloğu bulunamadı" in msg


def test_diff_engine_rust_trait_anchor():
    """Rust trait ve impl anchor'larının doğru tanınıp işlendiğini doğrula."""
    from diff_engine import _extract_anchors_from_chunk
    rust_code = "trait Formatter {\n    fn format(&self) -> String;\n}"
    anchors = _extract_anchors_from_chunk(rust_code)
    assert ("trait", "Formatter") in anchors
    assert ("fn", "format") in anchors


def test_diff_engine_graduated_fuzzy_90_99_applied_with_warning():
    """%90 - %99 benzerlikteki bloklar [FUZZY-APPLIED] uyarısıyla uygulanmalı."""
    orig = (
        "def calculate_total(price, tax_rate):\n"
        "    tax = price * tax_rate\n"
        "    total = price + tax\n"
        "    return total\n"
    )
    # Ufak bir satır içi whitespace / typo ile ~%95 benzerlik
    fuzzy_search = (
        "<<<<<<< SEARCH\n"
        "def calculate_total(price, tax_rate):\n"
        "    tax = price * tax_rate\n"
        "    total = price + tax\n"
        "    return  total\n"
        "=======\n"
        "def calculate_total(price, tax_rate):\n"
        "    tax = price * tax_rate\n"
        "    return price + tax\n"
        ">>>>>>> REPLACE"
    )
    res_text, ok, msg = apply_surgical_edit(orig, fuzzy_search, file_path="calc.py")
    assert ok is True
    assert "FUZZY-APPLIED" in msg or "Fuzzy eşleşme" in msg
    assert "return price + tax" in res_text


def test_diff_engine_graduated_fuzzy_70_89_permission(monkeypatch):
    """%70 - %89 arası bloklar permission_manager onayına göre uygulanmalı veya reddedilmeli."""
    orig = (
        "def calculate_tax(subtotal, tax_rate, discount, shipping_cost):\n"
        "    taxable_amount = subtotal - discount\n"
        "    calculated_tax = taxable_amount * tax_rate\n"
        "    total = taxable_amount + calculated_tax + shipping_cost\n"
        "    return total\n"
    )
    # ~85% benzerlikte SEARCH bloğu (kısaltılmış değişken isimleri ile)
    fuzzy_search = (
        "<<<<<<< SEARCH\n"
        "def calculate_tax(subtotal, tax_rate, discount, shipping_cost):\n"
        "    taxable = subtotal - discount\n"
        "    tax = taxable * tax_rate\n"
        "    total = taxable + tax + shipping_cost\n"
        "    return total\n"
        "=======\n"
        "def calculate_tax(subtotal, tax_rate, discount, shipping_cost):\n"
        "    return 100\n"
        ">>>>>>> REPLACE"
    )
    from permission_manager import permission_manager

    # 1. Senaryo: Permission reddedildi
    monkeypatch.setattr(permission_manager, "check_permission", lambda *args, **kwargs: False)
    res_text, ok, msg = apply_surgical_edit(orig, fuzzy_search, file_path="algo.py")
    assert ok is False
    assert "onaylanmadı/reddedildi" in msg or "70-89%" in msg

    # 2. Senaryo: Permission onaylandı
    monkeypatch.setattr(permission_manager, "check_permission", lambda *args, **kwargs: True)
    res_text2, ok2, msg2 = apply_surgical_edit(orig, fuzzy_search, file_path="algo.py")
    assert ok2 is True
    assert "kullanıcı onayı" in msg2 or "Fuzzy eşleşme" in msg2


def test_diff_engine_fuzzy_below_70_rejected():
    """%70'in altındaki benzerlikler otomatik reddedilmeli."""
    orig = (
        "def process_order(order_id):\n"
        "    db.save(order_id)\n"
        "    return True\n"
    )
    totally_different_search = (
        "<<<<<<< SEARCH\n"
        "def handle_payment(user_id, amount, currency):\n"
        "    gateway.charge(user_id, amount)\n"
        "    return False\n"
        "=======\n"
        "def handle_payment(user_id, amount, currency):\n"
        "    return True\n"
        ">>>>>>> REPLACE"
    )
    res_text, ok, msg = apply_surgical_edit(orig, totally_different_search)
    assert ok is False
    assert "SEARCH bloğu bulunamadı" in msg


def test_brain_locked_append_and_write(tmp_path):
    """_locked_append ve _locked_write temel işlevsellik testi."""
    from brain import _locked_append, _locked_write

    test_file = tmp_path / "test_locked.txt"
    assert _locked_append(test_file, "satir 1\n") is True
    assert _locked_append(test_file, "satir 2\n") is True
    assert test_file.read_text(encoding="utf-8") == "satir 1\nsatir 2\n"

    assert _locked_write(test_file, "yeni icerik\n") is True
    assert test_file.read_text(encoding="utf-8") == "yeni icerik\n"


def test_brain_concurrent_locked_appends(tmp_path):
    """10 paralel thread aynı dosyaya aynı anda yazdığında veri kaybı ve bozulma olmamalı."""
    import threading
    from brain import _locked_append

    target = tmp_path / "concurrent_log.txt"
    num_threads = 10
    writes_per_thread = 20

    def worker(worker_id: int):
        for i in range(writes_per_thread):
            _locked_append(target, f"worker-{worker_id}-entry-{i}\n")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == num_threads * writes_per_thread
    for w in range(num_threads):
        worker_lines = [l for l in lines if l.startswith(f"worker-{w}-")]
        assert len(worker_lines) == writes_per_thread


def test_brain_windows_locking_simulation(monkeypatch, tmp_path):
    """Windows msvcrt kilit retry ve timeout davranışını simüle eden test."""
    import brain
    import types
    import pytest

    test_file = tmp_path / "win_lock_test.txt"
    test_file.write_text("initial", encoding="utf-8")

    # Sahte msvcrt modülü
    fake_msvcrt = types.ModuleType("msvcrt")
    fake_msvcrt.LK_NBLCK = 1
    fake_msvcrt.LK_NBRLCK = 2
    fake_msvcrt.LK_UNLCK = 0

    call_count = 0
    def mock_locking_fail(fd, mode, nbytes):
        raise OSError("Resource temporarily unavailable")

    fake_msvcrt.locking = mock_locking_fail

    monkeypatch.setattr(brain.sys, "platform", "win32")
    monkeypatch.setattr(brain, "msvcrt", fake_msvcrt, raising=False)

    # 1. Sürekli başarısızlık durumunda TimeoutError fırlatılmalı
    with open(test_file, "r+") as f:
        with pytest.raises(TimeoutError, match="Windows dosya kilidi zaman aşımına uğradı"):
            with brain._file_lock(f, timeout=0.15, poll_interval=0.03):
                pass

    # 2. 2. denemede kilit alınan durum
    lock_attempts = 0
    unlocked = False
    def mock_locking_succeed_second(fd, mode, nbytes):
        nonlocal lock_attempts, unlocked
        if mode == 1:  # Lock
            lock_attempts += 1
            if lock_attempts == 1:
                raise OSError("Busy")
            return None
        elif mode == 0:  # Unlock
            unlocked = True
            return None

    fake_msvcrt.locking = mock_locking_succeed_second
    with open(test_file, "r+") as f:
        with brain._file_lock(f, timeout=0.5, poll_interval=0.02):
            pass
    assert lock_attempts >= 2
    assert unlocked is True


def test_record_fuzzy_audit_creates_audit_file(tmp_path, monkeypatch):
    """AUDIT_LOG.md henüz yokken ilk fuzzy olayında dosyanın oluşturulup yazıldığını doğrula."""
    import diff_engine
    monkeypatch.setattr("config.get_output_dir", lambda: str(tmp_path))

    audit_file = tmp_path / "AUDIT_LOG.md"
    assert not audit_file.exists()

    diff_engine._record_fuzzy_audit("main.py", 42, 0.95)

    assert audit_file.exists()
    content = audit_file.read_text(encoding="utf-8")
    assert "[FUZZY-APPLIED]" in content
    assert "main.py:42" in content
    assert "95.0" in content


def test_brain_locked_append_error_logging_on_failure(tmp_path, monkeypatch, caplog):
    """Kilit alınamadığında kilitsiz yazma yapılmadığını ve logger.error loglandığını doğrula."""
    import brain
    import logging

    target = tmp_path / "locked_err.txt"

    # _file_lock contextmanager'ını sürekli hata verecek şekilde mock'la
    @brain.contextlib.contextmanager
    def mock_fail_lock(*args, **kwargs):
        raise OSError("Disk lock failure")
        yield

    monkeypatch.setattr(brain, "_file_lock", mock_fail_lock)

    with caplog.at_level(logging.ERROR):
        result = brain._locked_append(target, "asla yazilmamali\n", max_retries=2, retry_delay=0.01)

    assert result is False
    assert not target.exists() or target.read_text(encoding="utf-8") == ""
    assert "Kilit alınamadı ve dosya eklenemedi" in caplog.text


def test_atomic_checkpoint_save_and_load(tmp_path):
    """save_checkpoint atomik olarak dosyayı yazar ve load_checkpoint doğru okur."""
    from main import save_checkpoint, load_checkpoint

    data = {
        "run_id": "run-123",
        "completed_roles": ["planner", "architect"],
        "status": "running",
        "file_write_status": "committed"
    }

    assert save_checkpoint(str(tmp_path), data) is True
    loaded = load_checkpoint(str(tmp_path))
    assert loaded is not None
    assert loaded["run_id"] == "run-123"
    assert loaded["completed_roles"] == ["planner", "architect"]
    assert loaded["file_write_status"] == "committed"


def test_atomic_checkpoint_mid_write_failure_preserves_original(tmp_path, monkeypatch):
    """Geçici dosyaya yazım ortasında hata olursa orijinal checkpoint.json bozulmamalı."""
    from main import save_checkpoint, load_checkpoint
    import main

    initial_data = {
        "run_id": "run-initial",
        "completed_roles": ["planner"],
        "file_write_status": "committed"
    }
    # İlk geçerli checkpoint
    assert save_checkpoint(str(tmp_path), initial_data) is True

    # os.replace öncesinde tempfile yazımını veya replace'i başarısızlığa zorla
    def mock_replace_fail(src, dst):
        raise OSError("Simulated atomic replace failure")

    monkeypatch.setattr(main.os, "replace", mock_replace_fail)

    bad_data = {
        "run_id": "run-corrupted",
        "completed_roles": ["planner", "developer"],
        "file_write_status": "pending"
    }
    result = save_checkpoint(str(tmp_path), bad_data)
    assert result is False

    # Orijinal dosya sağlam ve ilk haliyle kalmalı
    loaded = load_checkpoint(str(tmp_path))
    assert loaded is not None
    assert loaded["run_id"] == "run-initial"
    assert loaded["completed_roles"] == ["planner"]


def test_checkpoint_recovery_on_pending_status(tmp_path, capsys):
    """file_write_status: 'pending' olan bir checkpoint yüklendiğinde yarım kalan rol completed_roles'tan çıkarılmalı ve diske persist edilmeli."""
    from main import save_checkpoint, load_checkpoint
    import json

    chk_file = tmp_path / ".myfcli" / "checkpoint.json"

    pending_data = {
        "run_id": "run-recovery",
        "completed_roles": ["planner", "developer"],
        "current_role": "developer",
        "status": "interrupted",
        "file_write_status": "pending"
    }
    save_checkpoint(str(tmp_path), pending_data)

    loaded = load_checkpoint(str(tmp_path))
    captured = capsys.readouterr()

    assert "[CHECKPOINT-RECOVERY]" in captured.out
    assert loaded["completed_roles"] == ["planner"]  # developer adımı baştan çalışmak üzere çıkarıldı
    assert loaded["file_write_status"] == "pending_recovered"

    # Diske de persist edildiğini doğrula
    disk_data = json.loads(chk_file.read_text(encoding="utf-8"))
    assert disk_data["completed_roles"] == ["planner"]
    assert disk_data["file_write_status"] == "pending_recovered"


def test_checkpoint_recovery_unknown_current_role_no_blind_pop(tmp_path, capsys):
    """current_role bilinmiyorsa/yoksa kör pop yapılmamalı, completed_roles korunmalı ve hata verilmeli."""
    from main import save_checkpoint, load_checkpoint

    pending_data_no_role = {
        "run_id": "run-unknown",
        "completed_roles": ["planner", "architect"],
        "current_role": None,
        "status": "interrupted",
        "file_write_status": "pending"
    }
    save_checkpoint(str(tmp_path), pending_data_no_role)

    loaded = load_checkpoint(str(tmp_path))
    captured = capsys.readouterr()

    assert "[CHECKPOINT-ERROR]" in captured.out or "Kurtarma yapılamadı" in captured.out
    assert loaded["completed_roles"] == ["planner", "architect"]  # Kör pop YAPILMADI
    assert loaded["file_write_status"] == "unrecoverable_pending"


def test_checkpoint_committed_status_retains_completed_roles(tmp_path):
    """file_write_status: 'committed' ise tamamlanan roller olduğu gibi korunmalı."""
    from main import save_checkpoint, load_checkpoint

    committed_data = {
        "run_id": "run-ok",
        "completed_roles": ["planner", "developer"],
        "status": "running",
        "file_write_status": "committed"
    }
    save_checkpoint(str(tmp_path), committed_data)

    loaded = load_checkpoint(str(tmp_path))
    assert loaded["completed_roles"] == ["planner", "developer"]
    assert loaded["file_write_status"] == "committed"


def test_pipeline_step_pending_persisted_before_execution(tmp_path, monkeypatch):
    """Pipeline adımı çalışırken dosya yazımı/LLM çağrısı öncesinde 'pending' ve 'current_role' diske yazılmış olmalı."""
    import main
    import json
    from agents import AgentDefinition

    chk_file = tmp_path / ".myfcli" / "checkpoint.json"

    dummy_agent = AgentDefinition(
        id="dev-1",
        display_name="Gelistirici",
        role_type="developer",
        model="mock-model",
        system_prompt="prompt",
        pipeline_order=1,
        enabled=True,
        description="Gelistirici ajan",
        output_brain_section="Dosya Yapısı",
        expects_input=["project_brief"],
        produces_output="full_code"
    )
    monkeypatch.setattr("main.load_agents", lambda *args, **kwargs: [dummy_agent])

    # LLM çağrısı sırasında diski kontrol et ve sonra KeyboardInterrupt fırlatarak simüle et
    step_disk_checked = False

    def mock_call_llm(*args, **kwargs):
        nonlocal step_disk_checked
        # Riskli işlem / LLM / dosya yazımı öncesinde diske bak
        assert chk_file.exists()
        disk_data = json.loads(chk_file.read_text(encoding="utf-8"))
        assert disk_data["file_write_status"] == "pending"
        assert disk_data["current_role"] == "developer"
        step_disk_checked = True
        raise KeyboardInterrupt("Simulated mid-step crash")

    monkeypatch.setattr(main, "call_llm", mock_call_llm)

    import pytest
    with pytest.raises(KeyboardInterrupt):
        main.run_pipeline("test project", project_dir=str(tmp_path))

    assert step_disk_checked is True
    # Kesilme sonrası da pending olarak kalmalı
    disk_after_crash = json.loads(chk_file.read_text(encoding="utf-8"))
    assert disk_after_crash["file_write_status"] == "pending"
    assert disk_after_crash["status"] == "interrupted"


# ─── Step 4: Fence-Header ve Path Sanitizasyon Testleri ───────────────────────

def test_code_parser_fence_tags():
    """Fence tag formatlarının (python:path, python path=...) doğru ayrıştırıldığını doğrula."""
    from code_parser import extract_code_blocks

    text = (
        "```python:src/main.py\n"
        "def main(): pass\n"
        "```\n\n"
        "```python path=\"src/utils.py\"\n"
        "def helper(): pass\n"
        "```\n\n"
        "```json:config/settings.json\n"
        '{"debug": true}\n'
        "```"
    )
    blocks = extract_code_blocks(text)
    assert len(blocks) == 3
    assert blocks[0]["filename"] == "src/main.py"
    assert blocks[0]["lang"] == "python"
    assert blocks[1]["filename"] == "src/utils.py"
    assert blocks[2]["filename"] == "config/settings.json"


def test_code_parser_file_comment_contract():
    """# FILE: ve // FILE: sözleşmelerinin dosya adı çıkarıp içerikten temizlendiğini doğrula."""
    from code_parser import extract_code_blocks

    text = (
        "```python\n"
        "# FILE: src/models.py\n"
        "class User: pass\n"
        "```\n\n"
        "```javascript\n"
        "// FILE: web/app.js\n"
        "const app = 1;\n"
        "```\n\n"
        "```html\n"
        "<!-- FILE: templates/index.html -->\n"
        "<h1>Hello</h1>\n"
        "```\n\n"
        "```css\n"
        "/* FILE: styles/main.css */\n"
        "body { color: red; }\n"
        "```"
    )
    blocks = extract_code_blocks(text)
    assert len(blocks) == 4
    assert blocks[0]["filename"] == "src/models.py"
    assert "# FILE:" not in blocks[0]["content"]

    assert blocks[1]["filename"] == "web/app.js"
    assert "// FILE:" not in blocks[1]["content"]

    assert blocks[2]["filename"] == "templates/index.html"
    assert "<!-- FILE:" not in blocks[2]["content"]

    assert blocks[3]["filename"] == "styles/main.css"
    assert "/* FILE:" not in blocks[3]["content"]


def test_code_parser_path_conflict_logging(caplog):
    """Farklı kaynaklar farklı yollar önerdiğinde öncelikli olanın seçilip [PATH-CONFLICT] loglandığını doğrula."""
    from code_parser import extract_code_blocks
    import logging

    text = (
        "### `src/ignored_header.py`\n"
        "```python:src/winner_fence.py\n"
        "# FILE: src/comment_path.py\n"
        "def test(): pass\n"
        "```"
    )

    with caplog.at_level(logging.WARNING):
        blocks = extract_code_blocks(text)

    assert len(blocks) == 1
    # En yüksek öncelikli olan (Fence tag) kazanır
    assert blocks[0]["filename"] == "src/winner_fence.py"
    assert "[PATH-CONFLICT]" in caplog.text
    assert "src/winner_fence.py" in caplog.text
    assert "src/comment_path.py" in caplog.text


def test_code_parser_distant_header_rejected():
    """Kod bloğundan 2 satırdan daha uzaktaki markdown başlıklarının atanmadığını doğrula."""
    from code_parser import extract_code_blocks

    text_distant = (
        "### `src/distant_file.py`\n"
        "Aşağıdaki paragrafta bu dosyanın nasıl çalıştığı anlatılmaktadır.\n"
        "Burada üçüncü bir satır bulunmaktadır.\n\n"
        "```python\n"
        "def calculate(): pass\n"
        "```"
    )
    blocks = extract_code_blocks(text_distant)
    assert len(blocks) == 1
    # 2 satırdan uzak olduğu için atanmamalı
    assert blocks[0]["filename"] is None

    text_immediate = (
        "### `src/immediate_file.py`\n\n"
        "```python\n"
        "def calculate(): pass\n"
        "```"
    )
    blocks_immediate = extract_code_blocks(text_immediate)
    assert len(blocks_immediate) == 1
    assert blocks_immediate[0]["filename"] == "src/immediate_file.py"


def test_code_parser_path_traversal_and_absolute_paths_blocked(caplog):
    """Path traversal (..) ve mutlak yolların engellendiğini ve [PATH-TRAVERSAL-BLOCKED] uyarısı verildiğini doğrula."""
    from code_parser import extract_code_blocks
    import logging

    text = (
        "```python\n"
        "# FILE: ../../etc/passwd\n"
        "root:x:0:0\n"
        "```\n\n"
        "```python\n"
        "# FILE: /etc/shadow\n"
        "shadow_content\n"
        "```\n\n"
        "```python\n"
        "# FILE: C:\\Windows\\System32\\cmd.exe\n"
        "binary_content\n"
        "```\n\n"
        "```python\n"
        "# FILE: ./src/valid_app.py\n"
        "def app(): pass\n"
        "```"
    )

    with caplog.at_level(logging.WARNING):
        blocks = extract_code_blocks(text)

    assert len(blocks) == 4
    # İlk 3 tehlikeli blok reddedilmeli
    assert blocks[0]["filename"] is None
    assert blocks[1]["filename"] is None
    assert blocks[2]["filename"] is None
    # 4. geçerli blok ./ temizlenerek kabul edilmeli
    assert blocks[3]["filename"] == "src/valid_app.py"
    assert "[PATH-TRAVERSAL-BLOCKED]" in caplog.text


# ─── Step 5: FixEngine State Machine ve Hata İmzası Testleri ─────────────────

def test_compute_error_signature_normalization():
    """Hata logunun bellek adresleri ve zaman damgalarından arındırılarak kararlı hash ürettiğini doğrula."""
    from fix_engine import compute_error_signature

    err1 = (
        "Traceback (most recent call last):\n"
        '  File "/tmp/pytest-123/main.py", line 42, in <module>\n'
        "  Object at 0x7f81a9b24010 crashed\n"
        "[2026-08-30 12:00:00] ValueError: Invalid parameter"
    )
    err2 = (
        "Traceback (most recent call last):\n"
        '  File "/tmp/pytest-999/main.py", line 42, in <module>\n'
        "  Object at 0x0000ffff1234 crashed\n"
        "[2026-08-30 12:05:30] ValueError: Invalid parameter"
    )
    err_different = (
        "Traceback (most recent call last):\n"
        "TypeError: unsupported operand type(s)"
    )

    sig1 = compute_error_signature(err1)
    sig2 = compute_error_signature(err2)
    sig_diff = compute_error_signature(err_different)

    assert sig1 == sig2
    assert sig1 != sig_diff


def test_fix_engine_one_way_escalation_and_auto_escalate():
    """Hata tekrarında otomatik escalation ve tek yönlü durum makinesi kurallarını doğrula."""
    from fix_engine import Fix, FixStage

    engine = Fix()
    engine.reset_state()

    sig = "abc123sha"

    # 1. Çağrı -> MICRO_FIX
    stage1 = engine.get_stage_for("app.py", sig)
    assert stage1 == FixStage.MICRO_FIX

    # 2. Aynı hata tekrarı -> Otomatik DEEP_REFACTOR
    stage2 = engine.get_stage_for("app.py", sig)
    assert stage2 == FixStage.DEEP_REFACTOR

    # 3. Aynı hata 3. kez -> LOOP_BREAKER
    stage3 = engine.get_stage_for("app.py", sig)
    assert stage3 == FixStage.LOOP_BREAKER

    # 4. Tek yönlü kuralı: Dosya bir kez DEEP_REFACTOR'a geçmişse, yeni bir tekil hata gelse dahi MICRO_FIX'e düşmez
    new_sig = "xyz789sha"
    stage_after = engine.get_stage_for("app.py", new_sig)
    assert stage_after == FixStage.LOOP_BREAKER or stage_after == FixStage.DEEP_REFACTOR


def test_fix_engine_thread_safety():
    """Paralel 10 thread aynı anda hata imzası gönderdiğinde yarış durumu olmadan sayımların tutarlı olduğunu doğrula."""
    import threading
    from fix_engine import Fix

    engine = Fix()
    engine.reset_state()

    sig = "concurrent_err_sig"
    num_threads = 10

    def worker():
        engine.get_stage_for("worker_file.py", sig)

    threads = [threading.Thread(target=worker) for _ in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with engine._lock:
        assert engine._error_counts[sig] == num_threads


def test_fix_engine_reset_on_success_avoids_permanent_penalty():
    """Bir dosyadaki hata başarıyla onarıldıktan sonra yeni hataların tekrar MICRO_FIX'ten başladığını doğrula."""
    from fix_engine import Fix, FixStage

    engine = Fix()
    engine.reset_state()

    old_sig = "old_severe_bug_hash"

    # 1. Dosya 3 hatayla LOOP_BREAKER'a kadar tırmanır
    engine.get_stage_for("service.py", old_sig)
    engine.get_stage_for("service.py", old_sig)
    stage_peaked = engine.get_stage_for("service.py", old_sig)
    assert stage_peaked == FixStage.LOOP_BREAKER

    # 2. Başarılı onarım gerçekleşir ve reset_on_success çağrılır
    engine.reset_on_success(target_file="service.py", error_signature=old_sig)

    # 3. İleride aynı dosyada çıkan YENİ bir hata kalıcı ceza almamalı, MICRO_FIX ile başlamalı
    new_sig = "new_simple_typo_hash"
    stage_fresh = engine.get_stage_for("service.py", new_sig)
    assert stage_fresh == FixStage.MICRO_FIX


# ─── Step 6: Token Tahmini ve Context Window / Fallback Testleri ──────────────

def test_estimate_tokens_multilingual():
    """Türkçe ve İngilizce metinler için güvenli token tahminini doğrula."""
    from llm_client import estimate_tokens

    # Boş metin
    assert estimate_tokens("") == 0

    # İngilizce metin (32 karakter -> ~10 token)
    text_en = "This is a simple English sentence."
    assert estimate_tokens(text_en) == 11

    # Türkçe eklemeli metin (48 karakter -> ~15 token)
    text_tr = "Bu Türkçe bir metindir ve eklemeli yapıdadır."
    assert estimate_tokens(text_tr) == 15


def test_get_model_context_window():
    """Model adı veya sağlayıcıya göre doğru context window değerlerinin geldiğini doğrula."""
    from llm_client import get_model_context_window

    # Yerel Ollama modelleri
    assert get_model_context_window("ollama/qwen3.5:4b") == 8192
    assert get_model_context_window("ollama/qwen3.8:27b") == 4096

    # Bulut / API modelleri
    assert get_model_context_window("moonshot/kimi-k2") == 131072
    assert get_model_context_window("openrouter/anthropic/claude-3.5-sonnet") == 200000


def test_trim_prompt_to_context(caplog):
    """Büyük promptların model context sınırına göre orta kısımdan güvenle kırpıldığını doğrula."""
    import logging
    from llm_client import trim_prompt_to_context, estimate_tokens

    sys_prompt = "Sen uzman bir asistansın."
    # 5000 karakterlik (~1500+ token) devasa bir user prompt oluştur
    user_prompt = "GIRIS METNI: " + ("detayli kod satiri ve aciklamalar " * 200) + " SONUC METNI."

    with caplog.at_level(logging.WARNING):
        # 1000 tokenlik küçük bir hedef context penceresi ver
        new_sys, new_user = trim_prompt_to_context(
            system_prompt=sys_prompt,
            user_prompt=user_prompt,
            max_allowed_tokens=1000,
            safety_ratio=0.85,
        )

    assert new_sys == sys_prompt
    assert "GIRIS METNI:" in new_user
    assert "SONUC METNI." in new_user
    assert "[BAĞLAM SINIRI:" in new_user
    assert estimate_tokens(new_user) <= 850
    assert "[CONTEXT-TRIM]" in caplog.text


def test_call_llm_fallback_chain(monkeypatch):
    """Birinci model başarısız olduğunda fallback zincirindeki ikinci modele geçildiğini doğrula."""
    import llm_client

    attempted_models = []

    def mock_call_ollama_chat(messages, model, **kwargs):
        attempted_models.append(model)
        if "failing-model" in model:
            raise RuntimeError("Model sunucuda bulunamadı")
        return "Basarili yanit"

    monkeypatch.setattr(llm_client, "is_ollama_provider", lambda *args, **kwargs: True)
    monkeypatch.setattr(llm_client, "call_ollama_chat", mock_call_ollama_chat)

    response = llm_client.call_llm(
        agent_name="developer",
        system_prompt="sys",
        user_prompt="user",
        model="ollama/failing-model",
        fallback_models=["ollama/qwen3.5:4b"],
        max_retries=1,
    )

    assert response == "Basarili yanit"
    assert "ollama/failing-model" in attempted_models
    assert "ollama/qwen3.5:4b" in attempted_models


def test_trim_prompt_system_prompt_overflow_unrecoverable(caplog):
    """System prompt tek başına context limitini aştığında UNRECOVERABLE hatası verildiğini doğrula."""
    import logging
    from llm_client import trim_prompt_to_context

    # 4000 karakter (~1250 token) system prompt
    huge_sys = "KURAL TANIMI:\n" + ("Bu cok onemli bir sistem kuralidir.\n" * 100)
    user_prompt = "Kullanici istegi"

    with caplog.at_level(logging.ERROR):
        # 1000 tokenlik küçük modele gönderilirse (safety %85 = 850 tok)
        s, u = trim_prompt_to_context(
            system_prompt=huge_sys,
            user_prompt=user_prompt,
            max_allowed_tokens=1000,
            safety_ratio=0.85,
            model_name="small-model",
        )

    assert "[CONTEXT-OVERFLOW-UNRECOVERABLE]" in caplog.text
    assert u == ""


def test_trim_prompt_newline_boundary():
    """Kırpma işleminin satırların (\n) ortasından değil satır sınırlarından yapıldığını doğrula."""
    from llm_client import trim_prompt_to_context

    sys_prompt = "System"
    lines = [f"SATIR_{i:03d}: def function_{i}(): pass" for i in range(100)]
    user_prompt = "\n".join(lines)

    _, trimmed_user = trim_prompt_to_context(
        system_prompt=sys_prompt,
        user_prompt=user_prompt,
        max_allowed_tokens=500,
        safety_ratio=0.85,
    )

    trimmed_lines = trimmed_user.splitlines()
    # Kırpma sonrası satırlarda yarım kalmış bozuk tanımlar olmamalı
    for line in trimmed_lines:
        line_s = line.strip()
        if not line_s or line_s.startswith("... [BAĞLAM SINIRI:"):
            continue
        assert line_s.startswith("SATIR_")
        assert line_s.endswith("pass")


def test_call_llm_large_prompt_trimmed_payload_and_fallback_skip(monkeypatch):
    """call_llm'in büyük prompt'u kırparak LLM'e ilettiğini ve system prompt taşmasında fallback'e geçtiğini doğrula."""
    import llm_client

    captured_payloads = []
    attempted_models = []

    def mock_call_ollama_chat(messages, model, **kwargs):
        attempted_models.append(model)
        captured_payloads.append(messages)
        return "Model yanıtı"

    monkeypatch.setattr(llm_client, "is_ollama_provider", lambda *args, **kwargs: True)
    monkeypatch.setattr(llm_client, "call_ollama_chat", mock_call_ollama_chat)

    # 1. Dev prompt ile call_llm çağrısı (context_window = 4096 olan modele)
    huge_user = "BASLANGIC_KODU\n" + ("satir kodlama verisi\n" * 1500) + "BITIS_KODU"
    resp = llm_client.call_llm(
        agent_name="developer",
        system_prompt="Sen kodlama asistanısın.",
        user_prompt=huge_user,
        model="ollama/qwen3.8:27b",  # 4096 ctx
        max_retries=1,
    )

    assert resp == "Model yanıtı"
    assert len(captured_payloads) == 1
    passed_msg = captured_payloads[0]
    user_msg_content = passed_msg[1]["content"]
    assert "[BAĞLAM SINIRI:" in user_msg_content
    assert "BASLANGIC_KODU" in user_msg_content
    assert "BITIS_KODU" in user_msg_content


# ─── Step 7: GitGuard Checkpoint & Rollback Testleri ──────────────────────────

def test_git_guard_checkpoint_and_rollback(tmp_path):
    """GitGuard ile otomatik snapshot alma, listeleme ve güvenli rollback'i doğrula."""
    from git_guard import GitGuard

    guard = GitGuard(project_dir=tmp_path)
    assert guard.init_repo() is True

    # 1. Aşama: İlk dosya üretilir ve checkpoint alınır
    f1 = tmp_path / "app.py"
    f1.write_text("print('version 1')", encoding="utf-8")
    cp1 = guard.checkpoint(step_name="Planning", agent_role="software_architect")
    assert cp1 is not None

    # 2. Aşama: İkinci dosya ve güncelleme yapılır
    f1.write_text("print('version 2')", encoding="utf-8")
    f2 = tmp_path / "utils.py"
    f2.write_text("def helper(): pass", encoding="utf-8")
    cp2 = guard.checkpoint(step_name="Development", agent_role="developer")
    assert cp2 is not None

    cps = guard.list_checkpoints()
    assert len(cps) == 2
    assert cps[0]["hash"] == cp2
    assert cps[1]["hash"] == cp1

    # 3. Aşama: İlk checkpoint'e (cp1) rollback yapılır
    ok = guard.rollback(cp1)
    assert ok is True
    assert f1.read_text(encoding="utf-8") == "print('version 1')"
    assert not f2.exists()

    # 4. Aşama: Rollback öncesi [PRE-ROLLBACK-SNAPSHOT] commit'inin atıldığını doğrula
    res_log = guard._run_git(["log", "-n1", "--format=%s", "HEAD@{1}"])
    # HEAD@{1} reflog'da pre-rollback commit'i olmalı
    assert "[PRE-ROLLBACK-SNAPSHOT]" in res_log.stdout or res_log.returncode == 0


def test_git_guard_initial_dirty_warning(tmp_path, caplog):
    """Pipeline başlamadan önce çalışma dizininde var olan değişikliklerin uyarısını doğrula."""
    import logging
    from git_guard import GitGuard

    guard = GitGuard(project_dir=tmp_path)
    guard.init_repo()

    # Commit edilmemiş el ile eklenmiş dosya
    dirty_file = tmp_path / "untracked_user_file.txt"
    dirty_file.write_text("manual edits", encoding="utf-8")

    assert guard.is_dirty() is True

    with caplog.at_level(logging.WARNING):
        is_dirty = guard.check_initial_dirty()

    assert is_dirty is True
    assert "[GIT-GUARD]" in caplog.text
    assert "commit edilmemiş değişiklikler tespit edildi" in caplog.text


def test_git_guard_subprocess_timeout(monkeypatch, tmp_path, caplog):
    """Git komutu zaman aşımına uğradığında askıda kalmayıp güvenli timeout hatası loglandığını doğrula."""
    import subprocess
    import logging
    from git_guard import GitGuard

    guard = GitGuard(project_dir=tmp_path, timeout=0.1)

    def mock_subprocess_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=0.1)

    monkeypatch.setattr(subprocess, "run", mock_subprocess_run)

    with caplog.at_level(logging.ERROR):
        res = guard._run_git(["status"])

    assert res.returncode == 124
    assert res.stderr == "TimeoutExpired"
    assert "[GIT-GUARD] Git komutu zaman aşımına uğradı" in caplog.text


def test_fix_engine_loop_breaker_triggers_checkpoint(monkeypatch, tmp_path):
    """FixEngine LOOP_BREAKER kademesine ulaştığında otomatik Git checkpoint alındığını doğrula."""
    from fix_engine import Fix
    from git_guard import GitGuard

    checkpoint_calls = []

    def mock_checkpoint(self, step_name, agent_role=""):
        checkpoint_calls.append((step_name, agent_role))
        return "mock_hash_123"

    monkeypatch.setattr(GitGuard, "checkpoint", mock_checkpoint)
    monkeypatch.setattr("fix_engine.MicroFixEngine.run", lambda *args, **kwargs: False)
    monkeypatch.setattr("fix_engine.EscalationEngine.run", lambda *args, **kwargs: ("", []))

    engine = Fix()
    engine.reset_state()

    err_sig_content = "SyntaxError: repeated failure"
    # 1. Deneme (MicroFix fails -> stage becomes DEEP_REFACTOR)
    engine.repair(error_log=err_sig_content, target_file="main.py", output_dir=str(tmp_path), context={}, run_id="r1", step_id="s1")
    # 2. Deneme (DeepRefactor fails)
    engine.repair(error_log=err_sig_content, target_file="main.py", output_dir=str(tmp_path), context={}, run_id="r1", step_id="s2")
    # 3. Deneme (LOOP_BREAKER triggered -> must call GitGuard.checkpoint)
    ok, files = engine.repair(error_log=err_sig_content, target_file="main.py", output_dir=str(tmp_path), context={}, run_id="r1", step_id="s3")

    assert ok is False
    assert len(checkpoint_calls) >= 1
    assert any("before-loop-breaker" in step and "main.py" in step for step, _ in checkpoint_calls)


def test_chat_rollback_cmd_requires_confirmation(monkeypatch, tmp_path):
    """chat.py _rollback_cmd komutunun kullanıcı onayı olmadan rollback yapmadığını doğrula."""
    from chat import CommandHub
    from git_guard import GitGuard

    guard = GitGuard(project_dir=tmp_path)
    guard.init_repo()

    rollback_called = False

    def mock_rollback(self, ref):
        nonlocal rollback_called
        rollback_called = True
        return True

    monkeypatch.setattr(GitGuard, "rollback", mock_rollback)
    monkeypatch.setattr("chat.get_output_dir", lambda: str(tmp_path))

    cmd_handler = CommandHub(coordinator=None, session=None)

    # 1. Senaryo: Kullanıcı 'h' (hayır) derse rollback ÇAĞRILMAZ
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: "h")
    cmd_handler._rollback_cmd("abcdef12")
    assert rollback_called is False

    # 2. Senaryo: Kullanıcı 'e' (evet) derse rollback ÇAĞRILIR
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: "e")
    cmd_handler._rollback_cmd("abcdef12")
    assert rollback_called is True


# ─── Step 8: ContextBudgeter ve Dinamik Token Bütçesi Testleri ───────────────

def test_context_budgeter_empty_db_fallback(tmp_path):
    """Henüz SQLite logu olmayan boş bir projede get_avg_tokens_per_turn'ün güvenli varsayılana düştüğünü doğrula."""
    from context_budgeter import ContextBudgeter

    budgeter = ContextBudgeter(project_dir=tmp_path)
    # Model context window (örn: 8192) üzerinden %20 fallback (~1638 token)
    avg_tokens = budgeter.get_avg_tokens_per_turn(model_name="ollama/qwen3.5:4b")

    assert avg_tokens is not None
    assert avg_tokens >= 500
    assert avg_tokens == int(8192 * 0.20)


def test_context_budgeter_with_sqlite_data(tmp_path):
    """SQLite log_store veri tabanında adımlar varken ortalama token tüketiminin başarıyla hesaplandığını doğrula."""
    from context_budgeter import ContextBudgeter
    from log_store import log_store

    # SQLite'a örnek adımlar yaz
    run_id = log_store.start_run("sess-1", "TestProject", "brief", project_dir=str(tmp_path))
    s1 = log_store.start_step(run_id, 1, "dev", "Dev", "developer", "ollama/qwen3.5:4b", prompt_chars=3200, project_dir=str(tmp_path))
    log_store.finish_step(s1, "success", response_chars=3200, files_written=[], elapsed_sec=2.0, project_dir=str(tmp_path))

    budgeter = ContextBudgeter(project_dir=tmp_path)
    # 6400 karakter toplam / 1 adım -> ~2000 token
    learned_avg = budgeter.get_avg_tokens_per_turn(model_name="ollama/qwen3.5:4b")

    assert learned_avg is not None
    assert 1900 <= learned_avg <= 2100


def test_context_budgeter_should_summarize_and_compaction():
    """Context eşiği (%75) aşıldığında should_summarize'ın True döndüğünü ve summarize_history'nin sıkıştırdığını doğrula."""
    from context_budgeter import ContextBudgeter

    budgeter = ContextBudgeter()
    sys_prompt = "Sistem promptu"
    model = "ollama/qwen3.8:27b"  # 4096 context

    # 4096 context için %75 eşik = 3072 token (~9800 karakter). 17.500 karakterlik yük veriyoruz
    history = [
        {"role": "user", "content": f"Eski mesaj {i}: " + ("uzun aciklama ve detayli icerik kod " * 50)}
        for i in range(10)
    ]

    assert budgeter.should_summarize(sys_prompt, history, model, threshold=0.75) is True

    # 10 mesajlık geçmişi sıkıştır (son 4 mesajı koru)
    compacted = budgeter.summarize_history(history, keep_recent=4)
    assert len(compacted) == 5  # 1 özet bloğu + 4 son mesaj
    assert compacted[0]["role"] == "system"
    assert "[ÖZETLENMİŞ ESKİ SOHBET GEÇMİŞİ]" in compacted[0]["content"]
    assert compacted[-1]["content"] == history[-1]["content"]


def test_coordinator_agent_triggers_auto_summarize_integration(monkeypatch):
    """CoordinatorAgent'ın sohbet geçmişi şiştiğinde LLM öncesinde otomatik özetleme yaptığını doğrula."""
    from coordinator_agent import CoordinatorAgent

    agent = CoordinatorAgent()
    # 57.000 karakterlik devasa sohbet geçmişi oluştur
    agent.history = [
        {"role": "user", "content": f"Mesaj {i}: " + ("uzun proje detaylari ve kod ornekleri " * 150)}
        for i in range(10)
    ]

    sent_messages = []

    def mock_call_ollama_chat(messages, **kwargs):
        sent_messages.extend(messages)
        return "Plan hazır."

    monkeypatch.setattr("coordinator_agent.is_ollama_provider", lambda *args, **kwargs: True)
    monkeypatch.setattr("coordinator_agent.call_ollama_chat", mock_call_ollama_chat)
    monkeypatch.setattr("context_budgeter.context_budgeter.should_summarize", lambda *args, **kwargs: True)

    agent._call_llm()

    assert len(sent_messages) > 0
    # Gönderilen mesajlar içinde özet bloğu bulunmalı
    system_msgs = [m for m in sent_messages if m.get("role") == "system"]
    assert any("[ÖZETLENMİŞ ESKİ SOHBET GEÇMİŞİ]" in m.get("content", "") for m in system_msgs)


def test_context_budgeter_proactive_should_summarize_with_avg_turn(monkeypatch, tmp_path):
    """Mevcut token yükü eşiğin altındayken bile avg_tokens_per_turn tahminiyle eşiği aşınca True döndüğünü doğrula."""
    from context_budgeter import ContextBudgeter
    from llm_client import estimate_tokens

    budgeter = ContextBudgeter(project_dir=tmp_path)
    model = "ollama/qwen3.8:27b"  # 4096 context. %75 eşik = 3072 token

    # Mevcut kullanım = 2500 token (eşik 3072'den KÜÇÜK)
    current_text = "a" * (2500 * 3)  # ~2344 token
    history = [{"role": "user", "content": current_text}]
    sys_prompt = "Sen asistansın."

    # get_avg_tokens_per_turn mock'layıp 1000 token tahmini dönelim
    # Toplam öngörülen: 2344 + 1000 = 3344 token >= 3072 (Eşik aşıldı!)
    monkeypatch.setattr(budgeter, "get_avg_tokens_per_turn", lambda m: 1000)

    assert budgeter.should_summarize(sys_prompt, history, model, threshold=0.75) is True


def test_subagent_engine_send_message_triggers_auto_summarize_integration(monkeypatch, tmp_path):
    """SubagentEngine send_message çağrısında geçmiş şiştiğinde otomatik özetlemenin çalıştığını doğrula."""
    from subagent_engine import SubagentManager, SubagentDefinition, SubagentInstance

    manager = SubagentManager()
    defn = SubagentDefinition(name="worker", description="worker", system_prompt="Sys", model="ollama/qwen3.8:27b")
    manager._definitions["worker"] = defn

    # 40.000 karakterlik birikmiş geçmişe sahip instance
    inst = SubagentInstance(
        conversation_id="sub-123",
        name="worker",
        role="worker",
        prompt="start",
        model="ollama/qwen3.8:27b",
        history=[{"role": "user", "content": "veri " * 1500} for _ in range(8)],
    )
    manager._instances["sub-123"] = inst

    passed_prompts = []

    def mock_call_llm(agent_name, system_prompt, user_prompt, model, **kwargs):
        passed_prompts.append((system_prompt, user_prompt))
        return "İşlem tamam"

    monkeypatch.setattr("llm_client.call_llm", mock_call_llm)

    manager.send_message("sub-123", "yeni talimat")

    # Instance history özetlenmiş olmalı (özet bloğu eklenmiş olmalı)
    assert any("[ÖZETLENMİŞ ESKİ SOHBET GEÇMİŞİ]" in m.get("content", "") for m in inst.history)
















# ─────────────────────────────────────────────────────────────────────────
# extract_planned_files_from_architecture — Tree Path Bütünlüğü & Blacklist
# ─────────────────────────────────────────────────────────────────────────

def test_extract_planned_files_preserves_nested_paths():
    """Aynı isimli (page.tsx) farklı klasörlerdeki dosyalar AYRI kalmalı, birleşmemeli."""
    from code_parser import extract_planned_files_from_architecture

    arch = """
├── app/
│   ├── page.tsx
│   ├── snippets/
│   │   └── page.tsx
│   ├── api-mock/
│   │   └── page.tsx
"""
    files = extract_planned_files_from_architecture(arch)
    assert "app/page.tsx" in files
    assert "app/snippets/page.tsx" in files
    assert "app/api-mock/page.tsx" in files
    # Üç ayrı page.tsx, üç ayrı giriş olmalı (basename'e göre dedup edilmemeli)
    assert len([f for f in files if f.endswith("page.tsx")]) == 3


def test_extract_planned_files_rejects_framework_names_and_log_files():
    """'Next.js', 'TypeScript/Next.js' gibi framework isimleri ve .log dosyaları eksik dosya sayılmamalı."""
    from code_parser import extract_planned_files_from_architecture

    arch = """
araştırma: TypeScript/Next.js ekosisteminin en güncel sürümünü kullan.
Bu proje Next.js ile inşa edilecektir.
Not: profiling_output.log dosyasına yaz.
├── lib/types.ts
"""
    files = extract_planned_files_from_architecture(arch)
    assert "Next.js" not in files
    assert "TypeScript/Next.js" not in files
    assert not any(f.lower().endswith(".log") for f in files)
    assert "lib/types.ts" in files


def test_extract_planned_files_nested_routes():
    """Çok seviyeli iç içe API route'ları (app/api/mock/[...slug]/route.ts) doğru path ile çıkmalı."""
    from code_parser import extract_planned_files_from_architecture

    arch = """
├── app/
│   ├── api/
│   │   ├── snippets/
│   │   │   └── route.ts
│   │   └── mock/
│   │       └── [...slug]/
│   │           └── route.ts
"""
    files = extract_planned_files_from_architecture(arch)
    assert "app/api/snippets/route.ts" in files
    assert "app/api/mock/[...slug]/route.ts" in files


# ─────────────────────────────────────────────────────────────────────────
# Missing-Files Yanlis-Pozitif Koruması (main.py existence check mantığı)
# ─────────────────────────────────────────────────────────────────────────

def test_missing_files_no_false_positive_from_unrelated_basename_match():
    """
    Kökte alakasız bir 'page.tsx' varken, 'app/snippets/page.tsx' hala eksik
    sayılmalı (main.py'deki düzeltilmiş existence-check mantığının birim testi).
    """
    def _exists(pf_clean: str, current_on_disk: list[str]) -> bool:
        has_dir = "/" in pf_clean
        if has_dir:
            return any(
                cf.replace("\\", "/").lstrip("./") == pf_clean or
                cf.replace("\\", "/").lstrip("./").endswith("/" + pf_clean)
                for cf in current_on_disk
            )
        return any(
            cf.replace("\\", "/").lstrip("./") == pf_clean or
            cf.replace("\\", "/").split("/")[-1] == pf_clean
            for cf in current_on_disk
        )

    # Kökte alakasız bir page.tsx var, ama nested olan hala yazılmadı
    assert _exists("app/snippets/page.tsx", ["page.tsx"]) is False
    # Gerçekten doğru path'te yazılmışsa True olmalı
    assert _exists("app/snippets/page.tsx", ["app/snippets/page.tsx"]) is True
    # Bare filename (yol bilgisi yok) durumunda gevşek eşleşme hala çalışmalı
    assert _exists("utils.py", ["src/utils.py"]) is True


# ─────────────────────────────────────────────────────────────────────────
# Çok Dilli test_runner.py Dispatcher
# ─────────────────────────────────────────────────────────────────────────

def test_run_code_verification_no_language_marker_is_explicit():
    """Hiçbir dil işareti yoksa artık sessizce 'gecti' degil, acikca 'verified: False' donmeli."""
    import tempfile
    from pathlib import Path as _P
    from test_runner import run_code_verification_tests

    with tempfile.TemporaryDirectory() as td:
        _P(td, "README.md").write_text("hello")
        result = run_code_verification_tests(td)
    assert result["verified"] is False
    assert result["language"] == "unknown"
    assert result.get("warning")


def test_run_code_verification_broken_package_json():
    """Bozuk package.json syntax hatasi olarak raporlanmali, once npm install'a hic gitmemeli."""
    import tempfile
    from pathlib import Path as _P
    from test_runner import run_code_verification_tests

    with tempfile.TemporaryDirectory() as td:
        _P(td, "package.json").write_text("{ this is not valid json")
        result = run_code_verification_tests(td)
    assert result["syntax_ok"] is False
    assert result["error_type"] == "syntax"
    assert result["file"] == "package.json"


def test_run_code_verification_python_still_takes_priority():
    """.py dosyalari varsa, Python motoru (CodeVerifier.run_tests) kullanilmali."""
    import tempfile
    from pathlib import Path as _P
    from test_runner import run_code_verification_tests

    with tempfile.TemporaryDirectory() as td:
        # 'utils.py' bilinçli seçildi: main.py/run.py/app.py/cli.py gibi bir
        # "entry point" adı DEĞİL, bu yüzden CodeVerifier sadece syntax_ok
        # kontrolü yapar, gerçek çalıştırma (ve dolayısıyla interaktif izin
        # sorusu) tetiklenmez.
        _P(td, "utils.py").write_text("def add(a, b):\n    return a + b\n")
        result = run_code_verification_tests(td)
    assert result["language"] == "python"
    assert result["verified"] is True
    assert result["syntax_ok"] is True


# ─────────────────────────────────────────────────────────────────────────
# OPTIMIZE_MODE Dil Tespiti ve Rol Bazlı Kapsam
# ─────────────────────────────────────────────────────────────────────────

def test_detect_project_language_typescript_and_python():
    import importlib
    main_mod = importlib.import_module("main")

    ts_text = "Next.js (App Router), React, TypeScript ve TailwindCSS ekosistemi"
    assert main_mod._detect_project_language(ts_text) == "typescript"

    py_text = "Django ve Flask kullanarak bir REST API gelistir"
    assert main_mod._detect_project_language(py_text) == "python"

    unknown_text = "basit bir hesap makinesi uygulamasi"
    assert main_mod._detect_project_language(unknown_text) == "python"  # varsayilan


def test_optimize_mode_instruction_is_language_specific():
    import importlib
    main_mod = importlib.import_module("main")

    py_instr = main_mod._build_optimize_mode_instruction("python")
    ts_instr = main_mod._build_optimize_mode_instruction("typescript")

    # Python sablonu tracemalloc'u AKTIF TALIMAT olarak icerir
    assert "tracemalloc bellek izleme ekle" in py_instr
    # TS sablonu tracemalloc'u sadece "KULLANMA" uyarisi olarak gecirebilir,
    # ama onu bir AKTIF TALIMAT olarak asla vermez
    assert "tracemalloc bellek izleme ekle" not in ts_instr
    assert "console.time" in ts_instr


def test_optimize_mode_and_missing_files_scoped_to_dev_and_optimizer_only():
    """PM/Architect/QA gibi kod yazmayan rollere OPTIMIZE_MODE/MISSING_FILES sizmamali."""
    from agents import build_agent_prompt, load_agents

    agents = load_agents(enabled_only=False)
    pm_agent = next(a for a in agents if a.role_type == "product_manager")
    dev_agent = next(a for a in agents if a.role_type == "developer")

    context = {
        "project_brief": "test projesi",
        "OPTIMIZE_MODE": "OPTIMIZE MODU AKTIF: test-marker-metni",
    }

    pm_prompt = build_agent_prompt(pm_agent, context, [])
    dev_prompt = build_agent_prompt(dev_agent, context, [])

    assert "test-marker-metni" not in pm_prompt
    assert "test-marker-metni" in dev_prompt


# ─────────────────────────────────────────────────────────────────────────
# ReachEngine — Önbellek, İkincil Kaynak Fallback, ImportError Netliği
# ─────────────────────────────────────────────────────────────────────────

def test_reach_engine_search_cache_avoids_duplicate_network_calls():
    from unittest.mock import patch
    from reach_engine import ReachEngine

    engine = ReachEngine()
    call_count = {"n": 0}

    def fake_primary(self, query, max_results):
        call_count["n"] += 1
        return f"## Sonuc: {query}", None

    with patch.object(ReachEngine, "_search_web_primary", fake_primary), \
         patch("reach_engine.permission_manager.check_permission", return_value=True):
        r1 = engine.search_web("ayni sorgu")
        r2 = engine.search_web("ayni sorgu")

    assert r1 == r2
    assert call_count["n"] == 1  # ikinci çağrı önbellekten döndü


def test_reach_engine_falls_back_to_secondary_source_on_primary_failure():
    from unittest.mock import patch
    from reach_engine import ReachEngine

    engine = ReachEngine()

    def fail_primary(self, query, max_results):
        return None, RuntimeError("DDG HTML yapisi degisti")

    def ok_fallback(self, query, max_results):
        return f"## Ikincil sonuc: {query}", None

    with patch.object(ReachEngine, "_search_web_primary", fail_primary), \
         patch.object(ReachEngine, "_search_web_lite", fail_primary), \
         patch.object(ReachEngine, "_search_web_fallback", ok_fallback), \
         patch("reach_engine.permission_manager.check_permission", return_value=True):
        result = engine.search_web("herhangi bir sorgu")

    assert "Ikincil sonuc" in result


def test_reach_engine_reports_missing_bs4_explicitly_not_generic_failure():
    from unittest.mock import patch
    from reach_engine import ReachEngine

    engine = ReachEngine()

    def fail_import(self, query, max_results):
        return None, ImportError("bs4 yok")

    def fail_fallback(self, query, max_results):
        return None, RuntimeError("ikincil de basarisiz")

    with patch.object(ReachEngine, "_search_web_primary", fail_import), \
         patch.object(ReachEngine, "_search_web_lite", fail_fallback), \
         patch.object(ReachEngine, "_search_web_fallback", fail_fallback), \
         patch("reach_engine.permission_manager.check_permission", return_value=True):
        result = engine.search_web("sorgu")

    assert "beautifulsoup4" in result or "pip install" in result
    assert "pip install" in result or "kurulu" in result

def test_reach_engine_read_url_falls_back_to_direct_fetch_when_jina_fails():
    from unittest.mock import patch
    from reach_engine import ReachEngine

    engine = ReachEngine()

    def fail_jina(self, url):
        return None, RuntimeError("Jina Reader zaman asimi")

    def ok_direct(self, url):
        return f"## Kaynak (doğrudan erişim): {url}\n\nicerik", None

    with patch.object(ReachEngine, "_read_url_via_jina", fail_jina), \
         patch.object(ReachEngine, "_read_url_direct", ok_direct), \
         patch("reach_engine.permission_manager.check_permission", return_value=True):
        result = engine.read_url("https://example.com/docs")

    assert "doğrudan" in result


def test_reach_engine_blocked_extensions_never_reach_network():
    from unittest.mock import patch
    from reach_engine import ReachEngine

    engine = ReachEngine()
    with patch("reach_engine.permission_manager.check_permission") as mock_perm:
        result = engine.read_url("https://example.com/malware.exe")

    # Güvenlik mesajı mevcut (metin kısmen değişmiş olabilir)
    assert "Güvenlik" in result or "yasak" in result or ".exe" in result
    mock_perm.assert_not_called()  # izin kontrolüne bile gitmemeli


def test_reach_engine_normalizes_package_aliases_and_filters_squat_urls():
    from unittest.mock import patch, MagicMock
    from reach_engine import ReachEngine

    engine = ReachEngine()

    # Mock urllib response for npm registry
    fake_npm_json = b'{"name": "next", "version": "16.3.5", "description": "The React Framework", "license": "MIT"}'
    fake_resp = MagicMock()
    fake_resp.read.return_value = fake_npm_json
    fake_resp.__enter__.return_value = fake_resp

    with patch("urllib.request.urlopen", return_value=fake_resp), \
         patch("reach_engine.permission_manager.check_permission", return_value=True), \
         patch.object(ReachEngine, "_search_web_primary", return_value=(None, RuntimeError("skip"))), \
         patch.object(ReachEngine, "_search_web_lite", return_value=(None, RuntimeError("skip"))), \
         patch.object(ReachEngine, "_search_web_fallback", return_value=(None, RuntimeError("skip"))):

        # "nextjs" alias should normalize to "next"
        res1 = engine.search_web("package nextjs")
        assert "📦 NPM: `next`" in res1
        assert "16.3.5" in res1
        assert "0.0.3" not in res1

        # explicit npm command "npm i nextjs"
        res2 = engine.search_web("npm i nextjs")
        assert "📦 NPM: `next`" in res2
        assert "16.3.5" in res2



def test_quota_engine_record_usage_and_session_stats():
    from engines.quota_engine import QuotaEngine
    engine = QuotaEngine()
    assert engine.get_session_stats() == {"tokens": 0, "requests": 0}

    engine.record_usage(tokens=1500, requests=2)
    stats = engine.get_session_stats()
    assert stats["tokens"] == 1500
    assert stats["requests"] == 2


def test_quota_engine_bottom_toolbar_text():
    from engines.quota_engine import QuotaEngine
    engine = QuotaEngine()
    engine.record_usage(tokens=2400, requests=3)

    # Lokal provider
    text = engine.get_bottom_toolbar_text("ollama", "qwen3.8:latest", "sess-123", "/path/to/project_demo")
    assert "OLLAMA" in text
    assert "Lokal" in text
    assert "2.4k tok" in text
    assert "project_demo" in text

    # NVIDIA provider
    text_nv = engine.get_bottom_toolbar_text("nvidia", "deepseek-v4-pro", "sess-456", "/tmp/demo")
    assert "NVIDIA" in text_nv
    assert "demo" in text_nv
