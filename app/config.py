from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DISCORD_TOKEN: str
    DISCORD_CLIENT_ID: str = ""
    DISCORD_CLIENT_SECRET: str = ""
    DISCORD_REDIRECT_URI: str = "http://localhost:8000/auth/callback"
    SESSION_SECRET: str = "dev-secret-key"
    DATABASE_URL: str
    TIMEZONE: str = "Europe/Paris"

    class Config:
        env_file = ".env"

settings = Settings()
