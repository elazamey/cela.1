"""إدارة إعدادات الوكيل بأمان عبر متغيرات البيئة.

Configuration management for Celia Repo Agent.
All secrets are loaded from environment variables (optionally from a `.env`
file) and validated before the agent starts.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# Placeholder values shipped inside `.env.example` / `.env` templates.
# They must never be accepted as real credentials.
_PLACEHOLDER_PREFIXES = (
    "ghp_your_github_personal_access_token_here",
    "AIzaSy_your_gemini_api_key_here",
    "your_github_username",
)


def _is_placeholder(value: str) -> bool:
    return any(value.startswith(p) for p in _PLACEHOLDER_PREFIXES)


class Config:
    """Central configuration container, populated from the environment."""

    GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GITHUB_USERNAME: str = os.getenv("GITHUB_USERNAME", "")
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    DRY_RUN: bool = os.getenv("DRY_RUN", "false").strip().lower() in (
        "true",
        "1",
        "yes",
        "y",
    )

    @classmethod
    def validate(cls) -> None:
        """Verify that required credentials are present and look real.

        Raises:
            ValueError: when a required secret is missing or still set to
                the placeholder value from the template.
        """
        missing = []
        if not cls.GITHUB_TOKEN or _is_placeholder(cls.GITHUB_TOKEN):
            missing.append("GITHUB_TOKEN")
        if not cls.GEMINI_API_KEY or _is_placeholder(cls.GEMINI_API_KEY):
            missing.append("GEMINI_API_KEY")

        if missing:
            raise ValueError(
                "Missing or placeholder credentials for: "
                f"{', '.join(missing)}. "
                "Copy `.env.example` to `.env` and fill in real values "
                "(or export the variables in your shell / CI secrets)."
            )


# Validate as soon as the configuration is imported, matching the agent's
# fail-fast design.
Config.validate()
