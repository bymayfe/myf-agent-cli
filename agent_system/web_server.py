"""
web_server.py — MYF AI Web Agent & Dashboard (dsh web tarzı)

Özellikler:
- Canlı MYF-Agent & DeepSeek sohbeti (Streaming SSE)
- Oturum Geçmişi & Oturumlar Arası Geçiş (Session Switcher & New Session)
- Canlı Kod Önizleyici & Dosya Ağacı
- Canlı Ollama Model Taraması (ollama list / api/tags / api/ps ile VRAM tespiti)
- Tam Kapsamlı Ayarlar Menüsü (Tüm CLI Ayarları: Sağlayıcı, Model, Mod, Think, Sıcaklık, Ajan Modelleri, Otonomi Limiti, İzin)
- Güvenli Kapatma (Shutdown API)
"""

import os
import sys
import json
import time
import logging
import threading
import webbrowser
import urllib.request
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# Proje ana dizinini import yoluna ekle
_HERE = Path(__file__).parent.resolve()
_SYS_ROOT = _HERE
for _sub in [_SYS_ROOT / "core", _SYS_ROOT / "engines", _SYS_ROOT / "agents", _SYS_ROOT / "llm", _SYS_ROOT / "storage", _SYS_ROOT]:
    _p = str(_sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

from config import (
    get_output_dir, set_output_dir, get_active_provider_name, LLM_PARAMS,
    AGENT_MODELS, list_providers, set_active_provider, reload_config
)
from brain import list_output_files
from settings import settings
from session_manager import session_manager, Session
from coordinator_agent import CoordinatorAgent
from engines.quota_engine import quota_engine

logger = logging.getLogger("web_agent")


def get_available_models(provider_name: str) -> list[dict]:
    """Seçilen sağlayıcıya göre kullanılabilir model listesini döndürür."""
    if provider_name == "ollama":
        installed = []
        running = set()
        # 1. VRAM'de aktif model var mı? (api/ps)
        try:
            req = urllib.request.urlopen("http://localhost:11434/api/ps", timeout=1.2)
            ps_data = json.loads(req.read().decode("utf-8"))
            for m in ps_data.get("models", []):
                running.add(m.get("name", "").split(":")[0])
                running.add(m.get("name", ""))
        except Exception:
            pass

        # 2. İndirilmiş modeller (api/tags)
        try:
            req = urllib.request.urlopen("http://localhost:11434/api/tags", timeout=1.2)
            tags_data = json.loads(req.read().decode("utf-8"))
            for m in tags_data.get("models", []):
                name = m.get("name", "")
                size_gb = round(m.get("size", 0) / (1024**3), 1)
                is_active_vram = (name in running) or (name.split(":")[0] in running)
                label = f"{name} ({size_gb} GB)"
                if is_active_vram:
                    label += " ⚡ [VRAM'de Aktif]"
                installed.append({
                    "id": f"ollama/{name}" if not name.startswith("ollama/") else name,
                    "name": name,
                    "label": label,
                    "in_vram": is_active_vram,
                    "size_gb": size_gb,
                })
        except Exception:
            pass

        if not installed:
            installed = [
                {"id": "ollama/qwen3.8:latest", "name": "qwen3.8:latest", "label": "qwen3.8:latest (Lokal)", "in_vram": False},
                {"id": "ollama/qwen3.5:4b", "name": "qwen3.5:4b", "label": "qwen3.5:4b (Lokal)", "in_vram": False}
            ]
        return installed

    elif provider_name == "nvidia":
        return [
            {"id": "openai/deepseek-ai/deepseek-v4-pro-0813", "name": "deepseek-ai/deepseek-v4-pro-0813", "label": "DeepSeek V4 Pro (H100 Cloud, 65K)"},
            {"id": "openai/meta/llama-3.3-70b-instruct", "name": "meta/llama-3.3-70b-instruct", "label": "Meta LLaMA 3.3 70B Instruct"},
            {"id": "openai/mistralai/mistral-large-2-instruct", "name": "mistralai/mistral-large-2-instruct", "label": "Mistral Large 2 (128K)"},
            {"id": "openai/nvidia/llama-3.1-nemotron-70b-instruct", "name": "nvidia/llama-3.1-nemotron-70b-instruct", "label": "NVIDIA Nemotron 70B"}
        ]

    elif provider_name == "openrouter":
        return [
            {"id": "openrouter/qwen/qwen-2.5-coder-32b-instruct", "name": "qwen/qwen-2.5-coder-32b-instruct", "label": "Qwen 2.5 Coder 32B (OpenRouter)"},
            {"id": "openrouter/deepseek/deepseek-chat", "name": "deepseek/deepseek-chat", "label": "DeepSeek V3 / R1 (OpenRouter)"},
            {"id": "openrouter/anthropic/claude-3.5-sonnet", "name": "anthropic/claude-3.5-sonnet", "label": "Claude 3.5 Sonnet"},
            {"id": "openrouter/meta-llama/llama-3.3-70b-instruct", "name": "meta-llama/llama-3.3-70b-instruct", "label": "LLaMA 3.3 70B Instruct"}
        ]

    elif provider_name == "moonshot":
        return [
            {"id": "moonshot/moonshot-v1-8k", "name": "moonshot-v1-8k", "label": "Kimi K2 (8K Context)"},
            {"id": "moonshot/moonshot-v1-32k", "name": "moonshot-v1-32k", "label": "Kimi K2 (32K Context)"},
            {"id": "moonshot/moonshot-v1-128k", "name": "moonshot-v1-128k", "label": "Kimi K2 (128K Context)"}
        ]

    elif provider_name == "lm_studio":
        return [
            {"id": "openai/local-model", "name": "local-model", "label": "Lokal Aktif Model (Port 1234)"}
        ]

    elif provider_name == "llama_cpp":
        models = []
        # providers_config.json'daki available_models veya model_context_windows'tan oku
        try:
            from config import load_providers as _lp
            p_data = _lp().get("providers", {}).get("llama_cpp", {})
            avail = p_data.get("available_models", {})
            ctx_wins = p_data.get("model_context_windows", {})
            if avail:
                for m_name, m_info in avail.items():
                    ctx = m_info.get("context_window", p_data.get("default_context_window", 32768))
                    size = m_info.get("size", "")
                    desc = m_info.get("description", "")
                    label_parts = [m_name]
                    if size:
                        label_parts.append(f"({size})")
                    if desc:
                        label_parts.append(f"— {desc}")
                    models.append({
                        "id": f"openai/{m_name}",
                        "name": m_name,
                        "label": " ".join(label_parts),
                        "context_window": ctx,
                    })
            elif ctx_wins:
                for m_name, ctx in ctx_wins.items():
                    if m_name == "default":
                        continue
                    models.append({
                        "id": f"openai/{m_name}",
                        "name": m_name,
                        "label": f"{m_name} ({ctx // 1024}K ctx)",
                        "context_window": ctx,
                    })
        except Exception:
            pass

        # models/ klasörünü de tara (config'de olmayan GGUF'lar için)
        if not models:
            llama_models_dir = Path(__file__).parent.parent / "llama_server" / "models"
            if llama_models_dir.exists():
                for gguf in sorted(llama_models_dir.glob("*.gguf")):
                    m_name = gguf.stem
                    size_gb = round(gguf.stat().st_size / (1024 ** 3), 1)
                    models.append({
                        "id": f"openai/{m_name}",
                        "name": m_name,
                        "label": f"{m_name} ({size_gb} GB)",
                    })

        if not models:
            models = [{"id": "openai/default", "name": "default", "label": "Lokal Aktif Model (Port 8080)"}]

        return models

    return []


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>MYF AI — Web Kokpit & Ajan Kontrol Paneli</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <style>
    body { background-color: #0b0f19; color: #f3f4f6; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    .glass { background: rgba(17, 24, 39, 0.85); backdrop-filter: blur(16px); border: 1px solid rgba(255, 255, 255, 0.08); }
    .glass-card { background: rgba(31, 41, 55, 0.6); border: 1px solid rgba(255, 255, 255, 0.05); }
    .glass-modal { background: rgba(17, 24, 39, 0.98); backdrop-filter: blur(28px); border: 1px solid rgba(255, 255, 255, 0.12); }
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: rgba(0, 0, 0, 0.2); }
    ::-webkit-scrollbar-thumb { background: rgba(255, 255, 255, 0.15); border-radius: 3px; }
    ::-webkit-scrollbar-thumb:hover { background: rgba(255, 255, 255, 0.25); }
    .prose code { background: rgba(0,0,0,0.4); padding: 2px 6px; border-radius: 4px; font-family: monospace; font-size: 0.9em; }
    .prose pre { background: #030712 !important; padding: 12px; border-radius: 8px; overflow-x: auto; border: 1px solid rgba(255,255,255,0.08); }
  </style>
</head>
<body class="h-screen flex flex-col overflow-hidden">

  <!-- Header / Navigation Bar -->
  <header class="glass h-14 border-b border-gray-800 flex items-center justify-between px-6 z-20 shrink-0">
    <div class="flex items-center space-x-3">
      <div class="w-8 h-8 rounded-lg bg-gradient-to-tr from-cyan-500 to-blue-600 flex items-center justify-center font-bold text-white shadow-lg shadow-cyan-500/30">
        <i class="fa-solid fa-brain"></i>
      </div>
      <div>
        <span class="font-bold text-lg tracking-wide text-white">MYF <span class="text-cyan-400">AGENT</span></span>
        <span class="text-xs ml-2 px-2 py-0.5 rounded bg-cyan-950/80 border border-cyan-800/60 text-cyan-300 font-mono">v2.5 Web UI</span>
      </div>
    </div>

    <!-- Active Status Bar & Buttons -->
    <div class="flex items-center space-x-3 text-xs">
      <div class="flex items-center space-x-1.5 px-3 py-1 rounded-full bg-gray-900 border border-gray-800 text-gray-300">
        <span class="w-2 h-2 rounded-full bg-emerald-500 animate-pulse"></span>
        <span id="header-provider" class="font-semibold text-emerald-400">OLLAMA</span>
        <span class="text-gray-500">·</span>
        <span id="header-model" class="text-gray-300 font-mono">qwen3.8</span>
      </div>
      <div class="flex items-center space-x-2 px-3 py-1 rounded-full bg-gray-900 border border-gray-800 text-gray-300">
        <i class="fa-solid fa-coins text-amber-400"></i>
        <span id="header-quota">Sınırsız (Lokal)</span>
      </div>
      <div class="flex items-center space-x-2 px-3 py-1 rounded-full bg-gray-900 border border-gray-800 text-gray-300">
        <i class="fa-solid fa-folder-open text-blue-400"></i>
        <span id="header-dir" class="truncate max-w-[130px]">Proje</span>
      </div>
      
      <!-- Settings Button -->
      <button onclick="openSettings()" title="Tüm Ayarları Düzenle" class="flex items-center space-x-1.5 px-3 py-1 rounded-full bg-gray-900 border border-gray-700 hover:bg-gray-800 text-gray-200 transition-colors cursor-pointer">
        <i class="fa-solid fa-gear text-cyan-400"></i>
        <span>Ayarlar</span>
      </button>

      <!-- Shutdown Button -->
      <button onclick="stopServer()" title="Web Sunucusunu Durdur" class="flex items-center space-x-1.5 px-3 py-1 rounded-full bg-red-950/70 border border-red-800/80 hover:bg-red-900/90 text-red-300 transition-colors cursor-pointer">
        <i class="fa-solid fa-power-off text-red-400"></i>
        <span>Kapat</span>
      </button>
    </div>
  </header>

  <!-- Main 3-Column Layout -->
  <main class="flex-1 flex overflow-hidden">
    
    <!-- LEFT PANEL: Tabs (Files & Sessions) -->
    <aside class="w-80 glass border-r border-gray-800 flex flex-col shrink-0">
      <!-- Tabs Selector -->
      <div class="flex border-b border-gray-800 bg-gray-950/40 p-1">
        <button id="tab-files-btn" onclick="switchLeftTab('files')" class="flex-1 py-1.5 text-xs font-semibold text-cyan-400 border-b-2 border-cyan-400 flex items-center justify-center space-x-1.5">
          <i class="fa-solid fa-folder-tree"></i>
          <span>Dosyalar</span>
        </button>
        <button id="tab-sessions-btn" onclick="switchLeftTab('sessions')" class="flex-1 py-1.5 text-xs font-semibold text-gray-400 border-b-2 border-transparent hover:text-gray-200 flex items-center justify-center space-x-1.5">
          <i class="fa-solid fa-clock-rotate-left"></i>
          <span>Oturumlar</span>
        </button>
      </div>

      <!-- Tab 1: Files Container -->
      <div id="tab-files" class="flex-1 flex flex-col overflow-hidden">
        <div class="p-2.5 border-b border-gray-800/60 flex items-center justify-between">
          <span class="text-[11px] font-bold text-gray-400 uppercase tracking-wider">Aktif Proje Çıktıları</span>
          <button onclick="loadFiles()" class="text-gray-400 hover:text-cyan-400 text-xs p-1"><i class="fa-solid fa-rotate"></i></button>
        </div>
        <div id="file-tree" class="flex-1 overflow-y-auto p-2 space-y-1 text-sm font-mono">
          <div class="text-xs text-gray-500 p-2">Dosyalar yükleniyor...</div>
        </div>
      </div>

      <!-- Tab 2: Sessions Container -->
      <div id="tab-sessions" class="flex-1 flex flex-col overflow-hidden hidden">
        <div class="p-2.5 border-b border-gray-800/60 flex items-center justify-between">
          <span class="text-[11px] font-bold text-gray-400 uppercase tracking-wider">Tüm Oturumlar</span>
          <button onclick="newSession()" class="px-2 py-0.5 rounded bg-cyan-900/60 hover:bg-cyan-800 text-cyan-200 text-xs font-semibold">
            <i class="fa-solid fa-plus mr-1"></i> Yeni
          </button>
        </div>
        <div id="session-list" class="flex-1 overflow-y-auto p-2 space-y-1.5 text-xs">
          <div class="text-gray-500 p-2">Oturumlar taranıyor...</div>
        </div>
      </div>

      <!-- Active Session Footer -->
      <div class="p-3 border-t border-gray-800 bg-gray-950/60">
        <div class="text-xs text-gray-400 mb-1 flex justify-between">
          <span>Aktif Oturum:</span>
          <span id="left-session-id" class="font-mono text-cyan-400">sess-xxxx</span>
        </div>
        <div class="text-[11px] text-gray-500 truncate" id="left-folder-path">/projects/...</div>
      </div>
    </aside>

    <!-- CENTER PANEL: Chat & Agent Stream -->
    <section class="flex-1 flex flex-col bg-gray-950/40 relative">
      <!-- Chat Messages Container -->
      <div id="chat-container" class="flex-1 overflow-y-auto p-6 space-y-6">
        <div class="glass-card p-4 rounded-xl max-w-3xl">
          <div class="flex items-center space-x-2 text-cyan-400 font-bold mb-2">
            <i class="fa-solid fa-robot"></i>
            <span>MYF-Agent</span>
          </div>
          <div class="text-sm text-gray-300 leading-relaxed">
            Merhaba! Ben <strong>MYF-Agent</strong>, otonom yazılım geliştirme koordinatörünüz. İster doğrudan sohbet edin, ister sıfırdan bir full-stack proje tasarlayıp pipeline'ı başlatın! 🚀
          </div>
        </div>
      </div>

      <!-- Input Bar -->
      <div class="p-4 border-t border-gray-800 glass shrink-0">
        <form id="chat-form" onsubmit="sendMessage(event)" class="flex gap-2">
          <input 
            type="text" 
            id="prompt-input" 
            placeholder="Bir soru sorun, komut yazın veya /run ile pipeline başlatın..." 
            class="flex-1 bg-gray-900 border border-gray-700 rounded-xl px-4 py-3 text-sm focus:outline-none focus:border-cyan-500 text-white placeholder-gray-500 shadow-inner"
            autocomplete="off"
          />
          <button type="submit" id="send-btn" class="bg-gradient-to-r from-cyan-500 to-blue-600 hover:from-cyan-600 hover:to-blue-700 text-white px-5 py-3 rounded-xl text-sm font-semibold transition-all shadow-lg shadow-cyan-500/25 flex items-center space-x-2">
            <span>Gönder</span>
            <i class="fa-solid fa-paper-plane"></i>
          </button>
        </form>
      </div>
    </section>

    <!-- RIGHT PANEL: File Viewer / Code Preview -->
    <aside class="w-96 glass border-l border-gray-800 flex flex-col shrink-0">
      <div class="p-3 border-b border-gray-800 flex items-center justify-between">
        <span id="viewer-title" class="text-xs font-bold text-gray-400 truncate"><i class="fa-solid fa-code mr-1.5"></i> Kod Önizleyici</span>
        <span id="viewer-size" class="text-[11px] text-gray-500 font-mono">0 KB</span>
      </div>
      <div id="code-viewer" class="flex-1 overflow-auto p-3 font-mono text-xs text-gray-300 bg-gray-950/80 whitespace-pre">
        <div class="text-gray-600 p-4 text-center mt-12">
          <i class="fa-regular fa-file-code text-4xl mb-3 block"></i>
          Sol menüden görüntülemek istediğiniz dosyaya tıklayın.
        </div>
      </div>
    </aside>

  </main>

  <!-- COMPREHENSIVE SETTINGS MODAL -->
  <div id="settings-modal" class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-md hidden p-4 overflow-y-auto">
    <div class="glass-modal w-full max-w-xl rounded-2xl p-6 shadow-2xl space-y-4 border border-gray-700 my-auto max-h-[90vh] flex flex-col">
      
      <!-- Modal Header -->
      <div class="flex items-center justify-between border-b border-gray-800 pb-3 shrink-0">
        <div class="flex items-center space-x-2 text-cyan-400 font-bold text-base">
          <i class="fa-solid fa-sliders"></i>
          <span>MYF Agent Yapılandırması & Ayarlar</span>
        </div>
        <button onclick="closeSettings()" class="text-gray-400 hover:text-white"><i class="fa-solid fa-xmark text-lg"></i></button>
      </div>

      <!-- Modal Body (Scrollable) -->
      <div class="space-y-4 text-xs overflow-y-auto pr-1 flex-1">
        
        <!-- 1. Provider & Model Section -->
        <div class="p-3 rounded-xl bg-gray-900/60 border border-gray-800 space-y-3">
          <div class="font-bold text-gray-300 flex items-center space-x-1.5">
            <i class="fa-solid fa-microchip text-cyan-400"></i>
            <span>Model & Sağlayıcı Seçimi</span>
          </div>

          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="block text-gray-400 mb-1 font-semibold">LLM Sağlayıcı</label>
              <select id="setting-provider" onchange="onProviderChange(this.value)" class="w-full bg-gray-950 border border-gray-700 rounded-lg p-2.5 text-white focus:outline-none focus:border-cyan-500"></select>
            </div>
            <div>
              <label class="block text-gray-400 mb-1 font-semibold flex justify-between">
                <span>Model Seçimi</span>
                <span id="model-loading-indicator" class="hidden text-cyan-400 font-normal"><i class="fa-solid fa-spinner animate-spin"></i> taranıyor...</span>
              </label>
              <select id="setting-model" class="w-full bg-gray-950 border border-gray-700 rounded-lg p-2.5 text-white focus:outline-none focus:border-cyan-500 font-mono"></select>
            </div>
          </div>
        </div>

        <!-- 2. Execution & Think Mode -->
        <div class="p-3 rounded-xl bg-gray-900/60 border border-gray-800 space-y-3">
          <div class="font-bold text-gray-300 flex items-center space-x-1.5">
            <i class="fa-solid fa-play text-blue-400"></i>
            <span>Çalışma Modu & Düşünme (Reasoning)</span>
          </div>

          <div>
            <label class="block text-gray-400 mb-1 font-semibold">Çalışma Modu</label>
            <select id="setting-mode" class="w-full bg-gray-950 border border-gray-700 rounded-lg p-2.5 text-white focus:outline-none focus:border-cyan-500">
              <option value="sequential">1: Sıralı Pipeline (PM ➔ Mimar ➔ DEV ➔ QA ➔ Doc)</option>
              <option value="subagent">2: Dinamik Subagent Orkestrasyonu</option>
              <option value="interactive">3: Doğrudan İnteraktif Sohbet (MYF-Agent Canlı Kodlama)</option>
            </select>
          </div>

          <div class="flex items-center justify-between p-2 rounded-lg bg-gray-950/80 border border-gray-800">
            <div>
              <div class="font-semibold text-white">Think / Akıl Yürütme Modu</div>
              <div class="text-[11px] text-gray-500">DeepSeek & Qwen modellerinde derin düşünme tokenlarını canlı göster</div>
            </div>
            <input type="checkbox" id="setting-think" class="w-5 h-5 accent-cyan-500 rounded cursor-pointer">
          </div>
        </div>

        <!-- 3. Temperature & Max Tokens -->
        <div class="p-3 rounded-xl bg-gray-900/60 border border-gray-800 space-y-3">
          <div class="font-bold text-gray-300 flex items-center space-x-1.5">
            <i class="fa-solid fa-gauge-high text-amber-400"></i>
            <span>Üretim Parametreleri</span>
          </div>

          <div class="grid grid-cols-2 gap-4">
            <div>
              <div class="flex justify-between text-gray-400 mb-1 font-semibold">
                <span>Sıcaklık (Temp)</span>
                <span id="temp-val" class="font-mono text-cyan-400">0.2</span>
              </div>
              <input type="range" id="setting-temp" min="0.0" max="1.0" step="0.05" class="w-full accent-cyan-500" oninput="document.getElementById('temp-val').innerText = this.value">
            </div>
            <div>
              <div class="flex justify-between text-gray-400 mb-1 font-semibold">
                <span>Maks Token</span>
                <span id="tokens-val" class="font-mono text-cyan-400">8192</span>
              </div>
              <input type="range" id="setting-tokens" min="2048" max="32768" step="1024" class="w-full accent-cyan-500" oninput="document.getElementById('tokens-val').innerText = this.value">
            </div>
          </div>
        </div>

        <!-- 4. Autonomy & Security Limits -->
        <div class="p-3 rounded-xl bg-gray-900/60 border border-gray-800 space-y-3">
          <div class="font-bold text-gray-300 flex items-center space-x-1.5">
            <i class="fa-solid fa-shield-halved text-emerald-400"></i>
            <span>Güvenlik & Otonomi Limitleri</span>
          </div>

          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="block text-gray-400 mb-1 font-semibold">Güvenlik İzin Politikası</label>
              <select id="setting-permission" class="w-full bg-gray-950 border border-gray-700 rounded-lg p-2 text-white focus:outline-none focus:border-cyan-500">
                <option value="session_allow">Proje İçi Otomatik İzinli (Önerilen)</option>
                <option value="ask">Her Dosya/Komutta Sor</option>
                <option value="always_allow">Tam İzinli (Her Şeye İzin Ver)</option>
              </select>
            </div>
            <div>
              <label class="block text-gray-400 mb-1 font-semibold">Micro-Fix Onarım Denemesi</label>
              <select id="setting-fix-tries" class="w-full bg-gray-950 border border-gray-700 rounded-lg p-2 text-white focus:outline-none focus:border-cyan-500 font-mono">
                <option value="1">1 Deneme</option>
                <option value="3">3 Deneme (Varsayılan)</option>
                <option value="5">5 Deneme</option>
                <option value="10">10 Deneme</option>
              </select>
            </div>
          </div>

          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="block text-gray-400 mb-1 font-semibold">Otonom Adım Limiti (Cap)</label>
              <input type="number" id="setting-autonomy-cap" min="10" max="200" step="10" class="w-full bg-gray-950 border border-gray-700 rounded-lg p-2 text-white font-mono focus:outline-none focus:border-cyan-500">
            </div>
            <div>
              <label class="block text-gray-400 mb-1 font-semibold">Repo Haritası Tokenı</label>
              <input type="number" id="setting-repomap-tokens" min="512" max="8192" step="256" class="w-full bg-gray-950 border border-gray-700 rounded-lg p-2 text-white font-mono focus:outline-none focus:border-cyan-500">
            </div>
          </div>

          <div class="flex items-center justify-between pt-1">
            <label class="flex items-center space-x-2 text-gray-300 cursor-pointer">
              <input type="checkbox" id="setting-audit-log" class="w-4 h-4 accent-cyan-500 rounded">
              <span>Otomatik Denetim Günlüğü (AUDIT_LOG.md)</span>
            </label>
            <label class="flex items-center space-x-2 text-gray-300 cursor-pointer">
              <input type="checkbox" id="setting-warmup" class="w-4 h-4 accent-cyan-500 rounded">
              <span>Model Warmup (GPU Ön Isıtma)</span>
            </label>
          </div>
        </div>

      </div>

      <!-- Modal Footer -->
      <div class="flex justify-end space-x-2 pt-3 border-t border-gray-800 shrink-0">
        <button onclick="closeSettings()" class="px-4 py-2 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 text-xs font-semibold">İptal</button>
        <button onclick="saveSettings()" class="px-4 py-2 rounded-lg bg-cyan-600 hover:bg-cyan-500 text-white text-xs font-semibold shadow-lg shadow-cyan-500/30">Kaydet ve Uygula</button>
      </div>
    </div>
  </div>

  <script>
    let isStreaming = false;
    let activeLeftTab = 'files';
    let cachedSettings = {};

    function switchLeftTab(tab) {
      activeLeftTab = tab;
      const filesTab = document.getElementById('tab-files');
      const sessTab = document.getElementById('tab-sessions');
      const filesBtn = document.getElementById('tab-files-btn');
      const sessBtn = document.getElementById('tab-sessions-btn');

      if (tab === 'files') {
        filesTab.classList.remove('hidden');
        sessTab.classList.add('hidden');
        filesBtn.className = 'flex-1 py-1.5 text-xs font-semibold text-cyan-400 border-b-2 border-cyan-400 flex items-center justify-center space-x-1.5';
        sessBtn.className = 'flex-1 py-1.5 text-xs font-semibold text-gray-400 border-b-2 border-transparent hover:text-gray-200 flex items-center justify-center space-x-1.5';
        loadFiles();
      } else {
        filesTab.classList.add('hidden');
        sessTab.classList.remove('hidden');
        sessBtn.className = 'flex-1 py-1.5 text-xs font-semibold text-cyan-400 border-b-2 border-cyan-400 flex items-center justify-center space-x-1.5';
        filesBtn.className = 'flex-1 py-1.5 text-xs font-semibold text-gray-400 border-b-2 border-transparent hover:text-gray-200 flex items-center justify-center space-x-1.5';
        loadSessions();
      }
    }

    async function loadStatus() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        document.getElementById('header-provider').innerText = (data.provider || 'OLLAMA').toUpperCase();
        document.getElementById('header-model').innerText = (data.model || 'qwen3.8').split('/').pop().split(':')[0];
        document.getElementById('header-quota').innerText = data.quota || 'Aktif';
        document.getElementById('header-dir').innerText = data.project_dir_name || 'Yeni Proje';
        document.getElementById('left-session-id').innerText = data.session_id || 'sess-xxxx';
        document.getElementById('left-folder-path').innerText = data.project_dir || '';
      } catch (e) { console.error(e); }
    }

    async function loadFiles() {
      try {
        const res = await fetch('/api/files');
        const files = await res.json();
        const tree = document.getElementById('file-tree');
        tree.innerHTML = '';
        if (!files || files.length === 0) {
          tree.innerHTML = '<div class="text-xs text-gray-500 p-2">Henüz üretilmiş dosya yok.</div>';
          return;
        }
        files.forEach(f => {
          const div = document.createElement('div');
          div.className = 'flex items-center space-x-2 p-1.5 rounded hover:bg-gray-800/60 cursor-pointer text-gray-300 hover:text-white truncate';
          div.innerHTML = `<i class="fa-regular fa-file-code text-cyan-400 text-xs"></i><span class="truncate">${f}</span>`;
          div.onclick = () => viewFile(f);
          tree.appendChild(div);
        });
      } catch (e) { console.error(e); }
    }

    async function loadSessions() {
      try {
        const res = await fetch('/api/sessions');
        const sessions = await res.json();
        const container = document.getElementById('session-list');
        container.innerHTML = '';
        if (!sessions || sessions.length === 0) {
          container.innerHTML = '<div class="text-xs text-gray-500 p-2">Kayıtlı oturum bulunamadı.</div>';
          return;
        }
        sessions.forEach(s => {
          const div = document.createElement('div');
          div.className = 'p-2.5 rounded-xl bg-gray-900/70 border border-gray-800/80 hover:border-cyan-500/50 cursor-pointer transition-all';
          div.innerHTML = `
            <div class="flex items-center justify-between mb-1">
              <span class="font-bold text-gray-200 truncate">${s.title}</span>
              <span class="text-[10px] text-gray-500 font-mono">${s.mtime_str || ''}</span>
            </div>
            <div class="flex items-center justify-between text-[11px] text-gray-400">
              <span class="font-mono text-cyan-400">${s.session_id}</span>
              <span><i class="fa-regular fa-file mr-1"></i>${s.file_count || 0} dosya</span>
            </div>
          `;
          div.onclick = () => switchSession(s);
          container.appendChild(div);
        });
      } catch (e) { console.error(e); }
    }

    async function switchSession(s) {
      try {
        const res = await fetch('/api/sessions/switch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: s.session_id, path: s.path })
        });
        const data = await res.json();
        if (data.ok) {
          const chatCont = document.getElementById('chat-container');
          chatCont.innerHTML = '';
          if (data.history && data.history.length > 0) {
            data.history.forEach(m => appendMessage(m.role, m.content));
          } else {
            appendMessage('assistant', `<strong>${s.title}</strong> (${s.session_id}) oturumuna geçildi! Kodlamaya devam edebilirsiniz.`);
          }
          loadStatus();
          switchLeftTab('files');
        }
      } catch (e) { alert('Oturuma geçilemedi: ' + e); }
    }

    async function newSession() {
      try {
        const res = await fetch('/api/sessions/new', { method: 'POST' });
        const data = await res.json();
        if (data.ok) {
          const chatCont = document.getElementById('chat-container');
          chatCont.innerHTML = '';
          appendMessage('assistant', `✨ Yeni bir oturum başlatıldı (${data.session_id}). Projenizi anlatın, hemen inşa edelim! 🚀`);
          loadStatus();
          switchLeftTab('files');
        }
      } catch (e) { alert('Yeni oturum açılamadı: ' + e); }
    }

    async function viewFile(path) {
      document.getElementById('viewer-title').innerText = path;
      const viewer = document.getElementById('code-viewer');
      viewer.innerText = 'Yükleniyor...';
      try {
        const res = await fetch(`/api/file-content?path=${encodeURIComponent(path)}`);
        const text = await res.text();
        viewer.innerText = text;
        document.getElementById('viewer-size').innerText = `${(text.length / 1024).toFixed(1)} KB`;
      } catch (e) {
        viewer.innerText = 'Dosya okunamadı: ' + e;
      }
    }

    async function onProviderChange(provName, selectedModelId = '') {
      const modelSelect = document.getElementById('setting-model');
      const indicator = document.getElementById('model-loading-indicator');
      indicator.classList.remove('hidden');
      modelSelect.innerHTML = '<option>Modeller taranıyor...</option>';

      try {
        const res = await fetch(`/api/models?provider=${encodeURIComponent(provName)}`);
        const models = await res.json();
        modelSelect.innerHTML = '';
        models.forEach(m => {
          const opt = document.createElement('option');
          opt.value = m.id;
          opt.innerText = m.label;
          if (selectedModelId && (m.id === selectedModelId || m.name === selectedModelId)) {
            opt.selected = true;
          }
          modelSelect.appendChild(opt);
        });
      } catch (e) {
        modelSelect.innerHTML = '<option value="">Model listesi alınamadı</option>';
      } finally {
        indicator.classList.add('hidden');
      }
    }

    async function openSettings() {
      try {
        const res = await fetch('/api/settings');
        const s = await res.json();
        cachedSettings = s;
        
        // Sağlayıcı listesini doldur
        const provSelect = document.getElementById('setting-provider');
        provSelect.innerHTML = '';
        let activeProvName = 'ollama';
        (s.providers || []).forEach(p => {
          const opt = document.createElement('option');
          opt.value = p.name;
          opt.innerText = p.label + (p.is_active ? ' (Aktif)' : '');
          if (p.is_active) {
            opt.selected = true;
            activeProvName = p.name;
          }
          provSelect.appendChild(opt);
        });

        // Modelleri çek ve aktif modeli seç
        await onProviderChange(activeProvName, s.default_model || '');

        document.getElementById('setting-mode').value = s.execution_mode || 'interactive';
        document.getElementById('setting-think').checked = !!s.think_mode;
        document.getElementById('setting-temp').value = s.temperature || 0.2;
        document.getElementById('temp-val').innerText = s.temperature || 0.2;
        document.getElementById('setting-tokens').value = s.max_tokens || 8192;
        document.getElementById('tokens-val').innerText = s.max_tokens || 8192;
        document.getElementById('setting-permission').value = s.permission_mode || 'session_allow';
        document.getElementById('setting-fix-tries').value = s.micro_fix_max_tries || 3;
        document.getElementById('setting-autonomy-cap').value = s.full_autonomy_cap || 100;
        document.getElementById('setting-repomap-tokens').value = s.repomap_tokens || 2048;
        document.getElementById('setting-audit-log').checked = !!s.auto_audit_log;
        document.getElementById('setting-warmup').checked = !!s.warmup;

        document.getElementById('settings-modal').classList.remove('hidden');
      } catch (e) { alert('Ayarlar yüklenemedi: ' + e); }
    }

    function closeSettings() {
      document.getElementById('settings-modal').classList.add('hidden');
    }

    async function saveSettings() {
      const payload = {
        provider: document.getElementById('setting-provider').value,
        model: document.getElementById('setting-model').value,
        execution_mode: document.getElementById('setting-mode').value,
        think_mode: document.getElementById('setting-think').checked,
        temperature: parseFloat(document.getElementById('setting-temp').value),
        max_tokens: parseInt(document.getElementById('setting-tokens').value),
        permission_mode: document.getElementById('setting-permission').value,
        micro_fix_max_tries: parseInt(document.getElementById('setting-fix-tries').value),
        full_autonomy_cap: parseInt(document.getElementById('setting-autonomy-cap').value),
        repomap_tokens: parseInt(document.getElementById('setting-repomap-tokens').value),
        auto_audit_log: document.getElementById('setting-audit-log').checked,
        warmup: document.getElementById('setting-warmup').checked,
      };

      try {
        const res = await fetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (data.ok) {
          closeSettings();
          loadStatus();
          alert('✅ Tüm ayarlar başarıyla kaydedildi ve uygulandı!');
        }
      } catch (e) { alert('Ayarlar kaydedilemedi: ' + e); }
    }

    async function sendMessage(e) {
      e.preventDefault();
      if (isStreaming) return;
      const input = document.getElementById('prompt-input');
      const text = input.value.trim();
      if (!text) return;

      input.value = '';
      appendMessage('user', text);

      isStreaming = true;
      const sendBtn = document.getElementById('send-btn');
      sendBtn.disabled = true;
      sendBtn.classList.add('opacity-50');

      const botMsgDiv = appendMessage('assistant', '');
      const contentSpan = botMsgDiv.querySelector('.msg-content');

      try {
        const response = await fetch('/api/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ prompt: text })
        });

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let fullText = '';

        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          const chunk = decoder.decode(value, { stream: true });
          fullText += chunk;
          contentSpan.innerHTML = marked.parse(fullText);
          document.getElementById('chat-container').scrollTop = document.getElementById('chat-container').scrollHeight;
        }
      } catch (err) {
        contentSpan.innerText = 'Hata oluştu: ' + err;
      } finally {
        isStreaming = false;
        sendBtn.disabled = false;
        sendBtn.classList.remove('opacity-50');
        loadFiles();
        loadStatus();
      }
    }

    function appendMessage(role, text) {
      const container = document.getElementById('chat-container');
      const div = document.createElement('div');
      const isUser = role === 'user';
      div.className = `p-4 rounded-xl max-w-3xl ${isUser ? 'ml-auto bg-cyan-950/30 border border-cyan-800/40 text-white' : 'glass-card text-gray-200'}`;
      div.innerHTML = `
        <div class="flex items-center space-x-2 ${isUser ? 'text-cyan-300' : 'text-blue-400'} font-bold mb-1.5 text-xs">
          <i class="fa-solid ${isUser ? 'fa-user' : 'fa-robot'}"></i>
          <span>${isUser ? 'Sen' : 'MYF-Agent'}</span>
        </div>
        <div class="msg-content prose prose-invert text-sm leading-relaxed">${text ? marked.parse(text) : '<span class="animate-pulse">● ● ●</span>'}</div>
      `;
      container.appendChild(div);
      container.scrollTop = container.scrollHeight;
      return div;
    }

    async function stopServer() {
      if (confirm("MYF Agent Web Sunucusunu kapatmak istediğinize emin misiniz?")) {
        try {
          await fetch('/api/shutdown', { method: 'POST' });
        } catch(e) {}
        document.body.innerHTML = `
          <div class="h-screen flex flex-col items-center justify-center bg-gray-950 text-gray-400 font-sans space-y-4">
            <div class="w-16 h-16 rounded-full bg-red-950/60 border border-red-800/80 flex items-center justify-center text-2xl text-red-400 shadow-xl">
              <i class="fa-solid fa-power-off"></i>
            </div>
            <h1 class="text-xl font-bold text-gray-200">Web Sunucusu Başarıyla Durduruldu</h1>
            <p class="text-sm text-gray-500">Port serbest bırakıldı. Bu sekmeyi güvenle kapatabilirsiniz.</p>
          </div>
        `;
      }
    }

    // İlk yüklemeler
    loadStatus();
    loadFiles();
    setInterval(loadStatus, 10000);
    setInterval(() => {
      if (activeLeftTab === 'files') loadFiles();
    }, 15000);
  </script>
</body>
</html>
"""


class WebHarnessHandler(BaseHTTPRequestHandler):
    """DeepSeek Harness tarzı REST ve Streaming HTTP sunucu işleyicisi."""

    coordinator = None
    # ThreadingHTTPServer altında paralel istekler aynı coordinator/history
    # nesnesine yazabilir; /api/chat, /api/sessions/* ve /api/settings bu
    # kilidi kullanarak birbirini bozmadan sırayla çalışır.
    _coordinator_lock = threading.Lock()

    def log_message(self, format, *args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))

        elif path == "/api/status":
            prov = get_active_provider_name()
            quota_text = quota_engine.get_provider_quota_display(prov, LLM_PARAMS.get("api_key", ""))
            curr = session_manager.current_session
            pdir = str(curr.project_dir) if curr else get_output_dir()
            active_model = AGENT_MODELS.get("coordinator") or settings.default_model
            data = {
                "provider": prov,
                "model": active_model,
                "quota": quota_text,
                "project_dir": pdir,
                "project_dir_name": Path(pdir).name,
                "session_id": curr.session_id if curr else "sess",
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))

        elif path == "/api/models":
            params = parse_qs(parsed.query)
            prov = params.get("provider", [get_active_provider_name()])[0]
            models = get_available_models(prov)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(models).encode("utf-8"))

        elif path == "/api/sessions":
            sessions = session_manager.list_all_sessions()
            clean_sessions = []
            for s in sessions:
                clean_sessions.append({
                    "session_id": s["session_id"],
                    "title": s["title"],
                    "folder_name": s["folder_name"],
                    "path": s["path"],
                    "file_count": s.get("file_count", 0),
                    "msg_count": s.get("msg_count", 0),
                    "mtime_str": s.get("mtime_str", ""),
                })
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(clean_sessions).encode("utf-8"))

        elif path == "/api/settings":
            provs = list_providers()
            data = {
                "execution_mode": settings.execution_mode,
                "think_mode": settings.think_mode,
                "temperature": settings.temperature,
                "max_tokens": settings.max_tokens,
                "default_model": settings.default_model,
                "coordinator_name": settings.coordinator_name,
                "permission_mode": settings.permission_mode,
                "micro_fix_max_tries": settings.micro_fix_max_tries,
                "full_autonomy_cap": settings.full_autonomy_cap,
                "repomap_tokens": settings.repomap_tokens,
                "auto_audit_log": settings.auto_audit_log,
                "warmup": settings.warmup,
                "providers": provs,
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))

        elif path == "/api/files":
            files = list_output_files()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(files).encode("utf-8"))

        elif path == "/api/file-content":
            params = parse_qs(parsed.query)
            target = params.get("path", [""])[0]
            out_dir = Path(get_output_dir()).resolve()
            # GÜVENLİK: target bir mutlak yol veya "../" içerebilir ve pathlib
            # bunu proje dizini dışına taşırabilir (path traversal / arbitrary
            # file read). Birleştirilmiş yolu çözüp proje dizini altında
            # kaldığını doğrulamadan asla dosya açma.
            file_p = (out_dir / target).resolve()
            if out_dir not in file_p.parents and file_p != out_dir:
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"Erisim reddedildi: proje disina cikilamaz")
            elif file_p.exists() and file_p.is_file():
                try:
                    content = file_p.read_text(encoding="utf-8", errors="replace")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(content.encode("utf-8"))
                except Exception as exc:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(f"Hata: {exc}".encode("utf-8"))
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Dosya bulunamadi")

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/shutdown":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(b'{"status": "stopped"}')
            def _kill():
                time.sleep(0.4)
                os._exit(0)
            threading.Thread(target=_kill, daemon=True).start()
            return

        elif path == "/api/sessions/switch":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            try:
                payload = json.loads(body)
                target_path = payload.get("path", "")
                p = Path(target_path)
                s_obj = Session.load_from_dir(p)
                if s_obj:
                    with WebHarnessHandler._coordinator_lock:
                        session_manager.current_session = s_obj
                        set_output_dir(str(s_obj.project_dir))
                        if not WebHarnessHandler.coordinator:
                            WebHarnessHandler.coordinator = CoordinatorAgent()
                        WebHarnessHandler.coordinator.history = list(s_obj.conversation_history)
                    history_clean = [{"role": m.get("role", "user"), "content": m.get("content", "")} for m in s_obj.conversation_history]
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps({"ok": True, "history": history_clean}).encode("utf-8"))
                    return
            except Exception:
                pass
            self.send_response(400)
            self.end_headers()

        elif path == "/api/sessions/new":
            s = session_manager.create_new_session(title="Yeni Oturum", slug="yeni_proje")
            with WebHarnessHandler._coordinator_lock:
                if not WebHarnessHandler.coordinator:
                    WebHarnessHandler.coordinator = CoordinatorAgent()
                WebHarnessHandler.coordinator.reset(new_session=False)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "session_id": s.session_id, "project_dir": str(s.project_dir)}).encode("utf-8"))

        elif path == "/api/settings":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            try:
                payload = json.loads(body)
                if "execution_mode" in payload:
                    settings.execution_mode = payload["execution_mode"]
                if "think_mode" in payload:
                    settings.think_mode = bool(payload["think_mode"])
                if "temperature" in payload:
                    settings.temperature = float(payload["temperature"])
                if "max_tokens" in payload:
                    settings.max_tokens = int(payload["max_tokens"])
                if "permission_mode" in payload:
                    settings.permission_mode = payload["permission_mode"]
                if "micro_fix_max_tries" in payload:
                    settings.micro_fix_max_tries = int(payload["micro_fix_max_tries"])
                if "full_autonomy_cap" in payload:
                    settings.full_autonomy_cap = int(payload["full_autonomy_cap"])
                if "repomap_tokens" in payload:
                    settings.repomap_tokens = int(payload["repomap_tokens"])
                if "auto_audit_log" in payload:
                    settings.auto_audit_log = bool(payload["auto_audit_log"])
                if "warmup" in payload:
                    settings.warmup = bool(payload["warmup"])

                # Sağlayıcı ve Model Değişimi
                if "provider" in payload:
                    prov = payload["provider"]
                    if prov != get_active_provider_name():
                        set_active_provider(prov)

                if "model" in payload and payload["model"]:
                    chosen_m = payload["model"]
                    settings.default_model = chosen_m
                    settings.planning_model = chosen_m
                    settings.code_model = chosen_m
                    settings.micro_fix_model = chosen_m
                    settings.coordinator_model = chosen_m

                settings.save()
                reload_config()

                if WebHarnessHandler.coordinator:
                    with WebHarnessHandler._coordinator_lock:
                        WebHarnessHandler.coordinator.refresh_settings()

                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')
                return
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))

        elif path == "/api/chat":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            try:
                payload = json.loads(body)
                user_prompt = payload.get("prompt", "").strip()
            except Exception:
                user_prompt = ""

            if not user_prompt:
                self.send_response(400)
                self.end_headers()
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            if not WebHarnessHandler.coordinator:
                with WebHarnessHandler._coordinator_lock:
                    if not WebHarnessHandler.coordinator:
                        WebHarnessHandler.coordinator = CoordinatorAgent()

            def token_callback(token: str, token_type: str = "content"):
                if token_type == "content" and token:
                    data = token.encode("utf-8")
                    chunk_header = f"{len(data):X}\r\n".encode("utf-8")
                    try:
                        self.wfile.write(chunk_header + data + b"\r\n")
                        self.wfile.flush()
                    except Exception:
                        pass

            try:
                with WebHarnessHandler._coordinator_lock:
                    full_resp, is_pipeline = WebHarnessHandler.coordinator.chat(
                        user_prompt,
                        on_token=token_callback
                    )
            except Exception as exc:
                err_data = f"\n[HATA: {exc}]".encode("utf-8")
                self.wfile.write(f"{len(err_data):X}\r\n".encode("utf-8") + err_data + b"\r\n")

            # Final 0 chunk to close chunked response
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:
                pass


def start_web_server(port: int = 3005, open_browser: bool = True):
    """Web Agent sunucusunu başlatır."""
    server_address = ("0.0.0.0", port)
    # ThreadingHTTPServer: /api/chat uzun süren bir streaming isteğidir; eski
    # tek-thread'li HTTPServer bu istek bitene kadar /api/status, /api/files
    # gibi diğer tüm uçları da bloke ediyordu (arayüz "donmuş" görünüyordu).
    httpd = ThreadingHTTPServer(server_address, WebHarnessHandler)
    url = f"http://localhost:{port}"

    print("\n" + "=" * 60)
    print("  🚀 MYF AI Web Agent & Dashboard Başlatıldı!")
    print(f"  🌐 URL: {url}")
    print("  💻 Kapatmak için: Ctrl+C")
    print("=" * 60 + "\n")

    if open_browser:
        try:
            threading.Thread(target=lambda: (time.sleep(0.8), webbrowser.open(url)), daemon=True).start()
        except Exception:
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  [KAPATILIYOR] Web sunucusu durduruldu.")
        httpd.server_close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="MYF AI Web Agent")
    parser.add_argument("--port", type=int, default=3005, help="Web UI portu (varsayılan: 3005)")
    parser.add_argument("--no-browser", action="store_true", help="Tarayıcıyı otomatik açma")
    args = parser.parse_args()

    start_web_server(port=args.port, open_browser=not args.no_browser)
