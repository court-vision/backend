import os

ESPN_FANTASY_ENDPOINT = 'https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{}/segments/0/leagues/{}'
FEATURES_SERVER_ENDPOINT = 'https://cv-features-443549036710.us-central1.run.app'
SELF_ENDPOINT = 'https://cv-backend-443549036710.us-central1.run.app'
SECRET_KEY = os.getenv('JWT_SECRET_KEY')
ALGORITHM = 'HS256'
ACCESS_TOKEN_EXPIRE_DAYS = 5

DB_CREDENTIALS = {
	"user": os.getenv('DB_USER'),
	"password": os.getenv('DB_PASSWORD'),
	"host": os.getenv('DB_HOST'),
	"port": os.getenv('DB_PORT'),
	"database": os.getenv('DB_NAME')
}
CRON_TOKEN = os.getenv('CRON_TOKEN')