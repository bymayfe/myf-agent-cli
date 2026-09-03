"""
command_registry.py — Merkezi Slash Komut Kayıt ve Otomatik Tamamlama Motoru

Tüm CLI komutlarını, takma adlarını (alias), alt komutlarını ve açıklamalarını
tek bir noktada toplar. Tab tamamlama (WordCompleter) ve /help metnini otomatik üretir.
"""

from __future__ import annotations
from typing import Callable, Any, Optional
from dataclasses import dataclass, field


@dataclass
class CommandMeta:
    name: str
    desc: str
    aliases: list[str] = field(default_factory=list)
    subcommands: list[str] = field(default_factory=list)
    category: str = "Genel"


class CommandRegistry:
    """Tüm slash komutlarını dinamik yöneten merkezi sınıf."""

    _COMMANDS: list[CommandMeta] = [
        # ── Pipeline & Oturum ──
        CommandMeta("/run", "Pipeline'ı sıfırdan başlat (Chat modunda da açık onaylı tek yol)", aliases=["/start"], category="Pipeline"),
        CommandMeta("/continue", "Yarıda kalan (Ctrl+C) pipeline'ı kaldığı adımdan devam ettir", aliases=["/devam"], category="Pipeline"),
        CommandMeta("/resume", "Eski oturumlardan birini seç, yükle veya sil (d1, d1-5, d1 -f)", aliases=["/sessions", "/history"], category="Oturum"),
        CommandMeta("/new", "Yeni konuşturma / oturum başlat (yeni Session ID)", category="Oturum"),
        CommandMeta("/clear", "Ekranı ve mevcut sohbet hafızasını temizle", aliases=["/cls"], category="Oturum"),
        CommandMeta("/delete", "Oturum/Proje veya Ajan silme menüsü (d1, d1-5, d1 -f)", aliases=["/remove"], subcommands=["/delete session", "/delete session --files", "/delete agent", "/delete all"], category="Oturum"),
        CommandMeta("/purge", "TÜM eski oturum ve projeleri tamamen temizle / sil", category="Oturum"),
        CommandMeta("/reset", "Sohbeti ve oturumu sıfırla", category="Oturum"),
        CommandMeta("/dir", "Proje klasörünü Explorer/Finder'da aç", aliases=["/open"], category="Oturum"),
        CommandMeta("/status", "Üretilen dosyaları göster", category="Oturum"),
        CommandMeta("/changes", "Bekleyen kod değişikliklerini göster", category="Oturum"),
        CommandMeta("/apply", "Bekleyen değişiklikleri onayla ve uygula", subcommands=["/apply <dosya-yolu>"], category="Oturum"),
        CommandMeta("/discard", "Bekleyen değişiklikleri uygulamadan at", category="Oturum"),

        # ── Denetim & Loglar ──
        CommandMeta("/logs", "Pipeline loglarını göster (çalışma + adım + hata tabloları)", subcommands=[
            "/logs md", "/logs json", "/logs prompts", "/logs outputs", "/logs errors", "/logs summary",
        ], category="Denetim"),

        # ── Versiyon & Git ──
        CommandMeta("/checkpoints", "Otomatik oluşturulmuş git checkpoint listesini göster", aliases=["/commits"], category="Versiyon & Git"),
        CommandMeta("/rollback", "Belirtilen checkpoint commit'ine güvenle geri dön", aliases=["/revert"], subcommands=["/rollback list", "/rollback <commit_hash>"], category="Versiyon & Git"),

        # ── Kod Haritası & Araştırma & Web UI ──
        CommandMeta("/web", "MYF Web Harness & Dashboard kokpitini tarayıcıda aç (dsh web tarzı)", aliases=["/harness"], category="Araçlar"),
        CommandMeta("/graph", "Kod tabanı bilgi grafiğini ve çağrı yollarını göster", aliases=["/ui", "/codemap"], category="Araçlar"),
        CommandMeta("/search", "Web / GitHub üzerinde güvenli araştırma yap", aliases=["/reach"], category="Araçlar"),

        # ── Yapılandırma & Model ──
        CommandMeta("/model", "Tüm sistem modellerini tek seferde değiştir (Örn: /model qwen3.5:4b)", aliases=["/setmodel"], category="Ayarlar"),
        CommandMeta("/mode", "Çalışma modunu ayarla (1: Sıralı, 2: Subagent, 3: Chat)", aliases=["/mod"], subcommands=["/mode 1", "/mode 2", "/mode 3", "/mode sequential", "/mode subagent", "/mode chat"], category="Ayarlar"),
        CommandMeta("/settings", "Ayarlar menüsü (Model, sıcaklık, auto-audit vb.)", category="Ayarlar"),
        CommandMeta("/permission", "Güvenlik ve izin modunu ayarla (ask, session, sandbox)", aliases=["/security"], category="Ayarlar"),
        CommandMeta("/think", "Think modunu aç/kapat (toggle / on / off)", subcommands=["/think on", "/think off"], category="Ayarlar"),
        CommandMeta("/provider", "LLM sağlayıcı seç (Lokal / API)", category="Ayarlar"),
        CommandMeta("/quota", "Online model kota, bakiye ve oturum token kullanımını göster", aliases=["/limits", "/bakiye"], category="Ayarlar"),
        CommandMeta("/name", "Koordinatör ismini hemen değiştir", category="Ayarlar"),
        CommandMeta("/config", "Sistem yapılandırmasını göster", category="Ayarlar"),

        # ── Ajanlar & Yardım ──
        CommandMeta("/agents", "Aktif agent listesini göster", category="Ajanlar"),
        CommandMeta("/add", "Yeni agent ekle (wizard)", category="Ajanlar"),
        CommandMeta("/help", "Bu yardım metnini göster", category="Genel"),
        CommandMeta("/quit", "Çıkış", aliases=["/exit"], category="Genel"),
    ]

    @classmethod
    def get_completer_words(cls) -> list[str]:
        """prompt_toolkit WordCompleter için TÜM komutları, takma adları ve alt komutları otomatik döner."""
        words = set()
        for cmd in cls._COMMANDS:
            words.add(cmd.name)
            for alias in cmd.aliases:
                words.add(alias)
            for sub in cmd.subcommands:
                words.add(sub)
        return sorted(list(words))

    @classmethod
    def get_help_text(cls) -> str:
        """Kayıtlı komutlardan dinamik ve şık yardım metni üretir."""
        lines = ["\n  ═══ KULLANILABILIR KOMUTLAR ═══"]
        current_cat = ""
        for cmd in cls._COMMANDS:
            if cmd.category != current_cat:
                current_cat = cmd.category
                lines.append(f"\n  ── {current_cat} ──")
            names = [cmd.name] + cmd.aliases
            name_str = ", ".join(names)
            lines.append(f"  {name_str:<22} - {cmd.desc}")

        lines.append("\n  ── Sürükle & Bırak ──")
        lines.append("  Terminal ekranına dosya veya proje klasörü sürükleyip bırakabilirsiniz!")
        return "\n".join(lines) + "\n"
