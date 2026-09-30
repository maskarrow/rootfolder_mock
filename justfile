set windows-shell := ["powershell.exe", "-NoLogo", "-Command"]

# Postgres, dependencies, migrations. `--wait` returns once Postgres is healthy, so
# the migrations do not race its first start.
up:
    docker compose up -d --wait
    cd backend; uv sync
    cd frontend; npm install
    cd backend; uv run alembic upgrade head

# The app writes its own JSON request lines, hence `--no-access-log`.
api:
    cd backend; uv run uvicorn app.main:app --reload --reload-dir app --port 8010 --no-access-log

web:
    cd frontend; npm run dev

# Needs Postgres running (`just up`): tests use their own `<db>_test` database,
# recreated on every run. No network calls; Anthropic and Resend are faked.
test:
    cd backend; uv run pytest

lint:
    cd backend; uv run ruff check; uv run ruff format --check
    cd frontend; npm run lint; npx tsc --noEmit

# Two orgs with 50 items each. Idempotent.
seed:
    cd backend; uv run python seed.py

# The password is asked in the terminal, never passed as an argument, so it does
# not stay in the shell history. Add `--admin` for an org admin.
create-user email name org *flags:
    cd backend; uv run python create_user.py "{{email}}" "{{name}}" --org "{{org}}" {{flags}}

# The org's Anthropic key, asked in the terminal and stored encrypted.
set-org-key org:
    cd backend; uv run python set_org_key.py --org "{{org}}" --provider anthropic

# The periodic cleanup, by hand: expired sessions and old audit rows.
cleanup:
    cd backend; uv run python cleanup.py
