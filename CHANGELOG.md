# 📝 Değişiklik Günlüğü (Changelog)

Bu projedeki tüm önemli değişiklikler bu dosyada belgelenmektedir. Format [Keep a Changelog](https://keepachangelog.com/tr/1.0.0/) standardına uygundur.

---

## [1.1.0] - 2026-09-17

### 🌐 Agent-Reach Hibrit Arama & Platform Entegrasyonu
- **Panniantong/Agent-Reach Entegrasyonu:**
  - Jina Reader ve DuckDuckGo tabanlı arama motoruna GitHub (`gh`), YouTube (`yt-dlp`), V2EX, RSS/Atom, Reddit ve Twitter kanalları entegre edildi (`reach_engine.py`).
  - Akıllı yönlendirici (`reach_engine.search`) sorguyu veya URL'i inceleyerek en uygun platform kanalına otomatik yönlendirir; eksik araç durumunda DDG/Jina fallback yapar.
  - `/reach github`, `/reach youtube`, `/reach v2ex`, `/reach rss`, `/reach status` alt komutları eklendi.
  - Bağımlılık kurulum aracı eklendi (`reach_setup.py`).

### ⏱️ Cold-Start & Bulut API Gecikme İzleyicisi (ColdStartWatcher)
- **Canlı Terminal İzleme:**
  - `llm_client.py` ve `coordinator_agent.py` içine `ColdStartWatcher` bağlam yöneticisi entegre edildi.
  - Bulut API'si (NVIDIA NIM, Moonshot vb.) veya yerel büyük modeller uyanırken/kuyrukta beklerken terminalde canlı süre sayacı ve bilgilendirme basılır.

## [1.0.0] - 2026-09-03

### 🎉 İlk Kararlı Sürüm (Initial Public Release)

#### 🚀 Yeni Özellikler
- **5 Aşamalı Multi-Agent Pipeline Motoru:**
  - `Product Manager`: Detaylı gereksinim ve user-story analizi.
  - `Software Architect`: Modüler dizin ve bileşen mimarisi tasarımı.
  - `Developer`: Eksiksiz kaynak kod üretimi ve dosya sistemi entegrasyonu.
  - `QA Tester`: Fiziksel test, derleme denetimi ve syntax kontrolü.
  - `Documentation`: Mimari rapor ve kullanım kılavuzu oluşturma.
- **Otonom Micro-Fix & Escalation Döngüsü:**
  - Derleme veya çalıştırma hatası alan dosyalarda maksimum 3 adımlı hızlı onarım.
  - 3 denemede çözülemeyen durumlarda büyük modele eskalasyon.
  - Stuck-loop (aynı hatada takılma) tespiti ve alternatif prompt enjeksiyonu.
- **Kesintisiz Devam & Bağlam Yönetimi (Claude / Antigravity Style):**
  - Model token limitine ulaştığında (`finish_reason === "length"`) veya açık kalan kod bloğu tespit edildiğinde otomatik algılama.
  - Tekrar veya giriş cümlesi kurmadan, tam olarak kalınan son karakterden devam etme talimatı.
- **RepoMap & AST Kod Tabanı Ayrıştırma:**
  - Tree-sitter ile fonksiyon, sınıf ve interface sembol haritası çıkarma.
  - PageRank algoritması ile token bütçesine optimize edilmiş kod tabanı grafı.
- **Geniş Sağlayıcı Desteği:**
  - Lokal: Ollama (NDJSON stream), LM Studio, llama.cpp CUDA (FlashAttention-2).
  - Cloud: NVIDIA NIM (DGX Cloud), Moonshot AI (Kimi K2), OpenRouter.
- **İnteraktif CLI Arayüzü (prompt_toolkit):**
  - Streaming token akışı, düşünme (`<think>`) blokları, `/komut` hub'ı ve renkli bildirimler.
