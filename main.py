"""
Proxy Légifrance Cabinet - VERSION UNIQUE FINALE.

Objectif :
  ChatGPT Actions -> Proxy sécurisé -> OAuth PISTE -> API Légifrance officielle.

Variables Render obligatoires :
  PISTE_CLIENT_ID
  PISTE_CLIENT_SECRET
  PROXY_API_KEY
  PISTE_ENV = prod ou sandbox

Endpoints GPT :
  /healthz
  /privacy
  /search/code
  /search/jurisprudence
  /search/juriadmin
  /search/jorf
  /article/get
  /decision/get
  /search/global
  /research/legal
  /research/medical-loss-chance
"""

import os
import time
import re
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

PISTE_ENV = os.environ.get("PISTE_ENV", "prod").lower().strip()

if PISTE_ENV == "sandbox":
    PISTE_BASE = "https://sandbox-api.piste.gouv.fr/dila/legifrance/lf-engine-app"
    PISTE_OAUTH_URL = "https://sandbox-oauth.piste.gouv.fr/api/oauth/token"
else:
    PISTE_ENV = "prod"
    PISTE_BASE = "https://api.piste.gouv.fr/dila/legifrance/lf-engine-app"
    PISTE_OAUTH_URL = "https://oauth.piste.gouv.fr/api/oauth/token"


app = FastAPI(
    title="Legifrance Proxy Cabinet DARMON - Final",
    description="Proxy OAuth PISTE + moteur juridique Légifrance pour GPT.",
    version="10.0.0",
)

security = HTTPBearer()


# ----------------------------------------------------------------------------
# Sécurité proxy
# ----------------------------------------------------------------------------

def check_api_key(creds: HTTPAuthorizationCredentials = Depends(security)) -> None:
    if creds.credentials != PROXY_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token invalide",
        )


# ----------------------------------------------------------------------------
# OAuth PISTE
# ----------------------------------------------------------------------------

_token_cache: dict[str, Any] = {"token": None, "expires_at": 0.0}


async def get_piste_token() -> str:
    now = time.time()

    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    async with httpx.AsyncClient(timeout=25.0) as client:
        # Méthode officielle la plus fiable : Basic Auth
        resp = await client.post(
            PISTE_OAUTH_URL,
            data={"grant_type": "client_credentials", "scope": "openid"},
            auth=(PISTE_CLIENT_ID, PISTE_CLIENT_SECRET),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )

        # Fallback : credentials dans le body
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
    token = data.get("access_token")

    if not token:
        raise HTTPException(status_code=502, detail=f"Réponse OAuth PISTE invalide : {data}")

    _token_cache["token"] = token
    _token_cache["expires_at"] = now + int(data.get("expires_in", 3600))
    return token


async def piste_call(path: str, payload: dict) -> dict:
    token = await get_piste_token()

    async with httpx.AsyncClient(timeout=40.0) as client:
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
        raise HTTPException(status_code=resp.status_code, detail=f"Erreur PISTE : {resp.text}")

    return resp.json()


async def safe_piste_call(path: str, payload: dict) -> dict:
    try:
        return await piste_call(path, payload)
    except HTTPException as exc:
        return {"error": True, "status_code": exc.status_code, "detail": exc.detail}
    except Exception as exc:
        return {"error": True, "status_code": 500, "detail": str(exc)}


# ----------------------------------------------------------------------------
# Modèles
# ----------------------------------------------------------------------------

class CodeSearchIn(BaseModel):
    query: str = Field(..., description="Mots-clés, notion ou numéro d'article.")
    code: str = Field(..., description="Nom du code : Code civil, Code du travail, CESEDA, CPC...")
    page_size: int = Field(10, ge=1, le=50)


class JurisprudenceSearchIn(BaseModel):
    query: str = Field(..., description="Mots-clés juridiques.")
    date_from: str | None = Field(None, description="Date min YYYY-MM-DD")
    date_to: str | None = Field(None, description="Date max YYYY-MM-DD")
    juridiction: str | None = Field(None, description="Juridiction si connue.")
    page_size: int = Field(10, ge=1, le=50)


class JorfSearchIn(BaseModel):
    query: str
    date_from: str | None = None
    date_to: str | None = None
    page_size: int = Field(10, ge=1, le=50)


class ArticleGetIn(BaseModel):
    article_id: str


class DecisionGetIn(BaseModel):
    decision_id: str
    fonds: str = Field("JURI", description="JURI, JURICA, CETAT, CONSTIT, CNIL, JUFI")


class GlobalSearchIn(BaseModel):
    query: str
    code: str | None = None
    include_code: bool = True
    include_judicial: bool = True
    include_admin: bool = True
    include_jorf: bool = False
    date_from: str | None = None
    date_to: str | None = None
    page_size: int = Field(5, ge=1, le=20)


class LegalResearchIn(BaseModel):
    question: str
    domain: str | None = None
    page_size: int = Field(5, ge=1, le=20)


class MedicalLossChanceIn(BaseModel):
    date_from: str | None = Field("1960-01-01")
    date_to: str | None = None
    page_size: int = Field(10, ge=1, le=20)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _search_payload(fond: str, query: str, page_size: int, filters: list[dict] | None = None) -> dict:
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
    return [{
        "facette": facette,
        "dates": {
            "start": date_from or "1900-01-01",
            "end": date_to or "2099-12-31",
        },
    }]


def _infer_code(domain: str | None, question: str) -> str:
    text = f"{domain or ''} {question}".lower()

    if any(x in text for x in ["étranger", "etranger", "oqtf", "titre de séjour", "sejour", "ceseda", "préfecture", "prefecture"]):
        return "Code de l'entrée et du séjour des étrangers et du droit d'asile"
    if any(x in text for x in ["travail", "salarié", "salarie", "licenciement", "prud'homme", "prudhomme", "faute grave"]):
        return "Code du travail"
    if any(x in text for x in ["pénal", "penal", "infraction", "garde à vue", "correctionnel"]):
        return "Code pénal"
    if any(x in text for x in ["procédure civile", "procedure civile", "assignation", "référé", "refere", "cpc"]):
        return "Code de procédure civile"
    if any(x in text for x in ["administratif", "conseil d'état", "conseil d'etat", "tribunal administratif", "caa"]):
        return "Code de justice administrative"
    if any(x in text for x in ["commerce", "société", "societe", "bail commercial"]):
        return "Code de commerce"
    if any(x in text for x in ["santé", "sante", "médical", "medical", "patient", "hôpital", "hopital"]):
        return "Code de la santé publique"

    return "Code civil"


def _extract_ids(obj: Any) -> list[str]:
    found: list[str] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                if k.lower() in {"id", "textid", "cid"} and isinstance(v, str):
                    if re.match(r"^(JURITEXT|CETATEXT|LEGIARTI|LEGITEXT|JURICA)", v):
                        found.append(v)
                walk(v)
        elif isinstance(x, list):
            for item in x:
                walk(item)

    walk(obj)
    return list(dict.fromkeys(found))


# ----------------------------------------------------------------------------
# Endpoints publics
# ----------------------------------------------------------------------------

@app.get("/")
async def root() -> dict:
    return {
        "ok": True,
        "message": "Proxy Légifrance Cabinet DARMON actif.",
        "version": "10.0.0",
        "docs": "/docs",
    }


@app.get("/privacy", response_class=HTMLResponse)
async def privacy() -> str:
    return """
    <html>
      <head><title>Privacy Policy</title><meta charset="utf-8" /></head>
      <body>
        <h1>Privacy Policy</h1>
        <p>This service is used only to query the official French Légifrance API through PISTE.</p>
        <p>No personal data is stored, sold or shared by this proxy.</p>
        <p>Requests are transmitted to Légifrance only for legal research purposes.</p>
      </body>
    </html>
    """


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "env": PISTE_ENV, "version": "10.0.0", "piste_base": PISTE_BASE}


# ----------------------------------------------------------------------------
# Endpoints de base
# ----------------------------------------------------------------------------

@app.post("/search/code", dependencies=[Depends(check_api_key)])
async def search_code(body: CodeSearchIn) -> dict:
    payload = _search_payload(
        "CODE_DATE",
        body.query,
        body.page_size,
        [
            {"facette": "NOM_CODE", "valeurs": [body.code]},
            {"facette": "DATE_VERSION", "singleDate": int(time.time() * 1000)},
        ],
    )
    return await piste_call("/search", payload)


@app.post("/search/jurisprudence", dependencies=[Depends(check_api_key)])
async def search_jurisprudence(body: JurisprudenceSearchIn) -> dict:
    filters = _date_filter("DATE_DECISION", body.date_from, body.date_to)
    if body.juridiction:
        filters.append({"facette": "JURIDICTION_JUDICIAIRE", "valeurs": [body.juridiction]})

    return await piste_call("/search", _search_payload("JURI", body.query, body.page_size, filters))


@app.post("/search/juriadmin", dependencies=[Depends(check_api_key)])
async def search_juriadmin(body: JurisprudenceSearchIn) -> dict:
    filters = _date_filter("DATE_DECISION", body.date_from, body.date_to)
    if body.juridiction:
        filters.append({"facette": "JURIDICTION_ADMIN", "valeurs": [body.juridiction]})

    return await piste_call("/search", _search_payload("CETAT", body.query, body.page_size, filters))


@app.post("/search/jorf", dependencies=[Depends(check_api_key)])
async def search_jorf(body: JorfSearchIn) -> dict:
    filters = _date_filter("DATE_PUBLICATION", body.date_from, body.date_to)
    return await piste_call("/search", _search_payload("JORF", body.query, body.page_size, filters))


@app.post("/article/get", dependencies=[Depends(check_api_key)])
async def article_get(body: ArticleGetIn) -> dict:
    return await piste_call("/consult/getArticle", {"id": body.article_id})


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
        raise HTTPException(status_code=400, detail="fonds invalide")

    payload = {"textId": body.decision_id} if fonds in ["JURI", "JURICA", "CETAT"] else {"id": body.decision_id}
    return await piste_call(endpoint, payload)


# ----------------------------------------------------------------------------
# Endpoints avancés
# ----------------------------------------------------------------------------

@app.post("/search/global", dependencies=[Depends(check_api_key)])
async def search_global(body: GlobalSearchIn) -> dict:
    output: dict[str, Any] = {"query": body.query, "env": PISTE_ENV, "results": {}}

    if body.include_code:
        code_name = body.code or _infer_code(None, body.query)
        output["results"]["code"] = {
            "code": code_name,
            "data": await safe_piste_call("/search", _search_payload(
                "CODE_DATE",
                body.query,
                body.page_size,
                [
                    {"facette": "NOM_CODE", "valeurs": [code_name]},
                    {"facette": "DATE_VERSION", "singleDate": int(time.time() * 1000)},
                ],
            )),
        }

    if body.include_judicial:
        output["results"]["jurisprudence_judiciaire"] = await safe_piste_call(
            "/search",
            _search_payload("JURI", body.query, body.page_size, _date_filter("DATE_DECISION", body.date_from, body.date_to)),
        )

    if body.include_admin:
        output["results"]["jurisprudence_administrative"] = await safe_piste_call(
            "/search",
            _search_payload("CETAT", body.query, body.page_size, _date_filter("DATE_DECISION", body.date_from, body.date_to)),
        )

    if body.include_jorf:
        output["results"]["jorf"] = await safe_piste_call(
            "/search",
            _search_payload("JORF", body.query, body.page_size, _date_filter("DATE_PUBLICATION", body.date_from, body.date_to)),
        )

    output["extracted_ids"] = _extract_ids(output)
    return output


@app.post("/research/legal", dependencies=[Depends(check_api_key)])
async def legal_research(body: LegalResearchIn) -> dict:
    code_name = _infer_code(body.domain, body.question)

    result = {
        "question": body.question,
        "domain": body.domain,
        "inferred_code": code_name,
        "env": PISTE_ENV,
        "results": {
            "code": await safe_piste_call("/search", _search_payload(
                "CODE_DATE",
                body.question,
                body.page_size,
                [
                    {"facette": "NOM_CODE", "valeurs": [code_name]},
                    {"facette": "DATE_VERSION", "singleDate": int(time.time() * 1000)},
                ],
            )),
            "jurisprudence_judiciaire": await safe_piste_call(
                "/search", _search_payload("JURI", body.question, body.page_size, [])
            ),
            "jurisprudence_administrative": await safe_piste_call(
                "/search", _search_payload("CETAT", body.question, body.page_size, [])
            ),
        },
    }

    result["extracted_ids"] = _extract_ids(result)
    return result


@app.post("/research/medical-loss-chance", dependencies=[Depends(check_api_key)])
async def medical_loss_chance(body: MedicalLossChanceIn) -> dict:
    """
    Recherche spécialisée sur la perte de chance en responsabilité médicale.
    Combine plusieurs requêtes utiles pour éviter une recherche trop étroite.
    """
    queries = [
        "perte de chance responsabilité médicale",
        "perte de chance médecin patient",
        "retard diagnostic perte de chance",
        "préjudice perte de chance faute médicale",
    ]

    results = {}
    for q in queries:
        results[q] = await safe_piste_call(
            "/search",
            _search_payload(
                "JURI",
                q,
                body.page_size,
                _date_filter("DATE_DECISION", body.date_from, body.date_to),
            ),
        )

    output = {
        "topic": "perte de chance en responsabilité médicale",
        "env": PISTE_ENV,
        "queries": queries,
        "results": results,
    }
    output["extracted_ids"] = _extract_ids(output)
    return output
