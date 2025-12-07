from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL1: str
    DATABASE_URL2: str

    class Config:
        env_file = ".env"

settings = Settings(DATABASE_URL1="postgresql+asyncpg://postgres:xaxtyv-9zipbA-xosfur@db.urjytkrxhlammdtwplnv.supabase.co:5432/postgres", DATABASE_URL2="postgresql+asyncpg://postgres:rtVSWuhkROFxOpegJuElubldctbpXmBT@caboose.proxy.rlwy.net:33942/railway")