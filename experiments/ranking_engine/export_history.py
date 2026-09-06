"""Export only public basketball facts; no ORM import, migrations, or user data.

Run from backend: .venv/bin/python experiments/ranking_engine/export_history.py
The configured connection is forced read-only at connection and transaction level.
Credentials and connection details are never written to the export or logs.
"""
import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from dotenv import dotenv_values

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parents[1]


def main():
    url = dotenv_values(BACKEND / ".env").get("DATABASE_URL")
    if not url:
        raise SystemExit("No DATABASE_URL in backend/.env")
    try:
        conn = psycopg2.connect(
            url, connect_timeout=5, application_name="cv_ranking_research_readonly",
            options="-c default_transaction_read_only=on -c statement_timeout=20000",
        )
        conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.player_id, p.name, s.game_date,
                       s.pts,s.reb,s.ast,s.stl,s.blk,s.tov,
                       s.fgm,s.fga,s.fg3m,s.fg3a,s.ftm,s.fta,s.min
                FROM nba.player_game_stats s JOIN nba.players p ON p.id=s.player_id
                WHERE s.game_date >= '2025-10-01' AND s.game_date < '2026-05-01'
                ORDER BY s.player_id,s.game_date
            """)
            columns = [d.name for d in cur.description]
            rows = [[str(v) if hasattr(v, "isoformat") else v for v in row]
                    for row in cur.fetchall()]
        conn.rollback()
        conn.close()
    except Exception as exc:
        # Drivers may include credentials/hosts in error strings.
        raise SystemExit(f"Read-only export failed ({type(exc).__name__})") from None
    payload = json.dumps({"columns": columns, "rows": rows}, separators=(",", ":")).encode()
    (HERE / "data").mkdir(exist_ok=True)
    with gzip.GzipFile(str(HERE / "data/history.json.gz"), "wb", mtime=0) as f:
        f.write(payload)
    manifest = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source": "Configured database; nba.player_game_stats JOIN nba.players; read-only",
        "sha256_uncompressed": hashlib.sha256(payload).hexdigest(),
        "rows": len(rows), "players": len({r[0] for r in rows}),
        "min_date": min(r[2] for r in rows), "max_date": max(r[2] for r in rows),
        "warning": "Final corrected box scores, not a historical ingestion-time replay. No historical news, eligibility or preseason projection snapshots included.",
    }
    (HERE / "data/manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
