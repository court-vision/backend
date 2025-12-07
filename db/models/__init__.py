# Import all models to ensure they are registered with the database
from .users import User
from .verifications import Verification
from .teams import Team
from .lineups import Lineup

__all__ = [
    'User',
    'Verification',
    'Team',
    'Lineup'
]
