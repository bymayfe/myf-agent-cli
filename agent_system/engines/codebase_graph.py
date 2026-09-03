"""
codebase_graph.py — DeusData/codebase-memory-mcp Tabanlı Bilgi Grafiği ve Kod Zekası Motoru

Özellikler:
  - Tek statik ikili (codebase-memory-mcp.exe) ile JSON-RPC (MCP) üzerinden iletişim kurar.
  - Sub-ms hızında AST ve sembol grafiği üretir, SQLite tabanlı grafik saklar.
  - Sınıf/fonksiyon çağrı yolları (trace_path), mimari özet ve sembol araması sunar.
  - %99 token tasarrufu sağlar.
  - Eski repomap_engine.py için geriye dönük tam uyumlu build_repomap() fonksiyonu içerir.
"""

from __future__ import annotations
import os
import sys
import json
import logging
import threading
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

import shutil

_THIS_DIR = Path(__file__).parent
_PROJECT_ROOT = _THIS_DIR.parent

def _resolve_binary_path() -> Optional[Path]:
    """Windows, Linux ve macOS için uygun codebase-memory-mcp binary yolunu bulur."""
    cb_dir = _PROJECT_ROOT / "third_party" / "codebase_memory"
    candidates = [
        cb_dir / ("codebase-memory-mcp.exe" if sys.platform == "win32" else "codebase-memory-mcp"),
        cb_dir / "codebase-memory-mcp.exe",
        cb_dir / "codebase-memory-mcp",
    ]
    # Sistem PATH'inde var mı?
    which_p = shutil.which("codebase-memory-mcp")
    if which_p:
        candidates.append(Path(which_p))

    for c in candidates:
        if c.exists() and (c.is_file() or not c.is_dir()):
            return c
    return None

_BINARY_PATH = _resolve_binary_path()


def _readline_with_timeout(proc: Optional[subprocess.Popen], timeout: float = 3.0) -> Optional[str]:
    """Process stdout'undan belirtilen timeout süresi içinde tek bir satır okur (asla kilitlenmez)."""
    if proc is None or proc.stdout is None:
        return None
    try:
        if sys.platform != "win32":
            import select
            r, _, _ = select.select([proc.stdout], [], [], timeout)
            if r:
                return proc.stdout.readline()
            return None
        else:
            res = [None]
            def _reader():
                try:
                    res[0] = proc.stdout.readline()
                except Exception:
                    pass
            t = threading.Thread(target=_reader, daemon=True)
            t.start()
            t.join(timeout)
            return res[0]
    except Exception:
        return None


class CodebaseGraphEngine:
    """Yüksek performanslı Codebase Memory motoru (MCP Client - Cross-Platform)."""

    def __init__(self, binary_path: Optional[Path] = None):
        self.binary_path = binary_path or _BINARY_PATH
        self._proc: Optional[subprocess.Popen] = None
        self._req_id = 0
        self._lock = threading.RLock()
        self._project_cache: Dict[str, str] = {}  # abs_path -> project_name
        self._disabled = False

    def is_available(self) -> bool:
        """Binary dosyası mevcut ve çalışabilir durumda mı?"""
        if self._disabled:
            return False
        return self.binary_path is not None and self.binary_path.exists()

    def _ensure_process(self) -> bool:
        """MCP server sürecinin açık olduğundan emin ol (asla sonsuz bloklanmaz)."""
        if self._disabled or not self.is_available():
            return False

        if self._proc is not None and self._proc.poll() is None:
            return True

        try:
            runtime_dir = Path("/tmp/myfcli_cbm/runtime")
            cache_dir = Path("/tmp/myfcli_cbm/cache")
            runtime_dir.mkdir(parents=True, exist_ok=True)
            cache_dir.mkdir(parents=True, exist_ok=True)

            env = os.environ.copy()
            env["CBM_RUNTIME_DIR"] = str(runtime_dir)
            env["CBM_CACHE_DIR"] = str(cache_dir)

            self._proc = subprocess.Popen(
                [str(self.binary_path), "--ui=false"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                bufsize=1
            )
            # Initialize gönder
            self._req_id += 1
            init_req = {
                "jsonrpc": "2.0",
                "id": self._req_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "myfcli", "version": "2.5"}
                }
            }
            self._proc.stdin.write(json.dumps(init_req) + "\n")
            self._proc.stdin.flush()
            init_resp = _readline_with_timeout(self._proc, timeout=4.0)
            if not init_resp:
                logger.warning("Codebase-memory MCP yanıt vermedi, devre dışı bırakıldı (AST fallback devrede).")
                print("  ⚠️  [Codebase] Codebase-memory MCP servisi yanıt vermedi (kilitli/meşgul). Devre dışı bırakıldı -> Yerel AST Haritası devrede.")
                self.close()
                self._disabled = True
                return False
            return True
        except Exception as exc:
            logger.warning("Codebase-memory MCP başlatılamadı: %s", exc)
            print(f"  ⚠️  [Codebase] Codebase-memory MCP başlatılamadı ({exc}). Devre dışı bırakıldı -> Yerel AST Haritası devrede.")
            self.close()
            self._disabled = True
            return False

    def call_tool(self, name: str, arguments: Dict[str, Any], timeout: int = 10) -> Optional[Any]:
        """MCP tool çağrısı yap (timeout korumalı)."""
        with self._lock:
            if not self._ensure_process() or not self._proc:
                return None

            self._req_id += 1
            req = {
                "jsonrpc": "2.0",
                "id": self._req_id,
                "method": "tools/call",
                "params": {
                    "name": name,
                    "arguments": arguments
                }
            }

            try:
                self._proc.stdin.write(json.dumps(req) + "\n")
                self._proc.stdin.flush()
                raw_line = _readline_with_timeout(self._proc, timeout=float(timeout))
                if not raw_line:
                    logger.warning("MCP tool (%s) zaman aşımına uğradı.", name)
                    self.close()
                    return None
                data = json.loads(raw_line)
                if "error" in data:
                    logger.warning("MCP tool hatası (%s): %s", name, data["error"])
                    return None
                result = data.get("result", {})
                content = result.get("content", [])
                if isinstance(content, list) and content:
                    text_parts = [c.get("text", "") for c in content if isinstance(c, dict)]
                    return "\n".join(text_parts) if text_parts else content
                return result
            except Exception as exc:
                logger.warning("MCP call_tool hatası (%s): %s", name, exc)
                self.close()
                return None

    def close(self):
        """Süreci kapat ve pipe'ları serbest bırak."""
        with self._lock:
            if self._proc is not None:
                try:
                    self._proc.kill()
                    self._proc.communicate(timeout=0.5)
                except Exception:
                    pass
                self._proc = None

    def _get_or_index_project_name(self, project_dir: str) -> Optional[str]:
        """Verilen dizin için MCP proje ismini bul veya indeksle."""
        abs_p = str(Path(project_dir).resolve())
        if abs_p in self._project_cache:
            return self._project_cache[abs_p]

        # Projeleri listele
        res = self.call_tool("list_projects", {})
        if isinstance(res, str):
            try:
                res = json.loads(res)
            except Exception:
                res = {}

        if isinstance(res, dict):
            for p in res.get("projects", []):
                root_path = str(Path(p.get("root_path", "")).resolve())
                if root_path == abs_p:
                    p_name = p.get("name")
                    self._project_cache[abs_p] = p_name
                    return p_name

        # İndeksli değilse indeksle
        idx_res = self.call_tool("index_repository", {"repo_path": abs_p, "mode": "fast"})
        if isinstance(idx_res, str):
            try:
                idx_res = json.loads(idx_res)
            except Exception:
                idx_res = {}

        if isinstance(idx_res, dict) and idx_res.get("project"):
            p_name = idx_res.get("project")
            self._project_cache[abs_p] = p_name
            return p_name

        return None

    def index_repository(self, project_dir: str, mode: str = "fast") -> bool:
        """Projeyi analiz edip bilgi grafiğini günceller."""
        abs_p = str(Path(project_dir).resolve())
        res = self.call_tool("index_repository", {"repo_path": abs_p, "mode": mode})
        if res is not None:
            self._get_or_index_project_name(abs_p)
            return True
        return False

    def search_graph(self, query: str, project_dir: str, label: str = "") -> str:
        """Sembol ve fonksiyon araması yapar."""
        p_name = self._get_or_index_project_name(project_dir)
        args: Dict[str, Any] = {"query": query}
        if p_name:
            args["project"] = p_name
        if label:
            args["label"] = label
        res = self.call_tool("search_graph", args)
        if not res:
            return f"'{query}' için sembol bulunamadı."
        return str(res)

    def trace_call_path(self, symbol: str, project_dir: str, depth: int = 2) -> str:
        """Bir fonksiyonun çağrı zincirini ve ilişkilerini çıkarır."""
        p_name = self._get_or_index_project_name(project_dir)
        args: Dict[str, Any] = {"symbol": symbol, "depth": depth}
        if p_name:
            args["project"] = p_name
        res = self.call_tool("trace_path", args)
        if not res:
            return f"'{symbol}' için çağrı grafiği bulunamadı."
        return str(res)

    def get_code_snippet(self, symbol: str, project_dir: str) -> str:
        """Bir fonksiyon/sınıfın doğrudan kaynak kodunu çeker."""
        p_name = self._get_or_index_project_name(project_dir)
        args: Dict[str, Any] = {"symbol": symbol}
        if p_name:
            args["project"] = p_name
        res = self.call_tool("get_code_snippet", args)
        if not res:
            return f"'{symbol}' kod parçası bulunamadı."
        return str(res)

    def get_architecture(self, project_dir: str) -> str:
        """Projenin modül ve mimari özetini döner."""
        p_name = self._get_or_index_project_name(project_dir)
        if not p_name:
            return ""
        res = self.call_tool("get_architecture", {"project": p_name})
        return str(res) if res else ""

    def invalidate_cache(self, project_dir: Optional[str] = None) -> None:
        """Dosya yazıldığında veya düzeltildiğinde önbelleği geçersiz kılar, haritayı tazeler."""
        if project_dir:
            p_dir = str(Path(project_dir).resolve())
            if hasattr(self, "_repomap_cache"):
                self._repomap_cache.pop(p_dir, None)
            if hasattr(self, "_project_cache"):
                self._project_cache.pop(p_dir, None)
        else:
            if hasattr(self, "_repomap_cache"):
                self._repomap_cache.clear()
            if hasattr(self, "_project_cache"):
                self._project_cache.clear()

    def get_summary_repomap(self, project_dir: str, force_refresh: bool = False) -> str:
        """
        Ajanlar için kompakt ve yüksek kaliteli mimari/harita özeti oluşturur (Taze & Önbellekli).
        """
        p_dir = str(Path(project_dir).resolve())
        p_basename = os.path.basename(p_dir)
        
        if force_refresh:
            self.invalidate_cache(p_dir)

        # Önbellekte varsa ve geçerliyse anında döndür
        if hasattr(self, "_repomap_cache") and p_dir in self._repomap_cache:
            return self._repomap_cache[p_dir]

        if not hasattr(self, "_repomap_cache"):
            self._repomap_cache = {}

        print(f"  🔍 [Codebase] Kod tabanı ve sembol haritası inceleniyor: {p_basename}...")

        # Binary varsa mimariyi çek
        if self.is_available():
            try:
                arch = self.get_architecture(p_dir)
                if arch and len(arch.strip()) > 20 and "error" not in arch.lower():
                    result = f"[Codebase Memory Graph — High Performance]\n{arch}"
                    self._repomap_cache[p_dir] = result
                    print(f"  ✓  [Codebase] Codebase Memory MCP grafiği başarıyla yüklendi.")
                    return result
            except Exception:
                pass

        # Fallback: Basit Python AST Ağacı
        fallback_res = self._ast_fallback(p_dir)
        self._repomap_cache[p_dir] = fallback_res
        total_symbols = fallback_res.count("def ") + fallback_res.count("class ")
        print(f"  ✓  [Codebase] Yerel AST sembol haritası hazırlandı ({total_symbols} sembol çıkarıldı).")
        return fallback_res

    def _ast_fallback(self, project_dir: str) -> str:
        """Python dosyaları için ultra hızlı AST imza çıkarımı."""
        import ast
        lines = ["[Codebase Index — AST Local]\n"]
        p_str = str(Path(project_dir).resolve())
        if not os.path.exists(p_str):
            return "[Codebase] Dizin mevcut değil."

        ignored = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".myfcli"}

        for root, dirs, files in os.walk(p_str):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ignored]
            for fname in sorted(files):
                if not fname.endswith(".py") or fname.startswith("."):
                    continue
                full_path = os.path.join(root, fname)
                rel_path = os.path.relpath(full_path, p_str).replace("\\", "/")
                try:
                    with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                    tree = ast.parse(content, filename=rel_path)
                    file_signatures = []
                    for node in ast.iter_child_nodes(tree):
                        if isinstance(node, ast.ClassDef):
                            file_signatures.append(f"  class {node.name}:")
                            for sub in node.body:
                                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                    args = [a.arg for a in sub.args.args]
                                    file_signatures.append(f"    def {sub.name}({', '.join(args)})")
                        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            args = [a.arg for a in node.args.args]
                            file_signatures.append(f"  def {node.name}({', '.join(args)})")
                    
                    if file_signatures:
                        lines.append(f"File: {rel_path}")
                        lines.extend(file_signatures)
                        lines.append("")
                except Exception:
                    continue

        return "\n".join(lines) if len(lines) > 1 else "[Codebase] Boş veya taranabilir Python dosyası yok."


# Global Singleton
codebase_graph = CodebaseGraphEngine()


def build_repomap(project_dir: str, map_tokens: int = 2000) -> str:
    """Geriye dönük uyumluluk fonksiyonu."""
    return codebase_graph.get_summary_repomap(project_dir)
