"""
Proxy Légifrance pour ChatGPT Actions - Version Cabinet avancée.

Architecture :
  ChatGPT (Bearer token statique) -> Ce proxy -> OAuth PISTE -> API Légifrance

Variables d'environnement Render :
  PISTE_CLIENT_ID
  PISTE_CLIENT_SECRET
  PROXY_API_KEY
  PISTE_ENV = prod ou sandbox
"""

import os
import time
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse
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
    title="Legifrance Proxy Cabinet DARMON",
    description="Proxy OAuth + endpoints juridiques avancés pour exposer Légifrance à ChatGPT Actions.",
    version="2.0.0",
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

    Méthode principale : OAuth client_credentials en HTTP Basic Auth.
    Fallback : client_id/client_secret dans le body.
    """
    now = time.time()

    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    async with httpx.AsyncClient(timeout=20.0) as client:
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


async def safe_piste_call(path: str, payload: dict) -> dict:
    """Appel PISTE qui ne casse pas une recherche globale si un fond échoue."""
    try:
        return await piste_call(path, payload)
    except HTTPException as exc:
        return {
            "error": True,
            "status_code": exc.status_code,
            "detail": exc.detail,
        }
    except Exception as exc:
        return {
            "error": True,
            "status_code": 500,
            "detail": str(exc),
        }


# ----------------------------------------------------------------------------
# Modèles d'entrée
# ----------------------------------------------------------------------------

class CodeSearchIn(BaseModel):
    query: str = Field(..., description="Mots-clés, expression ou numéro d'article à rechercher")
    code: str = Field(..., description="Nom du code : Code civil, Code du travail, CESEDA, CPC...")
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


class GlobalSearchIn(BaseModel):
    query: str = Field(..., description="Question, notion, mots-clés ou expression juridique")
    code: str | None = Field(None, description="Code à interroger si pertinent")
    include_code: bool = Field(True, description="Inclure une recherche dans un code")
    include_judicial: bool = Field(True, description="Inclure jurisprudence judiciaire")
    include_admin: bool = Field(True, description="Inclure jurisprudence administrative")
    include_jorf: bool = Field(False, description="Inclure JORF")
    date_from: str | None = Field(None, description="Date min YYYY-MM-DD")
    date_to: str | None = Field(None, description="Date max YYYY-MM-DD")
    page_size: int = Field(5, ge=1, le=20)


class LegalResearchIn(BaseModel):
    question: str = Field(..., description="Question juridique à rechercher")
    domain: str | None = Field(None, description="Domaine : civil, travail, étranger, pénal, administratif, médical...")
    page_size: int = Field(5, ge=1, le=20)


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


def _date_filter(facette: str, date_from: str | None, date_to: str | None) -> list[dict]:
    if not date_from and not date_to:
        return []
    return [
        {
            "facette": facette,
            "dates": {
                "start": date_from or "1900-01-01",
                "end": date_to or "2099-12-31",
            },
        }
    ]


def _infer_code(domain: str | None, question: str) -> str:
    text = f"{domain or ''} {question}".lower()

    if any(x in text for x in ["étranger", "etranger", "oqtf", "titre de séjour", "sejour", "ceseda", "préfecture", "prefecture"]):
        return "Code de l'entrée et du séjour des étrangers et du droit d'asile"
    if any(x in text for x in ["travail", "salarié", "salarie", "licenciement", "faute grave", "prud'hommes", "prudhommes"]):
        return "Code du travail"
    if any(x in text for x in ["pénal", "penal", "infraction", "garde à vue", "correctionnel"]):
        return "Code pénal"
    if any(x in text for x in ["procédure civile", "procedure civile", "assignation", "référé", "refere", "cpc"]):
        return "Code de procédure civile"
    if any(x in text for x in ["administratif", "conseil d'etat", "conseil d'état", "tribunal administratif", "caa"]):
        return "Code de justice administrative"
    if any(x in text for x in ["commerce", "société", "societe", "bail commercial"]):
        return "Code de commerce"

    return "Code civil"


# ----------------------------------------------------------------------------
# Endpoints généraux
# ----------------------------------------------------------------------------

@app.get("/")
async def root() -> dict:
    return {
        "ok": True,
        "message": "Proxy Légifrance Cabinet DARMON actif. Utilise /healthz ou /docs.",
        "version": "2.0.0",
    }


@app.get("/privacy", response_class=HTMLResponse)
async def privacy() -> str:
    return """
    <html>
      <head>
        <title>Privacy Policy - Proxy Légifrance Cabinet DARMON</title>
        <meta charset="utf-8" />
      </head>
      <body>
        <h1>Privacy Policy</h1>
        <p>This service is used only to query the French Légifrance API through PISTE.</p>
        <p>No personal data is stored, sold or shared by this proxy.</p>
        <p>Requests are transmitted to Légifrance only for legal research purposes.</p>
      </body>
    </html>
    """


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "ok": True,
        "env": PISTE_ENV,
        "piste_base": PISTE_BASE,
        "version": "2.0.0",
    }


# ----------------------------------------------------------------------------
# Endpoints de recherche simples
# ----------------------------------------------------------------------------

@app.post("/search/code", dependencies=[Depends(check_api_key)])
async def search_code(body: CodeSearchIn) -> dict:
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
    filters: list[dict] = []
    filters.extend(_date_filter("DATE_DECISION", body.date_from, body.date_to))

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
    filters: list[dict] = []
    filters.extend(_date_filter("DATE_DECISION", body.date_from, body.date_to))

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
    filters: list[dict] = []
    filters.extend(_date_filter("DATE_PUBLICATION", body.date_from, body.date_to))

    payload = _search_payload(
        fond="JORF",
        query=body.query,
        page_size=body.page_size,
        filters=filters,
    )

    return await piste_call("/search", payload)


@app.post("/article/get", dependencies=[Depends(check_api_key)])
async def article_get(body: ArticleGetIn) -> dict:
    return await piste_call(
        "/consult/getArticle",
        {
            "id": body.article_id,
        },
    )


@app.post("/decision/get", dependencies=[Depends(check_api_key)])
async def decision_get(body: DecisionGetIn) -> dict:
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


# ----------------------------------------------------------------------------
# Endpoints avancés pour GPT juridique
# ----------------------------------------------------------------------------

@app.post("/search/global", dependencies=[Depends(check_api_key)])
async def search_global(body: GlobalSearchIn) -> dict:
    """
    Recherche transversale : code + jurisprudence judiciaire + jurisprudence administrative + JORF.
    Retourne les résultats par source, même si une source échoue.
    """
    output: dict[str, Any] = {
        "query": body.query,
        "environment": PISTE_ENV,
        "results": {},
    }

    if body.include_code:
        code_name = body.code or _infer_code(None, body.query)
        code_payload = _search_payload(
            fond="CODE_DATE",
            query=body.query,
            page_size=body.page_size,
            filters=[
                {
                    "facette": "NOM_CODE",
                    "valeurs": [code_name],
                },
                {
                    "facette": "DATE_VERSION",
                    "singleDate": int(time.time() * 1000),
                },
            ],
        )
        output["results"]["code"] = {
            "code": code_name,
            "data": await safe_piste_call("/search", code_payload),
        }

    if body.include_judicial:
        judicial_payload = _search_payload(
            fond="JURI",
            query=body.query,
            page_size=body.page_size,
            filters=_date_filter("DATE_DECISION", body.date_from, body.date_to),
        )
        output["results"]["jurisprudence_judiciaire"] = await safe_piste_call("/search", judicial_payload)

    if body.include_admin:
        admin_payload = _search_payload(
            fond="CETAT",
            query=body.query,
            page_size=body.page_size,
            filters=_date_filter("DATE_DECISION", body.date_from, body.date_to),
        )
        output["results"]["jurisprudence_administrative"] = await safe_piste_call("/search", admin_payload)

    if body.include_jorf:
        jorf_payload = _search_payload(
            fond="JORF",
            query=body.query,
            page_size=body.page_size,
            filters=_date_filter("DATE_PUBLICATION", body.date_from, body.date_to),
        )
        output["results"]["jorf"] = await safe_piste_call("/search", jorf_payload)

    return output


@app.post("/research/legal", dependencies=[Depends(check_api_key)])
async def legal_research(body: LegalResearchIn) -> dict:
    """
    Recherche juridique intelligente pour GPT :
    - infère le code utile ;
    - recherche dans le code ;
    - recherche en jurisprudence judiciaire ;
    - recherche en jurisprudence administrative.
    """
    code_name = _infer_code(body.domain, body.question)

    code_payload = _search_payload(
        fond="CODE_DATE",
        query=body.question,
        page_size=body.page_size,
        filters=[
            {
                "facette": "NOM_CODE",
                "valeurs": [code_name],
            },
            {
                "facette": "DATE_VERSION",
                "singleDate": int(time.time() * 1000),
            },
        ],
    )

    judicial_payload = _search_payload(
        fond="JURI",
        query=body.question,
        page_size=body.page_size,
        filters=[],
    )

    admin_payload = _search_payload(
        fond="CETAT",
        query=body.question,
        page_size=body.page_size,
        filters=[],
    )

    return {
        "question": body.question,
        "domain": body.domain,
        "inferred_code": code_name,
        "environment": PISTE_ENV,
        "results": {
            "code": await safe_piste_call("/search", code_payload),
            "jurisprudence_judiciaire": await safe_piste_call("/search", judicial_payload),
            "jurisprudence_administrative": await safe_piste_call("/search", admin_payload),
        },
    }
