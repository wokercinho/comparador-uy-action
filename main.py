from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
import re
import time
import httpx
from typing import Optional, Dict, Any
from urllib.parse import quote_plus
import unicodedata



app = FastAPI(title="Comparador Mayorista UY (Action)")

# Endpoint raíz para que no confunda con 404
@app.get("/")
def root():
    return {"status": "ok", "service": "comparador-uy-action"}

# ----- OpenAPI con 'servers' correcto (lee PUBLIC_URL de Render) -----
from fastapi.openapi.utils import get_openapi

PUBLIC_URL = os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_HOSTNAME")
if PUBLIC_URL and not PUBLIC_URL.startswith("http"):
    PUBLIC_URL = f"https://{PUBLIC_URL}"

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version="1.0", routes=app.routes)
    if PUBLIC_URL:
        schema["servers"] = [{"url": PUBLIC_URL}]
    app.openapi_schema = schema
    return schema

app.openapi = custom_openapi
# ---------------------------------------------------------------------

VTEX_TATA = "https://tata.com.uy/api/catalog_system/pub/products/search?ft="

class CompareIn(BaseModel):
    competitor: str   # 'tata' (otros reservados)
    store: str        # ej. 'Durazno'
    items: List[str]
    offset: int = 0
    limit: int = 250  # Render free despierta en la 1ra; luego va fluido

class ItemOut(BaseModel):
    idx: int
    query: str
    name: Optional[str] = None
    price: Optional[float] = None
    listPrice: Optional[float] = None
    inStock: Optional[bool] = None
    url: Optional[str] = None
    exclusivoOnline: Optional[bool] = None
    estado: str = "OK"

class CompareOut(BaseModel):
    competitor: str
    store: str
    offset: int
    limit: int
    total: int
    results: List[ItemOut]

_cache: Dict[str, Any] = {}

def _score(name: str, toks):
    name = (name or "").lower()
    return sum(1 for t in toks if t and t in name)

# ========= Matching mejorado para TATA (VTEX) con fallback HTML =========

# ========= Matching mejorado para TATA (VTEX) con fallback HTML =========

BRANDS = {
    "emigrante","shiva","maggi","knorr","costa","cololo","cocinero","alco","himalaya",
    "bella","union","bella union"
}

def _normalize(s: str) -> str:
    s = (s or "").lower().strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

_STOP = {"de","la","el","los","las","un","una","con","en","a","y","x","sin","al","por","para","del"}

def _extract_sizes(text: str):
    t = _normalize(text)
    masses, vols = set(), set()
    for m, unit in re.findall(r"(\d+)\s*(kg|g|gr)", t):
        n = int(m); masses.add(n*1000 if unit=="kg" else n)
    for m, unit in re.findall(r"(\d+)\s*(ml|l)", t):
        n = int(m); vols.add(n*1000 if unit=="l" else n)
    return masses, vols

def _is_noise(t: str) -> bool:
    if t in _STOP: return True
    if re.fullmatch(r"\d+(kg|g|gr|ml|l)", t): return True
    if re.fullmatch(r"\d+\s*(kg|g|gr|ml|l)", t): return True
    if re.fullmatch(r"x\d+", t): return True
    if t in {"pack","pct","bolsa","frasco","bot","pet","unidad","un"}: return True
    return False

def _build_tries(q: str):
    q0 = _normalize(q)
    toks = q0.split()
    if "0000" in toks and "harina" not in toks:
        toks = [t for t in toks if t != "0000"]
    toks_clean = [t for t in toks if not _is_noise(t)]

    tries = []
    def add_try(tokens):
        s = " ".join(tokens).strip()
        if s and s not in tries:
            tries.append(s)

    add_try(toks)                    # 1) tal cual
    add_try(toks_clean)              # 2) limpio
    if len(toks_clean) >= 2:         # 3) primeras dos e inversa
        add_try(toks_clean[:2])
        add_try([toks_clean[1], toks_clean[0]])
    marcas_en_query = [t for t in toks_clean if t in BRANDS]  # 4) solo marca
    if marcas_en_query:
        add_try(marcas_en_query[:1])
    for t in toks_clean:             # 5) tokens fuertes
        if len(t) >= 4:
            add_try([t])
    return tries

def _parse_price(txt: str) -> Optional[float]:
    if not txt: return None
    s = txt.replace("\xa0", " ").strip()
    s = re.sub(r"[^0-9,\.]", "", s)
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try: return float(s)
    except: return None

def _fetch_vtex_json_tata(query: str):
    base = "https://tata.com.uy/api/catalog_system/pub/products/search"
    urls = [
        f"{base}?ft={quote_plus(query)}&_from=0&_to=99&O=OrderByScoreDESC",
        f"{base}/{quote_plus(query)}?_from=0&_to=99&O=OrderByScoreDESC",
    ]
    headers = {"User-Agent": "Mozilla/5.0", "Accept-Language": "es-UY,es;q=0.9"}
    with httpx.Client(timeout=15, headers=headers) as cli:
        for url in urls:
            try:
                r = cli.get(url, follow_redirects=True)
                if r.status_code != 200: continue
                data = r.json()
                if isinstance(data, list) and data: return data
            except Exception as e:
                print("[TATA][JSON] error:", e)
                continue
    return []

def _fetch_html_busca_tata(query: str):
    urls = [
        f"https://tata.com.uy/busca?ft={quote_plus(query)}&O=OrderByScoreDESC",
        f"https://www.tata.com.uy/busca?ft={quote_plus(query)}&O=OrderByScoreDESC",
    ]
    headers = {"User-Agent": "Mozilla/5.0", "Accept-Language": "es-UY,es;q=0.9"}
    results = []
    with httpx.Client(timeout=15, headers=headers) as cli:
        for url in urls:
            try:
                r = cli.get(url, follow_redirects=True)
                if r.status_code != 200: continue
                html = r.text
                for href, precio in re.findall(r'href="(/[^"]+/p)".{0,500}?\$ ?([\d\.\,]+)', html, re.I | re.S):
                    slug = href.strip("/").split("/")[0]
                    name_guess = _normalize(slug).replace("-", " ")
                    price = _parse_price(precio)
                    results.append({
                        "productName": name_guess,
                        "linkText": slug,
                        "items": [{
                            "sellers": [{
                                "commertialOffer": {"Price": price, "ListPrice": price, "IsAvailable": True}
                            }]
                        }]
                    })
                if results: break
            except Exception as e:
                print("[TATA][HTML] error:", e)
                continue
    return results

def _fetch_products_tata(query: str):
    try:
        arr = _fetch_vtex_json_tata(query)
        if arr: return arr
        return _fetch_html_busca_tata(query)
    except Exception as e:
        print("[TATA] fetch error:", e)
        return []

def _best_vtex_tata(q: str) -> Optional[Dict[str, Any]]:
    try:
        key = f"tata:{_normalize(q)}"
        now = time.time()
        hit = _cache.get(key)
        if hit and now - hit["ts"] < 12 * 3600:
            return hit["data"]

        tries = _build_tries(q)
        best, best_score = None, -1

        toks_score = _normalize(q).split()
        q_masses, q_vols = _extract_sizes(q)
        has_brand = any(t in BRANDS for t in toks_score)

        for t in tries:
            arr = _fetch_products_tata(t)
            if not arr: continue

            def score_product(p):
                name = _normalize(f"{p.get('productName','')} {p.get('linkText','')}")
                s = 0
                for tok in toks_score:
                    if tok and tok in name: s += 1
                if has_brand and any(b in name for b in BRANDS): s += 2
                pm, pv = _extract_sizes(name)
                if q_masses and pm:
                    diff = min(abs(a-b) for a in q_masses for b in pm)
                    if min(q_masses) > 0:
                        pct = diff / float(min(q_masses))
                        s += 2 if pct <= 0.10 else (1 if pct <= 0.20 else 0)
                if q_vols and pv:
                    diff = min(abs(a-b) for a in q_vols for b in pv)
                    if min(q_vols) > 0:
                        pct = diff / float(min(q_vols))
                        s += 2 if pct <= 0.10 else (1 if pct <= 0.20 else 0)
                return s

            arr.sort(key=score_product, reverse=True)
            cand = arr[0]; sc = score_product(cand)
            if sc > best_score:
                best, best_score = cand, sc
            if best_score >= 3: break

        _cache[key] = {"ts": now, "data": best}
        return best
    except Exception as e:
        print("[TATA] best error:", e)
        return None
# ========= fin bloque TATA =========
