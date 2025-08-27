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

# ---------- Playwright (navegador real) ----------
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

_play = None
_browser = None
_context = None

def _ensure_browser():
    """Arranca Chromium una sola vez (modo headless)."""
    global _play, _browser, _context
    if _browser:
        return
    _play = sync_playwright().start()
    _browser = _play.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-setuid-sandbox",
        ],
    )
    _context = _browser.new_context(
        locale="es-UY",
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
        ),
    )

def _close_browser():
    global _play, _browser, _context
    try:
        if _context: _context.close()
        if _browser: _browser.close()
        if _play: _play.stop()
    except Exception:
        pass
    finally:
        _play = _browser = _context = None

# ---------- FastAPI ----------
app = FastAPI(title="Comparador UY (browser fallback)", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def _startup():
    try:
        _ensure_browser()
    except Exception as e:
        print("[BOOT] Playwright no iniciado:", e)

@app.on_event("shutdown")
def _shutdown():
    _close_browser()

# ---------- Modelos ----------
class CompareIn(BaseModel):
    competitor: str = Field(..., description="p.ej. 'tata'")
    store: str = Field(..., description="p.ej. 'Durazno'")
    offset: int = 0
    limit: int = 50
    items: List[str]

class ItemResult(BaseModel):
    input: str
    status: str                 # OK | Sin stock | No disponible | Error
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

# ---------- Utilidades ----------
_cache = {}

BRANDS = {
    "emigrante","shiva","maggi","knorr","costa","cololo","cocinero","alco","himalaya",
    "bella","union","bella union","arcor","nativa","yerba","delicias"
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
    if "0000" in toks and "harina" not in toks:
        toks = [t for t in toks if t != "0000"]
    toks_clean = [t for t in toks if not _is_noise(t)]
    tries: List[str] = []
    def add_try(tokens):
        s = " ".join(tokens).strip()
        if s and s not in tries:
            tries.append(s)
    add_try(toks)
    add_try(toks_clean)
    if len(toks_clean) >= 2:
        add_try(toks_clean[:2])
        add_try([toks_clean[1], toks_clean[0]])
    marcas = [t for t in toks_clean if t in BRANDS]
    if marcas: add_try(marcas[:1])
    for t in toks_clean:
        if len(t) >= 4: add_try([t])
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

# ---------- Fetch TATA rápido (HTTP) ----------
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

def _fetch_busca_html_tata(query: str):
    """Fallback liviano: /busca HTML (sin ejecutar JS)."""
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
                if r.status_code != 200: continue
                html = r.text
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

# ---------- Fallback fuerte: navegador real (Playwright) ----------
def _pdp_from_page(page, url: str) -> dict:
    """Abre PDP y extrae nombre + precio de forma robusta."""
    full = url if url.startswith("http") else ("https://tata.com.uy" + url)
    page.goto(full, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(600)

    html = page.content()

    # nombre
    name = None
    try:
        name = page.locator("h1").first.text_content(timeout=3000)
        name = name.strip() if name else None
    except Exception:
        pass
    if not name:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        name = _strip_tags(m.group(1)).strip() if m else None

    # precio (varios patrones)
    price = None
    for pat
