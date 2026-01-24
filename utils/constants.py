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


# ----------------------------- Networking ----------------------------- #
SCRAPER_API_KEY = os.getenv('SCRAPER_API_KEY')
# PROXY_USERNAME = os.getenv('PROXY_USERNAME')
# PROXY_PASSWORD = os.getenv('PROXY_PASSWORD')
# PROXY_HOST = os.getenv('PROXY_HOST')
# PROXY_PORT = os.getenv('PROXY_PORT')
# PROXY_STRING = f"{PROXY_USERNAME}:{PROXY_PASSWORD}@{PROXY_HOST}:{PROXY_PORT}"
# PROXIES = {
# 	"http": f"http://brd.superproxy.io:22225?auth={PROXY_TOKEN}",
# 	"https": f"http://brd.superproxy.io:22225?auth={PROXY_TOKEN}"
# }

# ----------------------------- League Information ----------------------------- #
LEAGUE_ID = os.getenv('DEV_LEAGUE_ID')

# ----------------------------- Lineup Generation ----------------------------- #
NUM_FREE_AGENTS = 100
