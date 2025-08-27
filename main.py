# ==================== main.py ====================
import re
import time
import httpx
import unicodedata
from urllib.parse import quote_plus
from typing import List, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Playwright (usado por request, sin globals)
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

# ---------- FastAPI ----------
app = FastAPI(title="Comparador UY (browser per-request)", version="2.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Modelos ----------
class CompareIn(BaseModel):
    competitor: str = Field(..., description="p.ej. 'tata'")
    store: str = Field(..., description="p.ej. 'Durazno'")
    offset: int = 0
    limit: int = 50
    items: List[str]

class ItemResult(BaseModel):
    input: str
    status: str                 # OK | Sin stock | No disponible | Error | No implementado
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

# ---------- Utils ----------
_cache = {}

BRANDS = {
    "emigrante","shiva","maggi","knorr","costa","cololo","cocinero",
    "alco","himalaya","bella","union","bella union","arcor","nativa",
    "yerba","delicias","cimarron","marolio","adonis","san remo"
}
_STOP = {"de","la","el","los","las","un","una","con","en","a","y","x","sin","al","por","para","del"}

def _normalize(s: str) -> str:
    s = (s or "").lower().strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def _extract_sizes(text: str):
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
    if re.fullmatch(r"x\d+", t): return True
    if t in {"pack","pct","bolsa","frasco","bot","pet","unidad","un"}: return True
    return False

def _build_tries(q: str) -> List[str]:
    toks = _normalize(q).split()
    # Regla especial: "0000" suele ensuciar excepto HARINA 0000
    if "0000" in toks and "harina" not in toks:
        toks = [t for t in toks if t != "0000"]
    toks_clean = [t for t in toks if not _is_noise(t)]
    tries: List[str] = []
    def add_try(tokens):
        s = " ".join(tokens).strip()
        if s and s not in tries:
            tries.append(s)
    add_try(toks)              # completo
    add_try(toks_clean)        # limpio
    if len(toks_clean) >= 2:   # primeras 2 palabras
        add_try(toks_clean[:2])
        add_try([toks_clean[1], toks_clean[0]])  # invertido
    marcas = [t for t in toks_clean if t in BRANDS]
    if marcas: add_try(marcas[:1])
    for t in toks_clean:
        if len(t) >= 4: add_try([t])             # token suelto (>=4)
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

def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s or "")

# ---------- Fetch TATA: rápido por HTTP ----------
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

def _fetch_busca_html_tata(query: str):
    """Fallback liviano: /busca HTML (sin JS)."""
    urls = [
        f"https://tata.com.uy/busca?ft={quote_plus(query)}&O=OrderByScoreDESC",
        f"https://www.tata.com.uy/busca?ft={quote_plus(query)}&O=OrderByScoreDESC",
    ]
    headers = {"User-Agent": "Mozilla/5.0", "Accept-Language": "es-UY,es;q=0.9"}
    res = []
    with httpx.Client(timeout=15, headers=headers) as cli:
        for url in urls:
            try:
                r = cli.get(url, follow_redirects=True)
                if r.status_code != 200:
                    continue
                html = r.text
                # Captura anchors a PDP + precio que suele renderizar en SSR
                for href, precio in re.findall(r'href="(/[^"]+/p)".{0,500}?\$ ?([\d\.\,]+)', html, re.I | re.S):
                    slug = href.strip("/").split("/")[0]
                    name_guess = _normalize(slug).replace("-", " ")
                    price = _parse_price(precio)
                    res.append({
                        "productName": name_guess,
                        "linkText": slug,
                        "items": [{
                            "sellers": [{
                                "commertialOffer": {"Price": price, "ListPrice": price, "IsAvailable": True}
                            }]
                        }]
                    })
                if res: break
            except Exception as e:
                print("[TATA][HTML-lite] error:", e)
                continue
    return res

# ---------- Fallback fuerte: navegador real (por request) ----------
def _pdp_from_page(page, url: str) -> dict:
    """Abre PDP y extrae nombre + precio (robusto)."""
    full = url if url.startswith("http") else ("https://tata.com.uy" + url)
    page.goto(full, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(700)

    html = page.content()

    # Nombre
    name = None
    try:
        name = page.locator("h1").first.text_content(timeout=3000)
        name = name.strip() if name else None
    except Exception:
        pass
    if not name:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        name = _strip_tags(m.group(1)).strip() if m else None

    # Precio (varios patrones)
    price = None
    for pat in [
        r'itemprop="price"\s+content="([0-9]+(?:[\.,][0-9]+)?)"',
        r'"Price"\s*:\s*([0-9]+(?:[\.,][0-9]+)?)',
        r'"ListPrice"\s*:\s*([0-9]+(?:[\.,][0-9]+)?)',
        r'\$ ?([\d\.\,]+)\s*</',
        r'"price"\s*:\s*"([0-9]+(?:[\.,][0-9]+)?)"',
    ]:
        m = re.search(pat, html, re.I | re.S)
        if m:
            price = _parse_price(m.group(1))
            if price is not None:
                break

    # Slug
    path = re.sub(r"https?://[^/]+", "", full)
    parts = [p for p in path.split("/") if p]
    slug = parts[-2] if parts and parts[-1] == "p" else (parts[-1] if parts else "")

    return {
        "productName": name or _normalize(slug).replace("-", " "),
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
    }

def _search_with_browser_tata(query: str):
    """Abre /busca, toma 1–3 PDPs y devuelve candidatos. (Browser por request)"""
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu","--disable-setuid-sandbox"],
        )
        context = browser.new_context(
            locale="es-UY",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        try:
            url = f"https://tata.com.uy/busca?ft={quote_plus(query)}&O=OrderByScoreDESC"
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(800)

            # Intento de seleccionar "Durazno" si aparece la UI (best effort)
            try:
                el = page.get_by_text("Durazno", exact=True).first
                if el.is_visible():
                    el.click()
                    page.wait_for_timeout(500)
            except Exception:
                pass

            anchors = page.locator('a[href$="/p"]').all()[:3]
            for a in anchors:
                try:
                    href = a.get_attribute("href")
                    if href:
                        prod = _pdp_from_page(page, href)
                        results.append(prod)
                except Exception as e:
                    print("[TATA][PW] PDP error:", e)
                    continue
        except PWTimeoutError:
            print("[TATA][PW] timeout en /busca")
        except Exception as e:
            print("[TATA][PW] error:", e)
        finally:
            try: page.close()
            except: pass
            try: context.close()
            except: pass
            try: browser.close()
            except: pass
    return results

# ---------- Scoring ----------
def _score_product_from_query(p: dict, q_tokens: List[str], q_masses, q_vols, has_brand: bool) -> int:
    name = _normalize(f"{p.get('productName','')} {p.get('linkText','')}")
    s = 0
    for tok in q_tokens:
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

def _extract_prices(product: dict):
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
    if not slug: return None
    slug = slug.strip("/")
    return f"https://tata.com.uy/{slug}/p"

def _fetch_products_tata(query: str):
    """Cascada: JSON VTEX -> HTML liviano -> Browser (por request)."""
    arr = _fetch_vtex_json_tata(query)
    if arr: return arr
    arr = _fetch_busca_html_tata(query)
    if arr: return arr
    arr = _search_with_browser_tata(query)
    return arr or []

def _best_tata(q: str):
    key = f"tata:{_normalize(q)}"
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit["ts"] < 6 * 3600:
        return hit["data"]

    tries = _build_tries(q)
    best, best_score = None, -1
    q_tokens = _normalize(q).split()
    q_masses, q_vols = _extract_sizes(q)
    has_brand = any(t in BRANDS for t in q_tokens)

    for t in tries:
        arr = _fetch_products_tata(t)
        if not arr: continue
        arr.sort(key=lambda p: _score_product_from_query(p, q_tokens, q_masses, q_vols, has_brand), reverse=True)
        cand = arr[0]
        sc = _score_product_from_query(cand, q_tokens, q_masses, q_vols, has_brand)
        if sc > best_score:
            best, best_score = cand, sc
        if best_score >= 3:
            break

    _cache[key] = {"ts": now, "data": best}
    return best

# ---------- Endpoints ----------
@app.get("/")
def root():
    return {"status": "ok", "service": "comparador-uy-browser", "ts": int(time.time())}

@app.post("/compare", response_model=CompareOut)
def compare(inb: CompareIn):
    comp = inb.competitor.lower().strip()
    if comp != "tata":
        # Para que el GPT no confunda "sin resultados" con "no implementado"
        return CompareOut(
            competitor=inb.competitor,
            store=inb.store,
            offset=inb.offset,
            limit=inb.limit,
            count=0,
            results=[ItemResult(input="", status="No implementado", notes="Sólo TATA implementado en este servicio")]
        )

    items = inb.items or []
    start = max(inb.offset, 0)
    end = min(start + max(inb.limit, 1), len(items)) if items else start
    slice_items = items[start:end] if items else []

    results: List[ItemResult] = []

    for q in slice_items:
        try:
            prod = _best_tata(q)
            if not prod:
                results.append(ItemResult(input=q, status="No disponible", notes="Sin coincidencias (JSON/HTML/Browser)"))
                continue

            price, list_price, available = _extract_prices(prod)
            url = _build_tata_url(prod)
            name = (prod.get("productName") or _normalize(prod.get("linkText", "")).replace("-", " ")).strip()

            if price is None:
                results.append(ItemResult(input=q, status="No disponible", name=name, url=url, notes="Sin precio"))
                continue

            status = "OK" if available else "Sin stock"
            results.append(ItemResult(input=q, status=status, name=name, price=price, listPrice=list_price, url=url))
        except Exception as e:
            results.append(ItemResult(input=q, status="Error", notes=f"{type(e).__name__}: {e}"))

    return CompareOut(competitor="TATA", store=inb.store, offset=inb.offset, limit=inb.limit, count=len(results), results=results)
# ================== fin main.py ===================

