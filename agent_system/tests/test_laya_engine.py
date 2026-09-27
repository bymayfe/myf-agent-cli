"""
test_laya_engine.py — Laya Karar Motoru Birim ve Entegrasyon Testleri
"""

import pytest
from engines.laya_engine import LayaDecisionEngine, get_laya_engine


def test_laya_engine_singleton():
    engine1 = get_laya_engine()
    engine2 = get_laya_engine()
    assert engine1 is engine2


def test_laya_engine_classify_fallback():
    engine = LayaDecisionEngine(enabled=True)
    choices = ["python_pytest", "node_npm", "rust_cargo"]
    context = "Running pytest on tests/test_app.py ... FAILED"
    
    selected, conf, ms = engine.classify(context, choices)
    assert selected == "python_pytest"
    assert conf > 0.0
    assert ms >= 0.0


def test_laya_engine_score_fallback():
    engine = LayaDecisionEngine(enabled=True)
    context = "Critical Exception: segmentation fault at 0x004"
    score_val, ms = engine.score(context, scale=(1, 10))
    assert 1.0 <= score_val <= 10.0
    assert ms >= 0.0


def test_laya_engine_decide_boolean_fallback():
    engine = LayaDecisionEngine(enabled=True)
    context = "Failed test again. Repeating same error. repeat failed."
    question = "Is the agent stuck in a loop?"
    
    is_loop, prob, ms = engine.decide_boolean(context, question)
    assert isinstance(is_loop, bool)
    assert 0.0 <= prob <= 1.0


def test_laya_classify_error_categories():
    engine = LayaDecisionEngine(enabled=True)
    
    # 1. Syntax Error
    res_syntax = engine.classify_error("SyntaxError: invalid syntax line 44")
    assert res_syntax["category"] == "syntax_error"
    assert res_syntax["suggested_action"] == "micro_fix"
    assert res_syntax["confidence"] >= 0.9
    
    # 2. Missing Dependency
    res_dep = engine.classify_error("ModuleNotFoundError: No module named 'fastapi'")
    assert res_dep["category"] == "missing_dependency"
    assert res_dep["suggested_action"] == "package_or_env_fix"

    # 3. Logic Bug
    res_logic = engine.classify_error("AssertionError: assert 5 == 10")
    assert res_logic["category"] == "logic_bug"
    assert res_logic["suggested_action"] == "targeted_refactor"


def test_laya_evaluate_loop_risk():
    engine = LayaDecisionEngine(enabled=True)
    sig1 = "sha256_abc123"
    recent = [sig1, "sha256_def456", sig1]
    
    # Same error signature twice in recent history
    is_loop, conf = engine.evaluate_loop_risk("error text", recent, sig1)
    assert is_loop is True
    assert conf == 1.0


def test_fix_engine_with_laya_diagnosis(monkeypatch, tmp_path):
    from engines.fix_engine import Fix, FixStage
    
    fix = Fix(model="mock_model")
    fix.reset_state()
    
    # MicroFix.run'ı mock'la
    recorded_diagnosis = {}
    def mock_micro_run(*args, **kwargs):
        recorded_diagnosis.update(kwargs.get("laya_diagnosis", {}))
        return True

    monkeypatch.setattr(fix.micro, "run", mock_micro_run)
    
    err_text = "SyntaxError: unexpected EOF while parsing at line 12"
    ok, written = fix.repair(
        error_log=err_text,
        target_file="test_code.py",
        output_dir=str(tmp_path),
        context={},
        run_id="run_test",
        step_id="step_test",
    )
    
    assert ok is True
    assert written == ["test_code.py"]
    assert recorded_diagnosis.get("category") == "syntax_error"
    assert recorded_diagnosis.get("suggested_action") == "micro_fix"


def test_laya_evaluate_qa_report():
    engine = LayaDecisionEngine(enabled=True)

    # 1. Raporda "Import Errors: time" geçiyor ama en sonda "STATUS: PASSED" var
    report_passed = (
        "## 1. Özet\n"
        "Kod incelendi. Tüm testler geçti.\n"
        "## 2. Sorunlar\n"
        "- Import Errors: `time` ve `sqlite3` kütüphaneleri requirements.txt içinde yok.\n"
        "## STATUS: PASSED\n"
    )
    res_passed = engine.evaluate_qa_report(report_passed)
    assert res_passed["passed"] is True
    assert "status_passed" in res_passed["reason"] or "builtins" in res_passed["reason"]

    # 2. Açıkça başarısız olan rapor
    report_failed = (
        "## 1. Özet\n"
        "Testler çalıştırılamadı.\n"
        "## STATUS: FAILED\n"
    )
    res_failed = engine.evaluate_qa_report(report_failed)
    assert res_failed["passed"] is False


def test_extract_planned_files_strips_project_root():
    from engines.code_parser import extract_planned_files_from_architecture

    arch_text = """
```
project_root/
├── src/
│   ├── main.py
│   └── database.py
├── tests/
│   └── test_main.py
└── requirements.txt
```
"""
    files = extract_planned_files_from_architecture(arch_text)
    assert "src/main.py" in files
    assert "src/database.py" in files
    assert "tests/test_main.py" in files
    assert "requirements.txt" in files
    # project_root/ öneki asla dosya adında kalmamalı
    assert not any(f.startswith("project_root") for f in files)


def test_laya_extract_pinpoint_diagnostic(tmp_path):
    engine = LayaDecisionEngine(enabled=True)

    # Örnek dosya oluştur
    test_file = tmp_path / "test_module.py"
    test_file.write_text(
        "import sys\n"
        "import os\n"
        "from ..service.bad import BadClass\n"
        "def test_func():\n"
        "    pass\n"
    )

    err_trace = (
        "Traceback:\n"
        "  File 'test_module.py', line 3, in <module>\n"
        "    from ..service.bad import BadClass\n"
        "ImportError: attempted relative import with no known parent package\n"
    )

    diag = engine.extract_pinpoint_diagnostic(
        error_log=err_trace,
        project_dir=str(tmp_path),
        history_note="1. deneme başarısız oldu.",
    )

    assert diag["file"] == "test_module.py"
    assert diag["line"] == 3
    assert "ImportError" in diag["error_msg"]
    assert "Satır 03: from ..service.bad import BadClass" in diag["diagnostic_card"]
    assert "1. deneme başarısız oldu." in diag["diagnostic_card"]


def test_laya_create_focused_retry_context(tmp_path):
    engine = LayaDecisionEngine(enabled=True)

    full_code = (
        "# filepath: config.py\nDEBUG=True\n\n"
        "# filepath: utils.py\ndef helper(): return 1\n\n"
        "# filepath: service/app.py\nclass App: pass\n\n"
        "# filepath: tests/test_app.py\ndef test_app(): assert 1 == 2\n"
    )

    err_log = "tests/test_app.py:1: in test_app\nassert 1 == 2\nAssertionError: assert 1 == 2"

    focused = engine.create_focused_retry_context(
        error_log=err_log,
        full_code_files=full_code,
        project_dir=str(tmp_path),
        retry_count=2,
    )

    assert "tests/test_app.py" in focused
    assert "NOKTA ATIŞI HATA TEŞHİSİ" in focused
    # Unrelated files like config.py and utils.py should be pruned out
    assert "DEBUG=True" not in focused
    # retry_count >= 2 should include STRATEJİ MÜDAHALESİ — DÖNGÜ KIRICI
    assert "STRATEJİ MÜDAHALESİ" in focused


def test_laya_generate_loop_breaking_intervention(tmp_path):
    engine = LayaDecisionEngine(enabled=True)

    # Dataclass argument order error
    err_dataclass = (
        "habit_tracker/tests/test_cli.py:2: in <module>\n"
        "    from habit_tracker.cli import main\n"
        "habit_tracker/models.py:5: in <module>\n"
        "    @dataclass\n"
        "/usr/lib/python3.14/dataclasses.py:687: in _init_fn\n"
        "E   TypeError: non-default argument 'name' follows default argument 'id'\n"
    )

    models_f = tmp_path / "habit_tracker" / "models.py"
    models_f.parent.mkdir(parents=True, exist_ok=True)
    models_f.write_text("@dataclass\nclass Habit:\n    id: int = None\n    name: str\n")

    diag = engine.extract_pinpoint_diagnostic(err_dataclass, project_dir=str(tmp_path))
    # Should resolve to habit_tracker/models.py, NOT test_cli.py or /usr/lib
    assert diag["file"] == "habit_tracker/models.py"
    assert diag["line"] == 5

    card = engine.generate_loop_breaking_intervention(err_dataclass, retry_count=2, project_dir=str(tmp_path))
    assert "STRATEJİ MÜDAHALESİ" in card
    assert "DATACLASS" in card
    assert "habit_tracker/models.py" in card


def test_laya_determine_repair_strategy(tmp_path):
    engine = LayaDecisionEngine(enabled=True)

    # 1. Dataclass error -> MUST be full_file
    res_dc = engine.determine_repair_strategy("TypeError: non-default argument 'name' follows default argument 'id'")
    assert res_dc["strategy"] == "full_file"
    assert "dataclass" in res_dc["reason"]

    # 2. ModuleNotFoundError -> MUST be micro_fix (kullanıcı kuralı: micro-fix sentaks ve import hatalarında uygulanır)
    res_mod = engine.determine_repair_strategy("ModuleNotFoundError: No module named 'services.task'")
    assert res_mod["strategy"] == "micro_fix"
    assert "import" in res_mod["reason"]

    # 3. Simple syntax error -> micro_fix
    res_syn = engine.determine_repair_strategy("SyntaxError: invalid syntax at line 10")
    assert res_syn["strategy"] == "micro_fix"





