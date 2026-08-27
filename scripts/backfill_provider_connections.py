"""
Move plaintext provider credentials out of usr.teams.league_info.

    python scripts/backfill_provider_connections.py --dry-run
    python scripts/backfill_provider_connections.py

Idempotent: a team that already has a `provider_connection_id` is skipped, and
teams whose league_info holds no credentials are left alone. Safe to re-run, and
safe to run while the API is serving -- `credential_service.hydrate` reads from
whichever place a given team's credentials currently are, so teams migrate
independently.

Requires CREDENTIAL_KEYS to be set; without it there is nothing to encrypt with
and the script refuses rather than silently doing nothing.
"""

import argparse
import json
import sys

from core import crypto
from db.base import db
from services import credential_service


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report what would move, change nothing")
    args = parser.parse_args()

    if not crypto.is_enabled():
        print("CREDENTIAL_KEYS is not set — nothing to encrypt with. Refusing.", file=sys.stderr)
        return 1

    from db.models.teams import Team

    db.connect(reuse_if_open=True)
    try:
        teams = list(Team.select())
        migrated = skipped_linked = skipped_no_secrets = failed = 0

        for team in teams:
            if team.provider_connection_id:
                skipped_linked += 1
                continue
            try:
                payload = json.loads(team.league_info)
            except (TypeError, ValueError):
                print(f"  team {team.team_id}: league_info is not valid JSON — skipped", file=sys.stderr)
                failed += 1
                continue

            _, secrets = credential_service.split_secrets(payload)
            if not secrets:
                skipped_no_secrets += 1
                continue

            fields = ", ".join(sorted(secrets))
            if args.dry_run:
                print(f"  team {team.team_id} (user {team.user_id}): would move {fields}")
                migrated += 1
                continue

            try:
                connection_id = credential_service.persist(team.user_id, team, payload)
                print(f"  team {team.team_id}: moved {fields} -> connection {connection_id}")
                migrated += 1
            except Exception as exc:  # keep going; one bad row should not stall the rest
                print(f"  team {team.team_id}: FAILED ({type(exc).__name__}: {exc})", file=sys.stderr)
                failed += 1

        verb = "would migrate" if args.dry_run else "migrated"
        print(
            f"\n{len(teams)} teams: {verb} {migrated}, "
            f"{skipped_linked} already linked, {skipped_no_secrets} had no credentials, {failed} failed"
        )
        if not args.dry_run and migrated:
            print("\nVerify no plaintext remains:")
            print("  SELECT count(*) FROM usr.teams")
            print("  WHERE league_info::jsonb ?| array['espn_s2','swid','yahoo_access_token','yahoo_refresh_token'];")
        return 1 if failed else 0
    finally:
        if not db.is_closed():
            db.close()


if __name__ == "__main__":
    raise SystemExit(main())
