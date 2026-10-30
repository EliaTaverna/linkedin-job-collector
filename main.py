import json
import logging
import os
import time
from datetime import date
from functools import lru_cache
from pathlib import Path

import gspread
import yaml
from bs4 import BeautifulSoup
from jobspy.linkedin import LinkedIn
from jobspy.linkedin.constant import empty_page, headers, jobs_per_page, max_results
from jobspy.model import ScraperInput
from jobspy.util import RequestsRotating, check_status, create_session
from langdetect import DetectorFactory, PROFILES_DIRECTORY, detect
from langdetect.lang_detect_exception import LangDetectException


logging.basicConfig(level=logging.INFO)
DetectorFactory.seed = 0

COLUMNS = [
    "title",
    "company",
    "job_url",
    "mentions_dutch",
    "date_posted",
    "ad_language",
    "description_status",
    "english likelihood title",
]

SKIPPED_TITLE_STATUS = "skipped_title_language"
_title_factory = None


def load_settings():
    path = Path(__file__).resolve().with_name("search.yaml")

    with path.open(encoding="utf-8") as config_file:
        parameters = yaml.safe_load(config_file)

    if not isinstance(parameters, dict):
        raise ValueError("search.yaml must contain key: value settings.")

    minimum = parameters.pop("min_english_likelihood_title", 0.50)
    batch_size = parameters.pop("batch_size", 10)
    request_delay = parameters.pop("request_delay_seconds", 5)
    if type(batch_size) is not int or batch_size not in (10, 20):
        raise ValueError("batch_size must be 10 or 20.")
    if (
        isinstance(request_delay, bool)
        or not isinstance(request_delay, (int, float))
        or not 0 <= request_delay <= 60
    ):
        raise ValueError("request_delay_seconds must be between 0 and 60.")

    if (
        isinstance(minimum, bool)
        or not isinstance(minimum, (int, float))
        or not 0 <= minimum <= 1
    ):
        raise ValueError(
            "min_english_likelihood_title must be between 0 and 1; "
            "0.50 means 50%."
        )

    allowed = {
        "search_term", "location", "results_wanted",
        "distance", "hours_old", "offset",
    }

    unknown = set(parameters) - allowed
    if unknown:
        raise ValueError(f"Unknown search.yaml settings: {unknown}")

    for name in ("search_term", "location", "results_wanted", "hours_old"):
        if name not in parameters:
            raise ValueError(f"Missing search.yaml setting: {name}")

    settings = ScraperInput(
        site_name=["linkedin"],
        fetch_description=False,
        description_format="plain",
        verbose=2,
        **parameters,
    )

    if settings.results_wanted > max_results:
        raise ValueError(f"results_wanted cannot exceed {max_results}.")

    if settings.offset >= max_results:
        raise ValueError(f"offset must be below {max_results}.")

    return settings, float(minimum), batch_size, float(request_delay)
