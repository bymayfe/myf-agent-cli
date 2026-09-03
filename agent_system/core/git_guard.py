"""
git_guard.py — Otomatik Git Checkpoint ve Rollback Motoru

Pipeline adımları, cerrahi onarımlar ve kullanıcı komutları için güvenli
Git anlık görüntü (snapshot) ve geri alma (rollback) yöneticisi.

SINIRLAMA VE ÇALIŞMA PRENSİBİ:
  Otomatik checkpoint commit'i atılmadan önce, mevcut working directory'de 
  kullanıcının kendi elle yaptığı, henüz commit edilmemiş değişiklikler varsa, 
  bu değişiklikler de snapshot'a dahil edilir. Pipeline başlangıcında 
  `check_initial_dirty()` çağrılarak kullanıcıya bir defaya mahsus açık uyarı verilir:
  "[GIT-GUARD] Pipeline başlamadan önce commit edilmemiş değişiklikler tespit edildi, 
   bunlar da otomatik checkpoint'lere dahil olacak."
"""

from __future__ import annotations
import logging
import subprocess
from pathlib import Path
from typing import Optional, Any

from config import get_output_dir

logger = logging.getLogger("git_guard")


class GitGuard:
    """
    Güvenli Git Checkpoint ve Geri Alma (Rollback) Motoru.
    
    Özellikler:
      - Subprocess Timeout: Tüm git çağrıları için 10 saniye zaman aşımı koruması.
      - Pre-Rollback Snapshot: Geri alma işlemi öncesinde mevcut durumun snapshot'ını 
        alarak ('[PRE-ROLLBACK-SNAPSHOT]') yanlışlıkla yapılan geri almalarda bile veri 
        kaybını önler.
      - Initial Dirty Detection: Pipeline öncesi kirli çalışma dizinini tespit edip uyarır.
    """

    def __init__(self, project_dir: Optional[str | Path] = None, timeout: float = 10.0):
        self.project_dir = Path(project_dir or get_output_dir()).resolve()
        self.timeout = timeout

    def _run_git(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        """Git komutunu timeout korumasıyla güvenli çalıştırır."""
        try:
            return subprocess.run(
                ["git"] + args,
                cwd=str(self.project_dir),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.error("[GIT-GUARD] Git komutu zaman aşımına uğradı (timeout=%ds): git %s", self.timeout, " ".join(args))
            return subprocess.CompletedProcess(args=["git"] + args, returncode=124, stdout="", stderr="TimeoutExpired")
        except FileNotFoundError:
            logger.warning("[GIT-GUARD] Sistemde 'git' komutu bulunamadı.")
            return subprocess.CompletedProcess(args=["git"] + args, returncode=127, stdout="", stderr="git not found")
        except Exception as exc:
            logger.error("[GIT-GUARD] Git çalıştırma hatası: %s", exc)
            return subprocess.CompletedProcess(args=["git"] + args, returncode=1, stdout="", stderr=str(exc))

    def is_git_repo(self) -> bool:
        """Hedef dizinin bir git deposu olup olmadığını kontrol eder."""
        if (self.project_dir / ".git").exists():
            return True
        res = self._run_git(["rev-parse", "--is-inside-work-tree"])
        return res.returncode == 0 and res.stdout.strip() == "true"

    def init_repo(self) -> bool:
        """Hedef dizinde git deposu yoksa başlatır ve kullanıcı bilgilerini ayarlar."""
        if self.is_git_repo():
            return True
        self.project_dir.mkdir(parents=True, exist_ok=True)
        res = self._run_git(["init"])
        if res.returncode == 0:
            self._run_git(["config", "user.name", "AgentSystem"])
            self._run_git(["config", "user.email", "agents@local"])
            return True
        return False

    def is_dirty(self) -> bool:
        """Working tree'de commit edilmemiş staged/unstaged değişiklik var mı?"""
        if not self.is_git_repo():
            return False
        res = self._run_git(["status", "--porcelain"])
        return res.returncode == 0 and bool(res.stdout.strip())

    def check_initial_dirty(self) -> bool:
        """
        Pipeline başlamadan önce dizinin kirli olup olmadığını denetler ve
        kullanıcıya/loglara bir defalık uyarı verir.
        """
        if self.is_dirty():
            msg = "[GIT-GUARD] Pipeline başlamadan önce commit edilmemiş değişiklikler tespit edildi, bunlar da otomatik checkpoint'lere dahil olacak."
            logger.warning(msg)
            print(f"\n  ⚠️  {msg}")
            return True
        return False

    def get_head_commit(self) -> Optional[str]:
        """Mevcut HEAD commit hash'ini döndürür."""
        if not self.is_git_repo():
            return None
        res = self._run_git(["rev-parse", "HEAD"])
        if res.returncode == 0:
            return res.stdout.strip()
        return None

    def checkpoint(self, step_name: str, agent_role: str = "") -> Optional[str]:
        """
        Mevcut çalışma dizininin durumunu bir [AUTO-CHECKPOINT] commit'i ile kaydeder.
        Commit hash'ini döndürür.
        """
        if not self.is_git_repo():
            ok = self.init_repo()
            if not ok:
                return None

        # Tüm değişiklikleri stage'e ekle
        self._run_git(["add", "-A"])

        role_tag = f"[{agent_role}] " if agent_role else ""
        commit_msg = f"[AUTO-CHECKPOINT] {role_tag}{step_name}"

        # Değişiklik varsa commit at
        if self.is_dirty() or not self.get_head_commit():
            res = self._run_git(["commit", "-m", commit_msg])
            if res.returncode != 0 and "nothing to commit" not in res.stdout:
                logger.warning("[GIT-GUARD] Checkpoint commit oluşturulamadı: %s", res.stderr)

        commit_hash = self.get_head_commit()
        if commit_hash:
            logger.info("[GIT-GUARD] Checkpoint alındı: %s (%s)", commit_hash[:8], commit_msg)
        return commit_hash

    def rollback(self, commit_ref: str) -> bool:
        """
        Belirtilen commit hash'ine veya referansa geri döner.
        Geri alma öncesi [PRE-ROLLBACK-SNAPSHOT] alarak veri kaybını önler.
        """
        if not self.is_git_repo():
            logger.error("[GIT-GUARD] Geri alma başarısız: Git deposu bulunamadı.")
            return False

        # 1. Pre-Rollback Snapshot al (mevcut durumu koru)
        self._run_git(["add", "-A"])
        self._run_git(["commit", "-m", f"[PRE-ROLLBACK-SNAPSHOT] Before rollback to {commit_ref}", "--allow-empty"])

        # 2. Hard Reset ile hedef commite dön
        res_reset = self._run_git(["reset", "--hard", commit_ref])
        if res_reset.returncode != 0:
            logger.error("[GIT-GUARD] Rollback reset başarısız: %s", res_reset.stderr)
            return False

        # 3. İzlenmeyen yeni dosyaları temizle
        self._run_git(["clean", "-fd"])
        logger.info("[GIT-GUARD] Başarıyla rollback yapıldı -> Hedef commit: %s", commit_ref)
        return True

    def list_checkpoints(self, limit: int = 15) -> list[dict[str, Any]]:
        """
        Depodaki otomatik checkpoint listesini döndürür.
        """
        if not self.is_git_repo():
            return []

        res = self._run_git([
            "log",
            f"-n{limit}",
            "--grep=\\[AUTO-CHECKPOINT\\]",
            "--pretty=format:%H|%an|%ad|%s",
            "--date=iso",
        ])

        if res.returncode != 0 or not res.stdout.strip():
            return []

        checkpoints = []
        for line in res.stdout.strip().splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                checkpoints.append({
                    "hash": parts[0],
                    "short_hash": parts[0][:8],
                    "author": parts[1],
                    "date": parts[2],
                    "message": parts[3],
                })
        return checkpoints


# Varsayılan global singleton
git_guard = GitGuard()
