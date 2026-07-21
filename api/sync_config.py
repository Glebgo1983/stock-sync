import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env.local")


def _parse_list(value):
  if not value:
    return set()
  return {item.strip() for item in value.split(",") if item.strip()}


def get_excluded_skus():
  return _parse_list(os.getenv("SYNC_EXCLUDED_SKUS"))


def get_excluded_mpfit_ids():
  return _parse_list(os.getenv("SYNC_EXCLUDED_MPFIT_IDS"))
