import os
import glob
import re
import unicodedata
from pathlib import Path

import pandas as pd
from flask import Flask, jsonify, render_template, request
from rapidfuzz import fuzz, process
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

app = Flask(__name__)

DATA_DIR = Path(__file__).parent / "data"
CSV_PATTERN = str(DATA_DIR / "kasko_guncel*.csv")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

df = None
brands = []
year_columns = []


def normalize(value):
    if value is None:
        return ""
    s = str(value).strip().upper()
    tr = str.maketrans({
        "Ç": "C", "Ğ": "G", "İ": "I", "Ö": "O",
        "Ş": "S", "Ü": "U", "Â": "A", "Î": "I", "Û": "U"
    })
    s = s.translate(tr)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def load_csv():
    global df, brands, year_columns

    files = sorted(glob.glob(CSV_PATTERN), key=os.path.getmtime, reverse=True)
    if not files:
        raise FileNotFoundError("data klasöründe kasko_guncel*.csv dosyası bulunamadı.")

    data = pd.read_csv(
        files[0],
        sep=";",
        skiprows=1,
        encoding="utf-8-sig",
        dtype=str,
        keep_default_na=False
    )

    data.columns = [str(c).strip() for c in data.columns]
    for col in ["Marka Kodu", "Tip Kodu", "Marka Adı", "Tip Adı"]:
        if col not in data.columns:
            raise ValueError(f"CSV içinde '{col}' sütunu bulunamadı.")

    data["_brand_norm"] = data["Marka Adı"].map(normalize)
    data["_type_norm"] = data["Tip Adı"].map(normalize)
    data["_full_norm"] = (data["Marka Adı"] + " " + data["Tip Adı"]).map(normalize)
    data["_row_key"] = data["Marka Kodu"].astype(str) + ":" + data["Tip Kodu"].astype(str)

    year_columns = [c for c in data.columns if re.fullmatch(r"\d{4}", str(c))]
    for y in year_columns:
        data[y] = pd.to_numeric(
            data[y].astype(str)
                .str.replace(".", "", regex=False)
                .str.replace(",", "", regex=False),
            errors="coerce"
        ).fillna(0).astype(int)

    df = data
    brands = sorted(df["_brand_norm"].dropna().unique().tolist())
    return Path(files[0]).name


ACTIVE_CSV = load_csv()


class VehicleTurn(BaseModel):
    year: int | None = None
    brand: str | None = None
    model_or_type: str | None = None
    descriptors: list[str] = Field(default_factory=list)
    is_greeting_only: bool = False


class CandidateSelection(BaseModel):
    matching_keys: list[str] = Field(default_factory=list)
    confidence: str = "medium"


class ClarifyingQuestion(BaseModel):
    question: str
    options: list[str] = Field(default_factory=list)


class ConversationReply(BaseModel):
    reply: str
    options: list[str] = Field(default_factory=list)


def get_ai_client():
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        return None
    return genai.Client(api_key=key)


def ai_json(prompt, schema, max_tokens=700):
    client = get_ai_client()
    if client is None:
        raise RuntimeError("Gemini API anahtarı henüz tanımlanmamış.")

    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            max_output_tokens=max_tokens,
            temperature=0.1,
        ),
    )
    return schema.model_validate_json(response.text).model_dump()


def extract_turn(message, state):
    state_text = (
        f"Bilinen yıl: {state.get('year') or 'yok'}\n"
        f"Bilinen marka: {state.get('brand') or 'yok'}\n"
        f"Bilinen model: {state.get('model_or_type') or 'yok'}"
    )

    prompt = f"""
Türkiye'deki araç kasko değer listesinde arama yapan bir asistan için
kullanıcının SON mesajındaki araç bilgilerini çıkar.

{state_text}

Son kullanıcı mesajı:
{message}

Kurallar:
- year: Son mesajda açıkça yıl söyleniyorsa yaz, yoksa null.
- brand: Son mesajda açıkça marka söyleniyorsa yaz, yoksa null.
- model_or_type: Son mesajda model/seri adı söyleniyorsa yaz. Örn: Passat, Golf,
  Corolla, 320i, Clio. Söylenmiyorsa null.
- descriptors: Motor, yakıt, güç, şanzıman, kasa, donanım/paket gibi SON mesajda
  verilen tüm ayırt edici ifadeleri yaz. Örn ["1.5", "benzinli", "business"].
- Kullanıcının söylediği bilgileri uydurma veya değiştirme.
- "merhaba", "selam" gibi sadece sohbet mesajıysa is_greeting_only=true.
- Plakadan araç bilgisi tahmin etme.
"""
    return ai_json(prompt, VehicleTurn)


def best_brand_name(raw_brand):
    if not raw_brand:
        return None, 0
    q = normalize(raw_brand)
    match = process.extractOne(q, brands, scorer=fuzz.WRatio)
    if not match:
        return None, 0
    return match[0], float(match[1])


def rows_from_keys(keys, year=None):
    if not keys:
        return df.iloc[0:0].copy()
    rows = df[df["_row_key"].isin(keys)].copy()
    if year and str(year) in year_columns:
        rows = rows[rows[str(year)] > 0].copy()
    return rows


def token_search_initial(year, brand, model_or_type, descriptors, raw_message):
    if not year or str(year) not in year_columns:
        return df.iloc[0:0].copy()

    pool = df[df[str(year)] > 0].copy()
    if pool.empty:
        return pool

    brand_norm = None
    if brand:
        brand_norm, brand_score = best_brand_name(brand)
        if brand_norm and brand_score >= 65:
            pool = pool[pool["_brand_norm"] == brand_norm].copy()

    model_q = normalize(model_or_type or "")
    descriptor_q = normalize(" ".join(descriptors or []))

    # Güçlü yöntem 1: model ifadesi Tip Adı içinde geçiyorsa tüm gerçek adayları al.
    if model_q:
        exactish = pool[pool["_type_norm"].str.contains(re.escape(model_q), regex=True, na=False)].copy()
        if not exactish.empty:
            return exactish.head(80)

        # Model birden fazla kelimeyse kelimelerin tümünün geçtiği kayıtları dene.
        words = [w for w in model_q.split() if len(w) >= 2]
        if words:
            mask = pd.Series(True, index=pool.index)
            for w in words:
                mask &= pool["_type_norm"].str.contains(rf"\b{re.escape(w)}\b", regex=True, na=False)
            exact_words = pool[mask].copy()
            if not exact_words.empty:
                return exact_words.head(80)

    # Marka yazılmadıysa "2021 Passat" gibi sorgularda ham mesajdan model kelimesini yakala.
    raw_norm = normalize(raw_message)
    stop = {
        "MODEL", "KASKO", "DEGER", "DEGERI", "NEDIR", "ARAC", "ARABA",
        str(year), "BENIM", "BUL", "BAK", "TL"
    }
    raw_words = [w for w in raw_norm.split() if w not in stop and len(w) >= 3]
    for w in raw_words:
        hits = pool[pool["_type_norm"].str.contains(rf"\b{re.escape(w)}\b", regex=True, na=False)]
        if 1 <= len(hits) <= 80:
            return hits.copy()

    # Son çare: fuzzy. Burada doğrudan sonuç dönmek yerine aday havuzu çıkarıyoruz.
    query = normalize(" ".join(filter(None, [brand or "", model_or_type or "", descriptor_q])))
    if not query:
        return pool.iloc[0:0].copy()

    matches = process.extract(
        query,
        pool["_full_norm"].tolist(),
        scorer=fuzz.WRatio,
        limit=min(30, len(pool))
    )
    if not matches:
        return pool.iloc[0:0].copy()

    good_indexes = []
    top_score = matches[0][1]
    for _, score, idx in matches:
        if score >= max(58, top_score - 12):
            good_indexes.append(pool.index[idx])

    return pool.loc[good_indexes].copy()



def global_model_candidates(model_or_type, raw_message="", limit=80):
    """
    Model/tip bilgisini model yılından bağımsız olarak tüm CSV içinde arar.
    Böylece 'Egea' -> FIAT gibi marka çıkarımları, hedef yıl o araç için uygun
    olmasa bile yapılabilir. Yazım hatalarında fuzzy arama kullanılır.
    """
    query = normalize(model_or_type or "")
    if not query:
        raw = normalize(raw_message)
        stop = {"MODEL", "KASKO", "DEGER", "DEGERI", "NEDIR", "ARAC", "ARABA",
                "BENIM", "BUL", "BAK", "TL"}
        words = [w for w in raw.split() if w not in stop and not w.isdigit() and len(w) >= 3]
        query = " ".join(words)

    if not query:
        return df.iloc[0:0].copy(), 0.0

    # Önce tam/kelime içerme.
    exact = df[df["_type_norm"].str.contains(re.escape(query), regex=True, na=False)].copy()
    if not exact.empty:
        return exact.head(limit), 100.0

    words = [w for w in query.split() if len(w) >= 2]
    if words:
        mask = pd.Series(True, index=df.index)
        for w in words:
            mask &= df["_type_norm"].str.contains(rf"\b{re.escape(w)}\b", regex=True, na=False)
        word_hits = df[mask].copy()
        if not word_hits.empty:
            return word_hits.head(limit), 96.0

    # Fuzzy aramayı benzersiz marka+tip kayıtları üzerinde yap.
    uniques = df[["_row_key", "Marka Adı", "Tip Adı", "_full_norm"]].drop_duplicates("_row_key")
    matches = process.extract(
        query,
        uniques["_full_norm"].tolist(),
        scorer=fuzz.WRatio,
        limit=min(40, len(uniques))
    )
    if not matches:
        return df.iloc[0:0].copy(), 0.0

    top_score = float(matches[0][1])
    # Çok zayıf eşleşmeleri kabul etme.
    if top_score < 62:
        return df.iloc[0:0].copy(), top_score

    keys = []
    for _, score, idx in matches:
        if score >= max(62, top_score - 8):
            keys.append(uniques.iloc[idx]["_row_key"])

    return df[df["_row_key"].isin(keys)].copy().head(limit), top_score


def natural_conversation_reply(facts, goal, options=None, fallback=None):
    """
    Kararları kod verir; Gemini yalnızca kullanıcıya söylenecek doğal cümleyi yazar.
    Böylece sohbet daha insani olur ama araç/değer uydurulmaz.
    """
    options = options or []
    fallback = fallback or goal
    prompt = f"""
Sen profesyonel ama sıcak konuşan bir araç kasko değeri asistanısın.
Kullanıcıyla kısa, doğal ve gerçekten sohbet ediyormuş gibi Türkçe konuş.

KESİN BİLGİLER:
{facts}

ŞİMDİKİ AMAÇ:
{goal}

Kurallar:
- Kesin bilgiler dışında marka, model, yıl, motor, paket veya fiyat UYDURMA.
- Kullanıcının daha önce verdiği bilgiyi tekrar sorma.
- Eğer bir bilgiyi anladıysan bunu doğal biçimde teyit et:
  "Anladım, aracınız 2004 model." gibi.
- Tek seferde mümkünse yalnızca bir sonraki gerekli bilgiyi sor.
- Resmî ama soğuk olmayan bir dil kullan.
- 1-3 kısa cümle yeterli.
- Teknik CSV, sütun, veri tabanı, algoritma gibi ifadeler kullanma.
- Çıktı sadece şemaya uygun olsun.
"""
    try:
        result = ai_json(prompt, ConversationReply, max_tokens=300)
        result["options"] = options[:5]
        return result
    except Exception:
        return {"reply": fallback, "options": options[:5]}


def infer_brand_from_model(state, turn, message):
    """
    Kullanıcı marka söylemese bile model/tipten markayı gerçek CSV kayıtlarından
    çıkarmaya çalışır. Örn: 'Egea' -> FIAT.
    """
    model_text = turn.get("model_or_type") or state.get("model_or_type")
    if not model_text:
        return state, df.iloc[0:0].copy(), 0.0

    global_rows, score = global_model_candidates(model_text, message)
    if global_rows.empty:
        return state, global_rows, score

    unique_brands = global_rows["Marka Adı"].drop_duplicates().tolist()
    if len(unique_brands) == 1 and score >= 70:
        state["brand"] = unique_brands[0]

    return state, global_rows, score

def candidate_lines(rows, year, max_rows=35):
    items = []
    for _, r in rows.head(max_rows).iterrows():
        items.append(
            f"{r['_row_key']} | {r['Marka Adı']} | {r['Tip Adı']} | "
            f"{year} değeri={int(r[str(year)])}"
        )
    return "\n".join(items)


def refine_with_ai(rows, year, user_message):
    if len(rows) <= 1:
        return rows

    prompt = f"""
Aşağıda kasko listesinden GERÇEK araç adayları var.
Kullanıcının son cevabına UYAN adayların row key'lerini seç.

Kullanıcı cevabı:
{user_message}

Adaylar:
{candidate_lines(rows, year)}

Kurallar:
- Sadece listede bulunan key'leri döndür.
- Kullanıcı "1.5 benzinli" derse 1.5 TSI gibi açıkça uyumlu kayıtları seçebilirsin.
- "dizel" -> TDI/CDI/dCi/HDi gibi açık dizel ifadeleriyle uyumlu adayları seç.
- "benzinli" -> TSI/TFSI/TCE vb. benzinli ifadelerle uyumlu adayları seç; hibriti
  benzinli diye otomatik seçme.
- "otomatik" denince DSG, TIPTRONIC, EDC, DCT, AT vb. otomatik ifadeleri dikkate al.
- Kullanıcının cevabı hiçbir adayı ayırmıyorsa TÜM mevcut key'leri döndür.
- Emin olmadığın bilgiyi uydurma.
"""
    result = ai_json(prompt, CandidateSelection)
    keys = [k for k in result["matching_keys"] if k in set(rows["_row_key"])]
    if not keys:
        return rows
    return rows[rows["_row_key"].isin(keys)].copy()


def common_model_hint(rows):
    if rows.empty:
        return ""
    types_list = [normalize(x).split() for x in rows["Tip Adı"].tolist()]
    if not types_list:
        return ""
    common = []
    for token in types_list[0]:
        if len(token) >= 3 and all(token in toks for toks in types_list[1:]):
            if token not in {"DSG", "BMT", "ACT", "SCR", "TIPTRONIC", "TIPTR"}:
                common.append(token)
    return " ".join(common[:2])


def make_question(rows, year):
    if rows.empty:
        return {
            "question": "Bu bilgilerle listede uygun araç bulamadım. Marka, model ve model yılını biraz daha açık yazar mısınız?",
            "options": []
        }

    prompt = f"""
Sen bir kasko değeri danışmanısın. Aşağıdaki GERÇEK araç adaylarının arasından
doğru aracı bulmak için kullanıcıya TEK bir kısa ve doğal Türkçe soru sor.

Adaylar:
{candidate_lines(rows, year, max_rows=30)}

Amaç:
- En ayırt edici bilgiyi sor: önce gerekiyorsa gövde/kasa, motor-yakıt,
  şanzıman, güç veya donanım/paket.
- Aynı soruda gereksiz çok ayrıntı isteme.
- Kullanıcıyla gerçek bir danışman gibi kibar, sıcak ve doğal konuş.
- Kullanıcının daha önce verdiği bilgiyi tekrar sorma.
- Mümkünse önce anladığın bilgiyi kısa biçimde teyit edip sonra tek sorunu sor.
- Sorunun cevabı adayları gerçekten daraltabilsin.
- 2-5 kısa seçenek üret. Seçenekler adaylarda gerçekten bulunan ayrımlardan gelsin.
- "Bilmiyorum" seçeneği ekleme; kullanıcı zaten serbestçe yazabilir.
- Fiyatı henüz söyleme.
"""
    try:
        return ai_json(prompt, ClarifyingQuestion)
    except Exception:
        model = common_model_hint(rows)
        return {
            "question": f"{model or 'Araç'} için birden fazla kayıt buldum. Motor, yakıt ve donanım paketini biraz daha belirtir misiniz?",
            "options": []
        }


def make_kasko_code(brand_code, type_code):
    """Marka Kodu + Tip Kodu birleştirilip soldan sıfırla 7 haneye tamamlanır."""
    brand_digits = re.sub(r"\D", "", str(brand_code))
    type_digits = re.sub(r"\D", "", str(type_code))
    combined = brand_digits + type_digits
    return combined.zfill(7)


def row_to_result(row, year, requested_year=None):
    requested_year = int(requested_year if requested_year is not None else year)
    listed_year = int(year)
    listed_value = int(row[str(listed_year)])

    if requested_year < listed_year:
        value = calculate_older_model_value(listed_value, listed_year, requested_year)
        calculated = True
        discount_years = listed_year - requested_year
    else:
        value = listed_value
        calculated = False
        discount_years = 0

    marka_kodu = re.sub(r"\D", "", str(row["Marka Kodu"]))
    tip_kodu = re.sub(r"\D", "", str(row["Tip Kodu"]))
    kasko_code = (marka_kodu + tip_kodu).zfill(7)

    return {
        "status": "found",
        "brand": str(row["Marka Adı"]),
        "type": str(row["Tip Adı"]),
        "year": requested_year,
        "value": value,
        "kasko_code": kasko_code,
        "calculated": calculated,
        "base_year": listed_year if calculated else None,
        "base_value": listed_value if calculated else None,
        "discount_years": discount_years,
        "source_file": ACTIVE_CSV
    }



def get_oldest_list_year():
    return min(int(y) for y in year_columns)


def calculate_older_model_value(base_value, base_year, target_year):
    """
    Listedeki en eski model yılından daha eski araçlar için her model yılı
    başına bir önceki yıl değeri üzerinden %10 indirim uygular.
    """
    year_difference = int(base_year) - int(target_year)
    if year_difference <= 0:
        return int(base_value)

    value = float(base_value)
    for _ in range(year_difference):
        value *= 0.90

    return int(round(value))


def professional_not_found_message():
    return (
        "Aracınıza uygun bir kasko değeri bulunamadı. "
        "Lütfen marka, model, motor, kasa veya donanım bilgilerini kontrol ederek tekrar deneyin."
    )


def process_search(message, incoming_state):
    state = {
        "year": incoming_state.get("year"),
        "brand": incoming_state.get("brand"),
        "model_or_type": incoming_state.get("model_or_type"),
        "candidate_keys": incoming_state.get("candidate_keys") or [],
    }

    turn = extract_turn(message, state)

    if turn.get("is_greeting_only") and not any(
        [state.get("year"), state.get("brand"), state.get("model_or_type"), state["candidate_keys"]]
    ):
        reply = natural_conversation_reply(
            "Henüz araç hakkında bilgi yok.",
            "Kullanıcıyı selamla ve model yılı ile marka/model bilgisini doğal biçimde iste.",
            fallback="Merhaba 👋 Memnuniyetle yardımcı olayım. Aracınızın model yılı ile marka/modelini söyler misiniz?"
        )
        return {"status": "need_info", "question": reply["reply"], "options": [], "state": state}

    # Yeni mesajdaki açık bilgileri hafızaya al.
    if turn.get("year"):
        if state.get("year") and int(turn["year"]) != int(state["year"]):
            state["candidate_keys"] = []
        state["year"] = int(turn["year"])

    if turn.get("brand"):
        state["brand"] = turn["brand"]

    if turn.get("model_or_type"):
        if state.get("model_or_type") and normalize(turn["model_or_type"]) != normalize(state["model_or_type"]):
            state["candidate_keys"] = []
        state["model_or_type"] = turn["model_or_type"]

    # Model verilmişse, marka söylenmemiş olsa bile tüm listeden markayı çıkarmaya çalış.
    state, global_rows, global_score = infer_brand_from_model(state, turn, message)

    # Kullanıcı yalnızca yılı söylediyse "bulunamadı" deme; sohbeti devam ettir.
    if state.get("year") and not state.get("brand") and not state.get("model_or_type"):
        reply = natural_conversation_reply(
            f"Aracın model yılı: {state['year']}. Marka ve model henüz bilinmiyor.",
            f"{state['year']} model bilgisini anladığını belirt ve aracın marka veya modelini sor.",
            fallback=f"Anladım, aracınız {state['year']} model. Peki markası veya modeli nedir?"
        )
        return {"status": "need_info", "question": reply["reply"], "options": [], "state": state}

    # Modeli anladık ama yıl yoksa marka çıkarımını da kullanarak yılı iste.
    if not state.get("year"):
        known = []
        if state.get("brand"):
            known.append(f"marka: {state['brand']}")
        if state.get("model_or_type"):
            known.append(f"model/tip: {state['model_or_type']}")
        reply = natural_conversation_reply(
            ", ".join(known) if known else "Model yılı henüz bilinmiyor.",
            "Bilinen araç bilgisini doğal biçimde teyit et ve yalnızca model yılını sor.",
            fallback="Aracı anladım. Model yılını da söyler misiniz?"
        )
        return {"status": "need_info", "question": reply["reply"], "options": [], "state": state}

    # Marka var ama model/tip yok.
    if state.get("brand") and not state.get("model_or_type"):
        reply = natural_conversation_reply(
            f"Model yılı: {state['year']}; marka: {state['brand']}; model/tip bilinmiyor.",
            "Bilinen yıl ve markayı teyit et, yalnızca model/tip bilgisini sor.",
            fallback=f"Anladım, {state['year']} model {state['brand']}. Peki aracın modeli nedir?"
        )
        return {"status": "need_info", "question": reply["reply"], "options": [], "state": state}

    requested_year = int(state["year"])
    oldest_year = get_oldest_list_year()

    if str(requested_year) not in year_columns and requested_year >= oldest_year:
        reply = natural_conversation_reply(
            f"İstenen model yılı {requested_year}; bu yıl mevcut değer yılları arasında değil.",
            "Kullanıcıya teknik ayrıntı vermeden aracına uygun kasko değeri bulunamadığını söyle ve model yılını kontrol etmesini iste.",
            fallback="Bu bilgilerle aracınıza uygun bir kasko değeri bulamadım. Model yılını kontrol eder misiniz?"
        )
        return {"status": "not_found", "message": reply["reply"], "state": state}

    search_year = oldest_year if requested_year < oldest_year else requested_year

    # Model/tip global olarak güçlü biçimde tanındıysa, hedef baz yılda gerçekten
    # aynı aracın bir değeri var mı kontrol et. Bu, örn. '2004 Egea' gibi
    # model-yıl çelişkilerini doğal biçimde yakalar.
    if not global_rows.empty and global_score >= 70:
        year_compatible = global_rows[global_rows[str(search_year)] > 0].copy()
        if year_compatible.empty:
            inferred_brand = state.get("brand") or "markası"
            reply = natural_conversation_reply(
                f"Kullanıcının söylediği model/tip: {state.get('model_or_type')}. "
                f"Bu model/tip listede {inferred_brand} markasıyla eşleşiyor. "
                f"Kullanıcının söylediği model yılı: {requested_year}. "
                f"Ancak bu araç tipi {search_year} baz yılında değer taşımıyor; yıl ile araç tipi birbiriyle uyumlu görünmüyor.",
                "Model/tipi tanıdığını ve mümkünse markasını anladığını söyle. Model yılı ile araç bilgisinin uyumlu görünmediğini nazikçe belirt ve model yılını kontrol etmesini sor.",
                fallback=f"{state.get('model_or_type')} modelini {inferred_brand} olarak anladım; ancak {requested_year} model yılı bu araçla uyumlu görünmüyor. Model yılını kontrol eder misiniz?"
            )
            return {"status": "need_info", "question": reply["reply"], "options": [], "state": state}

    # Devam eden adayları son kullanıcı cevabıyla daralt.
    if state["candidate_keys"]:
        candidates = rows_from_keys(state["candidate_keys"], search_year)
        candidates = refine_with_ai(candidates, search_year, message)
    else:
        candidates = token_search_initial(
            search_year,
            state.get("brand"),
            state.get("model_or_type"),
            turn.get("descriptors", []),
            message
        )

        # Eğer yıl bazlı arama başarısızsa ama model globalde tanındıysa
        # global adayların o baz yılda değeri olanlarını kullan.
        if candidates.empty and not global_rows.empty:
            candidates = global_rows[global_rows[str(search_year)] > 0].copy().head(80)

        if len(candidates) > 1 and turn.get("descriptors"):
            candidates = refine_with_ai(
                candidates,
                search_year,
                " ".join(turn["descriptors"])
            )

    candidates = candidates[candidates[str(search_year)] > 0].copy()

    if candidates.empty:
        state["candidate_keys"] = []
        known = (
            f"Yıl: {requested_year}; marka: {state.get('brand') or 'bilinmiyor'}; "
            f"model/tip: {state.get('model_or_type') or 'bilinmiyor'}."
        )
        reply = natural_conversation_reply(
            known,
            "Bu bilgilerle uygun kayıt bulunamadığını nazikçe söyle. Kullanıcının daha önce verdiği bilgileri tekrar isteme; eksik olabilecek motor, kasa veya donanım bilgisinden birini doğal biçimde iste.",
            fallback="Aracı tam eşleştiremedim. Motor, kasa veya donanım paketini biraz daha ayrıntılı söyler misiniz?"
        )
        return {"status": "need_info", "question": reply["reply"], "options": [], "state": state}

    unique_brands = candidates["Marka Adı"].drop_duplicates().tolist()
    if len(unique_brands) == 1:
        state["brand"] = unique_brands[0]

    state["candidate_keys"] = candidates["_row_key"].tolist()

    if len(candidates) == 1:
        row = candidates.iloc[0]
        state["candidate_keys"] = []
        result = row_to_result(row, search_year, requested_year=requested_year)
        result["state"] = state
        return result

    q = make_question(candidates, search_year)
    return {
        "status": "clarify",
        "question": q["question"],
        "options": q.get("options", [])[:5],
        "candidate_count": int(len(candidates)),
        "state": state
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.get("/api/status")
def status():
    return jsonify({
        "ok": True,
        "csv": ACTIVE_CSV,
        "rows": int(len(df)),
        "gemini_configured": bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")),
        "model": MODEL
    })


@app.post("/api/search")
def search():
    payload = request.get_json(silent=True) or {}
    message = str(payload.get("message", "")).strip()
    state = payload.get("state") or {}

    if not message:
        return jsonify({"ok": False, "error": "Lütfen bir araç bilgisi yazın."}), 400

    try:
        result = process_search(message, state)
        return jsonify({"ok": True, **result})
    except Exception as exc:
        return jsonify({
            "ok": False,
            "error": f"İşlem sırasında hata oluştu: {exc}"
        }), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
