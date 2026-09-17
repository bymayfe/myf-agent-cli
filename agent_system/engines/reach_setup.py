"""
reach_setup.py - Agent-Reach ve yan araçlarının kurulumunu sağlar.
"""
import subprocess
import sys
from pathlib import Path

def setup_reach_channels(channels=None):
    """Eksik bağımlılıkları venv içine kurar."""
    print("🔄 Reach Engine ek araçları kuruluyor...")
    venv_python = sys.executable
    
    # yt-dlp kurulumu (YouTube kanalı için)
    try:
        subprocess.run([venv_python, "-m", "pip", "install", "-U", "yt-dlp[default]"], check=True)
        print("✅ yt-dlp başarıyla kuruldu veya güncellendi.")
    except Exception as e:
        print(f"❌ yt-dlp kurulum hatası: {e}")

    # agent-reach kurulumu (eğer yoksa)
    try:
        subprocess.run([venv_python, "-m", "pip", "install", "git+https://github.com/Panniantong/Agent-Reach.git"], check=True)
        print("✅ agent-reach başarıyla kuruldu.")
    except Exception as e:
        print(f"❌ agent-reach kurulum hatası: {e}")
        
    print("🚀 Kurulum tamamlandı! `/reach status` ile durumu kontrol edebilirsiniz.")

if __name__ == "__main__":
    setup_reach_channels()
