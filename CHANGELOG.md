# 📝 Değişiklik Günlüğü (Changelog)

Bu projedeki tüm önemli değişiklikler bu dosyada belgelenmektedir. Format [Keep a Changelog](https://keepachangelog.com/tr/1.0.0/) standardına uygundur.

---

## [1.5.0] - 2026-09-18

### 🤖 Yeni Yerel GGUF Modelleri & llama.cpp Dinamik Model Entegrasyonu

#### Yeni Modeller (`llama_server/models/`)
- **Qwen2.5-Coder-7B-Instruct-abliterated-Q5_K_M** (5.1 GB) — Yüksek kaliteli Q5_K_M sansürsüz kodlama modeli
- **Qwen2.5-Coder-14B-Instruct-abliterated-IQ3_M** (6.5 GB) — Büyük 14B sansürsüz kodlama modeli
- **Huihui-Qwen3.8-27B-abliterated-UD-DW-Q4_K_M** (15.4 GB) — En güçlü lokal model, 27B sansürsüz

#### Web UI — `llama.cpp` Provider Dinamik Model Listesi
- `get_available_models("llama_cpp")` artık `providers_config.json`'daki `available_models` bölümünü okuyarak model adı, boyut ve açıklamayla birlikte dropdown'a sunar
- Config boşsa `llama_server/models/` klasörünü fiziksel olarak tarayarak GGUF dosyalarını listeler (fallback)

#### CLI — `llama_cpp` Provider Model Listesi
- `/provider` komutuyla `llama.cpp` seçildiğinde artık 6 model listelenir
- `list_provider_models()` fonksiyonundan `"default"` gibi meta key'ler filtrelendi

#### `providers_config.json` — `llama_cpp` Güncellendi
- `default_context_window`: `8192` → `32768`
- `available_models` bölümü eklendi: her model için `size`, `context_window`, `description` alanları
- `model_context_windows` tüm 6 modelle güncellendi; 27B için `16384` (VRAM koruması)

## [1.4.0] - 2026-09-18

### 🤝 Nezaket / Teşekkür ("eyw", "sağol", "teşekkürler") Kuralı & Gereksiz Test Döngüsü Engeli
- **Koordinatör Sistem Promptu Nezaket Kuralı (`coordinator_agent.py`):**
  - Kullanıcı "eyw", "sağol", "teşekkürler", "tamamdır", "eline sağlık" gibi memnuniyet iletisi yazdığında ajanın projeyi veya testleri baştan tekrar çalıştırması engellendi.
  - Nezaketle yanıt verme ve kullanıcıdan yeni bir istek bekleme talimatı kural olarak enjekte edildi.

## [1.3.0] - 2026-09-18

### 🧠 Çok Aşamalı Düşünme & Sıralı Akıl Yürütme Gösterimi ("Düşünce 1, 2, 3...")
- **Adımlı Terminal Düşünme Başlıkları (`chat.py`):**
  - CLI streaming akışında çok adımlı eylemler yürütülürken her akıl yürütme turu artık numaralandırılarak gösterilir (`┌── 💭 [DÜŞÜNCE 1 / REASONING]`, `└── 🎯 [YANIT 1 / MODEL ÇIKTISI]`, `┌── 💭 [DÜŞÜNCE 2 / REASONING]`...).
  - Düşünme adımları bittiğinde ve araçlar çalıştırılıp model sonraki tura geçtiğinde sayaç otomatik artarak adımlar arasındaki kronolojik ayrımı netleştirir.
  - Akıl yürütme yapmayan modellerde (GPT-4o vb.) düşünce başlıkları basılmadan temiz çıktı akışı sağlanır.

## [1.2.0] - 2026-09-17

### 📦 NPM & PyPI Resmi Registry Normalizasyonu ve Squat Filtresi
- **Terk Edilmiş Kukla Paket (0.0.3) Önleme:**
  - NPM kayıt defterinde 12 yıl önce terk edilmiş `nextjs: 0.0.3` gibi sahte/kukla paketlerin DuckDuckGo arama sonuçlarında ve registry sorgularında gerçek Next.js (v16.3.5) yerine geçmesi engellendi.
  - `_SQUAT_URL_PATTERN` ile `npmjs.com/package/(nextjs|reactjs|vuejs|expressjs)` bağlantıları web arama sonuçlarından elendi.
  - `nextjs`, `next.js` → `next`; `reactjs` → `react`; `vuejs` → `vue`; `tailwind` → `tailwindcss`; `sveltekit` → `@sveltejs/kit`; `nestjs` → `@nestjs/core`; `expressjs` → `express`; `angularjs` → `@angular/core` alias eşleştirmeleri tamamlandı.
  - Açık paket arama komutları (`package nextjs`, `npm i nextjs`, `pnpm add`, `yarn add`, `pip install`) zorunlu alias süzgecinden geçirildi.
- **Birim Testleri:** `test_enhancements.py` içine paket adı normalizasyonu ve squat URL filtreleme testleri eklenerek tüm 74 test yeşile çekildi.

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
