from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file = "../.env",
        env_ignore_empty=True,
        extra="ignore",
    )

    # LLM
    LLM_API_KEY:  str = ""
    LLM_BASE_URL: str = "https://api.groq.com/openai/v1"
    LLM_MODEL:    str = "llama-3.1-8b-instant"

    # Fallback LLM, used if the primary LLM is rate-limited (optional)
    LLM_FALLBACK_API_KEY:  str = ""
    LLM_FALLBACK_BASE_URL: str = "https://openrouter.ai/api/v1"
    LLM_FALLBACK_MODEL:    str = "meta-llama/llama-3.1-8b-instruct:free"

    # API config
    LIMIT: int = 3

settings = Settings()