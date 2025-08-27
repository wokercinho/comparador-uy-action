# Comparador Mayorista UY — Action para ChatGPT

Procesa hasta 300 artículos por llamada. Implementado: **Tata** (VTEX).

## Despliegue rápido en Render
1. Crea un repo en GitHub con estos archivos.
2. En Render.com: **New → Web Service → Connect repo**.
3. Build: `pip install -r requirements.txt`
4. Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Env Var: `API_KEY = TU_CLAVE_SECRETA`

## Probar
```bash
curl -X POST https://TU-URL/compare  -H "x-api-key: TU_CLAVE_SECRETA" -H "Content-Type: application/json"  -d '{"competitor":"tata","store":"Durazno","offset":0,"limit":200,"items":["ARROZ 0000 1KG","AZUCAR COMUN 1KG"]}'
```

## OpenAPI para tu GPT
Pega `openapi.yaml` en **Configure → Actions → Import from text** y reemplaza `servers.url` con tu URL.