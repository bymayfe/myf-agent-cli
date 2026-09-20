# 📝 Değişiklik Günlüğü (Changelog)

Bu projedeki tüm önemli değişiklikler bu dosyada belgelenmektedir. Format [Keep a Changelog](https://keepachangelog.com/tr/1.0.0/) standardına uygundur.

---

## [1.9.0] - 2026-09-20

### ⚡ Canlı Streaming (stream=True), Yerel Model Tespiti ve llama.cpp Ayarları
- **Gerçek Zamanlı Canlı Streaming & Token/Hız Sayacı (`llm_client.py`):**
  - `call_llm` fonksiyonuna `stream=True` desteği entegre edildi.
  - İlk token (TTFT) geldiği andan itibaren terminalde tek satırda anlık güncellenen (`\r`) canlı token sayacı ve hız göstergesi eklendi: `[INFO] ⚡ Yerel model üretiyor: {token} token ({tok_s} tok/s - {elapsed}s)...`
  - İstek bittiğinde tamamlama özeti basılır: `[INFO] ⚡ Yanıt alındı: {token} token ({elapsed}s — {tok_s} tok/s)`.
  - Ollama REST istemcisi (`call_ollama_chat`) için `on_token` callback'i bağlanarak aynı canlı sayaç aktifleştirildi.
  - Streaming desteklemeyen sağlayıcılar için otomatik senkron (non-streaming) geri dönüş (fallback) mekanizması eklendi.
- **Akıllı Yerel / Bulut Sağlayıcı Tespiti (`is_local_endpoint` & `ColdStartWatcher`):**
  - `localhost`, `127.0.0.1`, `8080`, `11434`, `1234` veya `llama_cpp`, `ollama`, `lm_studio` sağlayıcıları için `is_local_endpoint` tespiti eklendi.
  - Yerel modellerde yanıltıcı olan "Bulut sağlayıcı kuyruğu yoğun" ve "Cold-Start" mesajları engellendi; yerine `🧠 Yerel model promptu işliyor...` ve `⚡ Yerel model yanıtı üretiyor...` durum bildirimleri getirildi.
  - `coordinator_agent.py` ve `llm_client.py` içinde TTFT bekleme süresi şeffaf biçimde kapsandı.
- **llama.cpp Sunucu Yönetimi & Git Entegrasyonu (`llama_server/`):**
  - Donanım profiline (AMD Ryzen 7 8845HS + NVIDIA RTX 4070 8GB VRAM) göre optimize edilmiş başlatma ve durdurma betikleri (`llama_server_baslat.sh`, `llama_server_durdur.sh`, Windows `.bat` dosyaları, DeepSeek indirme betiği ve detaylı `README.md`) Git reposuna dahil edildi.
  - Gigabaytlarca yer kaplayan `.gguf` model ağırlıklarının Git'e girmesi engellenirken (`.gitignore`), repo klonlandığında tek komutla yerel AI sunucusunun ayağa kaldırılabilmesi sağlandı.

## [1.8.0] - 2026-09-20

### 🛡️ Pipeline Motorları İngilizce Prompt Mimarisi & CLI Sanal Ortam (.venv) Dayanıklılığı
- **Tüm İç Pipeline & Hata Onarım Motorlarının İngilizceye Taşınması (`agent_system`):**
  - `agents.py`: `_context_label()` ve `_task_hint()` etiket ve talimatları İngilizceye uyarlandı.
  - `fix_engine.py`: `MicroFixEngine` ve `EscalationEngine` cerrahi onarım promptları İngilizceye çevrildi.
  - `subagent_engine.py`: Lider Orkestratör planlama ve retry promptları İngilizceye çevrildi.
  - `profiling_engine.py`: `StuckLoopDetector.HINTS` 4 aşamalı döngü kırma stratejileri İngilizceye taşındı.
  - `context_budgeter.py`: `[SUMMARIZED CONVERSATION HISTORY]` başlığı uyarlandı ve 74/74 pytest testi başarıyla geçti.
- **CLI Sanal Ortam (.venv) ve litellm Bağımlılık İyileştirmesi (`chat.py`, `main.py`, `launch.sh`):**
  - Taşınan dizinler için `.venv` yolları onarıldı; `chat.py` ve `main.py` içerisine `.venv/lib/python*/site-packages` otomatik keşif mekanizması eklendi.
  - `launch.sh` doğrudan `.venv/bin/python3` üzerinden çalışacak şekilde kilitlendi.

## [1.7.0] - 2026-09-20

### 🌐 Sistem Promptlarının İngilizceye Taşınması & Türkçe Çıktı Kuralı
- **Koordinatör Sistem Promptu ve Mod Şablonları (`coordinator_agent.py`):**
  - Prompt şablonu, agent reach arama altyapı talimatları, mod açıklamaları ve brief başlıkları İngilizceye çevrildi.
- **Zorunlu Türkçe İletişim (OUTPUT LANGUAGE):**
  - Modelin kullanıcıya plan sunarken ve adımları açıklarken daima akıcı Türkçe kullanması, kod ve marker'ların İngilizce kalması kuralı eklendi.
- **Alt Ajan ve Pipeline Rol Şablonları (`subagent_engine.py`, `role_templates.json`):**
  - Tüm yerleşik alt uzmanlar (`researcher`, `architect`, `developer`, `debugger`, `tester`) ve hazır pipeline rolleri (`product_manager`, `software_architect`, `qa_tester` vb.) için sistem promptları İngilizceye taşındı.

## [1.6.0] - 2026-09-20

### 🛡️ Web Sunucusu Güvenliği & Eşzamanlı İstek Desteği
- **Path Traversal / Keyfi Dosya Okuma Açığı Kapatıldı (`web_server.py`):**
  - `/api/file-content` uç noktasında mutlak yol (`/etc/passwd`) veya `../` parametreleri ile proje dizini dışına çıkılması engellendi; birleşik yol `resolve()` edilerek proje sınırları doğrulanır, ihlal durumunda `403 Forbidden` döner.
- **Eşzamanlı İstek & Donma Koruması (`ThreadingHTTPServer` & `_coordinator_lock`):**
  - Tek iş parçacıklı `HTTPServer` yerine `ThreadingHTTPServer`'a geçildi; `/api/chat` streaming yanıtı sürerken arayüzün kilitlenmesi önlendi. Paylaşılan koordinatör nesnesine eşzamanlı erişim thread kilidi ile korundu.
- **Hata Yanıtlarında JSON Güvenliği (`/api/settings`):**
  - Exception mesajlarının düz string interpolasyonu yerine `json.dumps()` ile serileştirilmesi sağlanarak bozuk JSON üretimi engellendi.

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
