"""
log_store.py — Her proje için ayrı SQLite tabanlı log veritabanı.

Konum: <proje_klasoru>/.myfcli/logs.db

Tablolar:
  project_runs  — Pipeline çalıştırmaları (bir /generate = bir run)
  agent_steps   — Her agent adımı (planner, architect, dev, qa, reviewer hepsi)
  error_events  — Hata olayları (micro-fix, escalation, QA fail, LLM hatası)

Kullanım:
  from log_store import log_store

  run_id  = log_store.start_run(session_id, "TodoApp", "basit todo uygulaması")
  step_id = log_store.start_step(run_id, 1, "agent-001", "Ürün Yöneticisi", "product_manager", "ollama/gemma4:26b", prompt_chars=1200)
  log_store.finish_step(step_id, "success", response_chars=3400, files_written=[], elapsed_sec=18.2, full_output="...")
  log_store.finish_run(run_id, "success", total_agents=5, files_written=8, elapsed_sec=127.3)

  print(log_store.get_run_table())
  print(log_store.get_step_table(run_id))
  print(log_store.get_error_table(run_id))
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from tabulate import tabulate as _tabulate
    _HAS_TABULATE = True
except ImportError:
    _HAS_TABULATE = False


# ─────────────────────────────────────────────
# Yardımcı fonksiyonlar
# ─────────────────────────────────────────────

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _short_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _fmt_table(headers: list, rows: list, tablefmt: str = "simple") -> str:
    """Tablo formatla — tabulate varsa güzel, yoksa elle."""
    if not rows:
        return "(Kayıt yok)"
    if _HAS_TABULATE:
        return _tabulate(rows, headers=headers, tablefmt=tablefmt)
    # Manuel fallback
    col_widths = [max(len(str(h)), max((len(str(r[i])) for r in rows), default=0))
                  for i, h in enumerate(headers)]
    sep = "  ".join("-" * w for w in col_widths)
    header_row = "  ".join(str(h).ljust(w) for h, w in zip(headers, col_widths))
    lines = [header_row, sep]
    for row in rows:
        lines.append("  ".join(str(v).ljust(w) for v, w in zip(row, col_widths)))
    return "\n".join(lines)


# ─────────────────────────────────────────────
# DB yönetimi
# ─────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS project_runs (
    run_id          TEXT PRIMARY KEY,
    session_id      TEXT DEFAULT '',
    project_name    TEXT DEFAULT '',
    brief           TEXT DEFAULT '',
    status          TEXT DEFAULT 'running',
    total_agents    INT  DEFAULT 0,
    files_written   INT  DEFAULT 0,
    error_count     INT  DEFAULT 0,
    elapsed_sec     REAL DEFAULT 0.0,
    started_at      TEXT DEFAULT '',
    finished_at     TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS agent_steps (
    step_id         TEXT PRIMARY KEY,
    run_id          TEXT REFERENCES project_runs(run_id) ON DELETE CASCADE,
    step_number     INT  DEFAULT 0,
    agent_id        TEXT DEFAULT '',
    agent_name      TEXT DEFAULT '',
    agent_role      TEXT DEFAULT '',
    model           TEXT DEFAULT '',
    status          TEXT DEFAULT 'running',
    prompt_chars    INT  DEFAULT 0,
    prompt_text     TEXT DEFAULT '',
    response_chars  INT  DEFAULT 0,
    files_written   TEXT DEFAULT '[]',
    elapsed_sec     REAL DEFAULT 0.0,
    output_summary  TEXT DEFAULT '',
    full_output     TEXT DEFAULT '',
    started_at      TEXT DEFAULT '',
    finished_at     TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS error_events (
    error_id        TEXT PRIMARY KEY,
    run_id          TEXT REFERENCES project_runs(run_id) ON DELETE CASCADE,
    step_id         TEXT DEFAULT '',
    agent_name      TEXT DEFAULT '',
    agent_model     TEXT DEFAULT '',
    error_type      TEXT DEFAULT 'runtime',
    error_msg       TEXT DEFAULT '',
    file_path       TEXT DEFAULT '',
    retry_attempt   INT  DEFAULT 0,
    resolved        INT  DEFAULT 0,
    resolver        TEXT DEFAULT '',
    created_at      TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_steps_run   ON agent_steps(run_id);
CREATE INDEX IF NOT EXISTS idx_errors_run  ON error_events(run_id);
CREATE INDEX IF NOT EXISTS idx_errors_step ON error_events(step_id);
"""


def _get_db_path(project_dir: str | Path) -> Path:
    p = Path(project_dir) / ".myfcli"
    p.mkdir(parents=True, exist_ok=True)
    return p / "logs.db"


def _open(project_dir: str | Path) -> sqlite3.Connection:
    db_path = _get_db_path(project_dir)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_DDL)
    # Otomatik kolon migrasyonu
    try:
        conn.execute("ALTER TABLE agent_steps ADD COLUMN prompt_text TEXT DEFAULT ''")
    except Exception:
        pass
    conn.commit()
    return conn


# ─────────────────────────────────────────────
# LogStore
# ─────────────────────────────────────────────

class LogStore:
    """
    Per-proje SQLite log yöneticisi.

    Bağlantı havuzu project_dir bazlıdır.
    Tüm public metotlar project_dir parametresi alır (None ise config'den alır).
    """

    def __init__(self):
        # {project_dir_str → sqlite3.Connection}
        self._pool: dict[str, sqlite3.Connection] = {}

    @staticmethod
    def _fmt_table(headers: list, rows: list, tablefmt: str = "simple") -> str:
        return _fmt_table(headers, rows, tablefmt=tablefmt)

    # ── İç yardımcılar ──────────────────────────────────────────────────────

    def _cur_dir(self) -> str:
        try:
            from config import get_output_dir
            return get_output_dir()
        except Exception:
            return "."

    def _conn(self, project_dir: str | None) -> sqlite3.Connection:
        key = str(Path(project_dir or self._cur_dir()).resolve())
        if key not in self._pool:
            self._pool[key] = _open(key)
        return self._pool[key]

    def _exec(self, project_dir, sql: str, params=()):
        c = self._conn(project_dir)
        c.execute(sql, params)
        c.commit()

    def _fetch(self, project_dir, sql: str, params=()) -> list[sqlite3.Row]:
        return self._conn(project_dir).execute(sql, params).fetchall()

    # ── project_runs ────────────────────────────────────────────────────────

    def start_run(
        self,
        session_id: str,
        project_name: str,
        brief: str,
        project_dir: str = None,
    ) -> str:
        """Yeni bir pipeline çalışması başlat. run_id döndür."""
        run_id = _short_id("run")
        self._exec(
            project_dir,
            """INSERT INTO project_runs
               (run_id, session_id, project_name, brief, status, started_at)
               VALUES (?, ?, ?, ?, 'running', ?)""",
            (run_id, session_id, project_name, brief[:500], _now()),
        )
        return run_id

    def finish_run(
        self,
        run_id: str,
        status: str,
        total_agents: int = 0,
        files_written: int = 0,
        elapsed_sec: float = 0.0,
        project_dir: str = None,
    ) -> None:
        """Pipeline çalışmasını tamamla."""
        # error_count'u hesapla
        rows = self._fetch(
            project_dir,
            "SELECT COUNT(*) AS cnt FROM error_events WHERE run_id=?",
            (run_id,),
        )
        error_count = rows[0]["cnt"] if rows else 0
        self._exec(
            project_dir,
            """UPDATE project_runs
               SET status=?, total_agents=?, files_written=?,
                   error_count=?, elapsed_sec=?, finished_at=?
               WHERE run_id=?""",
            (status, total_agents, files_written, error_count,
             round(elapsed_sec, 2), _now(), run_id),
        )

    # ── agent_steps ─────────────────────────────────────────────────────────

    def start_step(
        self,
        run_id: str,
        step_number: int,
        agent_id: str,
        agent_name: str,
        agent_role: str,
        model: str,
        prompt_chars: int = 0,
        prompt_text: str = "",
        project_dir: str = None,
    ) -> str:
        """Yeni bir agent adımı başlat. step_id döndür."""
        step_id = _short_id("step")
        self._exec(
            project_dir,
            """INSERT INTO agent_steps
               (step_id, run_id, step_number, agent_id, agent_name,
                agent_role, model, status, prompt_chars, prompt_text, started_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)""",
            (step_id, run_id, step_number, agent_id, agent_name,
             agent_role, model, prompt_chars, prompt_text, _now()),
        )
        return step_id

    def finish_step(
        self,
        step_id: str,
        status: str,
        response_chars: int = 0,
        files_written: list = None,
        elapsed_sec: float = 0.0,
        full_output: str = "",
        project_dir: str = None,
    ) -> None:
        """Agent adımını tamamla ve çıktısını kaydet."""
        files_json = json.dumps(files_written or [], ensure_ascii=False)
        summary = full_output[:1000] + ("..." if len(full_output) > 1000 else "")
        self._exec(
            project_dir,
            """UPDATE agent_steps
               SET status=?, response_chars=?, files_written=?,
                   elapsed_sec=?, output_summary=?, full_output=?, finished_at=?
               WHERE step_id=?""",
            (status, response_chars, files_json,
             round(elapsed_sec, 2), summary, full_output, _now(), step_id),
        )

    # ── error_events ────────────────────────────────────────────────────────

    def log_error(
        self,
        run_id: str,
        step_id: str = "",
        agent_name: str = "",
        agent_model: str = "",
        error_type: str = "runtime",
        error_msg: str = "",
        file_path: str = "",
        retry_attempt: int = 0,
        project_dir: str = None,
    ) -> str:
        """Hata olayı kaydet. error_id döndür."""
        error_id = _short_id("err")
        self._exec(
            project_dir,
            """INSERT INTO error_events
               (error_id, run_id, step_id, agent_name, agent_model,
                error_type, error_msg, file_path, retry_attempt,
                resolved, resolver, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '', ?)""",
            (error_id, run_id, step_id or "", agent_name, agent_model,
             error_type, error_msg[:2000], file_path, retry_attempt, _now()),
        )
        return error_id

    def resolve_error(
        self,
        error_id: str,
        resolver: str,
        project_dir: str = None,
    ) -> None:
        """Hatanın çözüldüğünü işaretle."""
        self._exec(
            project_dir,
            "UPDATE error_events SET resolved=1, resolver=? WHERE error_id=?",
            (resolver, error_id),
        )

    def get_latest_run_id(self, project_dir: str = None) -> Optional[str]:
        """Bu proje için en son run_id'yi döndür."""
        rows = self._fetch(
            project_dir,
            "SELECT run_id FROM project_runs ORDER BY started_at DESC LIMIT 1",
        )
        return rows[0]["run_id"] if rows else None

    # ── Tablo çıktıları ─────────────────────────────────────────────────────

    def get_run_table(self, project_dir: str = None, limit: int = 15) -> str:
        """Proje çalışmalarını tablo olarak döndür."""
        rows = self._fetch(
            project_dir,
            """SELECT run_id, project_name, status, total_agents,
                      files_written, error_count,
                      ROUND(elapsed_sec, 1) AS elapsed_sec,
                      started_at
               FROM project_runs
               ORDER BY started_at DESC
               LIMIT ?""",
            (limit,),
        )
        headers = ["run_id", "proje", "durum", "ajan", "dosya", "hata", "sn", "baslangic"]
        data = [
            (
                r["run_id"],
                r["project_name"][:20],
                r["status"],
                r["total_agents"],
                r["files_written"],
                r["error_count"],
                f'{r["elapsed_sec"]}s',
                r["started_at"],
            )
            for r in rows
        ]
        return _fmt_table(headers, data, tablefmt="simple")

    def get_step_table(self, run_id: str, project_dir: str = None) -> str:
        """Bir pipeline çalışmasının agent adımlarını tablo olarak döndür."""
        rows = self._fetch(
            project_dir,
            """SELECT step_number, agent_name, agent_role, model,
                      status,
                      started_at, finished_at,
                      ROUND(elapsed_sec, 1) AS elapsed_sec,
                      files_written,
                      output_summary
               FROM agent_steps
               WHERE run_id=?
               ORDER BY step_number ASC""",
            (run_id,),
        )
        headers = ["#", "ajan", "rol", "model", "durum",
                   "basladi", "bitti", "sure", "dosyalar", "ozet"]
        data = [
            (
                r["step_number"],
                r["agent_name"][:20],
                r["agent_role"][:18],
                r["model"].split("/")[-1][:22],        # orn: gemma4:26b
                r["status"],
                r["started_at"][11:19]  if r["started_at"]  else "-",  # HH:MM:SS
                r["finished_at"][11:19] if r["finished_at"] else "-",  # HH:MM:SS
                f'{r["elapsed_sec"]}s',
                ", ".join(json.loads(r["files_written"] or "[]"))[:25],
                r["output_summary"][:45].replace("\n", " "),
            )
            for r in rows
        ]
        return _fmt_table(headers, data, tablefmt="simple")

    def get_error_table(
        self,
        run_id: str = None,
        project_dir: str = None,
        limit: int = 30,
    ) -> str:
        """Hata olaylarını tablo olarak döndür."""
        if run_id:
            rows = self._fetch(
                project_dir,
                """SELECT error_id, agent_name, agent_model, error_type,
                          retry_attempt, resolved, resolver,
                          file_path, error_msg, created_at
                   FROM error_events
                   WHERE run_id=?
                   ORDER BY created_at ASC
                   LIMIT ?""",
                (run_id, limit),
            )
        else:
            rows = self._fetch(
                project_dir,
                """SELECT error_id, agent_name, agent_model, error_type,
                          retry_attempt, resolved, resolver,
                          file_path, error_msg, created_at
                   FROM error_events
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (limit,),
            )
        headers = ["error_id", "ajan", "model", "tip", "deneme",
                   "cozuldu", "cozucu", "dosya", "mesaj", "zaman"]
        data = [
            (
                r["error_id"],
                r["agent_name"][:14],
                r["agent_model"].split("/")[-1][:16],
                r["error_type"],
                r["retry_attempt"],
                "✓" if r["resolved"] else "✗",
                r["resolver"][:12],
                Path(r["file_path"]).name[:20] if r["file_path"] else "-",
                r["error_msg"][:50].replace("\n", " "),
                r["created_at"][11:19],  # sadece saat
            )
            for r in rows
        ]
        return _fmt_table(headers, data, tablefmt="simple")

    def get_full_step_output(
        self,
        step_id: str,
        project_dir: str = None,
    ) -> str:
        """Belirli bir agent adımının tam çıktısını döndür."""
        rows = self._fetch(
            project_dir,
            "SELECT agent_name, agent_role, model, full_output FROM agent_steps WHERE step_id=?",
            (step_id,),
        )
        if not rows:
            return f"(step_id '{step_id}' bulunamadı)"
        r = rows[0]
        return (
            f"=== AGENT: {r['agent_name']} ({r['agent_role']}) | Model: {r['model']} ===\n\n"
            f"{r['full_output']}"
        )

    # ── Filtreleme & Arama ──────────────────────────────────────────────────

    def filter_by_type(
        self,
        error_type: str,
        project_dir: str = None,
        limit: int = 30,
    ) -> str:
        """
        Belirli hata tipine göre error_events filtrele.

        error_type örnekleri:
          syntax      — Python syntax hatası
          import      — Eksik kütüphane (ImportError/ModuleNotFoundError)
          runtime     — Çalışma zamanı hatası (TypeError, AttributeError, NameError...)
          assertion   — Test assertion hatası / pytest FAILED
          pytest      — Pytest çalıştırma hatası
          llm         — LLM yanıt vermedi / boş yanıt
          micro_fix   — Micro-Fix denemesi kaydı
          escalation  — Escalation tetiklendi
          qa_failed   — QA ajani ## STATUS: FAILED döndürdü
        """
        rows = self._fetch(
            project_dir,
            """SELECT error_id, run_id, agent_name, agent_model,
                      retry_attempt, resolved, resolver,
                      file_path, error_msg, created_at
               FROM error_events
               WHERE LOWER(error_type) = LOWER(?)
               ORDER BY created_at DESC
               LIMIT ?""",
            (error_type, limit),
        )
        if not rows:
            return f"('{error_type}' tipinde kayıt bulunamadı)"

        headers = ["error_id", "run_id", "ajan", "model", "deneme",
                   "cozuldu", "cozucu", "dosya", "mesaj", "zaman"]
        data = [
            (
                r["error_id"],
                r["run_id"],
                r["agent_name"][:14],
                r["agent_model"].split("/")[-1][:16],
                r["retry_attempt"],
                "✓" if r["resolved"] else "✗",
                r["resolver"][:12],
                Path(r["file_path"]).name[:20] if r["file_path"] else "-",
                r["error_msg"][:50].replace("\n", " "),
                r["created_at"][11:19],
            )
            for r in rows
        ]
        header = f"[{error_type.upper()}] — {len(rows)} kayıt"
        return header + "\n" + _fmt_table(headers, data, tablefmt="simple")

    def filter_runs_by_status(
        self,
        status: str,
        project_dir: str = None,
        limit: int = 15,
    ) -> str:
        """
        Çalışmaları duruma göre filtrele.

        status örnekleri: success | failed | partial | running
        """
        rows = self._fetch(
            project_dir,
            """SELECT run_id, project_name, total_agents, files_written,
                      error_count, ROUND(elapsed_sec,1) AS elapsed_sec, started_at
               FROM project_runs
               WHERE LOWER(status) = LOWER(?)
               ORDER BY started_at DESC
               LIMIT ?""",
            (status, limit),
        )
        if not rows:
            return f"('{status}' durumunda çalışma bulunamadı)"

        headers = ["run_id", "proje", "ajan", "dosya", "hata", "sn", "baslangic"]
        data = [
            (
                r["run_id"],
                r["project_name"][:20],
                r["total_agents"],
                r["files_written"],
                r["error_count"],
                f'{r["elapsed_sec"]}s',
                r["started_at"],
            )
            for r in rows
        ]
        header = f"[{status.upper()}] — {len(rows)} çalışma"
        return header + "\n" + _fmt_table(headers, data, tablefmt="simple")

    def get_error_type_summary(self, project_dir: str = None) -> str:
        """
        Bu proje için hata tiplerini sayısal özet olarak döndür.
        Hangi tip hata kaç kez olmuş, kaçı çözülmüş?
        """
        rows = self._fetch(
            project_dir,
            """SELECT error_type,
                      COUNT(*) AS toplam,
                      SUM(resolved) AS cozuldu,
                      COUNT(*) - SUM(resolved) AS acik
               FROM error_events
               GROUP BY error_type
               ORDER BY toplam DESC""",
        )
        if not rows:
            return "(Henüz hata kaydı yok)"

        headers = ["tip", "toplam", "cozuldu", "acik"]
        data = [(r["error_type"], r["toplam"], r["cozuldu"], r["acik"]) for r in rows]
        return _fmt_table(headers, data, tablefmt="simple")

    def get_all_error_types(self, project_dir: str = None) -> list[str]:
        """Bu projede görülmüş tüm hata tiplerini listele."""
        rows = self._fetch(
            project_dir,
            "SELECT DISTINCT error_type FROM error_events ORDER BY error_type",
        )
        return [r["error_type"] for r in rows]

    def export_full_logs_json(self, project_dir: str = None) -> str:
        """Tüm proje adımlarını, tam promptları, düşünme süreçlerini ve çıktıları içeren JSON dosyası üretir."""
        if project_dir:
            p_dir = Path(project_dir)
        else:
            try:
                from brain import get_output_dir
                p_dir = Path(get_output_dir())
            except Exception:
                p_dir = Path(".")
                
        export_path = p_dir / ".myfcli" / "full_pipeline_audit.json"
        
        runs = [dict(r) for r in self._fetch(str(p_dir), "SELECT * FROM project_runs ORDER BY started_at ASC")]
        steps = [dict(r) for r in self._fetch(str(p_dir), "SELECT * FROM agent_steps ORDER BY started_at ASC")]
        errors = [dict(r) for r in self._fetch(str(p_dir), "SELECT * FROM error_events ORDER BY created_at ASC")]
        
        data = {
            "project_dir": str(p_dir),
            "exported_at": _now(),
            "runs": runs,
            "steps": steps,
            "errors": errors,
        }
        export_path.parent.mkdir(parents=True, exist_ok=True)
        export_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return str(export_path)

    def export_full_logs_md(self, project_dir: str = None) -> str:
        """Tüm adımları, promptları ve çıktıları tek bir Markdown rapor dosyasına (AUDIT_LOG.md) döker."""
        if project_dir:
            p_dir = Path(project_dir)
        else:
            try:
                from brain import get_output_dir
                p_dir = Path(get_output_dir())
            except Exception:
                p_dir = Path(".")
                
        export_path = p_dir / "AUDIT_LOG.md"
        
        steps = self._fetch(str(p_dir), "SELECT * FROM agent_steps ORDER BY started_at ASC")
        errors = self._fetch(str(p_dir), "SELECT * FROM error_events ORDER BY created_at ASC")
        
        lines = [
            "# Multi-Agent Proje Denetim ve Süreç Günlüğü (Audit Log)",
            f"**Oluşturulma Tarihi:** {_now()}  ",
            f"**Proje Dizini:** `{p_dir}`  ",
            f"**Toplam Adım Sayısı:** {len(steps)}  ",
            "",
            "---",
            "## 📋 Adım Özeti",
            "",
        ]
        
        headers = ["#", "Ajan", "Rol", "Model", "Prompt Boyutu", "Yanıt Boyutu", "Süre", "Durum"]
        t_data = [(r["step_number"], r["agent_name"], r["agent_role"], r["model"].split("/")[-1], f"{r['prompt_chars']} ch", f"{r['response_chars']} ch", f"{r['elapsed_sec']}s", r["status"]) for r in steps]
        lines.append(_fmt_table(headers, t_data))
        lines.append("\n---\n## 🔬 Tüm Adım Detayları (Promptlar & Çıktılar)\n")
        
        for r in steps:
            files_str = ", ".join(json.loads(r["files_written"] or "[]")) or "Yok"
            lines.extend([
                f"### Adım #{r['step_number']}: {r['agent_name']} (`{r['agent_role']}`)",
                f"- **Model:** `{r['model']}`",
                f"- **Başlangıç - Bitiş:** {r['started_at']} - {r['finished_at']} ({r['elapsed_sec']}s)",
                f"- **Durum:** `{r['status']}`",
                f"- **Üretilen Dosyalar:** `{files_str}`",
                "",
                "#### 📥 Ajan'a Gönderilen Prompt:",
                "```text",
                (r["prompt_text"] or "(Kayıt yok)")[:10000],
                "```",
                "",
                "#### 📤 Ajan'dan Gelen Tam Çıktı / Kod:",
                "```text",
                (r["full_output"] or "(Boş çıktı)"),
                "```",
                "",
                "---",
            ])
            
        if errors:
            lines.append("## ⚠️ Hata & Onarım Olayları\n")
            e_headers = ["Hata ID", "Ajan", "Tip", "Deneme", "Çözüldü", "Çözücü", "Mesaj"]
            e_data = [(e["error_id"], e["agent_name"], e["error_type"], e["retry_attempt"], "✓" if e["resolved"] else "✗", e["resolver"] or "-", e["error_msg"][:60]) for e in errors]
            lines.append(_fmt_table(e_headers, e_data))
            
        export_path.write_text("\n".join(lines), encoding="utf-8")
        return str(export_path)

    def close_all(self) -> None:
        """Tüm açık DB bağlantılarını kapat."""
        for conn in self._pool.values():
            try:
                conn.close()
            except Exception:
                pass
        self._pool.clear()


# ─────────────────────────────────────────────
# Global singleton
# ─────────────────────────────────────────────
log_store = LogStore()
