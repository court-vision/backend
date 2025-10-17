import os

# ------------------------------- Routes ------------------------------- #
ESPN_FANTASY_ENDPOINT = 'https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{}/segments/0/leagues/{}'
FEATURES_SERVER_ENDPOINT = 'https://cv-features-443549036710.us-central1.run.app'
SELF_ENDPOINT = 'https://cv-backend-production.up.railway.app'
FRONTEND_API_ENDPOINT = 'https://www.courtvision.dev/api'
LOCAL_API_ENDPOINT = 'http://localhost:3000/api'
LOCAL_FEATURES_ENDPOINT = 'http://localhost:8080'


# ----------------------------- Authentication ------------------------------ #
SECRET_KEY = os.getenv('JWT_SECRET_KEY')
ALGORITHM = 'HS256'
ACCESS_TOKEN_EXPIRE_DAYS = 5
CRON_TOKEN = os.getenv('CRON_TOKEN')


# ----------------------------- Database Connection ----------------------------- #
DB_CREDENTIALS = {
	"user": os.getenv('DB_USER'),
	"password": os.getenv('DB_PASSWORD'),
	"host": os.getenv('DB_HOST'),
	"port": os.getenv('DB_PORT'),
	"database": os.getenv('DB_NAME')
}


# ----------------------------- Networking ----------------------------- #
PROXY_USERNAME = os.getenv('PROXY_USERNAME')
PROXY_PASSWORD = os.getenv('PROXY_PASSWORD')
PROXY_HOST = os.getenv('PROXY_HOST')
PROXY_PORT = os.getenv('PROXY_PORT')
PROXY_STRING = f"{PROXY_USERNAME}:{PROXY_PASSWORD}@{PROXY_HOST}:{PROXY_PORT}"
# PROXIES = {
# 	"http": f"http://brd.superproxy.io:22225?auth={PROXY_TOKEN}",
# 	"https": f"http://brd.superproxy.io:22225?auth={PROXY_TOKEN}"
# }

# ----------------------------- League Information ----------------------------- #
LEAGUE_ID = os.getenv('DEV_LEAGUE_ID')
