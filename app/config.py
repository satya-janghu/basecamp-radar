from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str
    PASSWORD: str
    SECRET_KEY: str
    GEMINI_API_KEY: str = ""
    BASECAMP_CLIENT_ID: str = ""
    BASECAMP_CLIENT_SECRET: str = ""
    BASECAMP_USER_AGENT: str = "Basecamp Radar (satya@getsatya.com)"
    RADAR_CORE_TEAM: str = "Satya"
    RADAR_TIMEZONE: str = "America/Los_Angeles"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
