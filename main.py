from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import httpx, re, time, os
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

BRANDS = {
    # marcas frecuentes; agregá las que uses
    "emigrante","shiva","maggi","knorr","costa","cololo","cocinero","alco","himalaya",
    # añadidos útiles
    "bella","union","bella union"
}

def _normalize(s: str) -> str:
    s = (s or "").lower().strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^\w\s]", " ", s)        # fuera puntuación
    return re.sub(r"\s+", " ", s).strip()

_STOP = {"de","la","el","los","las","un","una","con","en","a","y","x","sin","al","por","para","del"}

def _extract_sizes(text: str):
    """Devuelve (masas_en_g, volumenes_en_ml) como sets de enteros."""
    t = _normalize(text)
    masses, vols = set(), set()
    for m, unit in re.findall(r"(\d+)\s*(kg|g|gr)", t):
        n = int(m)
        masses.add(n*1000 if unit=="kg" else n)
    for m, unit in re.findall(r"(\d+)\s*(ml|l)", t):
        n = int(m)
        vols.add(n*1000 if unit=="l" else n)
    return masses, vols

def _is_noise(t: str) -> bool:
    if t in _STOP: return True
    if re.fullmatch(r"\d+(kg|g|gr|ml|l)", t): return True
    if re.fullmatch(r"\d+\s*(kg|g|gr|ml|l)", t): return True
    if re.fullmatch(r"x\d+", t): return True     # packs 3x, 6x
    if t in {"pack","pct","bolsa","frasco","bot","pet","unidad","un"}: return True
    return False

def _build_tries(q: str):
    q0 = _normalize(q)
    toks = q0.split()

    # quitar "0000" cuando NO es harina
    if "0000" in toks and "harina" not in toks:
        toks = [t for t in toks if t != "0000"]

    toks_clean = [t for t in toks if not _is_noise(t)]

    tries = []
    def add_try(tokens):
        s = " ".join(tokens).strip()
        if s and s not in tries:
            tries.append(s)

    # 1) tal cual normalizado
    add_try(toks)
    # 2) limpio de ruidos
    add_try(toks_clean)
    # 3) primeras dos palabras limpias y su inversa (marca+producto / producto+marca)
    if len(toks_clean) >= 2:
        add_try(toks_clean[:2])
        add_try([toks_clean[1], toks_clean[0]])
    # 4) solo marca si está
    marcas_en_query = [t for t in toks_clean if t in BRANDS]
    if marcas_en_query:
        add_try(marcas_en_query[:1])
    # 5) tokens fuertes (≥4 letras)
    for t in toks_clean:
        if len(t) >= 4:
            add_try([t])

    return tries

def _parse_price(txt: str) -> Optional[float]:
    # "$ 1.234,50" -> 1234.5 ; "$53,00" -> 53.0
    if not txt: return None
    s = txt.replace("\xa0", " ").strip()
    s = re.sub(r"[^0-9,\.]", "", s)
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None

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
                if r.status_code != 200:
                    continue
                data = r.json()
                if isinstance(data, list) and data:
                    return data
            except Exception:
                continue
    return []

def _fetch_html_busca_tata(query: str):
    # Fallback: página pública /busca (HTML)
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
                if r.status_code != 200:
                    continue
                html = r.text
                # anclas a PDP + precio cercano
                for href, precio in re.findall(r'href="(/[^"]+/p)".{0,500}?\$ ?([\d\.\,]+)', html, re.I | re.S):
                    slug = href.strip("/").split("/")[0]
                    name_guess = _normalize(slug).replace("-", " ")
                    price = _parse_price(precio)
                    results.append({
                        "productName": name_guess,
                        "linkText": slug,
                        "items": [{
                            "sellers": [{
                                "commertialOffer": {
                                    "Price": price,
                                    "ListPrice": price,
                                    "IsAvailable": True
                                }
                            }]
                        }]
                    })
                if results:
                    break
            except Exception:
                continue
    return results

def _fetch_products_tata(query: str):
    arr = _fetch_vtex


    tries = _build_tries(q)
    best = None
    best_score = -1

    toks_score = re.findall(r"[A-Za-zÁÉÍÓÚÜÑ0-9]+", _normalize(q))
    has_1kg = any(x in _normalize(q) for x in ["1kg","1000g"])
    has_brand = any(b in _normalize(q) for b in ["emigrante","shiva","maggi","knorr","costa","cololo","cocinero","alco","himalaya"])

    for t in tries:
        arr = _fetch_vtex_products_tata(t)
        if not arr:
            continue

        def score_product(p):
            name = f"{p.get('productName','')} {p.get('linkText','')}".lower()
            s = 0
            for tok in toks_score:
                if tok and tok in name:
                    s += 1
            if has_1kg and ("1kg" in name or "1000g" in name or "1 kg" in name or "1000 g" in name):
                s += 1
            if has_brand and any(b in name for b in ["emigrante","shiva","maggi","knorr","costa","cololo","cocinero","alco","himalaya"]):
                s += 1
            return s

        arr.sort(key=score_product, reverse=True)
        cand = arr[0]
        sc = score_product(cand)
        if sc > best_score:
            best, best_score = cand, sc
        # umbral razonable para cortar temprano
        if best_score >= 2:
            break

    _cache[key] = {"ts": now, "data": best}
    return best



@app.post("/compare", response_model=CompareOut)
def compare(body: CompareIn, x_api_key: str = Header(None)):
    # Autenticación simple por header (debe coincidir con API_KEY en Render)
    if not x_api_key or x_api_key != os.getenv("API_KEY", "change_me"):
        raise HTTPException(status_code=401, detail="bad api key")

    comp = body.competitor.lower().strip()
    if comp not in {"tata", "dorado", "clon", "superencasa", "mily"}:
        raise HTTPException(status_code=400, detail="competitor not supported")

    i0 = max(body.offset, 0)
    i1 = min(i0 + min(body.limit, 300), len(body.items))

    out: List[ItemOut] = []
    for idx in range(i0, i1):
        q = (body.items[idx] or "").strip()
        if not q:
            out.append(ItemOut(idx=idx, query=q, estado="VACIO"))
            continue

        if comp == "tata":
            p = _best_vtex_tata(q)
            if not p:
                out.append(ItemOut(idx=idx, query=q, estado="No disponible"))
                continue
            item = (p.get("items") or [{}])[0]
            seller = (item.get("sellers") or [{}])[0]
            offer = seller.get("commertialOffer") or {}
            price = offer.get("Price")
            listp = offer.get("ListPrice")
            url = f"https://tata.com.uy/{p.get('linkText','')}/p" if p.get("linkText") else None
            out.append(
                ItemOut(
                    idx=idx, query=q, name=p.get("productName",""),
                    price=price, listPrice=listp,
                    inStock=bool(offer.get("IsAvailable")),
                    url=url, exclusivoOnline=False, estado="OK"
                )
            )
        else:
            out.append(ItemOut(idx=idx, query=q, estado="No implementado"))

    return CompareOut(
        competitor=comp, store=body.store, offset=i0,
        limit=body.limit, total=len(body.items), results=out
    )
