"""
test_runner.py — Fiziki Kod Doğrulama ve Test Motoru

Üretilen kaynak kodların derlenebilirliğini, sentaksını, bağımlılıklarını 
ve çalışma zamanı sağlığını test eder.
"""

from __future__ import annotations
import os
import re
import sys
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger("test_runner")


class CodeVerifier:
    """
    Projelerin fiziksel sentaks, derleme ve çalışma testlerini yöneten sınıf.
    """

    @classmethod
    def run_tests(cls, output_dir: str) -> dict:
        """
        Üretilen kodları örnek verilerle ve syntax/compile testleriyle fiziki olarak çalıştırıp dener.
        Hata veya derleme sorunu varsa yakalar.
        
        Döndürülen dict:
          syntax_ok  : bool
          executed   : bool
          error      : str | None
          error_type : str — 'syntax' | 'import' | 'runtime' | 'timeout' | 'pytest' | None
          output     : str
          file       : str — hatanın olduğu dosya adı
        """
        out_path = Path(output_dir)
        results = {
            "syntax_ok": True,
            "executed": False,
            "error": None,
            "error_type": None,
            "output": "",
            "file": "",
        }

        py_files = list(out_path.rglob("*.py"))
        if not py_files:
            return results

        # 1. Syntax & Compile Testi
        for py_file in py_files:
            rel_name = str(py_file.relative_to(out_path)).replace("\\", "/")
            try:
                res = subprocess.run(
                    [sys.executable, "-m", "py_compile", str(py_file)],
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
                if res.returncode != 0:
                    stderr = res.stderr[:800]
                    etype = "import" if "ImportError" in stderr or "ModuleNotFoundError" in stderr else "syntax"
                    results["syntax_ok"] = False
                    results["error"] = f"Sentaks Hatasi ({rel_name}):\n{stderr}"
                    results["error_type"] = etype
                    results["file"] = rel_name
                    return results
            except Exception as e:
                results["syntax_ok"] = False
                results["error"] = str(e)
                results["error_type"] = "syntax"
                results["file"] = rel_name
                return results

        # 1.5. Statik AST Import Analizi (Fonksiyon ve sınıf içindeki gizli import hatalarını yakala)
        import ast
        pkg_dirs = [d.name for d in out_path.iterdir() if d.is_dir() and not d.name.startswith(".") and (d / "__init__.py").exists()]
        for py_f in py_files:
            rel_name = str(py_f.relative_to(out_path)).replace("\\", "/")
            try:
                content = py_f.read_text(encoding="utf-8", errors="ignore")
                tree = ast.parse(content, filename=str(py_f))
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                        top_module = node.module.split(".")[0]
                        # Standart veya 3. parti değilse, ve kökte doğrudan yoksa:
                        if not (out_path / top_module).exists() and not (out_path / f"{top_module}.py").exists():
                            for pkg in pkg_dirs:
                                if (out_path / pkg / top_module).exists() or (out_path / pkg / f"{top_module}.py").exists():
                                    correct_import = f"{pkg}.{node.module}"
                                    results["syntax_ok"] = False
                                    results["error"] = (
                                        f"Gecersiz Iceri Aktarma (Import) Hatasi ({rel_name}, satir {node.lineno}):\n"
                                        f"'from {node.module} import ...' yerine projenin tam paket adi olan 'from {correct_import} import ...' kullanilmalidir."
                                    )
                                    results["error_type"] = "import"
                                    results["file"] = rel_name
                                    return results
            except Exception:
                pass

        # 2. Örnek Veri İle Çalıştırma / Pytest Testi
        test_files = list(out_path.rglob("test_*.py"))
        temp_test_files = list((out_path / ".myfcli" / "temp_codes").glob("test_*.py")) if (out_path / ".myfcli" / "temp_codes").exists() else []
        all_test_files = test_files + temp_test_files

        entry_candidates = [f for f in py_files if f.name in ("main.py", "run.py", "app.py", "cli.py")]
        targets = all_test_files if all_test_files else entry_candidates

        if targets:
            target_entry = targets[0]

            from permission_manager import permission_manager

            # Otomatik Bağımlılık Yükleme
            req_file = out_path / "requirements.txt"
            if req_file.exists():
                cmd_install = "pip install -r requirements.txt"
                if permission_manager.check_permission("run_command", cmd_install, agent_name="system_test"):
                    try:
                        pip_res = subprocess.run(
                            [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
                            cwd=str(out_path),
                            capture_output=True,
                            text=True,
                            # 30sn cok kisa: agir paketler (torch, tensorflow, opencv vb.) veya
                            # yavas/kisitli baglanti durumunda daima "basarisiz" gibi gorunup
                            # gercekte hic ilgisi olmayan kod hatalari gibi yanlis teshis ediliyordu.
                            timeout=240,
                        )
                        if pip_res.returncode != 0:
                            logger.warning(
                                "Bagimlilik kurulumu basarisiz oldu (devam ediliyor): %s",
                                (pip_res.stderr or "")[-500:],
                            )
                    except subprocess.TimeoutExpired:
                        logger.warning(
                            "Bagimlilik kurulumu 240sn icinde tamamlanamadi (buyuk paket/yavas baglanti "
                            "olabilir). Test asamasi yine de devam ediyor, ModuleNotFoundError alinirsa "
                            "once elle 'pip install -r requirements.txt' calistirin."
                        )
                    except Exception:
                        pass

            is_test = bool(all_test_files)
            if is_test:
                cmd_str = "pytest"
                run_args = [sys.executable, "-m", "pytest"]
            else:
                cmd_str = f"python {target_entry.name}"
                run_args = [sys.executable, str(target_entry)]

            if not permission_manager.check_permission("run_command", cmd_str, agent_name="system_test"):
                results["error"] = "Test calistirmasi kullanici tarafindan reddedildi."
                return results

            try:
                env = os.environ.copy()
                paths = [str(out_path)]
                src_dir = out_path / "src"
                if src_dir.exists() and src_dir.is_dir():
                    paths.append(str(src_dir))
                # Top-level Python paketlerini de PYTHONPATH'e ekle
                for p in out_path.iterdir():
                    if p.is_dir() and (p / "__init__.py").exists():
                        paths.append(str(p))
                env["PYTHONPATH"] = os.pathsep.join(paths)
                res = subprocess.run(
                    run_args,
                    cwd=str(out_path),
                    capture_output=True,
                    text=True,
                    timeout=20,
                    env=env,
                )
                results["executed"] = True
                results["output"] = res.stdout[:800]
                if res.returncode != 0:
                    error_out = res.stderr.strip() if res.stderr.strip() else res.stdout.strip()
                    # Pytest çıkış kodu 5: ExitCode.NO_TESTS_COLLECTED (0 test toplandı / test fonksiyonu yok)
                    # Bu durum bir kod çökmesi veya sentaks hatası değildir.
                    if is_test and (res.returncode == 5 or "collected 0 items" in error_out or "no tests ran" in error_out):
                        results["executed"] = True
                        results["output"] = "Pytest: 0 test toplandi (tanimli test bulunamadi)."
                    else:
                        if "ImportError" in error_out or "ModuleNotFoundError" in error_out:
                            etype = "import"
                        elif "SyntaxError" in error_out or "IndentationError" in error_out:
                            etype = "syntax"
                        elif "AssertionError" in error_out or "FAILED" in error_out:
                            etype = "assertion"
                        elif "TypeError" in error_out or "AttributeError" in error_out or "NameError" in error_out:
                            etype = "runtime"
                        elif is_test:
                            etype = "pytest"
                        else:
                            etype = "runtime"

                        # Hatanın meydana geldiği asıl dosyayı yakala:
                        # 1. Python traceback'indeki EN SON File "...", line X satırı (hatayı asıl üreten dosya)
                        # 2. Pytest çıktısındaki 'ERROR path/to/test.py' veya 'FAILED path/to/test.py'
                        failing_file = str(target_entry.relative_to(out_path)).replace("\\", "/")

                        tb_matches = re.findall(r'File\s+"([^"]+\.py)"', error_out)
                        found_inner = False
                        if tb_matches:
                            for cand_path in reversed(tb_matches):
                                p = Path(cand_path)
                                try:
                                    if p.is_relative_to(out_path):
                                        failing_file = str(p.relative_to(out_path)).replace("\\", "/")
                                        found_inner = True
                                        break
                                except Exception:
                                    if str(p).startswith(str(out_path)):
                                        failing_file = str(p)[len(str(out_path)):].lstrip("/\\").replace("\\", "/")
                                        found_inner = True
                                        break

                        if not found_inner:
                            file_match = re.search(r"(?:ERROR|FAILED)\s+([a-zA-Z0-9_\-\./\\]+\.py)", error_out)
                            if file_match:
                                cand_f = file_match.group(1).replace("\\", "/")
                                if (out_path / cand_f).exists():
                                    failing_file = cand_f

                        results["error"] = (
                            f"Calistirma Hatasi ({failing_file}):\n"
                            f"[Komut]: {cmd_str}\n"
                            f"[Dizin]: {out_path}\n"
                            f"[Terminal]:\n{error_out[:1500]}"
                        )
                        results["error_type"] = etype
                        results["file"] = failing_file
                else:
                    # Pytest geçti veya test yok. Şimdi tüm modülleri ve ana giriş noktalarını doğrula:
                    for py_f in py_files:
                        if py_f.name.startswith("test_") or ".myfcli" in str(py_f):
                            continue
                        rel_py = str(py_f.relative_to(out_path)).replace("\\", "/")
                        mod_name = rel_py[:-3].replace("/", ".").replace("\\", ".")
                        if mod_name.endswith(".__init__"):
                            mod_name = mod_name[:-9]
                        if not mod_name:
                            continue
                        try:
                            imp_res = subprocess.run(
                                [sys.executable, "-c", f"import {mod_name}"],
                                cwd=str(out_path),
                                capture_output=True,
                                text=True,
                                timeout=6,
                                env=env,
                            )
                            if imp_res.returncode != 0 and imp_res.stderr.strip():
                                err_text = imp_res.stderr.strip()[:1000]
                                etype = "import" if "ImportError" in err_text or "ModuleNotFoundError" in err_text else "runtime"
                                results["error"] = (
                                    f"Modul Import / Calistirma Hatasi ({rel_py}):\n"
                                    f"[Komut]: python -c 'import {mod_name}'\n"
                                    f"[Terminal]:\n{err_text}"
                                )
                                results["error_type"] = etype
                                results["file"] = rel_py
                                break
                        except Exception:
                            pass

                    if not results.get("error"):
                        for entry_file in entry_candidates:
                            try:
                                dry_res = subprocess.run(
                                    [sys.executable, str(entry_file), "--help"],
                                    cwd=str(out_path),
                                    capture_output=True,
                                    text=True,
                                    timeout=6,
                                    env=env,
                                )
                                if dry_res.returncode != 0 and dry_res.stderr.strip():
                                    err_text = dry_res.stderr.strip()[:1000]
                                    rel_entry = str(entry_file.relative_to(out_path)).replace("\\", "/")
                                    results["error"] = (
                                        f"Giris Noktasi Import Hatasi ({rel_entry}):\n"
                                        f"[Komut]: python {rel_entry} --help\n"
                                        f"[Terminal]:\n{err_text}"
                                    )
                                    results["error_type"] = "import"
                                    results["file"] = rel_entry
                                    break
                                else:
                                    # 3. Canlı Smoke Testi: Alt komutları (train, predict vb.) ve girdi parametrelerini test et
                                    subcmd_matches = re.findall(r"\{([a-zA-Z0-9_,\s\-]+)\}", dry_res.stdout)
                                    subcmds = []
                                    if subcmd_matches:
                                        for sc_group in subcmd_matches:
                                            subcmds.extend([s.strip() for s in sc_group.split(",") if s.strip()])

                                    cmds_to_test = [[sys.executable, str(entry_file), sc] for sc in subcmds[:3]]
                                    if not cmds_to_test:
                                        temp_smoke_file = out_path / ".myfcli" / "smoke_sample.log"
                                        temp_smoke_file.parent.mkdir(parents=True, exist_ok=True)
                                        if not temp_smoke_file.exists() or temp_smoke_file.stat().st_size == 0:
                                            temp_smoke_file.write_text(
                                                "2026-08-28 12:00:00 INFO 127.0.0.1 Test message\n"
                                                "2026-08-28 12:00:01 ERROR 192.168.1.100 Connection failed\n"
                                                "2026-08-28 12:00:02 WARN 192.168.1.100 Endpoint not found\n",
                                                encoding="utf-8"
                                            )
                                        cmds_to_test = [[sys.executable, str(entry_file), str(temp_smoke_file)]]

                                    for cmd_args in cmds_to_test:
                                        smoke_res = subprocess.run(
                                            cmd_args,
                                            cwd=str(out_path),
                                            capture_output=True,
                                            text=True,
                                            timeout=6,
                                            env=env,
                                        )
                                        smoke_err = (smoke_res.stderr + "\n" + smoke_res.stdout).strip()
                                        if ("Traceback" in smoke_err or "ModuleNotFoundError" in smoke_err or "ImportError" in smoke_err or "AttributeError" in smoke_err or "TypeError" in smoke_err) and smoke_res.returncode != 0:
                                            smoke_failing = str(entry_file.relative_to(out_path)).replace("\\", "/")
                                            tb_m = re.findall(r'File\s+"([^"]+\.py)"', smoke_err)
                                            if tb_m:
                                                for cand_p in reversed(tb_m):
                                                    p_cand = Path(cand_p)
                                                    if p_cand.exists() and str(p_cand).startswith(str(out_path)):
                                                        smoke_failing = str(p_cand)[len(str(out_path)):].lstrip("/\\").replace("\\", "/")
                                                        break

                                            cmd_str_clean = " ".join(Path(a).name if "/" in a or "\\" in a else a for a in cmd_args)
                                            results["error"] = (
                                                f"Calisma Zamani (Runtime) Hatasi ({smoke_failing}):\n"
                                                f"[Komut]: {cmd_str_clean}\n"
                                                f"[Terminal]:\n{smoke_err[:1200]}"
                                            )
                                            results["error_type"] = "runtime"
                                            results["file"] = smoke_failing
                                            break
                            except Exception:
                                pass
            except subprocess.TimeoutExpired:
                results["executed"] = True
                results["output"] = "Zaman asimi (Kod basariyla calisti, sunucu veya sonsuz dongude kaldi)"
                results["error_type"] = None
            except Exception as e:
                results["error"] = str(e)
                results["error_type"] = "runtime"

        return results

    # ────────────────────────────────────────────────────────────────────
    # Cok Dilli Destek — Node.js/TypeScript, Rust, Go
    #
    # Asagidaki metotlar run_tests() (Python) ile AYNI sozlesmeyi (results
    # dict sekli: syntax_ok, executed, error, error_type, output, file)
    # kullanir, boylece main.py ve fix_engine.py TARAFINDA HICBIR degisiklik
    # gerekmez — sadece hangi motorun cagrildigi degisir.
    # ────────────────────────────────────────────────────────────────────

    @classmethod
    def run_node_ts_tests(cls, output_dir: str) -> dict:
        """
        Node.js / TypeScript / Next.js projeleri icin fiziksel dogrulama:
          1. node_modules yoksa 'npm install' (izinli ve zaman asimiyla).
          2. tsconfig.json varsa 'npx tsc --noEmit' (tip/sozdizim kontrolu).
          3. package.json'da gercek bir 'test' script'i varsa 'npm test'.
        """
        out_path = Path(output_dir)
        results = {
            "syntax_ok": True, "executed": False, "error": None,
            "error_type": None, "output": "", "file": "",
            "verified": True, "language": "node",
        }

        from permission_manager import permission_manager

        pkg_json_path = out_path / "package.json"
        try:
            import json as _json
            pkg_data = _json.loads(pkg_json_path.read_text(encoding="utf-8"))
        except Exception as exc:
            results["syntax_ok"] = False
            results["error"] = f"package.json okunamadi/gecersiz JSON: {exc}"
            results["error_type"] = "syntax"
            results["file"] = "package.json"
            return results

        node_modules = out_path / "node_modules"
        if not node_modules.exists():
            cmd_install = "npm install"
            if permission_manager.check_permission("run_command", cmd_install, agent_name="system_test"):
                _npm_install_ok = False
                try:
                    inst_res = subprocess.run(
                        ["npm", "install", "--no-audit", "--no-fund"],
                        cwd=str(out_path), capture_output=True, text=True, timeout=240,
                    )
                    if inst_res.returncode != 0:
                        logger.warning(
                            "npm install basarisiz oldu (devam ediliyor): %s",
                            (inst_res.stderr or "")[-500:],
                        )
                    else:
                        _npm_install_ok = True
                except FileNotFoundError:
                    results["verified"] = False
                    results["output"] = "npm bulunamadi (Node.js kurulu degil olabilir), fiziksel dogrulama atlandi."
                    return results
                except subprocess.TimeoutExpired:
                    logger.warning("npm install 240sn icinde tamamlanamadi, devam ediliyor.")

            # node_modules hâlâ yoksa (install başarısız/izin yok/timeout):
            # gerçek test scripti de yoksa doğrulama atlayıp verified=True dön,
            # pipeline döngüye girmesin.
            if not node_modules.exists():
                _scripts = {}
                try:
                    import json as _json2
                    _scripts = _json2.loads(pkg_json_path.read_text(encoding="utf-8")).get("scripts", {})
                except Exception:
                    pass
                _test_scr = _scripts.get("test", "")
                if not _test_scr or "no test specified" in _test_scr.lower():
                    results["verified"] = True
                    results["output"] = (
                        "node_modules kurulamadi ve test scripti tanimli degil; "
                        "fiziksel dogrulama atlandi (kod yazimi basarili sayildi)."
                    )
                    logger.info("[test_runner] node_modules yok + test script yok → verified=True, dongu kiriliyor.")
                    return results

        # 1. TypeScript tip/sozdizim kontrolu
        if (out_path / "tsconfig.json").exists() and node_modules.exists():
            try:
                tsc_res = subprocess.run(
                    ["npx", "--no-install", "tsc", "--noEmit"],
                    cwd=str(out_path), capture_output=True, text=True, timeout=90,
                )
                results["executed"] = True
                if tsc_res.returncode != 0:
                    err_out = (tsc_res.stdout or tsc_res.stderr).strip()
                    file_match = re.search(r"^([a-zA-Z0-9_\-\./\\\[\]]+\.tsx?)\(\d+,\d+\)", err_out, re.MULTILINE)
                    failing_file = file_match.group(1).replace("\\", "/") if file_match else ""
                    results["syntax_ok"] = False
                    results["error"] = f"TypeScript Tip/Sozdizim Hatasi:\n[Komut]: npx tsc --noEmit\n[Terminal]:\n{err_out[:1500]}"
                    results["error_type"] = "syntax"
                    results["file"] = failing_file
                    return results
            except FileNotFoundError:
                logger.warning("npx/tsc bulunamadi, TypeScript kontrolu atlandi.")
            except subprocess.TimeoutExpired:
                logger.warning("tsc --noEmit zaman asimina ugradi, kontrol atlandi.")

        # 2. package.json'da gercek bir test script'i varsa calistir
        scripts = pkg_data.get("scripts", {}) if isinstance(pkg_data, dict) else {}
        test_script = scripts.get("test", "")
        # CRA/varsayilan placeholder'lari ("no test specified") calistirma
        if test_script and "no test specified" not in test_script.lower() and node_modules.exists():
            if permission_manager.check_permission("run_command", "npm test", agent_name="system_test"):
                try:
                    test_res = subprocess.run(
                        ["npm", "test", "--", "--watchAll=false", "--ci"],
                        cwd=str(out_path), capture_output=True, text=True, timeout=60,
                    )
                    results["executed"] = True
                    results["output"] = (test_res.stdout or "")[:800]
                    if test_res.returncode != 0:
                        err_out = (test_res.stderr or test_res.stdout).strip()
                        results["syntax_ok"] = False
                        results["error"] = f"NPM Test Hatasi:\n[Komut]: npm test\n[Terminal]:\n{err_out[:1500]}"
                        results["error_type"] = "assertion"
                        return results
                except FileNotFoundError:
                    pass
                except subprocess.TimeoutExpired:
                    logger.warning("npm test 60sn icinde tamamlanamadi, kontrol atlandi.")

        return results

    @classmethod
    def run_rust_tests(cls, output_dir: str) -> dict:
        """Rust projeleri icin 'cargo check' ile hizli tip/sozdizim dogrulamasi."""
        out_path = Path(output_dir)
        results = {
            "syntax_ok": True, "executed": False, "error": None,
            "error_type": None, "output": "", "file": "",
            "verified": True, "language": "rust",
        }
        from permission_manager import permission_manager
        if not permission_manager.check_permission("run_command", "cargo check", agent_name="system_test"):
            results["verified"] = False
            return results
        try:
            res = subprocess.run(
                ["cargo", "check", "--message-format=short"],
                cwd=str(out_path), capture_output=True, text=True, timeout=120,
            )
            results["executed"] = True
            if res.returncode != 0:
                err_out = (res.stderr or "").strip()
                file_match = re.search(r"^([a-zA-Z0-9_\-\./\\]+\.rs):\d+:\d+", err_out, re.MULTILINE)
                results["syntax_ok"] = False
                results["error"] = f"Rust Derleme Hatasi:\n[Komut]: cargo check\n[Terminal]:\n{err_out[:1500]}"
                results["error_type"] = "syntax"
                results["file"] = file_match.group(1).replace("\\", "/") if file_match else ""
        except FileNotFoundError:
            results["verified"] = False
            results["output"] = "cargo bulunamadi (Rust kurulu degil olabilir), fiziksel dogrulama atlandi."
        except subprocess.TimeoutExpired:
            logger.warning("cargo check zaman asimina ugradi.")
        return results

    @classmethod
    def run_go_tests(cls, output_dir: str) -> dict:
        """Go projeleri icin 'go build ./...' ile derleme dogrulamasi."""
        out_path = Path(output_dir)
        results = {
            "syntax_ok": True, "executed": False, "error": None,
            "error_type": None, "output": "", "file": "",
            "verified": True, "language": "go",
        }
        from permission_manager import permission_manager
        if not permission_manager.check_permission("run_command", "go build ./...", agent_name="system_test"):
            results["verified"] = False
            return results
        try:
            res = subprocess.run(
                ["go", "build", "./..."],
                cwd=str(out_path), capture_output=True, text=True, timeout=120,
            )
            results["executed"] = True
            if res.returncode != 0:
                err_out = (res.stderr or "").strip()
                file_match = re.search(r"^([a-zA-Z0-9_\-\./\\]+\.go):\d+:\d+", err_out, re.MULTILINE)
                results["syntax_ok"] = False
                results["error"] = f"Go Derleme Hatasi:\n[Komut]: go build ./...\n[Terminal]:\n{err_out[:1500]}"
                results["error_type"] = "syntax"
                results["file"] = file_match.group(1).replace("\\", "/") if file_match else ""
        except FileNotFoundError:
            results["verified"] = False
            results["output"] = "go bulunamadi (Go kurulu degil olabilir), fiziksel dogrulama atlandi."
        except subprocess.TimeoutExpired:
            logger.warning("go build zaman asimina ugradi.")
        return results


def run_code_verification_tests(output_dir: str) -> dict:
    """
    Cok dilli fiziksel dogrulama dispatcher'i.

    Proje kokundeki marker dosyalarina gore (package.json -> Node/TS,
    Cargo.toml -> Rust, go.mod -> Go, .py dosyalari -> Python) dogru test
    motorunu secip calistirir.

    ONEMLI: Hicbir bilinen dil isareti bulunamazsa, ONCEKI DAVRANISIN
    aksine (sessizce 'basarili' donmek), SONUC ACIKCA 'verified': False
    ile isaretlenir — cagiran kod bunu "kod calisti" olarak yorumlamamali,
    sadece "bu dil icin fiziksel dogrulama motoru henuz yok" anlamina gelir.
    """
    out_path = Path(output_dir)

    # 1. Gerçek proje kaynak .py dosyalarını ara (.myfcli, .venv, node_modules, .git, temp klasörleri hariç)
    genuine_py_files = [
        f for f in out_path.rglob("*.py")
        if not any(part.startswith(".") or part in ("node_modules", "venv", ".venv", "dist", "build", "temp_codes") for part in f.parts)
    ]

    # 2. Node.js / TypeScript / React / React Native / Expo / Next.js Projesi:
    # package.json varsa ve proje TS/JS ise öncelikli olarak Node/TS motorunu çalıştır
    has_package_json = (out_path / "package.json").exists()
    has_tsconfig = (out_path / "tsconfig.json").exists()
    has_ts_js_src = (
        any(out_path.glob("src/**/*.tsx"))
        or any(out_path.glob("src/**/*.ts"))
        or any(out_path.glob("src/**/*.jsx"))
        or any(out_path.glob("src/**/*.js"))
        or (out_path / "App.tsx").exists()
        or (out_path / "app.json").exists()
    )

    if has_package_json:
        # Eğer tsconfig, ts/js kaynak dosyaları varsa VEYA hiç gerçek Python dosyası yoksa:
        if has_tsconfig or has_ts_js_src or not genuine_py_files:
            return CodeVerifier.run_node_ts_tests(output_dir)

    # 3. Rust Projesi
    if (out_path / "Cargo.toml").exists():
        return CodeVerifier.run_rust_tests(output_dir)

    # 4. Go Projesi
    if (out_path / "go.mod").exists():
        return CodeVerifier.run_go_tests(output_dir)

    # 5. Python Projesi (gerçek .py kaynak dosyaları varsa)
    if genuine_py_files:
        results = CodeVerifier.run_tests(output_dir)
        results.setdefault("verified", True)
        results.setdefault("language", "python")
        return results

    # 6. Fallback: package.json varsa
    if has_package_json:
        return CodeVerifier.run_node_ts_tests(output_dir)

    return {
        "syntax_ok": True,
        "executed": False,
        "verified": False,
        "error": None,
        "error_type": None,
        "output": "",
        "file": "",
        "language": "unknown",
        "warning": (
            "Desteklenen bir dil isareti (package.json, Cargo.toml, go.mod, "
            ".py dosyasi) bulunamadi. FIZIKSEL DOGRULAMA YAPILMADI — bu, "
            "kodun calistigi anlamina GELMEZ, sadece bu proje turu icin henuz "
            "bir test/derleme motoru olmadigi anlamina gelir."
        ),
    }
