import os

ESPN_FANTASY_ENDPOINT = 'https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{}/segments/0/leagues/{}'
FEATURES_SERVER_ENDPOINT = 'http://localhost:8080'
SECRET_KEY = os.getenv('JWT_SECRET_KEY')
ALGORITHM = 'HS256'
ACCESS_TOKEN_EXPIRE_DAYS = 5