from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import httpx, re, time, os

API_KEY = os.getenv("API_KEY", "change_me")

app = FastAPI(title="Comparador Mayorista UY (Action)")
# Página raíz para que no confunda
@app.get("/")
def root():
    return {"status": "ok", "service": "comparador-uy-action"}

# OpenAPI con 'servers' correcto:
from fastapi.openapi.utils import get_openapi
import os

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


VTEX_TATA = "https://tata.com.uy/api/catalog_system/pub/products/search?ft="

class CompareIn(BaseModel):
    competitor: str   # 'tata' (others reserved)
    store: str        # e.g., 'Durazno'
    items: List[str]
    offset: int = 0
    limit: int = 250

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

def _best_vtex_tata(q: str) -> Optional[Dict[str, Any]]:
    key = f"tata:{q.lower()}"
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit["ts"] < 12*3600:
        return hit["data"]

    toks = re.findall(r"[A-Za-zÁÉÍÓÚÜÑ0-9]+", q, re.I)
    with httpx.Client(timeout=12, headers={"User-Agent":"Mozilla/5.0"}) as cli:
        r = cli.get(VTEX_TATA + httpx.utils.quote(q))
        if r.status_code != 200:
            data = None
        else:
            try:
                arr = r.json()
                if not isinstance(arr, list) or not arr:
                    data = None
                else:
                    arr.sort(key=lambda p: _score(f"{p.get('productName','')} {p.get('linkText','')}", [t.lower() for t in toks]), reverse=True)
                    data = arr[0]
            except Exception:
                data = None
    _cache[key] = {"ts": now, "data": data}
    return data

@app.post("/compare", response_model=CompareOut)
def compare(body: CompareIn, x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
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
            out.append(ItemOut(idx=idx, query=q, estado="VACIO")); continue

        if comp == "tata":
            p = _best_vtex_tata(q)
            if not p:
                out.append(ItemOut(idx=idx, query=q, estado="No disponible")); continue
            item = (p.get("items") or [{}])[0]
            seller = (item.get("sellers") or [{}])[0]
            offer = seller.get("commertialOffer") or {}
            price = offer.get("Price")
            listp = offer.get("ListPrice")
            url = f"https://tata.com.uy/{p.get('linkText','')}/p" if p.get("linkText") else None
            out.append(ItemOut(idx=idx, query=q, name=p.get("productName",""), price=price, listPrice=listp, inStock=bool(offer.get("IsAvailable")), url=url, exclusivoOnline=False, estado="OK"))
        else:
            # Reserved for future competitors
            out.append(ItemOut(idx=idx, query=q, estado="No implementado"))

    return CompareOut(competitor=comp, store=body.store, offset=i0, limit=body.limit, total=len(body.items), results=out)
