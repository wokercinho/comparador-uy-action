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

def _normalize(s: str) -> str:
    # minúsculas + sin acentos y espacios normalizados
    s = s.lower().strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s)

_STOP = {
    "de","la","el","los","las","un","una","con","en","a","y","x","sin","al","por","para","del"
}

def _build_tries(q: str):
    q0 = _normalize(q)
    toks = q0.split()

    # quitar tamaños/unidades y "x3/x6", "pack", "pct"
    def is_noise(t: str) -> bool:
        if t in _STOP: return True
        if re.fullmatch(r"\d+(kg|g|ml|l)", t): return True
        if re.fullmatch(r"\d+\s*(kg|g|ml|l)", t): return True
        if re.fullmatch(r"x\d+", t): return True
        if t in {"pack","pct","bolsa","frasco","bot","pet"}: return True
        return False

    toks_clean = [t for t in toks if not is_noise(t)]

    # quitar "0000" si no hay "harina"
    if "0000" in toks_clean and "harina" not in toks_clean:
        toks_clean = [t for t in toks_clean if t != "0000"]

    tries = []
    def add_try(tokens):
        s = " ".join(tokens).strip()
        if s and s not in tries:
            tries.append(s)

    # 1) tal cual normalizado
    add_try(toks)
    # 2) limpio de ruidos
    add_try(toks_clean)
    # 3) primera pareja fuerte (dos primeras limpias)
    if len(toks_clean) >= 2:
        add_try(toks_clean[:2])
        # 3b) invertida (útil p/ “emigrante aceitunas”)
        add_try([toks_clean[1], toks_clean[0]])
    # 4) token a token (>=4 letras)
    for t in toks_clean:
        if len(t) >= 4:
            add_try([t])

    return tries

def _fetch_vtex_products_tata(query: str):
    # ampliar ventana y ordenar por score
    base = "https://tata.com.uy/api/catalog_system/pub/products/search"
    params = f"?ft={quote_plus(query)}&_from=0&_to=49&O=OrderByScoreDESC"
    url = base + params
    with httpx.Client(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as cli:
        r = cli.get(url, follow_redirects=True)
        if r.status_code != 200:
            return []
        try:
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception:
            return []

def _best_vtex_tata(q: str) -> Optional[Dict[str, Any]]:
    key = f"tata:{_normalize(q)}"
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit["ts"] < 12 * 3600:
        return hit["data"]

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
