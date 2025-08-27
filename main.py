from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import httpx, re, time, os
from urllib.parse import quote_plus  # importante para escapar la query
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
    # minúsculas + sin acentos
    s = s.lower().strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s)

def _build_tries(q: str):
    q0 = _normalize(q)
    toks = q0.split()

    # detectar "0000" (suele ser harina; para arroz estorba)
    if "0000" in toks and "harina" not in toks:
        toks_sin_0000 = [t for t in toks if t != "0000"]
    else:
        toks_sin_0000 = toks[:]

    # quitar tamaños y unidades (1kg, 1 kg, 1000g)
    toks_sin_size = [t for t in toks_sin_0000 if not re.fullmatch(r"(?:\d+kg|\d+\s*kg|\d+g|\d+\s*g|1kg|1\s*kg|1000g|1000\s*g)", t)]

    tries = []
    def add_try(tokens):
        s = " ".join(tokens).strip()
        if s and s not in tries:
            tries.append(s)

    add_try(q0)                          # 1) tal cual normalizado
    add_try(" ".join(toks_sin_0000))     # 2) sin "0000" si aplica
    add_try(" ".join(toks_sin_size))     # 3) sin tamaño

    # 4) primeras 2 palabras (ej: "arroz shiva")
    if len(toks_sin_size) >= 2:
        add_try(" ".join(toks_sin_size[:2]))
    # 5) primera palabra (ej: "arroz")
    if len(toks_sin_size) >= 1:
        add_try(toks_sin_size[0])

    return tries

def _fetch_vtex_products_tata(query: str):
    # usamos ft= + sc=1 + paginado básico
    base = "https://tata.com.uy/api/catalog_system/pub/products/search"
    params = f"?ft={quote_plus(query)}&sc=1&_from=0&_to=24"
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
    # cache 12 horas por consulta original
    key = f"tata:{_normalize(q)}"
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit["ts"] < 12 * 3600:
        return hit["data"]

    tries = _build_tries(q)
    best = None
    best_score = -1

    # tokens para score (de la consulta original)
    toks_score = re.findall(r"[A-Za-zÁÉÍÓÚÜÑ0-9]+", q, re.I)
    toks_lower = [t.lower() for t in toks_score]

    for t in tries:
        arr = _fetch_vtex_products_tata(t)
        if not arr:
            continue
        # rankear por coincidencias de tokens + preferir 1kg si venía en la query
        def score_product(p):
            name = f"{p.get('productName','')} {p.get('linkText','')}".lower()
            base_score = sum(1 for tok in toks_lower if tok and tok in name)
            # bonus si detectamos 1kg y aparece en el nombre
            has_1kg = any(x in _normalize(q) for x in ["1kg", "1 kg", "1000g", "1000 g"])
            if has_1kg and ("1kg" in name or "1 kg" in name or "1000g" in name or "1000 g" in name):
                base_score += 1
            return base_score

        arr.sort(key=score_product, reverse=True)
        cand = arr[0]
        sc = score_product(cand)
        if sc > best_score:
            best, best_score = cand, sc
        if best_score >= 2:
            break  # suficientemente bueno

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
