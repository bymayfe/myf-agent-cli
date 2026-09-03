# 📝 Değişiklik Günlüğü (Changelog)

Bu projedeki tüm önemli değişiklikler bu dosyada belgelenmektedir. Format [Keep a Changelog](https://keepachangelog.com/tr/1.0.0/) standardına uygundur.

---

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
