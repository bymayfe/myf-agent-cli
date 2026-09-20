# 🤖 MYF AI — llama.cpp Yerel Model Sunucusu (llama-server)

Bu dizin, yüksek performanslı yerel yapay zeka çıkarımı (inference) için optimize edilmiş `llama-server` başlatma ve yönetim betiklerini içerir.

## 🚀 Donanım & Performans Optimizasyonları

Betikler varsayılan olarak aşağıdaki donanım profiline göre yapılandırılmıştır:
- **İşlemci:** AMD Ryzen 7 8845HS (8 Core / 16 Thread)
- **Ekran Kartı:** NVIDIA GeForce RTX 4070 (8GB GDDR6 VRAM, CUDA)

### Aktif Parametreler:
- `--flash-attn on`: FlashAttention ile VRAM tasarrufu ve %20-30 hız kazancı.
- `--cache-type-k q8_0 --cache-type-v q8_0`: 32K context penceresini 8GB VRAM'e sığdırmak için 8-bit KV-Cache sıkıştırması.
- `-t 8`: 8 fiziksel çekirdek ile token üretimi (generation).
- `-tb 16`: 16 thread ile hızlı prompt işleme (prefill).
- `--fit on`: Model katmanlarını VRAM'e göre otomatik sığdırma.
- `--cont-batching`: Sürekli gruplama (continuous batching) desteği.
- `--reasoning-format deepseek --reasoning-preserve`: DeepSeek-R1 ve düşünme modelleri için düşünme zinciri koruması.

---

## 📦 Desteklenen Modeller (`models/`)

Aşağıdaki modeller test edilmiş ve `agent_system/llm/providers_config.json` ile entegre edilmiştir:

| Model Adı | Boyut | Quant | Rol / Uzmanlık |
| :--- | :--- | :--- | :--- |
| `Qwen2.5-Coder-14B-Instruct-abliterated-IQ3_M.gguf` | 6.5 GB | IQ3_M | Gelişmiş Kodlama (Önerilen) |
| `Qwen2.5-Coder-7B-Instruct-abliterated-Q5_K_M.gguf` | 5.1 GB | Q5_K_M | Hızlı Kodlama (Yüksek Kalite) |
| `Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf` | 4.4 GB | Q4_K_M | Standart Kodlama |
| `DeepSeek-R1-Distill-Llama-8B-Q4_K_M.gguf` | 4.6 GB | Q4_K_M | Akıl Yürütme & Mantık |
| `Ornith-1.5-9B-Q4_K_M.gguf` | 5.4 GB | Q4_K_M | Genel Amaçlı Sohbet & Analiz |
| `Huihui-Qwen3.8-27B-abliterated-UD-DW-Q4_K_M.gguf` | 15.4 GB | Q4_K_M | En Güçlü Model (~16 GB VRAM / Hibrit) |

> **Not:** Model dosyaları (`*.gguf`) gigabaytlarca yer kapladığı için Git reposuna dahil edilmez (`.gitignore`). Dosyaları `models/` klasörüne yerleştirmeniz veya menüden HuggingFace otomatik indirmesini seçmeniz yeterlidir.

---

## 🛠️ Kullanım

### Linux
```bash
# Sunucuyu başlat (model seçim menüsü açılır)
bash llama_server/llama_server_baslat.sh

# Doğrudan belirli bir modelle başlatmak için:
bash llama_server/llama_server_baslat.sh Qwen2.5-Coder-14B

# Sunucuyu durdurmak için:
bash llama_server/llama_server_durdur.sh
```

### Windows
- `llama_server_baslat.bat` dosyasına çift tıklayın veya CMD'den çalıştırın.
- Durdurmak için `llama_server_durdur.bat` dosyasını çalıştırın.

---

## 🌐 API Erişimi
Sunucu ayağa kalktığında varsayılan olarak şu adreste OpenAI uyumlu REST API sağlar:
`http://localhost:8080/v1`
