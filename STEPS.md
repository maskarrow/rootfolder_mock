# Pașii de deploy, în ordine

Lista de lucru a lui Alex, care se ocupă singur de DevOps: de la „merge pe
laptop” până la „clientul folosește prod-ul”, fără cumpărarea domeniului și a
emailurilor. Pentru fiecare pas: ce faci, în ce fișiere, cum verifici. Colegii
fac development și își gestionează singuri branch-urile. Tu folosești doar două
lucruri de la ei: `main`, pe care îl pui pe dev când vrei, și tag-urile, pe care
le pui pe prod (PLAN.md §5.8).

Pașii din etapa 3 modifică codul aplicației. Îi faci tu sau îi ceri colegilor,
dar trebuie să existe înainte de pasul indicat la fiecare.

Deciziile, motivele lor și starea la zi sunt în [PLAN.md](PLAN.md). Codul și
fișierele de configurare le scrieți voi; Claude poate verifica ce ai făcut.

**Repetiția cu mock-ul.** `delegate-mock` (PLAN.md §5.17) are forma tehnică a lui
Delegate și are deja tot ce cere etapa 3. Fiecare fișier de infrastructură îl
scrii și îl verifici întâi în repo-ul mock-ului, apoi îl copiezi în Delegate.
Primul deploy pe dev și primul pe prod sunt tot cu mock-ul. Rândurile marcate
**Mock:** spun ce e diferit la el. Testul lui de fum e tabelul „What each part
proves in a deploy” din README-ul lui.

Pașii folosesc aceste valori implicite, încă neconfirmate (PLAN.md §3). Dacă schimbi ceva,
se schimbă doar pașii legați de ele:
- **servere:** CX33 + Volume de 100 GB pentru prod, CX23 pentru dev; CPX32 și
  CPX22 dacă CX nu e în stoc;
- **backup:** copie completă a bazei în fiecare noapte și PDF-urile noi, pe
  Backblaze B2;
- **monitorizare:** UptimeRobot, Sentry în regiunea UE și Beszel, cu alerte pe
  email;
- **limita de upload:** 500 MB pentru PDF-urile de licitație; pentru chat și
  workspace rămâne de decis.

Valori folosite mai jos, pe care le poți schimba, dar la fel peste tot:
- uid-ul utilizatorului din containerul `api`: **10001**;
- subrețeaua rețelei interne Docker: **172.30.0.0/24**;
- folderul de pe servere: **/opt/delegate**; PDF-urile pe prod:
  **/mnt/storage/pdfs**, montat în container la **/data/storage**;
- versiunea unei imagini: **`sha-<commit>`**, cu commit-ul scurt.

Structura propusă pentru fișierele de infrastructură, aceeași în repo-ul
mock-ului:

```
backend/Dockerfile, backend/.dockerignore
frontend/Dockerfile, frontend/.dockerignore
deploy/
  compose.yaml            stack-ul comun: db, api, web, nginx, certbot
  compose.prod.yaml       ce diferă pe prod: Volume-ul pentru PDF-uri, limite de RAM
  .env.example            doar numele variabilelor, fără valori
  nginx/                  bootstrap.conf (doar portul 80) și app.conf
  scripts/deploy.sh       deploy și revenire
  scripts/backup.sh       backup-ul de noapte
  scripts/restore-test.sh testul lunar de restaurare
  cloud-init.yaml         configurarea inițială a serverelor
.github/workflows/        ci.yml, deploy-dev.yml, deploy-prod.yml
.github/dependabot.yml
```

`docker-compose.yml` de la rădăcină rămâne pentru dezvoltarea locală.

---

## Etapa 1. Pregătire

### 0. Laptopul
- **Windows:** WSL 2, dintr-un PowerShell deschis ca administrator:
  `wsl --install`, apoi restart. Fără el, Docker Desktop pornește, dar motorul
  lui nu.
- **Docker Desktop**, cu motorul WSL 2, și **Git for Windows**, care aduce Git
  Bash, `ssh` și `openssl`.
- **Pentru mock, local:** uv, just și Volta (README-ul mock-ului).
- **Seiful** (PLAN.md §3, punctul 6) îl alegi acum: din pasul 17 încolo, codurile
  de recuperare, cheile și valorile din `.env` ajung acolo.
- **Verificare:** `docker run --rm hello-world` merge.

### 1. Planul în repo
- `STEPS.md` și `PLAN.md` în `docs/deploy/`, pe un branch, apoi merge în `main`.
  Până atunci nu sunt salvate nicăieri.
- În același PR: în `docs/DEVELOPMENT.md` scoți afirmația că `main` e protejat
  (PLAN.md §5.9).
- **Mock:** repo-ul `delegate-mock` urcat pe GitHub, privat, sub același cont.

---

## Etapa 2. Containerele, pe laptop (fără bani)

Pașii 2-7 îi faci întâi în repo-ul mock-ului. Când trec acolo, copiezi fișierele
în Delegate, schimbi ce e marcat **Mock:** și repeți verificările.

### 2. Imaginea backend-ului
- **Unde:** `backend/Dockerfile` și `backend/.dockerignore`.
  - **Build în două etape, cu aceeași imagine de bază în amândouă:**
    `python:3.14-slim`, fixată pe digest.
    - Prima etapă: `uv` copiat din imaginea lui oficială (fixată și ea), apoi
      `uv sync --frozen --no-dev` cu `UV_PYTHON_DOWNLOADS=never` și
      `UV_LINK_MODE=copy`. Mediul virtual trebuie să folosească Python-ul
      imaginii, altfel nu mai pornește în a doua etapă.
    - A doua etapă: doar `.venv` și codul din `backend/` (`app/`, `alembic/`,
      `alembic.ini`, scripturile), în `/app`. `PATH` începe cu `/app/.venv/bin`,
      deci `python`, `alembic` și `uvicorn` merg fără uv.
  - **Rulează ca utilizator fără root, cu uid 10001.** Același uid deține folderul
    de PDF-uri de pe server (pasul 21).
  - **`ARG APP_VERSION`, apoi `ENV APP_VERSION`.** Versiunea intră în imagine la
    build. `APP_ENV` nu intră: vine din `.env`, ca aceeași imagine să meargă pe
    dev și pe prod.
  - **`.dockerignore`:** `.venv`, `__pycache__`, `.pytest_cache`, `.ruff_cache`,
    `storage/`, `tests/` și orice `.env`.
  - **Comanda de pornire**, fără `--reload`:

    ```
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=172.30.0.0/24 --timeout-graceful-shutdown 30
    ```

    Cu `--proxy-headers`, backend-ul vede IP-ul real al utilizatorului, iar limita
    de login-uri greșite pe IP nu blochează pe toată lumea. Subrețeaua e cea fixă
    de la pasul 4. După pasul 12 adaugi `--no-access-log`, altfel fiecare cerere
    apare de două ori în log.
  - **Healthcheck fără `curl`**, pe care imaginea `slim` nu-l are:
    `python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/live', timeout=3)"`.
    La Delegate, `/health` până la pasul 11.
  - **Aceeași imagine rulează și scripturile**, cu `python`, din `/app`:
    `alembic upgrade head`, `create_superadmin.py`, `set_password.py`,
    `cleanup.py`, `ingest_foundation.py`.
- **Mock:** scripturile lui sunt `create_user.py`, `set_org_key.py`, `seed.py` și
  `cleanup.py`; are deja `/health/live` și logurile JSON, deci `--no-access-log`
  intră de la început.
- **Verificare:**
  - `docker build --build-arg APP_VERSION=test -t api:test backend` trece;
  - imaginea are sub 1 GB;
  - `docker run --rm api:test id -u` nu răspunde `0`;
  - `docker run --rm -e DATABASE_URL=postgresql+psycopg://x:x@localhost/x api:test python -c "import app.main"`
    trece. Pornirea cu baza vine la pasul 6.

### 3. Imaginea frontend-ului
- **Unde:** în `frontend/next.config.ts`, `output: "standalone"`; apoi
  `frontend/Dockerfile` și `frontend/.dockerignore`.
  - **Build pe `node:22`**, fixat pe digest, aceeași versiune ca în câmpul
    `volta` din `package.json` (22.23.1): `npm ci`, apoi `npm run build`.
  - **`ARG APP_VERSION`, apoi `ENV APP_VERSION`, înainte de build.** Pasul 14 îl
    folosește pentru ID-ul de build și pentru subsol.
  - **Build-ul are nevoie de internet:** `next/font/google` descarcă fonturile
    atunci. Containerul pornit nu mai are nevoie.
  - **Imaginea finală primește trei foldere**, pentru că `standalone` nu le copiază
    singur:
    - `.next/standalone/` în `/app`;
    - `.next/static/` în `/app/.next/static`;
    - `public/` în `/app/public`.

    Fără ultimele două, pagina se încarcă fără CSS și fără JavaScript.
  - **`ENV HOSTNAME=0.0.0.0` și `ENV PORT=3000`.** Docker pune în `HOSTNAME` id-ul
    containerului, iar serverul Next ascultă pe ce găsește acolo. Pornirea:
    `node server.js`.
  - **Rulează ca utilizatorul `node`** din imagine, nu ca root.
  - **Healthcheck fără `curl`:**
    `node -e "fetch('http://localhost:3000/login').then(r=>process.exit(r.ok?0:1),()=>process.exit(1))"`.
  - **`API_INTERNAL_URL` se citește la pornire.** Rewrite-ul `/api` din
    `next.config.ts` rămâne doar pentru dezvoltare: destinația lui se fixează la
    build, iar în containere `/api` îl rutează nginx.
- **Mock:** nu are folderul `public/`, deci lipsește copierea lui.
- **Verificare** (cu stack-ul de la pasul 6): aceeași imagine, pornită o dată cu
  `API_INTERNAL_URL=http://api:8000` și o dată cu o adresă greșită. În primul caz
  pagina principală se încarcă cu datele din backend; în al doilea arată eroarea
  de conexiune.

### 4. Fișierele compose
- **Unde:** `deploy/compose.yaml`.
  - **`name: delegate`** în fișier (la mock, `delegate-mock`), ca numele
    containerelor și ale volumelor să nu depindă de folder.
  - **Serviciile** `db` (`pgvector/pgvector:pg17`), `api`, `web`, `nginx` și
    `certbot`, pe o rețea internă cu subrețeaua fixă `172.30.0.0/24`.
    `--forwarded-allow-ips` de la pasul 2 trebuie să conțină adresa lui nginx,
    care altfel se schimbă la fiecare pornire. `certbot` stă într-un profil
    (`profiles: [certbot]`), ca să ruleze doar la cerere.
  - **Doar nginx publică porturi**, 80 și 443. Postgres nu publică niciunul.
  - **Imaginile `api` și `web` vin din variabile** (`image: ${API_IMAGE}`), pe care
    le setează `deploy.sh`.
  - **`api`:**
    - `env_file: .env`;
    - `STORAGE_DIR=/data/storage`, pe un volum. Calea trebuie să fie absolută: în
      container, rădăcina proiectului e `/`, deci `storage` ar însemna `/storage`,
      unde utilizatorul fără root nu poate scrie;
    - `stop_grace_period: 40s`. Docker oprește forțat după 10 secunde, iar
      `--timeout-graceful-shutdown 30` n-ar avea timp să lucreze;
    - `/tmp` rămâne pe discul containerului, nu în `tmpfs`. Un upload stă întreg
      acolo până îl copiază aplicația în storage, iar 500 MB în `tmpfs` ar intra în
      limita de RAM;
    - `depends_on` pe `db`, cu `condition: service_healthy`, și healthcheck-ul de
      la pasul 2.
  - **`web`:** `API_INTERNAL_URL=http://api:8000`, `depends_on` pe `api`.
  - **`db`:**
    - parola din `.env` și healthcheck cu `pg_isready`;
    - `shm_size: 1g`. Postgres în Docker are implicit 64 MB de memorie partajată,
      iar construirea în paralel a unui index HNSW poate cădea cu „No space left
      on device”.
  - **Toate:** `restart: unless-stopped`.
  - **Limite de memorie pentru dev** (PLAN.md §7): `api` 1 GB, `web` 512 MB, `db`
    1,5 GB, `nginx` 128 MB.
- **`deploy/compose.prod.yaml`:**
  - PDF-urile pe Volume: `/mnt/storage/pdfs` montat la `/data/storage`;
  - limitele pentru 8 GB: `api` 1,5 GB, `web` 768 MB, `db` 4 GB, `nginx` 256 MB.
- **`deploy/.env.example`** conține doar numele variabilelor, lista de la pasul 25.
- **Verificare:** nu dau erori nici
  `docker compose -f deploy/compose.yaml config`, nici
  `docker compose -f deploy/compose.yaml -f deploy/compose.prod.yaml config`.

### 5. nginx (PLAN.md §5.4)
- **Unde:** `deploy/nginx/`: `bootstrap.conf`, doar pe portul 80, pentru primul
  certificat (pasul 26), și `app.conf`, configurația completă.
  - **Rutare:** `location /api/ { proxy_pass http://api:8000/; }`. Bara de la
    finalul adresei taie prefixul `/api`. Restul merge la `http://web:3000`.
  - **Antetele spre `api` și `web`:** `Host $host`,
    `X-Forwarded-For $proxy_add_x_forwarded_for`, `X-Forwarded-Proto $scheme`,
    `X-Forwarded-Host $host` și `X-Request-ID $request_id`. Aplicația preia ID-ul,
    deci o cerere are același ID în logurile lui nginx și în ale aplicației. Pune
    `$request_id` și în `log_format`.
  - **Uploaduri:** `client_max_body_size 510m` și `proxy_request_buffering off`, pe
    rutele de upload. La Delegate toate se termină în `/documents`
    (`location ~ ^/api/.+/documents$`); la mock e `/api/files`.
  - **Streaming:** `proxy_http_version 1.1` și `proxy_read_timeout 300s`.
    Backend-ul trimite `X-Accel-Buffering: no`, deci nginx nu bufferizează
    răspunsul. Nu adăuga `text/event-stream` la `gzip_types`.
  - **HSTS** doar în `location /api/`: interfața îl primește deja de la Next.
  - **Limitare de cereri**, toate cu `limit_req_status 429`. Fără el, nginx
    răspunde 503:
    - una generală pe `/api/`, de exemplu 20 pe secundă per IP, cu rafală;
    - una strictă pe `/api/auth/login`, de exemplu 1 pe secundă, rafală 5;
    - una pe ruta de chat care generează răspunsuri,
      `/api/chats/<id>/messages/stream` (la mock, `/api/stream`).
  - `server_tokens off`.
  - **Portul 80:** servește `/.well-known/acme-challenge/` din folderul comun cu
    certbot și redirecționează restul spre HTTPS.
- **Certificatul de test pe laptop:** autosemnat pentru `localhost`, generat din
  Git Bash, **în afara repo-ului**:
  `MSYS_NO_PATHCONV=1 openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=localhost" -keyout test.key -out test.crt`.
  Fără `MSYS_NO_PATHCONV=1`, Git Bash transformă `/CN=...` într-o cale Windows și
  certificatul nu se creează.
- **Verificare**, pe laptop:
  - 20 de login-uri greșite pe secundă primesc 429 de la nginx. Răspunsul lui
    nginx e o pagină HTML; al aplicației, după 10 greșeli pe același email, e
    JSON;
  - un răspuns lung vine cuvânt cu cuvânt, nu tot la final. La mock, „Stream
    test” arată primul `delta` pe la 20 de secunde și `done` pe la 26;
  - **Mock:** un upload de 450 MB trece, iar unul de 501 MB primește mesajul
    aplicației, nu pagina lui nginx.

### 6. Tot stack-ul pe laptop
- **`deploy/.env`** pentru laptop, necomis: ca la pasul 25, cu
  `APP_URL=https://localhost`, `CORS_ORIGINS=https://localhost` și
  `APP_ENV=local`. La Delegate, până la pasul 10, și cheile Anthropic, OpenAI și
  Mistral.
- **Ce**, în ordinea asta:
  1. `docker compose -f deploy/compose.yaml up -d db`;
  2. migrațiile: `docker compose -f deploy/compose.yaml run --rm api alembic upgrade head`.
     Înainte de `api`: la pornire, backend-ul citește tabelele și, pe o bază
     goală, cade;
  3. `docker compose -f deploy/compose.yaml up -d`;
  4. superadminul, din PowerShell, pentru că parola se cere în terminal:
     `docker compose -f deploy/compose.yaml run --rm api python create_superadmin.py <email> "<Nume>"`;
  5. organizația „Delegate”, cu cheile voastre.
- **Mock:** la punctul 4,
  `python create_user.py <email> "<Nume>" --org Delegate --admin`, apoi
  `python seed.py`.
- **Verificare:** testul de fum, folosit apoi la fiecare deploy:
  1. login;
  2. upload de PDF;
  3. OCR terminat;
  4. o întrebare cu răspuns în streaming;
  5. export PDF sau DOCX;
  6. logout;
  7. după pasul 14, subsolul arată versiunea construită;
  8. în jurnalul de acces, login-ul are IP-ul tău, nu pe al lui nginx. Pe laptop,
     Docker Desktop arată adresa lui de gateway; pe server apare IP-ul tău real.
- **Mock:** testul de fum e tabelul din README-ul lui. „Who am I via /api” arată
  `scheme: https`.

### 7. Scriptul de deploy
- **Unde:** `deploy/scripts/deploy.sh <mediu> <digest-api> <digest-web>`, cu
  `set -euo pipefail` și `--dry-run`.
  - **Argumentele vin din `SSH_ORIGINAL_COMMAND`** când îl pornește CI-ul. Cheia lui
    `deploy` are o comandă forțată (pasul 19), așa că SSH ignoră comanda trimisă și
    o pune în variabila asta. Scriptul o verifică strict: mediul e `dev` sau
    `prod`, fiecare digest e `sha256:` urmat de 64 de caractere hexa. Orice altceva
    iese cu eroare.
  - **Pașii lui:**
    1. pe prod, se oprește dacă `/mnt/storage` nu e montat (`mountpoint -q`).
       Altfel PDF-urile ar ajunge pe discul serverului;
    2. salvează baza (`pg_dump -Fc` din containerul `db`): pe dev, ca să poți
       reveni; pe prod, înainte de migrare. Păstrează ultimele trei copii;
    3. descarcă imaginile după digest;
    4. rulează migrațiile o singură dată, cu imaginea nouă:
       `docker compose run --rm api alembic upgrade head`;
    5. pornește containerele noi cu `up -d`;
    6. așteaptă cel mult 2 minute ca `/health/ready` să răspundă prin nginx și
       verifică că `version` din răspuns e versiunea nouă:
       `curl -fsS --resolve rootfolder.app:443:127.0.0.1 https://rootfolder.app/api/health/ready`.
       Până la pasul 11, Delegate are doar `/health`, fără versiune;
    7. dacă nu răspunde, repornește versiunea anterioară. Baza rămâne cum e:
       migrările doar adaugă (PLAN.md §5.8);
    8. notează versiunea curentă și pe cea anterioară în `/opt/delegate/versions`;
    9. păstrează imaginile ultimelor două versiuni și le șterge pe restul.
- **Pe laptop**, o imagine are digest doar după ce e urcată într-un registry.
  Pornește unul local, `docker run -d -p 5000:5000 --name registry registry:2`,
  și urcă imaginile acolo. Verificarea de sănătate merge pe
  `https://localhost`, cu `curl -k`, din cauza certificatului autosemnat.
- **Verificare:**
  - un deploy cu o imagine stricată intenționat (de exemplu cu o comandă de
    pornire greșită) revine singur la cea anterioară;
  - **Mock:** cu `JOB_SECONDS=120`, un upload urmat imediat de un deploy lasă
    fișierul `interrupted`.

---

## Etapa 3. Schimbări în aplicație (înainte de pasul 24)

Mock-ul le are deja pe toate. Pentru pașii 11, 12 și 14, fișierele lui sunt
modelul de urmat.

### 8. Căi relative pentru fișiere
- **De ce:** azi `documents.stored_path` ține calea completă, de exemplu
  `C:\...\storage\<uuid>.pdf`. Pe server, calea e alta, iar referințele se
  strică.
- **Unde:**
  - `backend/app/storage.py` salvează doar `<uuid>.pdf` și are o funcție care
    construiește calea completă din `STORAGE_DIR`;
  - toate locurile care deschid sau șterg fișiere folosesc funcția:
    `services/ocr.py`, `services/cleanup.py`, `routers/documents.py`,
    `routers/tenders.py`, `routers/chats.py`, `routers/workspaces.py`;
  - o migrare Alembic păstrează doar numele fișierului în rândurile existente:
    tot ce e după ultimul `/` sau `\`, pentru că rândurile de azi au căi de
    Windows.
- **Verificare:**
  - testele trec;
  - după un upload, în bază apare doar `<uuid>.pdf`;
  - OCR-ul, reîncercarea și ștergerea merg.

### 9. Limita de 500 MB (PLAN.md §5.12)
- **Unde:**
  - `storage.save_upload` aplică limita și la licitații;
  - `storage.save_local_copy`, folosit de `ingest_foundation.py`, verifică și el
    mărimea;
  - utilizatorul primește un mesaj clar.
- **Verificare:** un fișier de 501 MB e refuzat cu mesajul, unul de 499 MB trece.

### 10. Cheile furnizorilor doar per organizație (PLAN.md §5.10)
- **Unde:**
  - `backend/app/config.py` pierde cheile Anthropic, OpenAI și Mistral;
  - orice apel la un furnizor ia cheia organizației pentru care lucrează:
    - agentul și generarea;
    - embedding-urile, la indexare și la căutare;
    - titlurile;
    - OCR-ul sincron și cel în lot, inclusiv verificarea periodică a joburilor;
    - `ingest_foundation.py`;
  - ecranul de chei cere Anthropic, OpenAI și Mistral (TODO #26).
- **Verificare:**
  - aplicația pornește cu un `.env` fără chei de furnizor;
  - o organizație fără chei primește un mesaj clar;
  - una cu chei merge cap-coadă.

### 11. Verificările de sănătate
- **Unde:** `backend/app/main.py`, după modelul din mock.
  - `/health/live` nu atinge baza;
  - `/health/ready` verifică baza și răspunde 503 când ea nu răspunde;
  - ambele întorc `APP_VERSION`, primit la build (pasul 2), și `APP_ENV`, citit din
    `.env`.
- După pas: healthcheck-ul din imagine trece pe `/health/live`, iar `deploy.sh`
  pe `/health/ready`, cu verificarea versiunii.
- **Verificare:**
  - `/health/live` răspunde și cu baza oprită;
  - `/health/ready` dă eroare când baza e oprită.

### 12. Loguri structurate
- **Unde:** configurația de logging de la pornire, după modelul din mock
  (`backend/app/logs.py`).
  - Fiecare linie e în format JSON: nivel, mesaj, ID de cerere, versiune, mediu.
    Liniile lui uvicorn la fel.
  - ID-ul de cerere vine din `X-Request-ID`, trimis de nginx (pasul 5), sau e
    generat, și se întoarce în același antet.
  - Niciun log nu conține text din documente sau din prompt-uri; ele stau deja în
    bază.
- După pas: `--no-access-log` în comanda de pornire (pasul 2).
- **Verificare:**
  - `docker compose logs api` arată linii JSON;
  - toate liniile aceleiași cereri au același ID, iar el apare și în logul lui
    nginx.

### 13. Emailul (TODO #28)
- **Contul Resend:** cu autentificare în doi pași și o cheie API care poate doar
  să trimită. Cheia în seif.
- **Unde:** `backend/app/services/emails.py` și rutele de invitație și resetare.
  Până ai domeniu, Resend trimite de pe adresa lui de test, doar către adresa
  contului tău Resend.
- **Verificare:** o invitație ajunge în inbox.

### 14. Versiunea în interfață
- **Unde:** după modelul din mock (`frontend/next.config.ts` și pagina principală).
  - `generateBuildId` întoarce `APP_VERSION`. Așa, un tab deschis înainte de
    deploy nu mai cere fișiere care nu mai există; probabil asta e cauza TODO #13.
  - Subsolul arată versiunea, dată prin `env` în `next.config.ts`, nu printr-o
    variabilă `NEXT_PUBLIC_*`.
- Versiunea e `sha-<commit>`, și pe prod: prod-ul refolosește imaginile de pe dev
  (PLAN.md §5.8).
- **Verificare:** subsolul arată versiunea construită.

---

## Etapa 4. CI (gratuit)

### 15. Verificările la fiecare PR și la fiecare push în `main`
- **Unde:** `.github/workflows/ci.yml`.
  - **Backend:**
    - uv instalat cu acțiunea lui oficială;
    - ruff, adăugat în grupul `dev` din `pyproject.toml`. La prima rulare pe
      Delegate va găsi multe; le repari o dată, într-un PR separat;
    - Postgres `pgvector/pgvector:pg17`, pornit ca serviciu în job, cu
      healthcheck;
    - `DATABASE_URL` în mediul job-ului: testele își fac singure baza `_test`;
    - pytest;
    - `alembic upgrade head` pe o bază goală, apoi `alembic check` pe aceeași
      bază. `alembic check` pică dacă un model s-a schimbat fără migrare, dar și
      dacă un index există doar în migrare, nu și în model (la mock s-a întâmplat
      exact asta).
  - **Frontend:** Node instalat cu `node-version-file: frontend/package.json`
    (acțiunea citește câmpul `volta`), apoi `npm ci`, lint, `tsc --noEmit` și
    build.
  - **Reguli:**
    - rulează doar partea schimbată, cu filtre pe căi;
    - o rulare nouă o oprește pe cea veche;
    - actions fixate pe commit exact;
    - drepturi minime pentru joburi.
- Fără protecția branch-urilor, verificările doar informează
  (PLAN.md §5.9).
- **Mock:** același workflow. Mock-ul are deja ruff și trece de `alembic check`.
- **Verificare:** un PR cu un test stricat arată verificarea roșie.

### 16. Actualizări și secrete scăpate
- **Unde:**
  - `.github/dependabot.yml` pentru uv, npm, Docker și GitHub Actions;
  - gitleaks în CI, gratuit, pentru parole și chei scăpate în cod.
- **Verificare:** în câteva zile apare primul PR de la Dependabot.

---

## Etapa 5. Serverele (primii bani: ~23-56 € pe lună, după arhitectură, PLAN.md §3 punctul 2)

Cumpără-le abia când pasul 6 merge pe laptop, altfel stau degeaba. Cu mock-ul,
pasul 6 poate merge înainte ca Delegate să termine etapa 3.

### 17. Contul Hetzner
1. Te înscrii pe [accounts.hetzner.com](https://accounts.hetzner.com/signUp)
   ca persoană fizică, cu datele tale reale și un card sau PayPal.
2. **Verificarea contului.** Pentru clienții noi, Hetzner cere uneori o poză a
   buletinului sau o plată în avans de 20 € prin PayPal, care rămâne credit pe
   facturi. Durează de obicei până la o zi; fă pasul ăsta din timp.
3. În contul Hetzner pornești autentificarea în doi pași; codurile de
   recuperare în seif.
4. În [console.hetzner.com](https://console.hetzner.com/): „+ New project”, cu
   numele `delegate`. Toate resursele de mai jos stau în el.

### 18. Cheile SSH
- **Cheia ta:** `ssh-keygen -t ed25519 -C "alex@laptop"`, cu parolă pe cheie.
  Cheia publică se urcă în proiectul Hetzner.
- **Cheia CI-ului:** o pereche separată, fără parolă, pentru că CI-ul n-are cum s-o
  tasteze. Cheia privată merge doar în secretele GitHub, iar cea publică doar la
  utilizatorul `deploy`.
- **Colegii** primesc acces doar dacă e nevoie, fiecare cu cheia lui.
- **Cheile private nu pleacă niciodată de pe laptop**, în afară de cea a CI-ului,
  care stă doar în GitHub.

### 19. Fișierul cloud-init (PLAN.md §5.15)
- **Prod-ul a fost creat fără cloud-init** (7 octombrie 2026). Pentru el faci
  aceleași lucruri de mână, cu procedura „Configurarea de mână a unui server
  nou” de la finalul fișierului. Cloud-init-ul îl scrii după aceleași comenzi,
  înainte de serverul de dev.
- **Unde:** `deploy/cloud-init.yaml`.
  - **Utilizatorii:**
    - al tău, cu `sudo`;
    - `deploy`, în grupul `docker`, cu cheia CI-ului legată de o singură comandă
      în `authorized_keys`:
      `command="/opt/delegate/deploy/scripts/deploy.sh",restrict ssh-ed25519 AAAA... ci`
      (PLAN.md §5.5). `restrict` oprește terminalul și redirecționările.
  - **SSH:** fără parolă și fără root.
  - **Docker** din repo-ul oficial Docker, cu pluginul `compose`.
  - **fail2ban** și **actualizări automate de securitate**, cu repornire automată
    la 04:30 când o cer (PLAN.md §3, punctul 8). Containerele revin singure, din
    `restart: unless-stopped`.
  - **Logurile Docker cu rotație**, în `/etc/docker/daemon.json`:
    `{"log-driver": "json-file", "log-opts": {"max-size": "10m", "max-file": "5"}}`.
  - **Un fișier swap de 2 GB.** Serverele vin fără swap, iar fără el un vârf de
    memorie oprește un container în loc să-l încetinească.
  - **Folderul `/opt/delegate`**, al lui `deploy`.

### 20. Firewall-ul Hetzner
- În proiect: „Firewalls” → „Create Firewall”, cu numele `web`.
- **Reguli de intrare (Inbound),** fiecare de la „Any IPv4” și „Any IPv6”:
  - ICMP;
  - TCP 22;
  - TCP 80;
  - TCP 443.
- **Reguli de ieșire (Outbound):** niciuna. Fără reguli de ieșire, Hetzner lasă
  tot traficul de ieșire, iar serverele trebuie să ajungă la furnizorii LLM, la
  GHCR, la Let's Encrypt și la actualizări.
- Îl atașezi la server chiar din formularul de creare (pasul 21).

### 21. Serverele
- **Înainte:** arhitectura e aleasă (PLAN.md §3, punctul 2). Pentru ARM,
  imaginile trec întâi construcția pentru `linux/arm64` (pașii 2-3). Cloud-init-ul
  de la pasul 19 e gata.
- **Se plătește pe oră,** cu plafonul lunar din listă. Un server creat greșit îl
  ștergi și îl refaci pe câțiva cenți. Doar IP-ul se pierde, dacă nu e păstrat
  ca IP primar (mai jos).
- În proiect: „Servers” → „Add server”. Faci formularul de două ori, întâi
  pentru prod, apoi pentru dev:

  | Câmp | Prod | Dev |
  |---|---|---|
  | Location | Falkenstein (sau Nuremberg), unde e stoc; aceeași pentru amândouă | la fel ca prod |
  | Image | Ubuntu 26.04 (LTS, suportat de Docker și pe ARM) | la fel |
  | Type, x86 | „Regular Performance”, CPX32 | „Cost-Optimized” CX23 dacă e în stoc, altfel CPX22 |
  | Type, ARM | „Cost-Optimized”, Arm64, CAX21 | CAX11 |
  | Networking | IPv4 și IPv6 | IPv4 și IPv6 |
  | SSH keys | cheia ta (pasul 18). După creare nu se mai poate adăuga din consolă | la fel |
  | Volumes | doar pe ARM: „Create Volume”, 100 GB, ext4, **fără** „Automount” | niciunul |
  | Firewalls | `web` | `web` |
  | Backups | oprit | oprit |
  | Placement groups, Private networks | niciunul | niciunul |
  | Labels | `env=prod` | `env=dev` |
  | Cloud config | conținutul lui `deploy/cloud-init.yaml` | la fel |
  | Name | `prod` | `dev` |

  Apoi „Create & Buy now”.
- **IP-ul primar al prod-ului:** în „Primary IPs”, la IPv4-ul prod-ului, oprești
  „Auto delete”. Așa rămâne al tău și dacă ștergi și refaci serverul, iar DNS-ul
  nu se schimbă.
- **Volume-ul**, pe ARM, sau mai târziu pe x86, când discul de 160 GB nu mai
  ajunge:
  - îl montezi la `/mnt/storage` prin `/etc/fstab`, după UUID (`sudo blkid`), cu
    `nofail`; fără montarea automată a lui Hetzner, care l-ar pune la
    `/mnt/HC_Volume_<id>`;
  - apoi `mkdir /mnt/storage/pdfs` și `chown 10001:10001 /mnt/storage/pdfs`:
    containerul `api` rulează cu uid-ul 10001 (pasul 2) și altfel nu poate scrie.
- **Notezi** IPv4 și IPv6 ale fiecărui server; le folosești la DNS (pasul 24).

### 22. Verificarea serverelor
- Te conectezi cu cheia ta.
- `ssh root@...` e refuzat, la fel login-ul cu parolă.
- Cu cheia CI-ului, `ssh deploy@...` nu dă un terminal: răspunde eroarea de
  argumente a lui `deploy.sh`.
- `docker run --rm hello-world` merge.
- `sudo fail2ban-client status` arată fail2ban pornit.
- `swapon --show` arată swap-ul.
- Pe prod, `findmnt /mnt/storage` arată Volume-ul, și după un restart.

### 23. Accesul serverelor la imagini
- Un token GitHub care poate doar să citească imagini (un classic PAT cu
  `read:packages`). Tokenul stă în seif.
- Pe fiecare server, ca utilizatorul `deploy`:
  `docker login ghcr.io -u <cont-github> --password-stdin`, cu tokenul pe
  intrarea standard.

---

## Etapa 6. Dev (cere domeniul)

### 24. DNS în Cloudflare
- Toate înregistrările ca „DNS only”, cu TTL scurt (5 minute) la început:
  - `dev` duce la IP-ul de dev;
  - apex-ul și `www` duc la prod;
  - un CAA pentru `letsencrypt.org`.
- **Verificare:** `nslookup dev.rootfolder.app` întoarce IP-ul de dev.

### 25. Fișierele pe server
- În `/opt/delegate/`: fișierele din `deploy/`, copiate cu `scp -r` din commit-ul
  pe care îl pui, plus `.env`.
- **`.env`**, cu valori noi pentru fiecare mediu:
  - `POSTGRES_PASSWORD`, lungă și generată, și
    `DATABASE_URL=postgresql+psycopg://delegate:<parola>@db:5432/delegate`;
  - `API_KEYS_ENCRYPTION_KEY`, alta decât pe celelalte medii;
  - `RESEND_API_KEY` și `EMAIL_FROM`;
  - `APP_URL=https://dev.rootfolder.app` și `CORS_ORIGINS=https://dev.rootfolder.app`. Fără
    originea publică în `CORS_ORIGINS`, orice POST, inclusiv login-ul, primește
    403;
  - `APP_ENV=dev`;
  - la Delegate, până la pasul 10, cheile furnizorilor.
- `SESSION_COOKIE_SECURE` rămâne `true` (implicit), iar `STORAGE_DIR` vine din
  compose.
- `.env` are `chmod 600` și proprietar `deploy`. Valorile stau și în seif.
- Când un release schimbă ceva în `deploy/`, actualizezi întâi fișierele de pe
  server (procedura de la pasul 28), apoi faci deploy-ul.

### 26. Primul certificat
- Domeniul e `.app`: browserele deschid doar HTTPS (PLAN.md §5.3). Până ai
  certificatul, portul 80 îl verifici cu `curl`, nu din browser.

1. Pornești nginx cu `bootstrap.conf`, doar pe portul 80.
2. Ceri certificatul:
   `docker compose run --rm certbot certonly --webroot -w /var/www/certbot -d dev.rootfolder.app`.
3. Pornești nginx cu `app.conf`.
- **Reînnoirea:** un timer systemd pe server rulează zilnic
  `docker compose run --rm certbot renew`, apoi
  `docker compose exec nginx nginx -s reload`. Dacă reînnoirea pică, timer-ul
  trimite un email prin Resend (PLAN.md §5.14).
- **Verificare:** `https://dev.rootfolder.app` se deschide fără avertisment.

### 27. Primul deploy pe dev
- **Imaginile** le construiește workflow-ul de la pasul 29. Scrie acum doar partea
  lui de build și rulează-l manual pe `main`; partea de SSH o adaugi la pasul 29.
  Digest-urile apar în sumarul rulării.
- **Întâi cu mock-ul:**
  1. `deploy.sh dev <digest-api> <digest-web>`, rulat de tine prin SSH;
  2. `create_user.py ... --admin`, `seed.py`, apoi `set_org_key.py` cu cheia de dev
     a lui Anthropic;
  3. testul de fum al mock-ului;
  4. îl oprești de tot cu `docker compose -p delegate-mock down -v`, care șterge
     doar volumele lui, și pui în `/opt/delegate` fișierele lui Delegate.
- **Apoi Delegate:** `deploy.sh`, superadminul, organizația „Delegate”, cu adminul
  ei și cheile. La fiecare furnizor, chei separate pentru dev, cu o limită lunară
  de cheltuieli (PLAN.md §5.10).
- **Verificare:** testul de fum de la pasul 6, cu IP-ul tău real în jurnalul de
  acces.

### 28. Procedurile
- Scrii în secțiunea „Proceduri” de la finalul acestui fișier comenzile exacte
  folosite la pașii 19-27: pornirea unui server nou, deploy, revenire,
  schimbarea unui secret, actualizarea fișierelor din `deploy/`.
- **Verificare:** oricine din echipă le poate urma fără să te întrebe.

---

## Etapa 7. Deploy din GitHub (PLAN.md §5.8)

### 29. Deploy pe dev, când vrei
- **Unde:** `.github/workflows/deploy-dev.yml`, pornit manual.
  - Are un câmp „ref”, implicit `main`.
  - **Build-ul** (scris deja la pasul 27):
    - pentru `linux/amd64`;
    - cu `APP_VERSION=sha-<commit>` ca argument de build;
    - cu cache-ul GitHub Actions, ca să nu consume minute;
    - doar dacă imaginile `sha-<commit>` nu există deja în GHCR.
  - **Drepturi:** `packages: write` pentru build, `contents: read`.
  - **Deploy-ul:** SSH ca `deploy`, cu cheia CI-ului din secrete, și trimite
    `dev <digest-api> <digest-web>`.
    - Cheia serverului vine dintr-un secret, `DEV_KNOWN_HOSTS`: linia lui din
      `~/.ssh/known_hosts`, de pe laptop, după primul tău login. Niciodată
      `StrictHostKeyChecking=no`.
  - **Un singur deploy odată pe mediu**, cu `concurrency`.
- **Mock:** același workflow, în repo-ul lui. Ciclul de la pasul 31 îl faci întâi
  cu el.

### 30. Deploy pe prod, dintr-un tag
- **Unde:** `.github/workflows/deploy-prod.yml`, pornit manual.
  - Are un câmp „tag”, de exemplu `v1.4.0`.
  - Găsește commit-ul tag-ului. Dacă imaginile `sha-<commit>` există, le adaugă
    și eticheta `v1.4.0` fără să le reconstruiască
    (`docker buildx imagetools create`); altfel le construiește.
  - Rulează `deploy.sh` pe prod, cu cheia serverului din `PROD_KNOWN_HOSTS`.
  - Creează un GitHub Release cu notele generate automat.
- **Versiunea afișată pe prod rămâne `sha-<commit>`**: imaginea e aceeași de pe
  dev. Release-ul leagă tag-ul de commit.
- **Regula:** pui tag-ul pe commit-ul pe care l-ai testat pe dev. Doar așa, pe
  prod ajunge exact ce ai testat.

### 31. Ciclul complet, o dată, de probă
- Partea de dev acum; punctele 2 și 5 după pasul 37, când prod-ul există.
1. `main` pe dev.
2. Tag-ul `v0.1.0` pe același commit, apoi deploy pe prod.
3. Un `main` stricat intenționat pe dev.
4. Dev readus la `v0.1.0`, cu workflow-ul de dev și ref-ul `v0.1.0`.
5. Prod-ul trecut pe un tag mai vechi și înapoi.
- **Tag-urile nu se șterg.**

---

## Etapa 8. Backup-urile (înainte de orice date reale pe prod)

### 32. Backblaze B2
- Cont cu autentificare în doi pași.
- Un bucket în regiunea UE, cu Object Lock pornit, ca nicio copie să nu poată
  fi ștearsă timp de câteva zile (de exemplu 14).
- O regulă de ciclu de viață care șterge copiile mai vechi de 30 de zile, după ce
  li se termină blocarea.
- O cheie de aplicație care poate scrie, dar nu poate șterge. Ea merge pe
  server; cheia completă stă doar în seif.

### 33. Backup-ul de noapte
- **Unde:** `deploy/scripts/backup.sh`, pornit la 03:00 de un timer systemd.
  - `pg_dump -Fc` al bazei, din containerul `db`, comprimat și criptat, trimis
    direct în B2, fără copie pe disc.
  - restic trimite PDF-urile din `/mnt/storage/pdfs`, tot în B2.
  - Dacă ceva pică, unitatea systemd `OnFailure=` trimite un email prin API-ul
    Resend.
- **Cheia de criptare a backup-ului** stă în seif, nu pe server lângă copie.
- **`API_KEYS_ENCRYPTION_KEY`** stă tot în seif, separat de ea.
- **Verificare:** backup-ul rulat de două ori la rând, cu cheia fără drept de
  ștergere, trece de ambele dăți. restic scrie și șterge fișiere de blocare, deci
  aici se vede dacă cheia îi ajunge.

### 34. Testul lunar de restaurare
- **Unde:** `deploy/scripts/restore-test.sh`, în afara orelor de lucru.
  1. Descarcă ultima copie.
  2. O restaurează într-un container temporar `pgvector/pgvector:pg17` pe prod.
     Aceeași imagine ca baza: restaurarea creează extensia `vector`.
  3. Numără rândurile din tabelele principale.
  4. Trimite rezultatul pe email.
  5. Șterge containerul.

### 35. Exercițiul complet
- O dată, înainte de lansare: server nou, aplicație pornită, date restaurate, cu
  cronometrul pornit.
- Timpul obținut îl treci în plan.

### 36. Copia de pe laptop, dacă o păstrați
- Săptămânal sau la fiecare client nou: ultima copie din B2, pe un laptop cu
  BitLocker, într-un folder nesincronizat, criptată.
- Păstrezi doar ultima copie.

---

## Etapa 9. Prod și primul client

### 37. Prod
- Ca pașii 24-27, cu:
  - secrete diferite de dev: altă parolă a bazei, altă
    `API_KEYS_ENCRYPTION_KEY`, alte chei la furnizori;
  - `APP_URL=https://rootfolder.app` și `CORS_ORIGINS=https://rootfolder.app`;
  - certificatul pentru apex și pentru `www`; nginx redirecționează `www` spre
    apex;
  - `compose.prod.yaml`, cu PDF-urile pe Volume.
- **Întâi cu mock-ul**, ca pe dev: testul lui de fum, cu un upload de 450 MB care
  apare în `/mnt/storage/pdfs`. Apoi `docker compose -p delegate-mock down -v` și
  ștergi PDF-urile lui din `/mnt/storage/pdfs`.
- Apoi Delegate, și punctele 2 și 5 de la pasul 31.

### 38. Monitorizarea
- **UptimeRobot**, cu alerte pe email, pe `https://rootfolder.app/api/health/ready` și
  `https://dev.rootfolder.app/api/health/ready`, cu cuvântul cheie `"ok"`. Verifică și
  nginx, și aplicația, și baza.
- **Sentry:** cheia de conectare în `.env`; în cod, integrarea fără corpul
  cererilor și fără text din documente.
- **Beszel:**
  - panoul pe dev, agenții pe ambele servere;
  - alerte pe email pentru RAM, disc și CPU.

### 39. Înainte de datele clientului
- Backup-urile rulează, iar un test de restaurare a trecut (etapa 8).
- Întrebarea despre firmă și acordul de prelucrare a datelor e lămurită (PLAN.md
  §3, punctul 7).
- Lista firmelor care prelucrează date:
  - Hetzner;
  - Anthropic, OpenAI, Mistral;
  - Resend, Zoho;
  - Backblaze;
  - Sentry, UptimeRobot;
  - Cloudflare, care doar răspunde la DNS și nu vede documentele.

### 40. Primul client
- Organizația lui, adminul lui invitat, cheile lui introduse de el.
- **Arhiva:**
  - o copiezi pe prod cu `rsync`, în `/mnt/storage/import/<client>/`;
  - o indexezi montând folderul în container, care altfel nu-l vede:
    `docker compose run --rm -v /mnt/storage/import:/import:ro api python ingest_foundation.py /import/<client> --org <id>`;
  - după indexare ștergi folderul de import: fișierele au fost copiate în storage.
- O indexare curată pe prod e mai sigură decât mutarea bazei de pe un laptop.

---

## După lansare, ca rutină

- **Actualizare de securitate Next.js sau Python:**
  1. PR-ul de la Dependabot;
  2. `main` pe dev;
  3. tag;
  4. prod.

  Scopul e să dureze zece minute.
- **Lunar:** costurile și rezultatul testului de restaurare.
- **Trimestrial:** exercițiul complet de restaurare.
- **Upgrade de server:** când Beszel arată RAM peste 75% o săptămână sau disc
  peste 80%.

---

## Proceduri

Se completează la pasul 28, cu comenzile exacte, copiabile:
- pornirea unui server nou;
- deploy manual;
- revenirea la versiunea anterioară;
- schimbarea unui secret;
- actualizarea fișierelor din `deploy/` pe server;
- reînnoirea manuală a certificatului;
- restaurarea din backup.

### Configurarea de mână a unui server nou

Pentru un server creat fără cloud-init. Comenzile rulează ca `root`, pe Ubuntu
26.04, în ordinea de mai jos. **Nu închide sesiunea de root până nu ai intrat
cu utilizatorul tău dintr-un al doilea terminal** (punctul 4).

1. **Intri pe server**, de pe laptop:
   ```bash
   ssh root@<IP>
   ```
2. **Actualizezi sistemul:**
   ```bash
   apt update && apt -y full-upgrade
   ```
3. **Utilizatorul tău**, cu cheia de la root și cu `sudo`:
   ```bash
   adduser --gecos "" alex
   usermod -aG sudo alex
   install -d -m 700 -o alex -g alex /home/alex/.ssh
   install -m 600 -o alex -g alex /root/.ssh/authorized_keys /home/alex/.ssh/authorized_keys
   ```
   Parola cerută de `adduser` e doar pentru `sudo`, nu pentru SSH. O pui în seif.
4. **Testezi**, dintr-un al doilea terminal pe laptop: `ssh alex@<IP>`, apoi
   `sudo whoami`, care trebuie să răspundă `root`.
5. **SSH fără parolă și fără root.** Fișierul începe cu `00-` ca să aibă
   prioritate față de setările puse de Hetzner:
   ```bash
   printf '%s\n' 'PermitRootLogin no' 'PasswordAuthentication no' 'KbdInteractiveAuthentication no' 'PubkeyAuthentication yes' > /etc/ssh/sshd_config.d/00-hardening.conf
   sshd -t && systemctl restart ssh
   ```
   `sshd -t` nu afișează nimic dacă fișierul e corect. Din al doilea terminal,
   `ssh root@<IP>` trebuie să fie refuzat, iar `ssh alex@<IP>` să meargă.
6. **Swap de 2 GB:**
   ```bash
   fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
   echo '/swapfile none swap sw 0 0' >> /etc/fstab
   ```
7. **Docker din repo-ul oficial** (docs.docker.com, Ubuntu):
   ```bash
   apt install -y ca-certificates curl
   install -m 0755 -d /etc/apt/keyrings
   curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
   chmod a+r /etc/apt/keyrings/docker.asc
   printf '%s\n' 'Types: deb' 'URIs: https://download.docker.com/linux/ubuntu' "Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")" 'Components: stable' "Architectures: $(dpkg --print-architecture)" 'Signed-By: /etc/apt/keyrings/docker.asc' > /etc/apt/sources.list.d/docker.sources
   apt update && apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
   ```
8. **Logurile Docker cu rotație:**
   ```bash
   echo '{"log-driver": "json-file", "log-opts": {"max-size": "10m", "max-file": "5"}}' > /etc/docker/daemon.json
   systemctl restart docker
   ```
9. **fail2ban** pentru SSH:
   ```bash
   apt install -y fail2ban
   printf '%s\n' '[sshd]' 'enabled = true' 'backend = systemd' > /etc/fail2ban/jail.d/sshd.local
   systemctl enable --now fail2ban && systemctl restart fail2ban
   ```
10. **Actualizările automate de securitate.** Pe Ubuntu sunt pornite implicit;
    verifici cu `systemctl status unattended-upgrades`. Repornirea automată la
    04:30 (PLAN.md §3, punctul 8), dacă o aprobați:
    ```bash
    printf '%s\n' 'Unattended-Upgrade::Automatic-Reboot "true";' 'Unattended-Upgrade::Automatic-Reboot-Time "04:30";' > /etc/apt/apt.conf.d/52unattended-upgrades-local
    ```
11. **Utilizatorul `deploy` și folderul aplicației:**
    ```bash
    adduser --disabled-password --gecos "" deploy
    usermod -aG docker deploy
    install -d -o deploy -g deploy /opt/delegate
    ```
    Cheia CI-ului, legată de `deploy.sh`, o adaugi când există scriptul
    (pasul 7), în `/home/deploy/.ssh/authorized_keys`.
12. **Verificarea:** lista de la pasul 22, plus `docker compose version`.

Firewall-ul rămâne cel de la Hetzner (pasul 20), nu UFW pe server: porturile
publicate de Docker ocolesc UFW, dar nu pot ocoli un firewall care stă în fața
serverului.

Ora serverului rămâne UTC, ca toate logurile să fie în aceeași oră.
