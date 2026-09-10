import os

# ------------------------------- Routes ------------------------------- #
ESPN_FANTASY_ENDPOINT = 'https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{}/segments/0/leagues/{}'
FEATURES_SERVER_ENDPOINT = os.getenv('FEATURES_SERVER_ENDPOINT', 'http://localhost:8080')


# ----------------------------- Authentication ------------------------------ #
SECRET_KEY = os.getenv('JWT_SECRET_KEY') or 'secret-key-here-change-in-production'
ALGORITHM = 'HS256'
ACCESS_TOKEN_EXPIRE_DAYS = 5
CRON_TOKEN = os.getenv('CRON_TOKEN')
VERIFICATION_EMAIL_EXPIRE_SECONDS = 300


# ----------------------------- Database Connection ----------------------------- #
DB_CREDENTIALS = {
	"user": os.getenv('DB_USER'),
	"password": os.getenv('DB_PASSWORD'),
	"host": os.getenv('DB_HOST'),
	"port": os.getenv('DB_PORT'),
	"database": os.getenv('DB_NAME')
}


# ----------------------------- League Information ----------------------------- #
LEAGUE_ID = os.getenv('DEV_LEAGUE_ID')

# ----------------------------- Lineup Generation ----------------------------- #
NUM_FREE_AGENTS = 100

# ----------------------------- Provider credentials ----------------------------- #
# ESPN has no OAuth "connection" to reconnect: its credentials are the espn_s2 and
# SWID cookies a user pastes, plus the season the team was saved under — ESPN
# answers 401/403 for a private league its account cannot see in that season. So
# name all three suspects instead of sending people to redo cookies that are
# usually fine. Yahoo genuinely is an OAuth connection, so its wording stands.
PROVIDER_AUTH_MESSAGES = {
	'espn': "ESPN rejected this league's credentials — check espn_s2, SWID and the season in Manage Teams",
	'yahoo': 'Your Yahoo connection expired — reconnect it in Manage Teams',
}

# Used when the provider refused a request we sent *no* credentials on. Public
# ESPN leagues need none and answer 200, so reaching here means the league is
# private and nothing was loaded for it — a different fault from a rejected
# credential, and pointing at the wrong one costs hours.
PROVIDER_AUTH_MISSING_MESSAGES = {
	'espn': 'This ESPN league is private and no credentials were sent — add espn_s2 and SWID in Manage Teams',
	'yahoo': 'No Yahoo credentials were sent — reconnect your Yahoo account in Manage Teams',
}
