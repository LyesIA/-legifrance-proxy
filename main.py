"""
Proxy Légifrance pour ChatGPT Actions.

Architecture :
  ChatGPT (Bearer token statique) -> Ce proxy -> OAuth PISTE -> API Légifrance

Le proxy :
  1. S'authentifie auprès de PISTE avec client_credentials (Client ID/Secret en env)
  2. Cache le token PISTE (durée de vie ~1h) et le renouvelle automatiquement
  3. Expose des endpoints REST simples, protégés par un Bearer token statique
     (PROXY_API_KEY) que tu configures dans le GPT custom
  4. Traduit les requêtes simples en payloads PISTE complexes

Endpoints exposés :
  GET  /healthz                  - ping
  POST /search/code              - recherche dans un code (CCIV, CPC, CESEDA...)
  POST /search/jurisprudence     - jurisprudence judiciaire (Cass + CA via JURI/JURICA)
  POST /search/juriadmin         - jurisprudence administrative (CE, CAA, TA)
  POST /search/jorf              - Journal Officiel
  POST /article/get              - récupère un article par son ID
  POST /decision/get             - récupère une décision par son ID
"""
import os
import time
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

# ----------------------------------------------------------------------------
# Configuration (via variables d'environnement)
# ----------------------------------------------------------------------------
PISTE_CLIENT_ID = os.environ["PISTE_CLIENT_ID"]
PISTE_CLIENT_SECRET = os.environ["PISTE_CLIENT_SECRET"]
PROXY_API_KEY = os.environ["PROXY_API_KEY"]  # clé que ChatGPT enverra en Bearer

# Sandbox PISTE pour tests : api.piste.gouv.fr/dila/legifrance/sandbox
# Prod : api.piste.gouv.fr/dila/legifrance
PISTE_ENV = os.environ.get("PISTE_ENV", "prod")
PISTE_BASE = (
    "https://sandbox-api.piste.gouv.fr/dila/legifrance/lf-engine-app"
    if PISTE_ENV == "sandbox"
    else "https://api.piste.gouv.fr/dila/legifrance/lf-engine-app"
)
PISTE_OAUTH_URL = (
    "https://sandbox-oauth.piste.gouv.fr/api/oauth/token"
    if PISTE_ENV == "sandbox"
    else "https://oauth.piste.gouv.fr/api/oauth/token"
)

app = FastAPI(
    title="Legifrance Proxy for ChatGPT",
    description="Proxy OAuth + endpoints simplifiés pour exposer Légifrance à ChatGPT Actions.",
    version="1.0.0",
)
security = HTTPBearer()

# ----------------------------------------------------------------------------
# Auth ChatGPT -> proxy (Bearer token statique)
# ----------------------------------------------------------------------------
def check_api_key(creds: HTTPAuthorizationCredentials = Depends(security)) -> None:
    if creds.credentials != PROXY_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token invalide",
        )

# ----------------------------------------------------------------------------
# Cache de token PISTE
# ----------------------------------------------------------------------------
_token_cache: dict[str, Any] = {"token": None, "expires_at": 0.0}

async def get_piste_token() -> str:
    """Renvoie un access_token PISTE valide, en le rafraîchissant si besoin."""
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            PISTE_OAUTH_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": PISTE_CLIENT_ID,
                "client_secret": PISTE_CLIENT_SECRET,
                "scope": "openid",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if resp.status_code != 200:
        raise HTTPException(502, f"Auth PISTE échouée : {resp.text}")
    data = resp.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + int(data.get("expires_in", 3600))
    return _token_cache["token"]

async def piste_call(path: str, payload: dict) -> dict:
    """Appel POST authentifié à l'API PISTE Légifrance."""
    token = await get_piste_token()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{PISTE_BASE}{path}",
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "accept": "application/json",
            },
        )
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, resp.text)
    return resp.json()

# ----------------------------------------------------------------------------
# Modèles d'entrée (pensés pour ChatGPT : simples et plats)
# ----------------------------------------------------------------------------
class CodeSearchIn(BaseModel):
    query: str = Field(..., description="Mots-clés ou expression à rechercher")
    code: str = Field(
        ...,
        description="Nom du code, ex: 'Code civil', 'Code de procédure civile', "
        "'Code de l'entrée et du séjour des étrangers et du droit d'asile'",
    )
    page_size: int = Field(10, ge=1, le=50)

class JurisprudenceSearchIn(BaseModel):
    query: str = Field(..., description="Mots-clés (ex: 'clause résolutoire bail')")
    date_from: str | None = Field(None, description="Date min, format YYYY-MM-DD")
    date_to: str | None = Field(None, description="Date max, format YYYY-MM-DD")
    juridiction: str | None = Field(
        None,
        description="Filtre juridiction. Judiciaire: 'Cour de cassation', 'cours d'appel'. "
        "Administratif: 'Conseil d'Etat', 'Cours administratives d'appel', 'Tribunaux administratifs'.",
    )
    page_size: int = Field(10, ge=1, le=50)

class JorfSearchIn(BaseModel):
    query: str
    date_from: str | None = None
    date_to: str | None = None
    page_size: int = Field(10, ge=1, le=50)

class ArticleGetIn(BaseModel):
    article_id: str = Field(..., description="ID Légifrance, ex: 'LEGIARTI000006419279'")

class DecisionGetIn(BaseModel):
    decision_id: str = Field(..., description="ID Légifrance, ex: 'JURITEXT000048424...'")
    fonds: str = Field("JURI", description="JURI, JURICA, CETAT, CONSTIT, CNIL, JUFI")

# ----------------------------------------------------------------------------
# Helpers : construction des payloads PISTE (format complexe imposé par DILA)
# ----------------------------------------------------------------------------
def _search_payload(fond: str, query: str, page_size: int,
                    filters: list[dict] | None = None) -> dict:
    return {
        "recherche": {
            "champs": [{
                "typeChamp": "ALL",
                "criteres": [{"typeRecherche": "EXACTE",
                              "valeur": query, "operateur": "ET"}],
                "operateur": "ET",
            }],
            "filtres": filters or [],
            "pageNumber": 1,
            "pageSize": page_size,
            "sort": "PERTINENCE",
            "typePagination": "DEFAUT",
        },
        "fond": fond,
    }

# ----------------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------------
@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "env": PISTE_ENV}

@app.post("/search/code", dependencies=[Depends(check_api_key)])
async def search_code(body: CodeSearchIn) -> dict:
    """Recherche full-text dans un code spécifique."""
    payload = _search_payload(
        "CODE_DATE", body.query, body.page_size,
        filters=[{"facette": "NOM_CODE", "valeurs": [body.code]},
                 {"facette": "DATE_VERSION",
                  "singleDate": int(time.time() * 1000)}],
    )
    return await piste_call("/search", payload)

@app.post("/search/jurisprudence", dependencies=[Depends(check_api_key)])
async def search_jurisprudence(body: JurisprudenceSearchIn) -> dict:
    """Jurisprudence judiciaire (Cass + CA, fonds JURI)."""
    filters: list[dict] = []
    if body.date_from or body.date_to:
        filters.append({
            "facette": "DATE_DECISION",
            "dates": {"start": body.date_from or "1900-01-01",
                      "end": body.date_to or "2099-12-31"},
        })
    if body.juridiction:
        filters.append({"facette": "JURIDICTION_JUDICIAIRE",
                        "valeurs": [body.juridiction]})
    return await piste_call("/search",
                            _search_payload("JURI", body.query, body.page_size, filters))

@app.post("/search/juriadmin", dependencies=[Depends(check_api_key)])
async def search_juriadmin(body: JurisprudenceSearchIn) -> dict:
    """Jurisprudence administrative (fonds CETAT)."""
    filters: list[dict] = []
    if body.date_from or body.date_to:
        filters.append({
            "facette": "DATE_DECISION",
            "dates": {"start": body.date_from or "1900-01-01",
                      "end": body.date_to or "2099-12-31"},
        })
    if body.juridiction:
        filters.append({"facette": "JURIDICTION_ADMIN",
                        "valeurs": [body.juridiction]})
    return await piste_call("/search",
                            _search_payload("CETAT", body.query, body.page_size, filters))

@app.post("/search/jorf", dependencies=[Depends(check_api_key)])
async def search_jorf(body: JorfSearchIn) -> dict:
    """Recherche dans le Journal Officiel."""
    filters: list[dict] = []
    if body.date_from or body.date_to:
        filters.append({
            "facette": "DATE_PUBLICATION",
            "dates": {"start": body.date_from or "1900-01-01",
                      "end": body.date_to or "2099-12-31"},
        })
    return await piste_call("/search",
                            _search_payload("JORF", body.query, body.page_size, filters))

@app.post("/article/get", dependencies=[Depends(check_api_key)])
async def article_get(body: ArticleGetIn) -> dict:
    """Récupère un article par son ID Légifrance."""
    return await piste_call("/consult/getArticle", {"id": body.article_id})

@app.post("/decision/get", dependencies=[Depends(check_api_key)])
async def decision_get(body: DecisionGetIn) -> dict:
    """Récupère une décision (jurisprudence) par son ID."""
    endpoint = {
        "JURI": "/consult/juri",
        "JURICA": "/consult/juri",
        "CETAT": "/consult/juriAdmin",
        "CONSTIT": "/consult/decisionConstit",
        "CNIL": "/consult/cnil",
        "JUFI": "/consult/jufi",
    }.get(body.fonds.upper(), "/consult/juri")
    return await piste_call(endpoint, {"textId": body.decision_id})
