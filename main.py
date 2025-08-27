# ==================== main.py ====================
import re
import time
import json
import httpx
import unicodedata
from urllib.parse import quote_plus
from typing import List, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# -----------------------------------------------------------------------------
# App
# -----------------------------------------------------------------------------
app = FastAPI(title="Comparador UY Action", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Cache simple en memoria
_cache = {}

# -----------------------------------------------------------------------------
# Esquemas
# -----------------------------------------------------------------------------
class CompareIn(BaseModel):
    competitor: str = Field(..., description="p.ej. 'tata'")
    store: str = Field(..., description="p.ej. 'Durazno'")
    offset: int = 0
    limit: int = 50
    items: List[str]

class ItemResult(BaseModel):
    input: str
    status: str               # "OK" | "No disponible" | "Sin stock" | "Error"
    name: Optional[str] = None
    price: Optional[float] = None
    listPrice: Optional[float] = None
    url: Optional[str] = None
    notes: Optional[str] = None

class CompareOut(BaseModel):
    competitor: str
    store: str
    offset: int
    limit: int
    count: int
    results: List[ItemResult]

# -----------------------------------------------------------------------------
# Normalización y utilidades
# -----------------------------------------------------------------------------
BRANDS = {
    # frecuentes (sumá si querés)
    "emigrante","shiva","maggi","knorr","costa","cololo","cocinero","alco","himalaya",
    # útiles para casos reales
    "bella","union","bella union","arcor","nativa","yerba","delicias"
}

def _normalize(s: str) -> str:
    s = (s or "").lower().strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

_STOP = {"de","la","el","los","las","un","una","con","en","a","y","x","sin","al","por","para","del"}

def _extract_sizes(text: str):
    """Devuelve (masas_en_g, volumenes_en_ml) como sets de enteros."""
    t = _normalize(text)
    masses, vols = set(), set()
    for m, unit in re.findall(r"(\d+)\s*(kg|g|gr)", t):
        n = int(m); masses.add(n*1000 if unit == "kg" else n)
    for m, unit in re.findall(r"(\d+)\s*(ml|l)", t):
        n = int(m); vols.add(n*1000 if unit == "l" else n)
    return masses, vols

def _is_noise(t: str) -> bool:
    if t in _STOP: return True
    if re.fullmatch(r"\d+(kg|g|gr|ml|l)", t): return True
    if re.fullmatch(r"\d+\s*(kg|g|gr|ml|l)", t): return True
    if re.fullmatch(r"x\d+", t): return True    # packs 3x, 6x
    if t in {"pack","pct","bolsa","frasco","bot","pet","unidad","un"}: return True
    return False

def _build_tries(q: str) -> List[str]:
    """Genera variantes de búsqueda (limpia, marca+producto, tokens fuertes, etc.)."""
    toks = _normalize(q).split()

    # quitar "0000" cuando NO es harina
    if "0000" in toks and "harina" not in toks:
        toks = [t for t in toks if t != "0000"]

    toks_clean = [t for t in toks if not _is_noise(t)]

    tries: List[str] = []
    def add_try(tokens):
        s = " ".join(tokens).strip()
        if s and s not in tries:
            tries.append(s)

    add_try(toks)                   # 1) tal cual
    add_try(toks_clean)             # 2) limpio
    if len(toks_clean) >= 2:        # 3) primeras dos e inversa
        add_try(toks_clean[:2])
        add_try([toks_clean[1], toks_clean[0]])
    marcas_en_query = [t for t in toks_clean if t in BRANDS]
    if marcas_en_query:             # 4) solo marca
        add_try(marcas_en_query[:1])
    for t in toks_clean:            # 5) tokens fuertes
        if len(t) >= 4:
            add_try([t])

    return tries

def _parse_price(txt: str) -> Optional[float]:
    """'$ 1.234,50' -> 1234.5 ; '$53,00' -> 53.0 ; '53.00' -> 53.0"""
    if not txt:
        return None
    s = txt.replace("\xa0", " ").strip()
    s = re.sub(r"[^0-9,\.]", "", s)
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except:
        return None

def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s or "")

# -----------------------------------------------------------------------------
# Fetch TATA: VTEX JSON + /busca (HTML) + PDP (HTML)
# -----------------------------------------------------------------------------
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
            except Exception as e:
                print("[TATA][JSON] error:", e)
                continue
    return []

def _fetch_pdp_tata(cli: httpx.Client, href: str) -> dict:
    """Carga la PDP y saca H1 + precio (varios patrones). Devuelve dict 'tipo VTEX'."""
    url = href if href.startswith("http") else "https://tata.com.uy" + href
    try:
        r = cli.get(url, follow_redirects=True)
        if r.status_code != 200:
            return {}
        html = r.text

        # Nombre: <h1> o <title>
        m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)
        name = _strip_tags(m.group(1)).strip() if m else None
        if not name:
            m2 = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
            name = _strip_tags(m2.group(1)).strip() if m2 else None

        # Precio: múltiples patrones
        price = None
        for pat in [
            r'\$ ?([\d\.\,]+)\s*</',                                           # $ 53,00</
            r'"Price"\s*:\s*([0-9]+(?:[\.,][0-9]+)?)',                         # "Price": 53.00
            r'"ListPrice"\s*:\s*([0-9]+(?:[\.,][0-9]+)?)',
            r'itemprop="price"\s+content="([0-9]+(?:[\.,][0-9]+)?)"',          # meta itemprop
            r'"price"\s*:\s*"([0-9]+(?:[\.,][0-9]+)?)"',                       # json-ld string
        ]:
            m = re.search(pat, html, re.I | re.S)
            if m:
                price = _parse_price(m.group(1))
                if price is not None:
                    break

        # Slug de la URL
        path = re.sub(r"https?://[^/]+", "", url)
        parts = [p for p in path.split("/") if p]
        slug = parts[-2] if parts and parts[-1] == "p" else (parts[-1] if parts else None)

        if not (name or slug):
            return {}

        return {
            "productName": name or _normalize(slug).replace("-", " "),
            "linkText": slug or "",
            "items": [{
                "sellers": [{
                    "commertialOffer": {
                        "Price": price,
                        "ListPrice": price,
                        "IsAvailable": True
                    }
                }]
            }]
        }
    except Exception as e:
        print("[TATA][PDP] error:", e)
        return {}

def _fetch_html_busca_tata(query: str):
    """Busca en /busca; si hay links a PDP, abre hasta 3 y arma objetos con nombre+precio reales."""
    urls = [
        f"https://tata.com.uy/busca?ft={quote_plus(query)}&O=OrderByScoreDESC",
        f"https://www.tata.com.uy/busca?ft={quote_plus(query)}&O=OrderByScoreDESC",
    ]
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "es-UY,es;q=0.9",
        "Referer": "https://tata.com.uy/"
    }
    results = []
    with httpx.Client(timeout=15, headers=headers) as cli:
        for url in urls:
            try:
                r = cli.get(url, follow_redirects=True)
                if r.status_code != 200:
                    continue
                html = r.text

                # Tomamos hasta 3 PDPs para confirmar
                links = re.findall(r'href="(/[^"]+/p)"', html, re.I)
                seen = set()
                for href in links:
                    if href in seen:
                        continue
                    seen.add(href)
                    prod = _fetch_pdp_tata(cli, href)
                    if prod:
                        results.append(prod)
                    if len(results) >= 3:
                        break

                # Si no se pudo abrir PDP, inferimos desde tarjetas
                if not results:
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
                        if len(results) >= 3:
                            break

                if results:
                    break
            except Exception as e:
                print("[TATA][HTML] error:", e)
                continue
    return results

def _fetch_products_tata(query: str):
    try:
        arr = _fetch_vtex_json_tata(query)
        if arr:
            return arr
        return _fetch_html_busca_tata(query)
    except Exception as e:
        print("[TATA] fetch error:", e)
        return []

# -----------------------------------------------------------------------------
# Scoring
# -----------------------------------------------------------------------------
def _score_product_from_query(p: dict, q_tokens: List[str], q_masses, q_vols, has_brand: bool) -> int:
    name = _normalize(f"{p.get('productName','')} {p.get('linkText','')}")
    s = 0
    for tok in q_tokens:
        if tok and tok in name:
            s += 1
    if has_brand and any(b in name for b in BRANDS):
        s += 2
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

def _best_vtex_tata(q: str):
    try:
        key = f"tata:{_normalize(q)}"
        now = time.time()
        hit = _cache.get(key)
        if hit and now - hit["ts"] < 12 * 3600:
            return hit["data"]

        tries = _build_tries(q)
        best, best_score = None, -1

        q_tokens = _normalize(q).split()
        q_masses, q_vols = _extract_sizes(q)
        has_brand = any(t in BRANDS for t in q_tokens)

        for t in tries:
            arr = _fetch_products_tata(t)
            if not arr:
                continue

            arr.sort(
                key=lambda p: _score_product_from_query(p, q_tokens, q_masses, q_vols, has_brand),
                reverse=True
            )
            cand = arr[0]
            sc = _score_product_from_query(cand, q_tokens, q_masses, q_vols, has_brand)
            if sc > best_score:
                best, best_score = cand, sc
            if best_score >= 3:   # umbral razonable para cortar
                break

        _cache[key] = {"ts": now, "data": best}
        return best
    except Exception as e:
        print("[TATA] best error:", e)
        return None

# -----------------------------------------------------------------------------
# Helpers precio/URL
# -----------------------------------------------------------------------------
def _extract_prices(product: dict):
    """Devuelve (price, list_price, available)."""
    try:
        for it in product.get("items", []) or []:
            for seller in it.get("sellers", []) or []:
                offer = seller.get("commertialOffer") or {}
                price = offer.get("Price")
                list_price = offer.get("ListPrice") or price
                available = offer.get("IsAvailable", True)
                if price is not None:
                    return float(price), float(list_price or price), bool(available)
    except Exception:
        pass
    return None, None, False

def _build_tata_url(product: dict) -> Optional[str]:
    slug = product.get("linkText")
    if not slug:
        return None
    slug = slug.strip("/")
    return f"https://tata.com.uy/{slug}/p"

# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------
@app.get("/")
def root():
    return {"status": "ok", "service": "comparador-uy-action", "ts": int(time.time())}

@app.post("/compare", response_model=CompareOut)
def compare(inb: CompareIn):
    # Por ahora solo TATA (fácil de extender)
    if inb.competitor.lower().strip() != "tata":
        return CompareOut(
            competitor=inb.competitor,
            store=inb.store,
            offset=inb.offset,
            limit=inb.limit,
            count=0,
            results=[]
        )

    items = inb.items or []
    start = max(inb.offset, 0)
    end = min(start + max(inb.limit, 1), len(items)) if items else start
    slice_items = items[start:end] if items else []

    results: List[ItemResult] = []

    for q in slice_items:
        try:
            prod = _best_vtex_tata(q)
            if not prod:
                results.append(ItemResult(
                    input=q, status="No disponible", notes="Sin coincidencias en VTEX/HTML/PDP"
                ))
                continue

            price, list_price, available = _extract_prices(prod)
            url = _build_tata_url(prod)
            name = prod.get("productName") or _normalize(prod.get("linkText", "")).replace("-", " ")

            if price is None:
                results.append(ItemResult(
                    input=q, status="No disponible", name=name, url=url, notes="Sin precio"
                ))
                continue

            status = "OK" if available else "Sin stock"
            results.append(ItemResult(
                input=q, status=status, name=name, price=price, listPrice=list_price, url=url
            ))
        except Exception as e:
            results.append(ItemResult(
                input=q, status="Error", notes=f"{type(e).__name__}: {e}"
            ))

    return CompareOut(
        competitor="TATA",
        store=inb.store,
        offset=inb.offset,
        limit=inb.limit,
        count=len(results),
        results=results
    )

# Nota Render (Start command):
# uvicorn main:app --host 0.0.0.0 --port $PORT
# ================== fin main.py ===================
