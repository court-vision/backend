"""
Dump the OpenAPI schema to stdout, offline.

    .venv/bin/python scripts/export_openapi.py > openapi.json

No server and no database: `main.py` does its DB work in the lifespan handler,
which never runs here — importing the app is enough to build the schema. The
env vars below only need to *exist* for settings to import; their values are
never used.

The frontend checks a copy of this output into `frontend/openapi/openapi.json`
and generates its API types from it (`bun run generate:api`). Regenerate the
snapshot from this script after any schema-visible backend change; the
frontend's scheduled openapi-drift workflow compares the live /openapi.json
against the snapshot and fails when they diverge.

Output is sorted and indented so diffs are stable.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost:5432/x")
os.environ.setdefault("CLERK_JWKS_URL", "https://fake.clerk.dev/.well-known/jwks.json")
os.environ.setdefault("CLERK_SECRET_KEY", "sk_test_fake")
os.environ.setdefault("PIPELINE_API_TOKEN", "export-only")

from main import app  # noqa: E402

json.dump(app.openapi(), sys.stdout, indent=2, sort_keys=True)
sys.stdout.write("\n")
