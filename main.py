"""
Proxy Légifrance pour ChatGPT Actions.

Architecture :
  ChatGPT (Bearer token statique) -> Ce proxy -> OAuth PISTE -> API Légifrance

Variables d'environnement Render :
  PISTE_CLIENT_ID
  PISTE_CLIENT_SECRET
  PROXY_API_KEY
  PISTE_ENV = sandbox ou prod
"""

import os
import time
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field


# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

PISTE_CLIENT_ID = os.environ["PISTE_CLIENT_ID"]
PISTE_CLIENT_SECRET = os.environ["PISTE_CLIENT_SECRET"]
PROXY_API_KEY = os.environ["PROXY_API_KEY"]

PISTE_ENV = os.environ.get("PISTE_ENV", "prod").lower()

if PISTE_ENV == "sandbox":
    PISTE_BASE = "https://sandbox-api.piste.gouv.fr/dila/legifrance/lf-engine-app"
    PISTE_OAUTH_URL = "https://sandbox-oauth.piste.gouv.fr/api/oauth/token"
else:
    PISTE_BASE = "https://api.piste.gouv.fr/dila/legifrance/lf-engine-app"
    PISTE_OAUTH_URL = "https://oauth.piste.gouv.fr/api/oauth/token"


app = FastAPI(
    title="Legifrance Proxy for ChatGPT",
    description="Proxy OAuth + endpoints simplifiés pour exposer Légifrance à ChatGPT Actions.",
    version="1.1.0",
)

security = HTTPBearer()


# ----------------------------------------------------------------------------
# Auth ChatGPT -> Proxy
# ----------------------------------------------------------------------------

def check_api_key(creds: HTTPAuthorizationCredentials = Depends(security)) -> None:
    if creds.credentials != PROXY_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token invalide",
        )


# ----------------------------------------------------------------------------
# Token PISTE
# ----------------------------------------------------------------------------

_token_cache: dict[str, Any] = {
    "token": None,
    "expires_at": 0.0,
}


async def get_piste_token() -> str:
    """
    Récupère un access_token PISTE.

    On tente d'abord l'authentification OAuth client_credentials avec HTTP Basic Auth.
    Si PISTE refuse, on tente ensuite avec client_id/client_secret dans le body.
    """
    now = time.time()

    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    async with httpx.AsyncClient(timeout=20.0) as client:
        # Méthode 1 : HTTP Basic Auth
        resp = await client.post(
            PISTE_OAUTH_URL,
            data={
                "grant_type": "client_credentials",
                "scope": "openid",
            },
            auth=(PISTE_CLIENT_ID, PISTE_CLIENT_SECRET),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )

        # Méthode 2 : client_id / client_secret dans le body
        if resp.status_code != 200:
            resp = await client.post(
                PISTE_OAUTH_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": PISTE_CLIENT_ID,
                    "client_secret": PISTE_CLIENT_SECRET,
                    "scope": "openid",
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
            )

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Authentification PISTE échouée : {resp.status_code} - {resp.text}",
        )

    data = resp.json()
    access_token = data.get("access_token")

    if not access_token:
        raise HTTPException(
            status_code=502,
            detail=f"Réponse PISTE invalide : {data}",
        )

    _token_cache["token"] = access_token
    _token_cache["expires_at"] = now + int(data.get("expires_in", 3600))

    return access_token


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
                "Accept": "application/json",
            },
        )

    if resp.status_code >= 400:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Erreur PISTE : {resp.text}",
        )

    return resp.json()


# ----------------------------------------------------------------------------
# Modèles d'entrée
# ----------------------------------------------------------------------------

class CodeSearchIn(BaseModel):
    query: str = Field(..., description="Mots-clés ou expression à rechercher")
    code: str = Field(
        ...,
        description="Nom du code : Code civil, Code de procédure civile, CESEDA...",
    )
    page_size: int = Field(10, ge=1, le=50)


class JurisprudenceSearchIn(BaseModel):
    query: str = Field(..., description="Mots-clés de recherche")
    date_from: str | None = Field(None, description="Date min YYYY-MM-DD")
    date_to: str | None = Field(None, description="Date max YYYY-MM-DD")
    juridiction: str | None = Field(None, description="Filtre juridiction")
    page_size: int = Field(10, ge=1, le=50)


class JorfSearchIn(BaseModel):
    query: str
    date_from: str | None = None
    date_to: str | None = None
    page_size: int = Field(10, ge=1, le=50)


class ArticleGetIn(BaseModel):
    article_id: str = Field(..., description="Exemple : LEGIARTI000006419279")


class DecisionGetIn(BaseModel):
    decision_id: str = Field(..., description="ID de décision Légifrance")
    fonds: str = Field("JURI", description="JURI, JURICA, CETAT, CONSTIT, CNIL, JUFI")


# ----------------------------------------------------------------------------
# Helpers payload PISTE
# ----------------------------------------------------------------------------

def _search_payload(
    fond: str,
    query: str,
    page_size: int,
    filters: list[dict] | None = None,
) -> dict:
    return {
        "recherche": {
            "champs": [
                {
                    "typeChamp": "ALL",
                    "criteres": [
                        {
                            "typeRecherche": "EXACTE",
                            "valeur": query,
                            "operateur": "ET",
                        }
                    ],
                    "operateur": "ET",
                }
            ],
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

@app.get("/")
async def root() -> dict:
    return {
        "ok": True,
        "message": "Proxy Légifrance actif. Utilise /healthz ou /docs.",
    }


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "ok": True,
        "env": PISTE_ENV,
        "piste_base": PISTE_BASE,
    }


@app.post("/search/code", dependencies=[Depends(check_api_key)])
async def search_code(body: CodeSearchIn) -> dict:
    """Recherche full-text dans un code spécifique."""
    payload = _search_payload(
        fond="CODE_DATE",
        query=body.query,
        page_size=body.page_size,
        filters=[
            {
                "facette": "NOM_CODE",
                "valeurs": [body.code],
            },
            {
                "facette": "DATE_VERSION",
                "singleDate": int(time.time() * 1000),
            },
        ],
    )

    return await piste_call("/search", payload)


@app.post("/search/jurisprudence", dependencies=[Depends(check_api_key)])
async def search_jurisprudence(body: JurisprudenceSearchIn) -> dict:
    """Jurisprudence judiciaire."""
    filters: list[dict] = []

    if body.date_from or body.date_to:
        filters.append(
            {
                "facette": "DATE_DECISION",
                "dates": {
                    "start": body.date_from or "1900-01-01",
                    "end": body.date_to or "2099-12-31",
                },
            }
        )

    if body.juridiction:
        filters.append(
            {
                "facette": "JURIDICTION_JUDICIAIRE",
                "valeurs": [body.juridiction],
            }
        )

    payload = _search_payload(
        fond="JURI",
        query=body.query,
        page_size=body.page_size,
        filters=filters,
    )

    return await piste_call("/search", payload)


@app.post("/search/juriadmin", dependencies=[Depends(check_api_key)])
async def search_juriadmin(body: JurisprudenceSearchIn) -> dict:
    """Jurisprudence administrative."""
    filters: list[dict] = []

    if body.date_from or body.date_to:
        filters.append(
            {
                "facette": "DATE_DECISION",
                "dates": {
                    "start": body.date_from or "1900-01-01",
                    "end": body.date_to or "2099-12-31",
                },
            }
        )

    if body.juridiction:
        filters.append(
            {
                "facette": "JURIDICTION_ADMIN",
                "valeurs": [body.juridiction],
            }
        )

    payload = _search_payload(
        fond="CETAT",
        query=body.query,
        page_size=body.page_size,
        filters=filters,
    )

    return await piste_call("/search", payload)


@app.post("/search/jorf", dependencies=[Depends(check_api_key)])
async def search_jorf(body: JorfSearchIn) -> dict:
    """Recherche au Journal officiel."""
    filters: list[dict] = []

    if body.date_from or body.date_to:
        filters.append(
            {
                "facette": "DATE_PUBLICATION",
                "dates": {
                    "start": body.date_from or "1900-01-01",
                    "end": body.date_to or "2099-12-31",
                },
            }
        )

    payload = _search_payload(
        fond="JORF",
        query=body.query,
        page_size=body.page_size,
        filters=filters,
    )

    return await piste_call("/search", payload)


@app.post("/article/get", dependencies=[Depends(check_api_key)])
async def article_get(body: ArticleGetIn) -> dict:
    """Récupère un article par son ID Légifrance."""
    return await piste_call(
        "/consult/getArticle",
        {
            "id": body.article_id,
        },
    )


@app.post("/decision/get", dependencies=[Depends(check_api_key)])
async def decision_get(body: DecisionGetIn) -> dict:
    """Récupère une décision par son ID."""
    fonds = body.fonds.upper()

    endpoint = {
        "JURI": "/consult/juri",
        "JURICA": "/consult/juri",
        "CETAT": "/consult/juriAdmin",
        "CONSTIT": "/consult/decisionConstit",
        "CNIL": "/consult/cnil",
        "JUFI": "/consult/jufi",
    }.get(fonds)

    if not endpoint:
        raise HTTPException(
            status_code=400,
            detail="fonds invalide. Valeurs acceptées : JURI, JURICA, CETAT, CONSTIT, CNIL, JUFI",
        )

    if fonds in ["JURI", "JURICA", "CETAT"]:
        payload = {"textId": body.decision_id}
    else:
        payload = {"id": body.decision_id}

    return await piste_call(endpoint, payload)
