"""Configuration and secrets loading."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def get_gemini_api_key() -> str:
    """
    Get Gemini API key.
    Locally: from GEMINI_API_KEY env var.
    On AWS: can be loaded from Secrets Manager when AWS_SECRETS_ENABLED=true.
    """
    # TODO: When deploying to AWS, add:
    # if os.environ.get("AWS_SECRETS_ENABLED") == "true":
    #     import boto3
    #     client = boto3.client("secretsmanager")
    #     return client.get_secret_value(SecretId="gemini-api-key")["SecretString"]
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ValueError(
            "GEMINI_API_KEY is not set. Add it to .env or set the environment variable."
        )
    return key


def get_storage_mode() -> str:
    """Storage mode: 'local' or 's3'."""
    return os.environ.get("STORAGE_MODE", "local")


def get_upload_dir() -> Path:
    """Directory for uploaded files (local mode)."""
    path = Path(os.environ.get("UPLOAD_DIR", "uploads"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_output_dir() -> Path:
    """Directory for processed output files (local mode)."""
    path = Path(os.environ.get("OUTPUT_DIR", "outputs"))
    path.mkdir(parents=True, exist_ok=True)
    return path
