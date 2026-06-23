# Proxy Légifrance pour ChatGPT Custom GPT

Reproduit, côté ChatGPT, l'accès à Légifrance que Claude obtient via OpenLegi —
mais avec **tes propres clés PISTE**, sans intermédiaire tiers.

## Architecture

```
   ┌──────────┐   Bearer statique    ┌──────────────┐   OAuth client_creds   ┌─────────────┐
   │  GPT     │─────────────────────▶│  Ce proxy    │───────────────────────▶│   PISTE/    │
   │  custom  │   (PROXY_API_KEY)    │  FastAPI     │   (Client ID/Secret)   │  Légifrance │
   └──────────┘                      └──────────────┘                        └─────────────┘
```

Le proxy fait trois choses :
1. Authentifie ChatGPT par un simple `Bearer <PROXY_API_KEY>`
2. Obtient et cache un `access_token` PISTE (durée ~1h, renouvelé auto)
3. Traduit des appels REST simples en payloads PISTE complexes

## Prérequis PISTE (DILA)

1. Créer un compte sur https://piste.gouv.fr
2. Créer une **application** dans ton espace
3. Souscrire à l'API **Légifrance** (Sandbox d'abord, Production ensuite après validation DILA)
4. Récupérer **Client ID** et **Client Secret** dans la section "OAuth"
5. Scope nécessaire : `openid`

## Déploiement sur Fly.io (le plus simple, gratuit jusqu'à ~3 GB/mois)

```bash
# 1. Installe flyctl
curl -L https://fly.io/install.sh | sh

# 2. Login
fly auth login

# 3. Depuis le dossier du projet, lance l'app (modifie d'abord le 'app =' dans fly.toml)
fly launch --no-deploy --copy-config

# 4. Configure les secrets (jamais en clair dans le code)
fly secrets set \
  PISTE_CLIENT_ID="ton_client_id" \
  PISTE_CLIENT_SECRET="ton_client_secret" \
  PROXY_API_KEY="$(openssl rand -hex 32)" \
  PISTE_ENV="sandbox"

# (Récupère et garde la PROXY_API_KEY générée :)
fly secrets list   # ne montre pas les valeurs, fais 'openssl rand -hex 32' à part et stocke-la

# 5. Déploie
fly deploy

# 6. Teste
curl https://ton-app.fly.dev/healthz
```

## Déploiement local pour tester

```bash
pip install -r requirements.txt

export PISTE_CLIENT_ID="..."
export PISTE_CLIENT_SECRET="..."
export PROXY_API_KEY="test-key-locale"
export PISTE_ENV="sandbox"

uvicorn app.main:app --reload --port 8080
```

Test :
```bash
curl -X POST http://localhost:8080/search/code \
  -H "Authorization: Bearer test-key-locale" \
  -H "Content-Type: application/json" \
  -d '{"query":"clause résolutoire","code":"Code civil","page_size":5}'
```

## Configuration du GPT custom dans ChatGPT

1. ChatGPT → **Explorer les GPT** → **Créer un GPT**
2. Onglet **Configure** → tout en bas, **Actions** → **Create new action**
3. **Authentication** :
   - Type : `API Key`
   - Auth Type : `Bearer`
   - API Key : colle ta `PROXY_API_KEY`
4. **Schema** : colle le contenu de `openapi.yaml` (remplace d'abord
   `REMPLACE-PAR-TON-DOMAINE.fly.dev` par ton vrai domaine Fly)
5. **Privacy policy** : URL bidon ou page statique
6. Dans les instructions du GPT, ajoute par exemple :
   > Tu es un assistant juridique français. Utilise les actions Legifrance
   > pour vérifier toute référence à un article de code ou à une décision.
   > Ne cite jamais une jurisprudence sans l'avoir confirmée via searchJurisprudence
   > ou searchJuriAdmin. Renvoie toujours l'ID Légifrance des décisions citées.

## Passage Sandbox → Production PISTE

Quand DILA a validé ton dossier de production :
```bash
fly secrets set PISTE_ENV="prod" \
                PISTE_CLIENT_ID="prod_client_id" \
                PISTE_CLIENT_SECRET="prod_client_secret"
```

## Sécurité

- `PROXY_API_KEY` n'est **pas** ton secret PISTE — c'est une clé que **toi seul** génères pour
  contrôler qui peut appeler ton proxy. Si tu la régénères, change-la aussi dans la config du GPT.
- Le Client Secret PISTE n'est **jamais** envoyé à ChatGPT ni à OpenAI : il reste sur ton serveur Fly.
- Limite l'IP source si Fly le permet, ou ajoute un rate-limit (slowapi) en évolution.

## Extensions possibles

- Ajouter `lister_codes_juridiques`, `rechercher_conventions_collectives`, décisions CNIL/Constit
  (même logique : un endpoint `/search/cc`, payload PISTE adapté)
- Logger les requêtes (fichier ou Sentry) pour audit cabinet
- Mettre un cache Redis sur les recherches fréquentes
- Exposer aussi à Claude via un serveur MCP custom (FastMCP) — tu réutilises 100% du code

## Limitations honnêtes

- Les payloads PISTE sont sensibles : si DILA modifie un nom de facette
  (`NOM_CODE`, `JURIDICTION_JUDICIAIRE`...), il faut ajuster. La doc Swagger officielle est sur :
  https://developer.aife.economie.gouv.fr/api-catalog → API Légifrance
- ChatGPT Actions tronque les très grosses réponses : pour les articles longs,
  considère un endpoint qui ne renvoie que les champs utiles (titre, numéro, texte, date version).
