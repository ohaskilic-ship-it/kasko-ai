# KaskoAI — Yapay Zekâ Destekli Kasko Değeri Bulucu

Bu proje, TSB kasko değer listesi CSV dosyasından araç değerini bulur.
Gemini yalnızca kullanıcının doğal dilde verdiği araç bilgisini yapılandırır;
nihai kasko değeri doğrudan CSV kaydından okunur. Böylece yapay zekânın
değer uydurması engellenir.

## Proje yapısı

```text
kasko-ai/
├── app.py
├── requirements.txt
├── render.yaml
├── .gitignore
├── .env.example
├── README.md
├── data/
│   └── kasko_guncel.csv
├── templates/
│   └── index.html
└── static/
    ├── style.css
    └── app.js
```

## 1) Gemini API anahtarı

Google AI Studio'dan bir Gemini API anahtarı oluşturun.
Anahtarı GitHub'a koymayın.

Render'da:
- Environment Variable adı: `GEMINI_API_KEY`
- Value: Gemini anahtarınız
- `GEMINI_MODEL`: `gemini-3.5-flash-lite`

## 2) Yerelde çalıştırma

Python 3.11+ önerilir.

Windows PowerShell:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:GEMINI_API_KEY="BURAYA_API_KEY"
python app.py
```

Sonra:
http://127.0.0.1:5000

## 3) Aylık kasko listesi güncelleme

Yeni TSB CSV dosyasını `data` klasörüne koyun.
Dosya adının `kasko_guncel` ile başlaması yeterli:

- `kasko_guncel.csv`
- `kasko_guncel_agustos.csv`
- `kasko_guncel_eylul.csv`

Uygulama en son değiştirilmiş `kasko_guncel*.csv` dosyasını otomatik seçer.

GitHub'a push ettikten sonra Render otomatik deploy eder.

## 4) GitHub

Yeni bir repository oluşturun ve bu klasörde:

```bash
git init
git add .
git commit -m "İlk sürüm"
git branch -M main
git remote add origin REPO_ADRESINIZ
git push -u origin main
```

## 5) Render

Render > New > Web Service > GitHub repository seçin.

- Runtime: Python 3
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:app`
- Plan: Free

Environment Variables:
- `GEMINI_API_KEY`
- `GEMINI_MODEL=gemini-3.5-flash-lite`

Deploy edin.

## Güvenlik

Gemini API anahtarını `index.html`, `app.js`, GitHub veya CSV içine yazmayın.
Sadece Render Environment Variables içinde tutun.

## V2: Akıllı soru-cevaplı eşleştirme

Bu sürümde araç tek kayda düşmeden kasko değeri gösterilmez. Sistem gerçek CSV
adaylarını tutar; motor/yakıt, kasa, şanzıman veya donanım gibi ayırt edici
bilgileri sırayla sorar. Yıl değeri 0 olan kayıtlar sonuç olarak gösterilmez.


## Final Mobile-First Tasarım
- Mobil ekran öncelikli arayüz
- Alt veri kaynağı/dosya adı bilgileri kaldırıldı
- Sonuçta yalnızca Kasko Kodu gösterilir
- Mobil klavye ve sohbet kaydırma davranışı iyileştirildi


## Eski model araç hesabı
Listedeki en eski model yılından daha eski araçlarda, aynı aracın en eski
liste yılı değeri baz alınır. Her model yılı için bir önceki yıl değeri
üzerinden %10 indirim ardışık olarak uygulanır. Hesaplanmış sonuçlar,
doğrudan listedeki sonuçlardan ayrı şekilde kullanıcıya açıklanır.


## Akıllı sohbet sürümü
- Yalnızca yıl yazıldığında hata vermek yerine konuşmayı devam ettirir.
- Marka yazılmasa bile model/tip bilgisinden gerçek CSV kayıtları üzerinden marka çıkarımı yapar.
- Yazım hatalarında fuzzy eşleştirme kullanır.
- Model ile model yılı birbiriyle uyumsuz görünüyorsa bunu doğal dille kullanıcıya açıklar.
- Gemini karar vermez; gerçek araç/değer eşleştirmesini kod ve CSV yapar. Gemini yalnızca doğal konuşma üretir.
