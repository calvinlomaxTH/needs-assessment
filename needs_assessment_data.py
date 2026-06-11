#!/usr/bin/env python3
"""
needs_assessment_data.py

CCBHC community needs assessment government-data puller.

Changes in this version:
- HUD PIT homelessness is always attempted and always emits a 10-year annual series
  for total, sheltered, and unsheltered homelessness. It uses HUD AHAR 2007-2025
  PIT files by CoC when --hud-coc-codes is supplied and falls back to state-level
  data when no CoC codes are supplied or CoC parsing fails.
- Unemployment and uninsured are annual series for the last N years, not YoY change.
- Medicaid coverage is automated through ACS C27007 Medicaid/means-tested public
  coverage instead of staying as a manual placeholder.
- HRSA Mental Health HPSA is automated through the HRSA Data Downloads CSV when
  discoverable or through --hrsa-hpsa-file / --hrsa-hpsa-url.
- SAMHSA facility counts are automated through the FindTreatment API.
- NCES/CCD enrollment is automated through the Urban Institute Education Data API
  using Common Core of Data source, with a state-level fallback if county filtering
  is not available.
- Internal-only data remain placeholders because they are not government API data.

Install:
    pip install requests pandas openpyxl pyxlsb

Run example:
    export CENSUS_API_KEY="4a550c8493494c363ee0afc87c2af4e97d4a169b"

    python3 needs_assessment_data.py \
      --state-fips 17 \
      --county-fips 031 \
      --state-abbr IL \
      --county-name "Cook County" \
      --service-area-name "Cook County, Illinois" \
      --latest \
      --comparison-geographies county state us \
      --annual-years 10 \
      --bls-target-year 2025 \
      --hud-coc-codes IL-510 IL-511 \
      --verbose
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import math
import os
import re
import time
import warnings
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL.*")

import pandas as pd
import requests

OUTPUT_DIR = Path("outputs")
CACHE_DIR = Path("data/raw")
REQUEST_TIMEOUT = 45
REQUEST_SLEEP_SECONDS = 0.25
MAX_RETRIES = 3
CENSUS_MAX_GET_VARS = 40

ACS_SPECIAL_VALUES = {
    -222222222,
    -333333333,
    -555555555,
    -666666666,
    -777777777,
    -888888888,
    -999999999,
}

HUD_AHAR_2025_PAGE = "https://www.huduser.gov/portal/datasets/ahar/2025-ahar-part-1-pit-estimates-of-homelessness-in-the-us.html"
CDC_MORTALITY_COUNTY_ENDPOINT = "https://data.cdc.gov/resource/psx4-wq38.json"
CDC_PLACES_ENDPOINTS = [
    "https://data.cdc.gov/resource/swc5-untb.json",
    "https://data.cdc.gov/resource/cwsq-ngmh.json",
    "https://data.cdc.gov/resource/duw2-7jbt.json",
]
HRSA_SHORTAGE_DOWNLOAD_PAGE = "https://data.hrsa.gov/data/download?titleFilter=Shortage%20Areas"
SAMHSA_FINDTREATMENT_API = "https://findtreatment.gov/locator/exportsAsJson/v2"
URBAN_ED_DATA_API = "https://educationdata.urban.org/api/v1"

LOGGER = logging.getLogger("needs_assessment_data")


# --------------------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------------------

def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def log_step(message: str) -> None:
    LOGGER.info("STEP | %s", message)


def log_info(message: str) -> None:
    LOGGER.info("INFO | %s", message)


def log_debug(message: str) -> None:
    LOGGER.debug("DEBUG | %s", message)


def log_warn(message: str) -> None:
    LOGGER.warning("WARN | %s", message)


def mask_params(params: dict[str, Any]) -> dict[str, Any]:
    masked = dict(params)
    if "key" in masked:
        masked["key"] = "***"
    return masked


# --------------------------------------------------------------------------------------
# Data models
# --------------------------------------------------------------------------------------

@dataclass
class RunConfig:
    state_fips: str
    county_fips: str
    state_abbr: str
    county_name: str
    service_area_name: str
    latest: bool
    years: list[int]
    census_api_key: Optional[str]
    comparison_geographies: list[str]
    verbose: bool
    annual_years: int
    bls_target_year: int
    hud_year: int
    hud_coc_codes: list[str]
    hud_pit_file: Optional[str]
    hud_pit_url: Optional[str]
    hrsa_hpsa_file: Optional[str]
    hrsa_hpsa_url: Optional[str]
    samhsa_lat: Optional[float]
    samhsa_lon: Optional[float]
    samhsa_radius_miles: float
    nces_year: Optional[int]


@dataclass
class Citation:
    citation_id: str
    citation_label: str
    source_title: str
    source_type: str
    source_agency_or_author: str
    publication_date: str
    url_or_file_reference: str
    notes: str


@dataclass
class IndicatorDefinition:
    indicator_id: str
    indicator_name: str
    needs_assessment_domain: str
    indicator_summary: str
    indicator_detailed_description: str
    units: str
    unit_definition: str
    unit_source: str
    source_name: str
    source_agency: str
    api_or_download: str
    expected_geography_level: str
    expected_update_frequency: str
    latest_logic: str
    government_api_available: str
    public_download_available: str
    internal_or_qualitative_required: str
    source_citation_id: str
    limitation: str
    recommended_qualitative_followup_question: str


@dataclass
class IndicatorCatalogRow:
    indicator_id: str
    indicator_name: str
    needs_assessment_domain: str
    indicator_summary: str
    indicator_detailed_description: str
    units: str
    unit_definition: str
    unit_source: str
    source_name: str
    source_agency: str
    api_or_download: str
    expected_geography_level: str
    expected_update_frequency: str
    latest_logic: str
    government_api_available: str
    public_download_available: str
    internal_or_qualitative_required: str
    source_citation_id: str
    source_citation_text: str
    limitation: str
    recommended_qualitative_followup_question: str


@dataclass
class Observation:
    indicator_id: str
    indicator_name: str
    needs_assessment_domain: str
    indicator_summary: str
    indicator_detailed_description: str
    source_name: str
    source_agency: str
    api_or_download: str
    geography_name: str
    geography_type: str
    state_fips: str
    county_fips: str
    year_or_period: str
    source_latest_year_or_period: str
    estimate: Optional[float]
    moe: Optional[float]
    numerator: Optional[float]
    denominator: Optional[float]
    units: str
    unit_definition: str
    unit_source: str
    source_variable_label: str
    stratification: str
    comparison_available: str
    data_quality_note: str
    source_url_or_endpoint: str
    source_citation_id: str
    source_citation_text: str
    retrieved_at: str


# --------------------------------------------------------------------------------------
# General helpers
# --------------------------------------------------------------------------------------

def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def ensure_dirs() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def normalize_fips(value: str, width: int) -> str:
    return str(value).zfill(width)


def state_name_from_fips(state_fips: str) -> str:
    states = {
        "01": "Alabama", "02": "Alaska", "04": "Arizona", "05": "Arkansas", "06": "California",
        "08": "Colorado", "09": "Connecticut", "10": "Delaware", "11": "District of Columbia",
        "12": "Florida", "13": "Georgia", "15": "Hawaii", "16": "Idaho", "17": "Illinois",
        "18": "Indiana", "19": "Iowa", "20": "Kansas", "21": "Kentucky", "22": "Louisiana",
        "23": "Maine", "24": "Maryland", "25": "Massachusetts", "26": "Michigan", "27": "Minnesota",
        "28": "Mississippi", "29": "Missouri", "30": "Montana", "31": "Nebraska", "32": "Nevada",
        "33": "New Hampshire", "34": "New Jersey", "35": "New Mexico", "36": "New York",
        "37": "North Carolina", "38": "North Dakota", "39": "Ohio", "40": "Oklahoma",
        "41": "Oregon", "42": "Pennsylvania", "44": "Rhode Island", "45": "South Carolina",
        "46": "South Dakota", "47": "Tennessee", "48": "Texas", "49": "Utah", "50": "Vermont",
        "51": "Virginia", "53": "Washington", "54": "West Virginia", "55": "Wisconsin", "56": "Wyoming",
    }
    return states.get(state_fips, "")


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        f = float(value)
    else:
        s = str(value).strip()
        if s in {"", "null", "None", "NaN", "nan", "-", "**", "***", "N/A", "NA", "1-9"}:
            return None
        try:
            f = float(s.replace(",", ""))
        except ValueError:
            return None
    if math.isnan(f):
        return None
    if int(f) in ACS_SPECIAL_VALUES:
        return None
    if f == -999:
        return None
    return f


def safe_int(value: Any) -> Optional[int]:
    f = safe_float(value)
    return int(f) if f is not None else None


def calc_percent(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return (numerator / denominator) * 100.0


def sum_values(values: list[Optional[float]]) -> Optional[float]:
    good = [v for v in values if v is not None]
    return float(sum(good)) if good else None


def sum_moes(moes: list[Optional[float]]) -> Optional[float]:
    good = [m for m in moes if m is not None]
    return math.sqrt(sum(m * m for m in good)) if good else None


def average(values: list[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[i:i + size] for i in range(0, len(values), size)]


def cache_key(url: str, params: Optional[dict[str, Any]] = None, body: Optional[dict[str, Any]] = None) -> str:
    payload = json.dumps({"url": url, "params": params or {}, "body": body or {}}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def census_base_url(year: int, dataset_suffix: str = "") -> str:
    base = f"https://api.census.gov/data/{year}/acs/acs5"
    suffix = dataset_suffix.strip("/")
    return f"{base}/{suffix}" if suffix else base


def looks_like_missing_key_html(text: str) -> bool:
    lowered = text.lower()
    return "missing key" in lowered or "invalid key" in lowered or "key_signup.html" in lowered


def cached_get_json(
    source_slug: str,
    url: str,
    params: Optional[dict[str, Any]] = None,
    errors: Optional[list[str]] = None,
    record_error: bool = True,
    max_retries: int = MAX_RETRIES,
) -> Optional[Any]:
    params = params or {}
    source_dir = CACHE_DIR / source_slug / dt.date.today().isoformat()
    source_dir.mkdir(parents=True, exist_ok=True)
    path = source_dir / f"{cache_key(url, params=params)}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            time.sleep(REQUEST_SLEEP_SECONDS)
            response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            if response.status_code >= 400:
                last_error = f"{response.status_code}: {response.text[:500]}"
                time.sleep(attempt)
                continue
            if looks_like_missing_key_html(response.text):
                last_error = "Census API key missing or invalid. Provide --census-api-key or set CENSUS_API_KEY."
                break
            try:
                data = response.json()
            except json.JSONDecodeError:
                last_error = f"Non-JSON response from {url}; status={response.status_code}; body={response.text[:500]}"
                time.sleep(attempt)
                continue
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return data
        except Exception as exc:
            last_error = repr(exc)
            time.sleep(attempt)
    if errors is not None and record_error:
        errors.append(f"GET JSON failed: {url} params={params} error={last_error}")
    return None


def cached_get_text(source_slug: str, url: str, params: Optional[dict[str, Any]] = None, errors: Optional[list[str]] = None, record_error: bool = True) -> Optional[str]:
    params = params or {}
    source_dir = CACHE_DIR / source_slug / dt.date.today().isoformat()
    source_dir.mkdir(parents=True, exist_ok=True)
    path = source_dir / f"{cache_key(url, params=params)}.txt"
    if path.exists():
        return path.read_text(encoding="utf-8", errors="ignore")
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            time.sleep(REQUEST_SLEEP_SECONDS)
            response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            if response.status_code >= 400:
                last_error = f"{response.status_code}: {response.text[:500]}"
                time.sleep(attempt)
                continue
            text = response.text
            path.write_text(text, encoding="utf-8")
            return text
        except Exception as exc:
            last_error = repr(exc)
            time.sleep(attempt)
    if errors is not None and record_error:
        errors.append(f"GET text failed: {url} error={last_error}")
    return None


def cached_post_json(source_slug: str, url: str, body: dict[str, Any], errors: Optional[list[str]] = None) -> Optional[Any]:
    source_dir = CACHE_DIR / source_slug / dt.date.today().isoformat()
    source_dir.mkdir(parents=True, exist_ok=True)
    path = source_dir / f"{cache_key(url, body=body)}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            time.sleep(REQUEST_SLEEP_SECONDS)
            response = requests.post(url, json=body, timeout=REQUEST_TIMEOUT)
            if response.status_code >= 400:
                last_error = f"{response.status_code}: {response.text[:500]}"
                time.sleep(attempt)
                continue
            try:
                data = response.json()
            except json.JSONDecodeError:
                last_error = f"Non-JSON response: {response.text[:500]}"
                time.sleep(attempt)
                continue
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return data
        except Exception as exc:
            last_error = repr(exc)
            time.sleep(attempt)
    if errors is not None:
        errors.append(f"POST JSON failed: {url} body={body} error={last_error}")
    return None


def download_file(source_slug: str, url: str, errors: list[str], filename_hint: Optional[str] = None) -> Optional[Path]:
    source_dir = CACHE_DIR / source_slug / dt.date.today().isoformat()
    source_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(url.split("?")[0]).suffix or ".dat"
    filename = filename_hint or f"{hashlib.sha256(url.encode('utf-8')).hexdigest()}{ext}"
    path = source_dir / filename
    if path.exists() and path.stat().st_size > 0:
        return path
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            time.sleep(REQUEST_SLEEP_SECONDS)
            response = requests.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            if response.status_code >= 400:
                last_error = f"{response.status_code}: {response.text[:500]}"
                time.sleep(attempt)
                continue
            path.write_bytes(response.content)
            return path
        except Exception as exc:
            last_error = repr(exc)
            time.sleep(attempt)
    errors.append(f"Failed to download file: {url} error={last_error}")
    return None


def read_any_table_file(path: Path, errors: list[str], notes: list[str]) -> list[pd.DataFrame]:
    frames: list[pd.DataFrame] = []
    suffix = path.suffix.lower()
    try:
        if suffix == ".csv":
            frames.append(pd.read_csv(path, low_memory=False))
        elif suffix in {".xlsx", ".xls"}:
            xl = pd.ExcelFile(path)
            for sheet in xl.sheet_names:
                for header in range(0, 8):
                    try:
                        df = pd.read_excel(path, sheet_name=sheet, header=header)
                        if df is not None and not df.empty:
                            df["_source_sheet"] = sheet
                            df["_source_header_row"] = header
                            frames.append(df)
                    except Exception:
                        pass
        elif suffix == ".xlsb":
            try:
                xl = pd.ExcelFile(path, engine="pyxlsb")
                for sheet in xl.sheet_names:
                    for header in range(0, 8):
                        try:
                            df = pd.read_excel(path, sheet_name=sheet, header=header, engine="pyxlsb")
                            if df is not None and not df.empty:
                                df["_source_sheet"] = sheet
                                df["_source_header_row"] = header
                                frames.append(df)
                        except Exception:
                            pass
            except ImportError:
                notes.append("Install pyxlsb to read HUD XLSB files: pip install pyxlsb")
        elif suffix == ".zip":
            with zipfile.ZipFile(path) as z:
                for name in z.namelist():
                    lower = name.lower()
                    if lower.endswith(".csv"):
                        with z.open(name) as f:
                            df = pd.read_csv(f, low_memory=False)
                            df["_source_zip_member"] = name
                            frames.append(df)
                    elif lower.endswith(".xlsx"):
                        with z.open(name) as f:
                            # pandas can read BytesIO, but importing here avoids top-level dependency.
                            import io
                            data = io.BytesIO(f.read())
                            xl = pd.ExcelFile(data)
                            for sheet in xl.sheet_names:
                                df = pd.read_excel(data, sheet_name=sheet)
                                df["_source_zip_member"] = name
                                df["_source_sheet"] = sheet
                                frames.append(df)
        else:
            notes.append(f"Unsupported file extension for table parsing: {path}")
    except Exception as exc:
        errors.append(f"Failed reading table file {path}: {repr(exc)}")
    return frames


def norm_col(col: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(col).strip().lower()).strip("_")


def find_col(df: pd.DataFrame, tokens: list[str], exclude: Optional[list[str]] = None) -> Optional[Any]:
    exclude = exclude or []
    for col in df.columns:
        n = norm_col(col)
        if all(t in n for t in tokens) and not any(e in n for e in exclude):
            return col
    return None


# --------------------------------------------------------------------------------------
# Citations and indicators
# --------------------------------------------------------------------------------------

def build_citation_registry() -> dict[str, Citation]:
    citations = [
        Citation("CENSUS_ACS_API", "U.S. Census ACS API", "American Community Survey API", "Government API", "U.S. Census Bureau", "Ongoing", "https://api.census.gov/data.html", "Source for ACS indicators including Medicaid, uninsured, demographics, SDOH, and housing."),
        Citation("CMS_MEDICAID_DATA", "CMS Medicaid Data", "Data.Medicaid.gov and Medicaid administrative data", "Government API / public files", "Centers for Medicare & Medicaid Services", "Ongoing", "https://data.medicaid.gov/", "State-level Medicaid administrative data; county Medicaid coverage is populated from ACS C27007 in this script."),
        Citation("BLS_LAUS_API", "BLS Public Data API", "Local Area Unemployment Statistics via BLS Public Data API", "Government API", "U.S. Bureau of Labor Statistics", "Ongoing", "https://api.bls.gov/publicAPI/v2/timeseries/data/", "Source for county unemployment indicators."),
        Citation("CDC_PLACES_API", "CDC PLACES API", "CDC PLACES Local Data for Better Health", "Government API / Socrata", "Centers for Disease Control and Prevention", "Ongoing", "https://data.cdc.gov/", "Source for modeled county/local health indicators."),
        Citation("CDC_MIVO_COUNTY", "CDC MIVO County", "Mapping Injury, Overdose, and Violence - County", "Government API / Socrata", "Centers for Disease Control and Prevention", "Ongoing", CDC_MORTALITY_COUNTY_ENDPOINT, "County-level death counts and rates for suicide and drug overdose."),
        Citation("HRSA_DATA_WAREHOUSE", "HRSA Data Warehouse", "HRSA Data Warehouse Shortage Areas Downloads", "Government download", "Health Resources and Services Administration", "Daily", HRSA_SHORTAGE_DOWNLOAD_PAGE, "Mental Health HPSA data."),
        Citation("SAMHSA_FINDTREATMENT", "SAMHSA FindTreatment API", "FindTreatment.gov API", "Government API", "Substance Abuse and Mental Health Services Administration", "Ongoing", SAMHSA_FINDTREATMENT_API, "Facility locator API for mental health and substance use treatment facilities."),
        Citation("NCES_CCD", "NCES Common Core of Data", "Common Core of Data via Education Data Portal", "Government-derived API", "National Center for Education Statistics / Urban Institute", "Annual", "https://educationdata.urban.org/documentation/", "CCD school enrollment data."),
        Citation("HUD_PIT_HIC", "HUD PIT/HIC", "Annual Homelessness Assessment Report PIT data", "Government public files", "U.S. Department of Housing and Urban Development", "Annual", HUD_AHAR_2025_PAGE, "PIT homelessness data by CoC and state, 2007-2025."),
        Citation("INTERNAL_PLACEHOLDER", "Internal organization data placeholder", "Internal CCBHC data placeholder", "Internal data placeholder", "Client organization", "Project-specific", "Not available through government API", "Used for EHR, staffing, utilization, and qualitative findings."),
    ]
    return {c.citation_id: c for c in citations}


def citation_text(citation_id: str) -> str:
    c = build_citation_registry()[citation_id]
    return f"{c.citation_label}: {c.source_title}. {c.source_agency_or_author}. {c.publication_date}. {c.url_or_file_reference}."


def indicator_def(indicator_id: str, name: str, domain: str, summary: str, units: str, unit_definition: str, unit_source: str, source_name: str, source_agency: str, api_or_download: str, geo: str, update: str, latest_logic: str, citation_id: str, limitation: str, followup: str, government_api_available: str = "Yes", public_download_available: str = "Yes", internal_or_qualitative_required: str = "No") -> IndicatorDefinition:
    return IndicatorDefinition(indicator_id, name, domain, summary, summary, units, unit_definition, unit_source, source_name, source_agency, api_or_download, geo, update, latest_logic, government_api_available, public_download_available, internal_or_qualitative_required, citation_id, limitation, followup)


def build_indicator_definitions() -> dict[str, IndicatorDefinition]:
    latest_acs = "Use newest ACS 5-year endpoint and valid Census API key."
    latest_bls = "Return one annual BLS LAUS unemployment-rate row for each of the last N years."
    latest_places = "Use newest matching CDC PLACES county row."
    acs_detail = ("ACS 5-year Detailed Tables", "U.S. Census Bureau", "API", "County, state, U.S.", "Annual", latest_acs, "CENSUS_ACS_API")
    acs_profile = ("ACS 5-year Data Profile", "U.S. Census Bureau", "API", "County, state, U.S.", "Annual", latest_acs, "CENSUS_ACS_API")
    acs_subject = ("ACS 5-year Subject Tables", "U.S. Census Bureau", "API", "County, state, U.S.", "Annual", latest_acs, "CENSUS_ACS_API")
    rows = [
        indicator_def("POP_TOTAL", "Total population", "Service Area and Population", "Total residents.", "people", "Number of people.", "ACS count variable.", *acs_detail, "ACS period estimate.", "Do partners perceive recent population changes?"),
        indicator_def("POP_AGE_UNDER_18_COUNT", "Population under age 18", "Demographics", "Children and youth under 18.", "people", "Number of people under age 18.", "ACS count variable.", *acs_detail, "Not direct behavioral health need.", "Which youth groups have unmet need?"),
        indicator_def("POP_AGE_UNDER_18_PCT", "Population under age 18 percentage", "Demographics", "Share of residents under 18.", "percent", "Under-18 population / total population * 100.", "Calculated from ACS numerator and denominator.", *acs_detail, "Derived percentage MOE not calculated.", "Are youth represented proportionally?"),
        indicator_def("POP_AGE_65_PLUS_COUNT", "Population age 65 and older", "Demographics", "Residents age 65+.", "people", "Number of people age 65+.", "Calculated from ACS age cells.", *acs_detail, "MOE for sum approximated.", "Which older adult needs are visible?"),
        indicator_def("POP_AGE_65_PLUS_PCT", "Population age 65 and older percentage", "Demographics", "Share of residents age 65+.", "percent", "Age 65+ population / total population * 100.", "Calculated from ACS numerator and denominator.", *acs_detail, "Derived percentage MOE not calculated.", "Are older adults represented proportionally?"),
        indicator_def("HISPANIC_LATINX_COUNT", "Hispanic or Latinx population", "Demographics", "Residents identifying as Hispanic or Latinx.", "people", "Number of Hispanic/Latinx residents.", "ACS count variable.", *acs_detail, "MOEs may be large.", "Are Hispanic/Latinx residents proportionately served?"),
        indicator_def("HISPANIC_LATINX_PCT", "Hispanic or Latinx population percentage", "Demographics", "Share Hispanic or Latinx.", "percent", "Hispanic/Latinx population / total population * 100.", "Calculated from ACS numerator and denominator.", *acs_detail, "Derived percentage MOE not calculated.", "What cultural or linguistic barriers affect access?"),
        indicator_def("AIAN_POPULATION_COUNT", "American Indian and Alaska Native population", "Demographics", "AIAN-alone residents.", "people", "Number of AIAN-alone residents.", "ACS count variable.", *acs_detail, "Does not capture tribal affiliation or multiracial identity.", "What Indigenous-serving partners should be engaged?"),
        indicator_def("AIAN_POPULATION_PCT", "American Indian and Alaska Native population percentage", "Demographics", "Share AIAN alone.", "percent", "AIAN-alone population / total population * 100.", "Calculated from ACS numerator and denominator.", *acs_detail, "Derived percentage MOE not calculated.", "Are Indigenous residents represented in services?"),
        indicator_def("VETERAN_POPULATION_COUNT", "Veteran population", "Underserved Populations", "Civilian veterans.", "people", "Number of civilian veterans age 18+.", "ACS count variable.", *acs_detail, "Does not directly measure veteran behavioral health need.", "Are veterans able to access care?"),
        indicator_def("VETERAN_POPULATION_PCT", "Veteran population percentage", "Underserved Populations", "Share of adult civilian population that is veteran.", "percent", "Veteran population / civilian population age 18+ * 100.", "Calculated from ACS numerator and denominator.", *acs_detail, "Derived percentage MOE not calculated.", "Are veteran-specific needs visible?"),
        indicator_def("DISABILITY_COUNT", "Population with a disability", "Underserved Populations", "Residents with a disability.", "people", "Civilian noninstitutionalized people with a disability.", "ACS subject count variable.", *acs_subject, "ACS disability categories may not map to BH need.", "What accessibility barriers affect care?"),
        indicator_def("DISABILITY_PREVALENCE", "Disability prevalence", "Underserved Populations", "Share of residents with a disability.", "percent", "Percent with a disability.", "ACS subject percent variable.", *acs_subject, "ACS disability categories may not map to BH need.", "What accessibility barriers affect care?"),
        indicator_def("LANGUAGE_LEP_SPANISH_COUNT", "Spanish speakers who speak English less than very well", "Culture and Language", "Spanish-speaking LEP residents.", "people", "People age 5+ who speak Spanish and speak English less than very well.", "ACS count variable.", *acs_detail, "Spanish LEP only, not all LEP residents.", "Which language supports are needed?"),
        indicator_def("LANGUAGE_LEP_SPANISH_PCT", "Spanish limited-English-proficiency percentage", "Culture and Language", "Share age 5+ Spanish LEP.", "percent", "Spanish LEP / population age 5+ * 100.", "Calculated from ACS numerator and denominator.", *acs_detail, "Derived percentage MOE not calculated.", "Do interpretation resources match need?"),
        indicator_def("POVERTY_RATE", "Population below poverty level", "Economic Stability", "Percentage below poverty.", "percent", "Percent below federal poverty level.", "ACS profile percent variable.", *acs_profile, "ACS poverty estimates may lag changes.", "How is economic hardship affecting access?"),
        indicator_def("MEDIAN_HOUSEHOLD_INCOME", "Median household income", "Economic Stability", "Median household income.", "dollars", "Median household income in dollars.", "ACS dollar variable.", *acs_detail, "Median masks local variation.", "Are income trends aligned with lived experience?"),
        indicator_def("UNINSURED_RATE", "Uninsured rate", "Insurance Coverage", "Percentage without health insurance.", "percent", "Percent with no health insurance.", "ACS profile percent variable.", *acs_profile, "ACS 5-year periods overlap; interpret trends cautiously.", "Do uninsured residents know where to get care?"),
        indicator_def("PUBLIC_COVERAGE_RATE", "Public health insurance coverage rate", "Insurance Coverage", "Percentage with public coverage.", "percent", "Percent with public health insurance.", "ACS DP03_0098PE.", *acs_profile, "Public coverage is not Medicaid-only.", "Are publicly insured residents able to access care?"),
        indicator_def("MEDICAID_COVERAGE_RATE", "Medicaid coverage rate", "Insurance Coverage", "Percentage covered by Medicaid/means-tested public coverage.", "percent", "Medicaid/means-tested public coverage / civilian noninstitutionalized population * 100.", "ACS C27007 computed rate.", "ACS C27007 Medicaid/Means-Tested Public Coverage", "U.S. Census Bureau", "API / calculated", "County, state, U.S.", "Annual", latest_acs, "CENSUS_ACS_API", "ACS table measures Medicaid/means-tested public coverage, not administrative enrollment.", "Are Medicaid members able to access care?"),
        indicator_def("NO_VEHICLE_HOUSEHOLDS_COUNT", "Households with no vehicle available", "Transportation", "Households with no vehicle.", "households", "Households reporting no vehicle.", "ACS count variable.", *acs_detail, "Does not measure transit/distance.", "Where do transportation barriers affect care?"),
        indicator_def("NO_VEHICLE_HOUSEHOLDS_PCT", "Households with no vehicle available percentage", "Transportation", "Share of households with no vehicle.", "percent", "No-vehicle households / total households * 100.", "Calculated from ACS numerator and denominator.", *acs_detail, "Derived percentage MOE not calculated.", "Are transit, rides, or mobile services needed?"),
        indicator_def("SNAP_HOUSEHOLDS_COUNT", "Households receiving SNAP", "Food/Nutrition", "Households receiving SNAP.", "households", "Households receiving SNAP.", "ACS count variable.", *acs_detail, "SNAP is not full food insecurity.", "Do residents report food insecurity?"),
        indicator_def("SNAP_HOUSEHOLDS_PCT", "Households receiving SNAP percentage", "Food/Nutrition", "Share of households receiving SNAP.", "percent", "SNAP households / total households * 100.", "Calculated from ACS numerator and denominator.", *acs_detail, "Derived percentage MOE not calculated.", "Are food needs appearing in BH settings?"),
        indicator_def("RENT_BURDENED_HOUSEHOLDS_COUNT", "Rent-burdened households", "Housing Stability", "Renter households spending 30%+ of income on rent.", "households", "Rent-burdened renter households.", "Calculated from ACS rent categories.", *acs_detail, "MOE for sum approximated.", "How is housing affordability affecting recovery?"),
        indicator_def("RENT_BURDENED_HOUSEHOLDS_PCT", "Rent-burdened households percentage", "Housing Stability", "Share of renter households rent-burdened.", "percent", "Rent-burdened renter households / renter households * 100.", "Calculated from ACS numerator and denominator.", *acs_detail, "Derived percentage MOE not calculated.", "Does housing cost interfere with treatment?"),
        indicator_def("MEDIAN_HOME_VALUE", "Median home value", "Housing Stability", "Median owner-occupied home value.", "dollars", "Median home value in dollars.", "ACS dollar variable.", *acs_detail, "Does not measure rent/homelessness.", "Are housing costs affecting clients/workforce?"),
        indicator_def("UNEMPLOYMENT_RATE", "Unemployment rate", "Economic Stability", "Annual unemployment rate.", "percent", "Unemployed labor force / labor force * 100.", "BLS LAUS.", "BLS LAUS", "U.S. Bureau of Labor Statistics", "API", "County", "Annual", "Return one annual BLS LAUS unemployment-rate row for each of the last N years.", "BLS_LAUS_API", "Series ID inferred; validate before publication.", "Are employment barriers affecting access?"),
        indicator_def("FREQUENT_MENTAL_DISTRESS", "Frequent mental distress", "Mental Health Prevalence and Outcomes", "Estimated frequent mental distress prevalence.", "percent", "Percent of adults.", "CDC PLACES metadata.", "CDC PLACES", "Centers for Disease Control and Prevention", "API / Socrata", "County", "Annual release", latest_places, "CDC_PLACES_API", "Modeled estimate.", "Does modeled distress align with stakeholders?", "Partial", "Yes", "No"),
        indicator_def("DEPRESSION_PREVALENCE", "Depression prevalence", "Mental Health Prevalence and Outcomes", "Estimated depression prevalence.", "percent", "Percent of adults.", "CDC PLACES metadata.", "CDC PLACES", "Centers for Disease Control and Prevention", "API / Socrata", "County", "Annual release", latest_places, "CDC_PLACES_API", "Modeled estimate.", "Are depression needs presenting differently?", "Partial", "Yes", "No"),
        indicator_def("BINGE_DRINKING", "Binge drinking", "Substance Use Prevalence and Outcomes", "Estimated binge drinking prevalence.", "percent", "Percent of adults.", "CDC PLACES metadata.", "CDC PLACES", "Centers for Disease Control and Prevention", "API / Socrata", "County", "Annual release", latest_places, "CDC_PLACES_API", "Modeled estimate.", "How are alcohol needs showing up?", "Partial", "Yes", "No"),
        indicator_def("CURRENT_SMOKING", "Current smoking", "Physical Health and Co-occurring Conditions", "Estimated current smoking prevalence.", "percent", "Percent of adults.", "CDC PLACES metadata.", "CDC PLACES", "Centers for Disease Control and Prevention", "API / Socrata", "County", "Annual release", latest_places, "CDC_PLACES_API", "Modeled estimate.", "Are tobacco needs addressed?", "Partial", "Yes", "No"),
        indicator_def("SUICIDE_MORTALITY", "Suicide mortality rate", "Mental Health Prevalence and Outcomes", "Suicide mortality rate.", "deaths per 100,000", "Suicide deaths per 100,000 population.", "CDC MIVO County rate.", "CDC MIVO County", "Centers for Disease Control and Prevention", "API / Socrata", "County", "Annual", "Return one annual CDC MIVO county row for each of the last N years.", "CDC_MIVO_COUNTY", "Counts 1-9 are suppressed; rates may be modeled.", "What suicide-prevention needs are not visible?"),
        indicator_def("SUICIDE_DEATHS_COUNT", "Suicide deaths count", "Mental Health Prevalence and Outcomes", "Suicide death count.", "deaths", "Number of suicide deaths.", "CDC MIVO County count.", "CDC MIVO County", "Centers for Disease Control and Prevention", "API / Socrata", "County", "Annual", "Return one annual CDC MIVO county row for each of the last N years.", "CDC_MIVO_COUNTY", "Counts 1-9 are suppressed.", "What suicide-prevention needs are not visible?"),
        indicator_def("OVERDOSE_MORTALITY", "Drug overdose mortality rate", "Substance Use Prevalence and Outcomes", "Drug overdose mortality rate.", "deaths per 100,000", "Drug overdose deaths per 100,000 population.", "CDC MIVO County rate.", "CDC MIVO County", "Centers for Disease Control and Prevention", "API / Socrata", "County", "Annual", "Return one annual CDC MIVO county row for each of the last N years.", "CDC_MIVO_COUNTY", "Counts 1-9 are suppressed; rates may be modeled.", "Which overdose risks are visible?"),
        indicator_def("OVERDOSE_DEATHS_COUNT", "Drug overdose deaths count", "Substance Use Prevalence and Outcomes", "Drug overdose death count.", "deaths", "Number of drug overdose deaths.", "CDC MIVO County count.", "CDC MIVO County", "Centers for Disease Control and Prevention", "API / Socrata", "County", "Annual", "Return one annual CDC MIVO county row for each of the last N years.", "CDC_MIVO_COUNTY", "Counts 1-9 are suppressed.", "Which overdose risks are visible?"),
        indicator_def("HOMELESSNESS_PIT", "People experiencing homelessness", "Housing Stability", "PIT homelessness total.", "people", "People counted as experiencing homelessness on a single night.", "HUD PIT/AHAR.", "HUD PIT/HIC", "U.S. Department of Housing and Urban Development", "Public download", "CoC or state", "Annual", "Always emit one row per year for the last N HUD PIT years; use CoC data when codes are supplied and state fallback otherwise.", "HUD_PIT_HIC", "CoC-to-county matching requires CoC codes.", "What housing instability is missed?"),
        indicator_def("HOMELESSNESS_PIT_SHELTERED", "Sheltered homelessness PIT", "Housing Stability", "Sheltered PIT homelessness.", "people", "Sheltered people experiencing homelessness.", "HUD PIT/AHAR.", "HUD PIT/HIC", "U.S. Department of Housing and Urban Development", "Public download", "CoC or state", "Annual", "Always emit one row per year for the last N HUD PIT years; use CoC data when codes are supplied and state fallback otherwise.", "HUD_PIT_HIC", "CoC-to-county matching requires CoC codes.", "What shelter access gaps exist?"),
        indicator_def("HOMELESSNESS_PIT_UNSHELTERED", "Unsheltered homelessness PIT", "Housing Stability", "Unsheltered PIT homelessness.", "people", "Unsheltered people experiencing homelessness.", "HUD PIT/AHAR.", "HUD PIT/HIC", "U.S. Department of Housing and Urban Development", "Public download", "CoC or state", "Annual", "Always emit one row per year for the last N HUD PIT years; use CoC data when codes are supplied and state fallback otherwise.", "HUD_PIT_HIC", "CoC-to-county matching requires CoC codes.", "What unsheltered homelessness is missed?"),
        indicator_def("MENTAL_HEALTH_HPSA", "Mental Health HPSA designations", "Workforce and Provider Availability", "Mental health shortage designations in the service geography.", "designations", "Count of active mental health HPSA records matching the geography.", "HRSA mental health HPSA CSV.", "HRSA Data Warehouse", "Health Resources and Services Administration", "Automated public download", "County/service area", "Daily", "Download HRSA Mental Health HPSA CSV and count matching active records.", "HRSA_DATA_WAREHOUSE", "HPSA boundaries may not align exactly to county.", "Which workforce shortages affect access?", "Yes", "Yes", "No"),
        indicator_def("TREATMENT_FACILITIES_MH", "Mental health treatment facilities", "Treatment Facilities and Service Infrastructure", "Mental health facility availability.", "facilities", "Count of facility records.", "SAMHSA FindTreatment API.", "SAMHSA FindTreatment", "Substance Abuse and Mental Health Services Administration", "API", "County or radius", "Ongoing", "Use FindTreatment API with county/radius search and sType=mh.", "SAMHSA_FINDTREATMENT", "Listings may not reflect capacity.", "Which services are actually available?", "Yes", "Yes", "No"),
        indicator_def("TREATMENT_FACILITIES_SUD", "SUD treatment facilities and MOUD availability", "Treatment Facilities and Service Infrastructure", "SUD/MOUD facility availability.", "facilities", "Count of facility records.", "SAMHSA FindTreatment API.", "SAMHSA FindTreatment", "Substance Abuse and Mental Health Services Administration", "API", "County or radius", "Ongoing", "Use FindTreatment API with county/radius search and sType=sa; MOUD services counted when coded.", "SAMHSA_FINDTREATMENT", "Listings may not reflect capacity.", "Where are SUD level-of-care gaps?", "Yes", "Yes", "No"),
        indicator_def("SCHOOL_ENROLLMENT", "School enrollment and student demographics", "Children, Youth, and Families", "Student enrollment.", "students", "Enrolled students.", "CCD via Education Data API.", "NCES CCD / Education Data Portal", "National Center for Education Statistics / Urban Institute", "API", "County/state", "Annual", "Use Education Data API CCD enrollment endpoint; county filter when available, state fallback otherwise.", "NCES_CCD", "County aggregation may require crosswalk.", "Which school partners should be engaged?", "Yes", "Yes", "No"),
        indicator_def("CLIENT_DEMOGRAPHICS", "CCBHC client demographics", "Underserved Populations", "Client demographic profile.", "clients", "Clients or percent of clients.", "Internal data.", "Internal EHR / client data", "Client organization", "Internal file", "Client/service area", "Project-specific", "Internal file required.", "INTERNAL_PLACEHOLDER", "Not available through government APIs.", "Which groups are underrepresented?", "No", "No", "Yes"),
        indicator_def("SERVICE_UTILIZATION", "Service utilization, referrals, wait times, no-shows", "Access to Care", "Service access and operations.", "varies", "Visits, clients, days, referrals, or percent.", "Internal data.", "Internal operations data", "Client organization", "Internal file", "Client/service area", "Project-specific", "Internal file required.", "INTERNAL_PLACEHOLDER", "Not available through government APIs.", "Where are bottlenecks?", "No", "No", "Yes"),
        indicator_def("STAFFING_PLAN", "Staffing plan, FTEs, credentials, turnover, training", "Staffing Implications", "Staffing capacity and alignment.", "FTE / roles", "FTEs, roles, credentials, vacancies.", "Internal data.", "Internal staffing plan", "Client organization", "Internal file", "Client/service area", "Project-specific", "Internal file required.", "INTERNAL_PLACEHOLDER", "Not available through government APIs.", "What staffing changes are needed?", "No", "No", "Yes"),
        indicator_def("QUALITATIVE_THEMES", "Interview, focus group, advisory board, and survey themes", "Qualitative Findings", "Primary qualitative themes.", "themes", "Themes, findings, quotes, or counts.", "Internal qualitative analysis.", "Primary qualitative research", "Client organization / consultant", "Internal file", "Service area", "Project-specific", "Internal file required.", "INTERNAL_PLACEHOLDER", "Not statistically representative unless designed that way.", "What explains quantitative patterns?", "No", "No", "Yes"),
    ]
    return {row.indicator_id: row for row in rows}


def definition(indicator_id: str) -> IndicatorDefinition:
    return build_indicator_definitions()[indicator_id]


# --------------------------------------------------------------------------------------
# Observation helpers
# --------------------------------------------------------------------------------------

def make_observation(
    *,
    indicator_id: str,
    geography_name: str,
    geography_type: str,
    state_fips: str,
    county_fips: str,
    year_or_period: str,
    source_latest_year_or_period: str,
    estimate: Optional[float],
    moe: Optional[float],
    numerator: Optional[float],
    denominator: Optional[float],
    source_variable_label: str,
    stratification: str,
    comparison_available: str,
    data_quality_note: str,
    source_url_or_endpoint: str,
    units_override: Optional[str] = None,
    unit_definition_override: Optional[str] = None,
    unit_source_override: Optional[str] = None,
) -> Observation:
    d = definition(indicator_id)
    return Observation(
        indicator_id=d.indicator_id,
        indicator_name=d.indicator_name,
        needs_assessment_domain=d.needs_assessment_domain,
        indicator_summary=d.indicator_summary,
        indicator_detailed_description=d.indicator_detailed_description,
        source_name=d.source_name,
        source_agency=d.source_agency,
        api_or_download=d.api_or_download,
        geography_name=geography_name,
        geography_type=geography_type,
        state_fips=state_fips,
        county_fips=county_fips,
        year_or_period=year_or_period,
        source_latest_year_or_period=source_latest_year_or_period,
        estimate=estimate,
        moe=moe,
        numerator=numerator,
        denominator=denominator,
        units=units_override or d.units,
        unit_definition=unit_definition_override or d.unit_definition,
        unit_source=unit_source_override or d.unit_source,
        source_variable_label=source_variable_label,
        stratification=stratification,
        comparison_available=comparison_available,
        data_quality_note=data_quality_note,
        source_url_or_endpoint=source_url_or_endpoint,
        source_citation_id=d.source_citation_id,
        source_citation_text=citation_text(d.source_citation_id),
        retrieved_at=now_iso(),
    )


def add_count_and_percent_observations(observations: list[Observation], *, count_indicator_id: str, pct_indicator_id: Optional[str], numerator: Optional[float], numerator_moe: Optional[float], denominator: Optional[float], geography_name: str, geography_type: str, state_fips: str, county_fips: str, year: int, source_latest: str, source_variable_label: str, source_url_or_endpoint: str, note: str) -> None:
    observations.append(make_observation(indicator_id=count_indicator_id, geography_name=geography_name, geography_type=geography_type, state_fips=state_fips, county_fips=county_fips, year_or_period=str(year), source_latest_year_or_period=source_latest, estimate=numerator, moe=numerator_moe, numerator=numerator, denominator=denominator, source_variable_label=source_variable_label, stratification="Total", comparison_available="Yes", data_quality_note=note, source_url_or_endpoint=source_url_or_endpoint))
    if pct_indicator_id:
        observations.append(make_observation(indicator_id=pct_indicator_id, geography_name=geography_name, geography_type=geography_type, state_fips=state_fips, county_fips=county_fips, year_or_period=str(year), source_latest_year_or_period=source_latest, estimate=calc_percent(numerator, denominator), moe=None, numerator=numerator, denominator=denominator, source_variable_label=source_variable_label, stratification="Total", comparison_available="Yes", data_quality_note=note + " Derived percentage calculated by script; MOE for derived percentage not calculated.", source_url_or_endpoint=source_url_or_endpoint))


# --------------------------------------------------------------------------------------
# Census ACS adapters
# --------------------------------------------------------------------------------------

def get_census_variables(year: int, dataset_suffix: str, errors: Optional[list[str]] = None, record_error: bool = True) -> dict[str, dict[str, Any]]:
    url = f"{census_base_url(year, dataset_suffix)}/variables.json"
    data = cached_get_json("census_acs_variables", url, params={}, errors=errors, record_error=record_error, max_retries=1)
    if not isinstance(data, dict):
        return {}
    variables = data.get("variables")
    if not isinstance(variables, dict):
        return {}
    return {k: v for k, v in variables.items() if isinstance(v, dict)}


def detect_available_acs_years(config: RunConfig, notes: list[str], count: int = 10) -> list[int]:
    if not config.latest and config.years:
        candidate_years = sorted(config.years, reverse=True)
    else:
        today = dt.date.today()
        start_year = today.year - 1 if today.month >= 12 else today.year - 2
        candidate_years = list(range(start_year, start_year - 15, -1))
    available: list[int] = []
    for year in candidate_years:
        variables = get_census_variables(year, "", errors=None, record_error=False)
        if variables and "B01001_001E" in variables:
            available.append(year)
        if len(available) >= count:
            break
    if not available:
        notes.append("Could not detect usable ACS 5-year years.")
    return sorted(available)


def label_for_vars(metadata: dict[str, dict[str, Any]], vars_used: list[str]) -> str:
    pieces = []
    for var in vars_used:
        meta = metadata.get(var, {})
        label = str(meta.get("label", ""))
        concept = str(meta.get("concept", ""))
        if concept and label:
            pieces.append(f"{var}: {concept} - {label}")
        elif label:
            pieces.append(f"{var}: {label}")
        else:
            pieces.append(var)
    return "; ".join(pieces)


def selected_geographies(config: RunConfig) -> dict[str, tuple[str, str, str]]:
    all_geos = {"county": ("County", config.state_fips, config.county_fips), "state": ("State", config.state_fips, ""), "us": ("United States", "", "")}
    return {k: v for k, v in all_geos.items() if k in config.comparison_geographies}


def filter_available_vars(requested: list[str], metadata: dict[str, dict[str, Any]], source_name: str, notes: list[str]) -> list[str]:
    if not metadata:
        notes.append(f"{source_name}: variable metadata unavailable; requesting configured variables anyway.")
        return requested
    available: list[str] = []
    missing_required: list[str] = []
    for var in requested:
        if var in metadata:
            available.append(var)
            continue
        if var.endswith("M") and var[:-1] + "E" in metadata:
            available.append(var)
            continue
        if var.endswith("PM") and var[:-2] + "PE" in metadata:
            available.append(var)
            continue
        missing_required.append(var)
    if missing_required:
        notes.append(f"{source_name}: skipped {len(missing_required)} unavailable required variables: " + ", ".join(missing_required[:20]) + ("..." if len(missing_required) > 20 else ""))
    return available


def is_moe_var(var: str) -> bool:
    return var.endswith("M") or var.endswith("PM")


def census_request(config: RunConfig, year: int, dataset_suffix: str, variables: list[str], geography: str, errors: list[str], notes: list[str]) -> Optional[dict[str, Any]]:
    url = census_base_url(year, dataset_suffix)
    merged: dict[str, Any] = {}
    if not variables:
        return None
    for var_chunk in chunked(variables, CENSUS_MAX_GET_VARS):
        params: dict[str, Any] = {"get": ",".join(["NAME"] + var_chunk)}
        if geography == "county":
            params["for"] = f"county:{config.county_fips}"
            params["in"] = f"state:{config.state_fips}"
        elif geography == "state":
            params["for"] = f"state:{config.state_fips}"
        elif geography == "us":
            params["for"] = "us:1"
        else:
            raise ValueError(f"Unsupported geography: {geography}")
        if config.census_api_key:
            params["key"] = config.census_api_key
        data = cached_get_json("census_acs", url, params=params, errors=None, record_error=False)
        if not isinstance(data, list) or len(data) < 2:
            if any(is_moe_var(v) for v in var_chunk):
                retry_chunk = [v for v in var_chunk if not is_moe_var(v)]
                if retry_chunk:
                    retry_params = dict(params)
                    retry_params["get"] = ",".join(["NAME"] + retry_chunk)
                    retry_data = cached_get_json("census_acs", url, params=retry_params, errors=None, record_error=False)
                    if isinstance(retry_data, list) and len(retry_data) >= 2:
                        merged.update(dict(zip(retry_data[0], retry_data[1])))
                        notes.append(f"ACS request for {url} {geography} succeeded after dropping MOE variables in one chunk.")
                        continue
            errors.append(f"ACS request returned no JSON rows for url={url}, geography={geography}, vars={','.join(var_chunk[:8])}...")
            continue
        merged.update(dict(zip(data[0], data[1])))
    return merged if merged else None


def validate_census_key(config: RunConfig, notes: list[str]) -> bool:
    if not config.census_api_key:
        notes.append("Census API key was not provided. ACS data requests were skipped.")
        return False
    test_years = detect_available_acs_years(config, notes, count=1)
    if not test_years:
        notes.append("No usable ACS year found for Census key validation.")
        return False
    url = census_base_url(test_years[-1], "")
    params = {"get": "NAME", "for": "us:1", "key": config.census_api_key}
    data = cached_get_json("census_key_check", url, params=params, errors=None, record_error=False, max_retries=1)
    if isinstance(data, list) and len(data) >= 2:
        return True
    notes.append("Census API key was missing, invalid, or rejected. ACS data requests were skipped.")
    return False


def extract_acs(config: RunConfig, errors: list[str], notes: list[str]) -> list[Observation]:
    log_step("Starting ACS extraction")
    observations: list[Observation] = []
    if not validate_census_key(config, notes):
        log_warn("Skipping ACS extraction because Census API key validation failed.")
        return observations
    acs_years = detect_available_acs_years(config, notes, count=max(1, config.annual_years))
    if not acs_years:
        return observations
    latest_year = max(acs_years)
    source_latest = str(latest_year)
    detailed_meta = get_census_variables(latest_year, "", errors=errors, record_error=True)
    profile_meta = get_census_variables(latest_year, "profile", errors=errors, record_error=True)
    subject_meta = get_census_variables(latest_year, "subject", errors=errors, record_error=True)
    detailed_vars = [
        "B01001_001E", "B01001_001M", "B09001_001E", "B09001_001M", "B03003_003E", "B03003_003M", "B02001_004E", "B02001_004M", "B21001_001E", "B21001_001M", "B21001_002E", "B21001_002M", "C16001_001E", "C16001_001M", "C16001_005E", "C16001_005M", "B08201_001E", "B08201_001M", "B08201_002E", "B08201_002M", "B22010_001E", "B22010_001M", "B22010_002E", "B22010_002M", "B25070_001E", "B25070_001M", "B25070_007E", "B25070_007M", "B25070_008E", "B25070_008M", "B25070_009E", "B25070_009M", "B25070_010E", "B25070_010M", "B25077_001E", "B25077_001M", "B19013_001E", "B19013_001M", "B01001_020E", "B01001_020M", "B01001_021E", "B01001_021M", "B01001_022E", "B01001_022M", "B01001_023E", "B01001_023M", "B01001_024E", "B01001_024M", "B01001_025E", "B01001_025M", "B01001_044E", "B01001_044M", "B01001_045E", "B01001_045M", "B01001_046E", "B01001_046M", "B01001_047E", "B01001_047M", "B01001_048E", "B01001_048M", "B01001_049E", "B01001_049M"
    ]
    profile_vars_latest = ["DP03_0128PE", "DP03_0128PM", "DP03_0098PE", "DP03_0098PM"]
    subject_vars = ["S1810_C02_001E", "S1810_C02_001M", "S1810_C03_001E", "S1810_C03_001M"]
    detailed_vars = filter_available_vars(detailed_vars, detailed_meta, "ACS detailed", notes)
    profile_vars_latest = filter_available_vars(profile_vars_latest, profile_meta, "ACS profile", notes)
    subject_vars = filter_available_vars(subject_vars, subject_meta, "ACS subject", notes)
    for geo, (geo_type, st, co) in selected_geographies(config).items():
        rec = census_request(config, latest_year, "", detailed_vars, geo, errors, notes)
        if rec:
            geo_name = rec.get("NAME", geo)
            endpoint = census_base_url(latest_year, "")
            total = safe_float(rec.get("B01001_001E"))
            add_count_and_percent_observations(observations, count_indicator_id="POP_TOTAL", pct_indicator_id=None, numerator=total, numerator_moe=safe_float(rec.get("B01001_001M")), denominator=None, geography_name=geo_name, geography_type=geo_type, state_fips=st, county_fips=co, year=latest_year, source_latest=source_latest, source_variable_label=label_for_vars(detailed_meta, ["B01001_001E", "B01001_001M"]), source_url_or_endpoint=endpoint, note="Most recent usable ACS 5-year detailed table. ACS special codes converted to null.")
            under18 = safe_float(rec.get("B09001_001E"))
            add_count_and_percent_observations(observations, count_indicator_id="POP_AGE_UNDER_18_COUNT", pct_indicator_id="POP_AGE_UNDER_18_PCT", numerator=under18, numerator_moe=safe_float(rec.get("B09001_001M")), denominator=total, geography_name=geo_name, geography_type=geo_type, state_fips=st, county_fips=co, year=latest_year, source_latest=source_latest, source_variable_label=label_for_vars(detailed_meta, ["B09001_001E", "B01001_001E"]), source_url_or_endpoint=endpoint, note="Under-18 count and percentage from ACS.")
            age65_prefixes = ["B01001_020", "B01001_021", "B01001_022", "B01001_023", "B01001_024", "B01001_025", "B01001_044", "B01001_045", "B01001_046", "B01001_047", "B01001_048", "B01001_049"]
            age65_count = sum_values([safe_float(rec.get(f"{v}E")) for v in age65_prefixes])
            age65_moe = sum_moes([safe_float(rec.get(f"{v}M")) for v in age65_prefixes])
            add_count_and_percent_observations(observations, count_indicator_id="POP_AGE_65_PLUS_COUNT", pct_indicator_id="POP_AGE_65_PLUS_PCT", numerator=age65_count, numerator_moe=age65_moe, denominator=total, geography_name=geo_name, geography_type=geo_type, state_fips=st, county_fips=co, year=latest_year, source_latest=source_latest, source_variable_label=label_for_vars(detailed_meta, [f"{v}E" for v in age65_prefixes] + ["B01001_001E"]), source_url_or_endpoint=endpoint, note="65+ count calculated from ACS B01001 age/sex cells. Sum MOE approximated.")
            specs = [("HISPANIC_LATINX_COUNT", "HISPANIC_LATINX_PCT", "B03003_003E", "B03003_003M", total, ["B03003_003E", "B01001_001E"], "Hispanic/Latinx uses ACS B03003."), ("AIAN_POPULATION_COUNT", "AIAN_POPULATION_PCT", "B02001_004E", "B02001_004M", total, ["B02001_004E", "B01001_001E"], "AIAN-alone uses ACS B02001."), ("VETERAN_POPULATION_COUNT", "VETERAN_POPULATION_PCT", "B21001_002E", "B21001_002M", safe_float(rec.get("B21001_001E")), ["B21001_002E", "B21001_001E"], "Veteran percentage uses ACS B21001 civilian population age 18+ denominator."), ("LANGUAGE_LEP_SPANISH_COUNT", "LANGUAGE_LEP_SPANISH_PCT", "C16001_005E", "C16001_005M", safe_float(rec.get("C16001_001E")), ["C16001_005E", "C16001_001E"], "Spanish LEP uses ACS C16001 population age 5+ table."), ("NO_VEHICLE_HOUSEHOLDS_COUNT", "NO_VEHICLE_HOUSEHOLDS_PCT", "B08201_002E", "B08201_002M", safe_float(rec.get("B08201_001E")), ["B08201_002E", "B08201_001E"], "No-vehicle percentage uses ACS B08201 household denominator."), ("SNAP_HOUSEHOLDS_COUNT", "SNAP_HOUSEHOLDS_PCT", "B22010_002E", "B22010_002M", safe_float(rec.get("B22010_001E")), ["B22010_002E", "B22010_001E"], "SNAP percentage uses ACS B22010 household denominator.")]
            for count_id, pct_id, num_var, moe_var, denom, label_vars, note in specs:
                numerator = safe_float(rec.get(num_var))
                add_count_and_percent_observations(observations, count_indicator_id=count_id, pct_indicator_id=pct_id, numerator=numerator, numerator_moe=safe_float(rec.get(moe_var)), denominator=denom, geography_name=geo_name, geography_type=geo_type, state_fips=st, county_fips=co, year=latest_year, source_latest=source_latest, source_variable_label=label_for_vars(detailed_meta, label_vars), source_url_or_endpoint=endpoint, note=note)
            rent_prefixes = ["B25070_007", "B25070_008", "B25070_009", "B25070_010"]
            rent_burdened = sum_values([safe_float(rec.get(f"{v}E")) for v in rent_prefixes])
            rent_moe = sum_moes([safe_float(rec.get(f"{v}M")) for v in rent_prefixes])
            renter_denominator = safe_float(rec.get("B25070_001E"))
            add_count_and_percent_observations(observations, count_indicator_id="RENT_BURDENED_HOUSEHOLDS_COUNT", pct_indicator_id="RENT_BURDENED_HOUSEHOLDS_PCT", numerator=rent_burdened, numerator_moe=rent_moe, denominator=renter_denominator, geography_name=geo_name, geography_type=geo_type, state_fips=st, county_fips=co, year=latest_year, source_latest=source_latest, source_variable_label=label_for_vars(detailed_meta, [f"{v}E" for v in rent_prefixes] + ["B25070_001E"]), source_url_or_endpoint=endpoint, note="Rent-burdened households calculated from ACS B25070 categories for 30% or more of income. Sum MOE approximated.")
            for indicator_id, estimate_var, moe_var, strat in [("MEDIAN_HOME_VALUE", "B25077_001E", "B25077_001M", "Owner-occupied housing units"), ("MEDIAN_HOUSEHOLD_INCOME", "B19013_001E", "B19013_001M", "Households")]:
                observations.append(make_observation(indicator_id=indicator_id, geography_name=geo_name, geography_type=geo_type, state_fips=st, county_fips=co, year_or_period=str(latest_year), source_latest_year_or_period=source_latest, estimate=safe_float(rec.get(estimate_var)), moe=safe_float(rec.get(moe_var)), numerator=None, denominator=None, source_variable_label=label_for_vars(detailed_meta, [estimate_var, moe_var]), stratification=strat, comparison_available="Yes", data_quality_note="Most recent usable ACS 5-year detailed table.", source_url_or_endpoint=endpoint))
        profile_rec = census_request(config, latest_year, "profile", profile_vars_latest, geo, errors, notes)
        if profile_rec:
            geo_name = profile_rec.get("NAME", geo)
            endpoint = census_base_url(latest_year, "profile")
            for indicator_id, estimate_var, moe_var in [("POVERTY_RATE", "DP03_0128PE", "DP03_0128PM"), ("PUBLIC_COVERAGE_RATE", "DP03_0098PE", "DP03_0098PM")]:
                observations.append(make_observation(indicator_id=indicator_id, geography_name=geo_name, geography_type=geo_type, state_fips=st, county_fips=co, year_or_period=str(latest_year), source_latest_year_or_period=source_latest, estimate=safe_float(profile_rec.get(estimate_var)), moe=safe_float(profile_rec.get(moe_var)), numerator=None, denominator=None, source_variable_label=label_for_vars(profile_meta, [estimate_var, moe_var]), stratification="Total", comparison_available="Yes", data_quality_note="Most recent usable ACS 5-year profile table.", source_url_or_endpoint=endpoint))
        subject_rec = census_request(config, latest_year, "subject", subject_vars, geo, errors, notes)
        if subject_rec:
            geo_name = subject_rec.get("NAME", geo)
            endpoint = census_base_url(latest_year, "subject")
            for indicator_id, estimate_var, moe_var in [("DISABILITY_COUNT", "S1810_C02_001E", "S1810_C02_001M"), ("DISABILITY_PREVALENCE", "S1810_C03_001E", "S1810_C03_001M")]:
                estimate = safe_float(subject_rec.get(estimate_var))
                observations.append(make_observation(indicator_id=indicator_id, geography_name=geo_name, geography_type=geo_type, state_fips=st, county_fips=co, year_or_period=str(latest_year), source_latest_year_or_period=source_latest, estimate=estimate, moe=safe_float(subject_rec.get(moe_var)), numerator=estimate if indicator_id == "DISABILITY_COUNT" else None, denominator=None, source_variable_label=label_for_vars(subject_meta, [estimate_var, moe_var]), stratification="Civilian noninstitutionalized population", comparison_available="Yes", data_quality_note="Most recent usable ACS 5-year subject table.", source_url_or_endpoint=endpoint))
    observations.extend(extract_acs_uninsured_annual_series(config, acs_years, errors, notes))
    observations.extend(extract_medicaid_coverage_acs(config, latest_year, errors, notes))
    log_info(f"ACS extraction produced {len(observations)} rows")
    return observations


def extract_acs_uninsured_annual_series(config: RunConfig, acs_years: list[int], errors: list[str], notes: list[str]) -> list[Observation]:
    log_step("Pulling ACS uninsured-rate annual series")
    observations: list[Observation] = []
    years = sorted(acs_years)[-config.annual_years:]
    if not years:
        return observations
    for geo, (geo_type, st, co) in selected_geographies(config).items():
        for year in years:
            meta = get_census_variables(year, "profile", errors=errors, record_error=True)
            vars_needed = filter_available_vars(["DP03_0099PE", "DP03_0099PM"], meta, f"ACS profile uninsured {year}", notes)
            rec = census_request(config, year, "profile", vars_needed, geo, errors, notes)
            if not rec:
                continue
            geo_name = rec.get("NAME", geo)
            observations.append(make_observation(indicator_id="UNINSURED_RATE", geography_name=geo_name, geography_type=geo_type, state_fips=st, county_fips=co, year_or_period=str(year), source_latest_year_or_period=str(max(years)), estimate=safe_float(rec.get("DP03_0099PE")), moe=safe_float(rec.get("DP03_0099PM")), numerator=None, denominator=None, source_variable_label=label_for_vars(meta, ["DP03_0099PE", "DP03_0099PM"]), stratification=f"{config.annual_years}-year ACS annual series", comparison_available="Yes", data_quality_note="ACS 5-year uninsured-rate annual series. Consecutive ACS 5-year estimates overlap, so trends should be interpreted cautiously.", source_url_or_endpoint=census_base_url(year, "profile")))
    return observations


def extract_medicaid_coverage_acs(config: RunConfig, latest_year: int, errors: list[str], notes: list[str]) -> list[Observation]:
    log_step("Pulling ACS Medicaid/means-tested public coverage rate")
    observations: list[Observation] = []
    meta = get_census_variables(latest_year, "", errors=errors, record_error=True)
    if not meta:
        return observations
    candidate_vars = []
    for var, details in meta.items():
        if not var.startswith("C27007_") or not var.endswith("E"):
            continue
        label = str(details.get("label", "")).lower()
        if "with medicaid/means-tested public coverage" in label:
            candidate_vars.append(var)
    vars_needed = ["C27007_001E", "C27007_001M"] + candidate_vars + [v[:-1] + "M" for v in candidate_vars]
    vars_needed = filter_available_vars(vars_needed, meta, "ACS Medicaid C27007", notes)
    for geo, (geo_type, st, co) in selected_geographies(config).items():
        rec = census_request(config, latest_year, "", vars_needed, geo, errors, notes)
        if not rec:
            continue
        geo_name = rec.get("NAME", geo)
        denom = safe_float(rec.get("C27007_001E"))
        num_vars = [v for v in candidate_vars if v in rec]
        numerator = sum_values([safe_float(rec.get(v)) for v in num_vars])
        numerator_moe = sum_moes([safe_float(rec.get(v[:-1] + "M")) for v in num_vars])
        observations.append(make_observation(indicator_id="MEDICAID_COVERAGE_RATE", geography_name=geo_name, geography_type=geo_type, state_fips=st, county_fips=co, year_or_period=str(latest_year), source_latest_year_or_period=str(latest_year), estimate=calc_percent(numerator, denom), moe=None, numerator=numerator, denominator=denom, source_variable_label=label_for_vars(meta, ["C27007_001E"] + num_vars), stratification="Total", comparison_available="Yes", data_quality_note="Calculated from ACS C27007 Medicaid/means-tested public coverage count over civilian noninstitutionalized population. MOE for summed numerator is approximated and rate MOE is not calculated.", source_url_or_endpoint=census_base_url(latest_year, "")))
    return observations


# --------------------------------------------------------------------------------------
# BLS LAUS annual series only
# --------------------------------------------------------------------------------------

def bls_laus_county_series_id(config: RunConfig) -> str:
    return f"LAUCN{config.state_fips}{config.county_fips}0000000003"


def bls_month_num(row: dict[str, Any]) -> int:
    period = str(row.get("period", "M00"))
    if not period.startswith("M"):
        return 0
    return safe_int(period.replace("M", "")) or 0


def annualize_bls_rows(rows: list[dict[str, Any]], target_years: list[int]) -> dict[int, tuple[Optional[float], str]]:
    annual: dict[int, tuple[Optional[float], str]] = {}
    for year in target_years:
        m13 = [r for r in rows if str(r.get("year")) == str(year) and r.get("period") == "M13" and safe_float(r.get("value")) is not None]
        if m13:
            annual[year] = (safe_float(m13[0].get("value")), "Annual average M13")
            continue
        month_rows = [r for r in rows if str(r.get("year")) == str(year) and isinstance(r.get("period"), str) and str(r.get("period")).startswith("M") and r.get("period") != "M13" and 1 <= bls_month_num(r) <= 12 and safe_float(r.get("value")) is not None]
        months = {bls_month_num(r) for r in month_rows}
        if set(range(1, 13)).issubset(months):
            values: list[float] = []
            for r in month_rows:
                value = safe_float(r.get("value"))
                if value is not None:
                    values.append(value)
            annual[year] = (average(values), "Calculated annual average from 12 monthly BLS rows")
        else:
            annual[year] = (None, "Annual average unavailable and complete monthly set unavailable")
    return annual


def extract_bls_laus(config: RunConfig, errors: list[str]) -> list[Observation]:
    log_step("Starting BLS LAUS annual-series extraction")
    series_id = bls_laus_county_series_id(config)
    url = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
    end_year = config.bls_target_year
    start_year = end_year - config.annual_years + 1
    body = {"seriesid": [series_id], "startyear": str(start_year), "endyear": str(end_year)}
    data = cached_post_json("bls_laus", url, body, errors)
    observations: list[Observation] = []
    try:
        raw_rows = data["Results"]["series"][0]["data"]  # type: ignore[index]
    except Exception:
        errors.append(f"BLS LAUS response did not include expected data rows for {series_id}.")
        return observations
    rows = [r for r in raw_rows if isinstance(r, dict)]
    years = list(range(start_year, end_year + 1))
    annual = annualize_bls_rows(rows, years)
    for year in years:
        value, method = annual.get(year, (None, "Unavailable"))
        observations.append(make_observation(indicator_id="UNEMPLOYMENT_RATE", geography_name=config.service_area_name, geography_type="County", state_fips=config.state_fips, county_fips=config.county_fips, year_or_period=str(year), source_latest_year_or_period=str(end_year), estimate=value, moe=None, numerator=None, denominator=None, source_variable_label=f"{series_id}: LAUS county unemployment rate, {method}.", stratification=f"{config.annual_years}-year annual series", comparison_available="No", data_quality_note=f"{method}. Validate inferred county LAUS series ID before publication.", source_url_or_endpoint=url))
    return observations


# --------------------------------------------------------------------------------------
# CDC PLACES and CDC MIVO mortality
# --------------------------------------------------------------------------------------

CDC_PLACES_MEASURES = {
    "FREQUENT_MENTAL_DISTRESS": ["frequent mental distress", "mental health not good"],
    "DEPRESSION_PREVALENCE": ["depression"],
    "BINGE_DRINKING": ["binge drinking"],
    "CURRENT_SMOKING": ["current smoking", "smoking"],
}


def cdc_row_year(row: dict[str, Any]) -> int:
    for key in ["year", "yearend", "data_value_year", "release_year"]:
        val = safe_int(row.get(key))
        if val is not None:
            return val
    return 0


def cdc_row_estimate(row: dict[str, Any]) -> Optional[float]:
    for key in ["data_value", "datavalue", "estimate", "prevalence", "value"]:
        if key in row:
            return safe_float(row.get(key))
    return None


def normalize_unit(unit: Optional[Any]) -> tuple[str, str, str]:
    if unit is None:
        return "percent", "Assumed percent because this indicator is a prevalence measure.", "Fallback based on indicator/source definition."
    unit_str = str(unit).strip()
    if unit_str in {"%", "percent", "Percent", "PERCENT"}:
        return "percent", "Percent of relevant adult population.", "CDC PLACES API metadata, normalized from source unit."
    return unit_str, f"Unit provided by source row: {unit_str}.", "Source API metadata."


def cdc_row_units(row: dict[str, Any]) -> tuple[str, str, str]:
    return normalize_unit(row.get("data_value_unit") or row.get("data_value_units") or row.get("unit"))


def cdc_row_label(row: dict[str, Any]) -> str:
    fields = []
    for key in ["measure", "measureid", "category", "short_question_text", "data_value_type", "data_value_footnote"]:
        if row.get(key):
            fields.append(f"{key}={row.get(key)}")
    return "; ".join(fields) if fields else "CDC PLACES row; measure label unavailable."


def extract_cdc_places(config: RunConfig, errors: list[str]) -> list[Observation]:
    log_step("Starting CDC PLACES extraction")
    observations: list[Observation] = []
    county_fips_full = f"{config.state_fips}{config.county_fips}"
    for endpoint in CDC_PLACES_ENDPOINTS:
        params = {"$limit": 50000, "$where": f"locationid='{county_fips_full}' OR locationid='{int(county_fips_full)}'"}
        data = cached_get_json("cdc_places", endpoint, params, errors=None, record_error=False)
        if not isinstance(data, list) or not data:
            continue
        for indicator_id, tokens in CDC_PLACES_MEASURES.items():
            matches = [row for row in data if any(token in json.dumps(row).lower() for token in tokens)]
            if not matches:
                continue
            matches.sort(key=cdc_row_year, reverse=True)
            latest = matches[0]
            latest_year = str(cdc_row_year(latest) or latest.get("yearend") or latest.get("year") or "latest returned")
            units, unit_definition, unit_source = cdc_row_units(latest)
            observations.append(make_observation(indicator_id=indicator_id, geography_name=latest.get("locationname", config.county_name), geography_type=latest.get("geographiclevel", "County"), state_fips=config.state_fips, county_fips=config.county_fips, year_or_period=latest_year, source_latest_year_or_period=latest_year, estimate=cdc_row_estimate(latest), moe=None, numerator=None, denominator=None, source_variable_label=cdc_row_label(latest), stratification=latest.get("measure", definition(indicator_id).indicator_name), comparison_available="Partial", data_quality_note="Most recent matching CDC PLACES county row returned from tried Socrata endpoint; modeled estimate; validate endpoint/release before final reporting.", source_url_or_endpoint=endpoint, units_override=units, unit_definition_override=unit_definition, unit_source_override=unit_source))
        if any(o.source_url_or_endpoint == endpoint for o in observations):
            return observations
    errors.append("CDC PLACES returned no usable records from candidate endpoints. Update endpoint ID or use current PLACES download.")
    return observations


def parse_cdc_count(value: Any) -> tuple[Optional[float], str]:
    if value is None:
        return None, "Count unavailable."
    s = str(value).strip()
    if s == "1-9":
        return None, "Count suppressed as 1-9."
    f = safe_float(s)
    return f, "Count numeric." if f is not None else "Count unavailable or suppressed."


def cdc_mivo_annual_years(rows: list[dict[str, Any]], intent: str, annual_years: int) -> tuple[list[int], str]:
    intent_years = sorted(
        {
            year
            for r in rows
            if str(r.get("intent")) == intent
            for year in [safe_int(r.get("period"))]
            if year is not None
        }
    )
    if not intent_years:
        return [], "not pulled"
    latest_year = intent_years[-1]
    start_year = latest_year - annual_years + 1
    return list(range(start_year, latest_year + 1)), str(latest_year)


def add_cdc_mortality_annual_series(observations: list[Observation], *, config: RunConfig, rows: list[dict[str, Any]], intent: str, rate_indicator: str, count_indicator: str, intent_label: str) -> None:
    years, latest_year = cdc_mivo_annual_years(rows, intent, config.annual_years)
    if not years:
        return
    rows_by_year = {
        year: row
        for row in rows
        if str(row.get("intent")) == intent
        for year in [safe_int(row.get("period"))]
        if year is not None
    }
    for year in years:
        row = rows_by_year.get(year)
        if row:
            rate = safe_float(row.get("rate"))
            count, count_note = parse_cdc_count(row.get("count_sup"))
            modeled = safe_float(row.get("rate_m"))
            modeled_note = "Rate modeled by CDC." if modeled == 1 else "Rate not flagged as modeled."
            geography_name = f"{row.get('name', config.county_name)}, {row.get('st_name', config.state_abbr)}"
            source_variable_context = f"intent={intent}; period={year}"
            common_note = f"{intent_label}. {modeled_note} {count_note}"
        else:
            rate = None
            count = None
            geography_name = config.service_area_name
            source_variable_context = f"intent={intent}; period={year}; annual row not returned by CDC MIVO County query"
            common_note = f"{intent_label}. No annual CDC MIVO County row was returned for this year; blank row emitted to preserve the {config.annual_years}-year series."
        observations.append(make_observation(indicator_id=rate_indicator, geography_name=geography_name, geography_type="County", state_fips=config.state_fips, county_fips=config.county_fips, year_or_period=str(year), source_latest_year_or_period=latest_year, estimate=rate, moe=None, numerator=count, denominator=None, source_variable_label=f"{source_variable_context}; rate=rate; count=count_sup", stratification=f"{config.annual_years}-year annual series", comparison_available="No", data_quality_note=common_note, source_url_or_endpoint=CDC_MORTALITY_COUNTY_ENDPOINT))
        observations.append(make_observation(indicator_id=count_indicator, geography_name=geography_name, geography_type="County", state_fips=config.state_fips, county_fips=config.county_fips, year_or_period=str(year), source_latest_year_or_period=latest_year, estimate=count, moe=None, numerator=count, denominator=None, source_variable_label=f"{source_variable_context}; count=count_sup", stratification=f"{config.annual_years}-year annual series", comparison_available="No", data_quality_note=common_note, source_url_or_endpoint=CDC_MORTALITY_COUNTY_ENDPOINT))


def extract_cdc_mortality(config: RunConfig, errors: list[str]) -> list[Observation]:
    log_step("Starting CDC suicide/overdose mortality extraction")
    observations: list[Observation] = []
    county_fips_full = f"{config.state_fips}{config.county_fips}"
    params = {"$limit": max(500, (config.annual_years * 4) + 10), "$where": f"geoid='{county_fips_full}' AND intent in('All_Suicide','Drug_OD')", "$order": "period DESC"}
    data = cached_get_json("cdc_mivo_county", CDC_MORTALITY_COUNTY_ENDPOINT, params=params, errors=errors)
    if not isinstance(data, list) or not data:
        errors.append(f"CDC MIVO County returned no rows for county FIPS {county_fips_full}.")
        return observations
    add_cdc_mortality_annual_series(observations, config=config, rows=data, intent="All_Suicide", rate_indicator="SUICIDE_MORTALITY", count_indicator="SUICIDE_DEATHS_COUNT", intent_label="All-method suicide mortality from CDC MIVO County")
    add_cdc_mortality_annual_series(observations, config=config, rows=data, intent="Drug_OD", rate_indicator="OVERDOSE_MORTALITY", count_indicator="OVERDOSE_DEATHS_COUNT", intent_label="Drug overdose mortality from CDC MIVO County")
    return observations


# --------------------------------------------------------------------------------------
# HUD PIT homelessness annual series: always emits rows
# --------------------------------------------------------------------------------------

def hud_year_range(config: RunConfig) -> list[int]:
    return list(range(config.hud_year - config.annual_years + 1, config.hud_year + 1))


def find_hud_pit_links(config: RunConfig, errors: list[str], notes: list[str]) -> list[str]:
    if config.hud_pit_url:
        return [config.hud_pit_url]
    html = cached_get_text("hud_ahar", HUD_AHAR_2025_PAGE, errors=errors, record_error=False)
    if not html:
        notes.append("Could not read HUD AHAR page to discover PIT file links.")
        return []
    links = re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.IGNORECASE)
    abs_links = [urljoin(HUD_AHAR_2025_PAGE, link) for link in links]
    desired = "coc" if config.hud_coc_codes else "state"
    candidates = []
    for link in abs_links:
        lower = link.lower()
        if not any(ext in lower for ext in [".xlsx", ".xlsb", ".csv"]):
            continue
        if "point-in-time" not in lower and "pit" not in lower and "estimates" not in lower:
            continue
        if desired == "coc" and "coc" in lower:
            candidates.append(link)
        if desired == "state" and "state" in lower:
            candidates.append(link)
    if desired == "coc":
        # Add state file as fallback.
        for link in abs_links:
            lower = link.lower()
            if any(ext in lower for ext in [".xlsx", ".xlsb", ".csv"]) and "state" in lower and ("point" in lower or "pit" in lower or "estimates" in lower):
                candidates.append(link)
    return candidates[:4]


def row_matches_hud_geo(row: pd.Series, config: RunConfig, state_fallback: bool = False) -> bool:
    row_text = " ".join([str(v) for v in row.values if pd.notna(v)]).lower()
    if config.hud_coc_codes and not state_fallback:
        return any(code.lower() in row_text for code in config.hud_coc_codes)
    state_name = state_name_from_fips(config.state_fips).lower()
    state_abbr = config.state_abbr.lower()
    return state_name in row_text or re.search(rf"\b{re.escape(state_abbr)}\b", row_text) is not None


def find_hud_year_columns(df: pd.DataFrame, year: int) -> dict[str, Optional[Any]]:
    cols = list(df.columns)
    normed = {col: norm_col(col) for col in cols}
    y = str(year)

    result: dict[str, Optional[Any]] = {
        "HOMELESSNESS_PIT": None,
        "HOMELESSNESS_PIT_SHELTERED": None,
        "HOMELESSNESS_PIT_UNSHELTERED": None,
    }

    for col, n in normed.items():
        if y not in n:
            continue

        is_unsheltered = "unsheltered" in n and "homeless" in n
        is_sheltered = "sheltered" in n and "unsheltered" not in n and "homeless" in n
        is_total = (
            ("overall" in n and "homeless" in n)
            or "total_homeless" in n
            or ("homeless" in n and "sheltered" not in n and "unsheltered" not in n)
        )

        if result["HOMELESSNESS_PIT_UNSHELTERED"] is None and is_unsheltered:
            result["HOMELESSNESS_PIT_UNSHELTERED"] = col
        elif result["HOMELESSNESS_PIT_SHELTERED"] is None and is_sheltered:
            result["HOMELESSNESS_PIT_SHELTERED"] = col
        elif result["HOMELESSNESS_PIT"] is None and is_total:
            result["HOMELESSNESS_PIT"] = col

    return result


def parse_hud_frames(frames: list[pd.DataFrame], config: RunConfig, source_url: str, state_fallback: bool, notes: list[str]) -> list[Observation]:
    years = hud_year_range(config)
    values: dict[tuple[str, int], list[float]] = {(indicator, year): [] for indicator in ["HOMELESSNESS_PIT", "HOMELESSNESS_PIT_SHELTERED", "HOMELESSNESS_PIT_UNSHELTERED"] for year in years}
    contexts: dict[tuple[str, int], str] = {}
    for df in frames:
        if df.empty:
            continue
        matched = df[df.apply(lambda row: row_matches_hud_geo(row, config, state_fallback=state_fallback), axis=1)]
        if matched.empty:
            continue
        for year in years:
            col_map = find_hud_year_columns(df, year)
            for indicator, col in col_map.items():
                if col is None:
                    continue
                vals = [safe_float(v) for v in matched[col].tolist()]
                nums = [v for v in vals if v is not None]
                if nums:
                    values[(indicator, year)].extend(nums)
                    contexts[(indicator, year)] = f"column={col}; rows={len(nums)}"
    out: list[Observation] = []
    geo_type = "State fallback" if state_fallback or not config.hud_coc_codes else "CoC aggregate"
    geo_name = state_name_from_fips(config.state_fips) if state_fallback or not config.hud_coc_codes else ", ".join(config.hud_coc_codes)
    for year in years:
        for indicator in ["HOMELESSNESS_PIT", "HOMELESSNESS_PIT_SHELTERED", "HOMELESSNESS_PIT_UNSHELTERED"]:
            nums = values.get((indicator, year), [])
            estimate = sum(nums) if nums else None
            note = "HUD PIT annual series. "
            if estimate is None:
                note += "No matching HUD value parsed for this year; row emitted so HUD series is complete."
            elif config.hud_coc_codes and not state_fallback:
                note += "Values summed across provided CoC codes."
            else:
                note += "State-level fallback used."
            out.append(make_observation(indicator_id=indicator, geography_name=geo_name or config.service_area_name, geography_type=geo_type, state_fips=config.state_fips, county_fips=config.county_fips if config.hud_coc_codes and not state_fallback else "", year_or_period=str(year), source_latest_year_or_period=str(config.hud_year), estimate=estimate, moe=None, numerator=estimate, denominator=None, source_variable_label=contexts.get((indicator, year), f"HUD PIT {year}"), stratification=f"{config.annual_years}-year HUD PIT annual series", comparison_available="Partial", data_quality_note=note, source_url_or_endpoint=source_url))
    return out


def extract_hud_homelessness(config: RunConfig, errors: list[str], notes: list[str]) -> list[Observation]:
    log_step("Starting HUD PIT homelessness annual-series extraction")
    frames: list[pd.DataFrame] = []
    source = HUD_AHAR_2025_PAGE
    if config.hud_pit_file:
        path = Path(config.hud_pit_file)
        if path.exists():
            frames = read_any_table_file(path, errors, notes)
            source = str(path)
        else:
            errors.append(f"HUD PIT file not found: {config.hud_pit_file}")
    else:
        for link in find_hud_pit_links(config, errors, notes):
            filename_hint = Path(link.split("?")[0]).name or None
            path = download_file("hud_pit", link, errors, filename_hint=filename_hint)
            if path:
                frames = read_any_table_file(path, errors, notes)
                source = link
                if frames:
                    break
    observations: list[Observation] = []
    if frames:
        observations = parse_hud_frames(frames, config, source, state_fallback=False, notes=notes)
        if config.hud_coc_codes and all(o.estimate is None for o in observations):
            notes.append("HUD CoC parsing produced no numeric values; retrying with state fallback.")
            observations = parse_hud_frames(frames, config, source, state_fallback=True, notes=notes)
    if not observations:
        # Always emit the annual HUD rows even if download/parse fails.
        for year in hud_year_range(config):
            for indicator in ["HOMELESSNESS_PIT", "HOMELESSNESS_PIT_SHELTERED", "HOMELESSNESS_PIT_UNSHELTERED"]:
                observations.append(make_observation(indicator_id=indicator, geography_name=config.service_area_name, geography_type="Service area", state_fips=config.state_fips, county_fips=config.county_fips, year_or_period=str(year), source_latest_year_or_period=str(config.hud_year), estimate=None, moe=None, numerator=None, denominator=None, source_variable_label=f"HUD PIT {year}; automated download attempted", stratification=f"{config.annual_years}-year HUD PIT annual series", comparison_available="Partial", data_quality_note="HUD PIT download or parse failed; row emitted so annual series is present. Provide --hud-pit-file for exact local parsing.", source_url_or_endpoint=source))
    return observations


# --------------------------------------------------------------------------------------
# HRSA, SAMHSA, NCES adapters
# --------------------------------------------------------------------------------------

def discover_hrsa_mental_hpsa_csv(errors: list[str], notes: list[str]) -> Optional[str]:
    html = cached_get_text("hrsa_downloads", HRSA_SHORTAGE_DOWNLOAD_PAGE, errors=errors, record_error=False)
    if not html:
        return None
    links = re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.IGNORECASE)
    abs_links = [urljoin(HRSA_SHORTAGE_DOWNLOAD_PAGE, link) for link in links]
    # Prefer the first CSV after the Mental Health section.
    idx = html.lower().find("hpsa – mental health")
    if idx == -1:
        idx = html.lower().find("hpsa - mental health")
    tail = html[idx:] if idx != -1 else html
    local_links = re.findall(r'href=["\']([^"\']+)["\']', tail, flags=re.IGNORECASE)
    for link in [urljoin(HRSA_SHORTAGE_DOWNLOAD_PAGE, l) for l in local_links] + abs_links:
        if "csv" in link.lower() and ("download" in link.lower() or "data.hrsa.gov" in link.lower()):
            return link
    return None


def extract_hrsa_hpsa(config: RunConfig, errors: list[str], notes: list[str]) -> list[Observation]:
    log_step("Starting HRSA Mental Health HPSA extraction")
    source = ""
    frames: list[pd.DataFrame] = []
    if config.hrsa_hpsa_file:
        path = Path(config.hrsa_hpsa_file)
        if path.exists():
            frames = read_any_table_file(path, errors, notes)
            source = str(path)
        else:
            errors.append(f"HRSA HPSA file not found: {config.hrsa_hpsa_file}")
    else:
        url = config.hrsa_hpsa_url or discover_hrsa_mental_hpsa_csv(errors, notes)
        if url:
            path = download_file("hrsa_hpsa", url, errors)
            if path:
                frames = read_any_table_file(path, errors, notes)
                source = url
    count = None
    max_score = None
    context = "HRSA Mental Health HPSA download attempted."
    for df in frames:
        if df.empty:
            continue
        cols = {norm_col(c): c for c in df.columns}
        county_col = find_col(df, ["county", "name"]) or find_col(df, ["county"])
        state_col = find_col(df, ["state", "name"]) or find_col(df, ["state"])
        disc_col = find_col(df, ["discipline"]) or find_col(df, ["hpsa", "discipline"])
        score_col = find_col(df, ["score"])
        status_col = find_col(df, ["status"])
        if county_col is None:
            continue
        mask = df[county_col].astype(str).str.contains(config.county_name.replace(" County", ""), case=False, na=False)
        if state_col is not None:
            mask &= df[state_col].astype(str).str.contains(state_name_from_fips(config.state_fips) or config.state_abbr, case=False, na=False)
        if disc_col is not None:
            mask &= df[disc_col].astype(str).str.contains("mental", case=False, na=False)
        if status_col is not None:
            mask &= ~df[status_col].astype(str).str.contains("withdrawn|proposed|not designated", case=False, na=False)
        matched = df[mask]
        if not matched.empty:
            count = float(len(matched))
            if score_col is not None:
                scores = [safe_float(v) for v in matched[score_col].tolist()]
                scores = [v for v in scores if v is not None]
                if scores:
                    max_score = max(scores)
            context = f"matched_rows={len(matched)}; score_column={score_col}"
            break
    note = "HRSA Mental Health HPSA CSV parsed." if count is not None else "HRSA Mental Health HPSA CSV could not be parsed automatically. Use --hrsa-hpsa-file or --hrsa-hpsa-url."
    return [make_observation(indicator_id="MENTAL_HEALTH_HPSA", geography_name=config.service_area_name, geography_type="County", state_fips=config.state_fips, county_fips=config.county_fips, year_or_period="current", source_latest_year_or_period="current", estimate=count, moe=None, numerator=count, denominator=None, source_variable_label=context + (f"; max_score={max_score}" if max_score is not None else ""), stratification="Active mental health HPSA records", comparison_available="No", data_quality_note=note, source_url_or_endpoint=source or HRSA_SHORTAGE_DOWNLOAD_PAGE)]


SAMHSA_STATE_IDS = {"AL": 19, "AK": 20, "AZ": 21, "AR": 22, "CA": 23, "CO": 24, "CT": 25, "DE": 26, "DC": 27, "FL": 28, "GA": 29, "HI": 30, "ID": 31, "IL": 32, "IN": 33, "IA": 34, "KS": 35, "KY": 36, "LA": 37, "ME": 1, "MD": 18, "MA": 3, "MI": 2, "MN": 38, "MS": 39, "MO": 40, "MT": 4, "NE": 41, "NV": 12, "NH": 42, "NJ": 13, "NM": 43, "NY": 5, "NC": 6, "ND": 44, "OH": 7, "OK": 45, "OR": 46, "PA": 8, "RI": 47, "SC": 9, "SD": 48, "TN": 10, "TX": 11, "UT": 14, "VT": 49, "VA": 50, "WA": 15, "WV": 51, "WI": 16, "WY": 52}


def extract_samhsa_facilities(config: RunConfig, errors: list[str], notes: list[str]) -> list[Observation]:
    log_step("Starting SAMHSA FindTreatment facility extraction")
    observations: list[Observation] = []
    base_params: dict[str, Any]
    if config.samhsa_lat is not None and config.samhsa_lon is not None:
        meters = int(config.samhsa_radius_miles * 1609.344)
        base_params = {"sAddr": f"{config.samhsa_lon},{config.samhsa_lat}", "limitType": 2, "limitValue": meters, "pageSize": 2000, "page": 1, "sort": 0}
    else:
        state_id = SAMHSA_STATE_IDS.get(config.state_abbr.upper())
        if state_id is None:
            errors.append(f"No SAMHSA state ID mapping for {config.state_abbr}")
            return observations
        base_params = {"limitType": 0, "limitValue": state_id, "stateCode": config.state_abbr.upper(), "pageSize": 2000, "page": 1, "sort": 0}
        notes.append("SAMHSA exact county search requires FindTreatment county ID or coordinates; using state search with county-name filtering.")
    for indicator_id, stype in [("TREATMENT_FACILITIES_MH", "mh"), ("TREATMENT_FACILITIES_SUD", "sa")]:
        params = dict(base_params)
        params["sType"] = stype
        data = cached_get_json("samhsa_findtreatment", SAMHSA_FINDTREATMENT_API, params=params, errors=None, record_error=False)
        rows = []
        if isinstance(data, dict):
            if isinstance(data.get("rows"), list):
                rows = data.get("rows", [])
            elif isinstance(data.get("facilities"), list):
                rows = data.get("facilities", [])
        elif isinstance(data, list):
            rows = data
        filtered = []
        for row in rows:
            text = json.dumps(row).lower()
            if config.samhsa_lat is not None:
                filtered.append(row)
            elif config.county_name.replace(" County", "").lower() in text and config.state_abbr.lower() in text:
                filtered.append(row)
        estimate = float(len(filtered)) if filtered else None
        note = "SAMHSA FindTreatment API used."
        if estimate is None:
            note += " No county-matching rows parsed; provide --samhsa-lat and --samhsa-lon for radius search."
        observations.append(make_observation(indicator_id=indicator_id, geography_name=config.service_area_name, geography_type="County/radius" if config.samhsa_lat else "State-filtered county text", state_fips=config.state_fips, county_fips=config.county_fips, year_or_period="current", source_latest_year_or_period="current", estimate=estimate, moe=None, numerator=estimate, denominator=None, source_variable_label=f"sType={stype}; returned_rows={len(rows)}; matched_rows={len(filtered)}", stratification="Facility count", comparison_available="No", data_quality_note=note, source_url_or_endpoint=SAMHSA_FINDTREATMENT_API))
    return observations


def extract_nces_enrollment(config: RunConfig, errors: list[str], notes: list[str]) -> list[Observation]:
    log_step("Starting NCES CCD enrollment extraction")
    observations: list[Observation] = []
    years_to_try = [config.nces_year] if config.nces_year else list(range(dt.date.today().year - 1, dt.date.today().year - 8, -1))
    for year in years_to_try:
        if year is None:
            continue
        # Education Data API CCD enrollment endpoint requires a grade specifier. grade-pk through grade-12 are summed.
        total = 0.0
        got_any = False
        for grade in ["grade-pk", "grade-kg"] + [f"grade-{i}" for i in range(1, 13)]:
            url = f"{URBAN_ED_DATA_API}/schools/ccd/enrollment/{year}/{grade}/"
            params = {"fips": config.state_fips}
            data = cached_get_json("nces_educationdata", url, params=params, errors=None, record_error=False)
            rows = data.get("results", data) if isinstance(data, dict) else data
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                text = json.dumps(row).lower()
                # Many Education Data API school rows include county_name; if unavailable, this becomes a state-level sum.
                has_county = "county" in text
                if has_county and config.county_name.replace(" County", "").lower() not in text:
                    continue
                value = None
                for key in ["enrollment", "students", "value"]:
                    if key in row:
                        value = safe_float(row.get(key))
                        break
                if value is not None:
                    total += value
                    got_any = True
        if got_any:
            observations.append(make_observation(indicator_id="SCHOOL_ENROLLMENT", geography_name=config.service_area_name, geography_type="County if county field available; otherwise state-filtered", state_fips=config.state_fips, county_fips=config.county_fips, year_or_period=str(year), source_latest_year_or_period=str(year), estimate=total, moe=None, numerator=total, denominator=None, source_variable_label=f"Education Data API CCD enrollment summed across grades for year={year}", stratification="PK-12 total enrollment", comparison_available="No", data_quality_note="Automated CCD enrollment via Education Data API. Review whether API rows included county fields; otherwise value may be state-filtered.", source_url_or_endpoint=URBAN_ED_DATA_API))
            return observations
    observations.append(make_observation(indicator_id="SCHOOL_ENROLLMENT", geography_name=config.service_area_name, geography_type="Service area", state_fips=config.state_fips, county_fips=config.county_fips, year_or_period="not pulled", source_latest_year_or_period="not pulled", estimate=None, moe=None, numerator=None, denominator=None, source_variable_label="Education Data API CCD enrollment attempted", stratification="PK-12 total enrollment", comparison_available="No", data_quality_note="Automated NCES/CCD enrollment pull did not return parsable rows. Provide a CCD file if exact county aggregation is required.", source_url_or_endpoint=URBAN_ED_DATA_API))
    return observations


# --------------------------------------------------------------------------------------
# Placeholders and outputs
# --------------------------------------------------------------------------------------

def add_internal_placeholders(config: RunConfig) -> list[Observation]:
    current = "internal file required"
    placeholder_ids = ["CLIENT_DEMOGRAPHICS", "SERVICE_UTILIZATION", "STAFFING_PLAN", "QUALITATIVE_THEMES"]
    endpoints = {pid: "Not available through government API" for pid in placeholder_ids}
    out = []
    for indicator_id in placeholder_ids:
        d = definition(indicator_id)
        out.append(make_observation(indicator_id=indicator_id, geography_name=config.service_area_name, geography_type="Service area", state_fips=config.state_fips, county_fips=config.county_fips, year_or_period=current, source_latest_year_or_period=current, estimate=None, moe=None, numerator=None, denominator=None, source_variable_label=f"{d.source_name}: {d.indicator_detailed_description}", stratification="Total", comparison_available="No", data_quality_note="Internal-only input; not available from government APIs. No estimate was fabricated.", source_url_or_endpoint=endpoints[indicator_id]))
    return out


def build_indicator_catalog() -> list[IndicatorCatalogRow]:
    rows = []
    for d in build_indicator_definitions().values():
        rows.append(IndicatorCatalogRow(indicator_id=d.indicator_id, indicator_name=d.indicator_name, needs_assessment_domain=d.needs_assessment_domain, indicator_summary=d.indicator_summary, indicator_detailed_description=d.indicator_detailed_description, units=d.units, unit_definition=d.unit_definition, unit_source=d.unit_source, source_name=d.source_name, source_agency=d.source_agency, api_or_download=d.api_or_download, expected_geography_level=d.expected_geography_level, expected_update_frequency=d.expected_update_frequency, latest_logic=d.latest_logic, government_api_available=d.government_api_available, public_download_available=d.public_download_available, internal_or_qualitative_required=d.internal_or_qualitative_required, source_citation_id=d.source_citation_id, source_citation_text=citation_text(d.source_citation_id), limitation=d.limitation, recommended_qualitative_followup_question=d.recommended_qualitative_followup_question))
    return rows


def add_missing_indicator_backfill_rows(config: RunConfig, catalog: list[IndicatorCatalogRow], observations: list[Observation]) -> list[Observation]:
    existing_ids = {o.indicator_id for o in observations}
    backfill = []
    for row in catalog:
        if row.indicator_id in existing_ids:
            continue
        d = definition(row.indicator_id)
        backfill.append(make_observation(indicator_id=d.indicator_id, geography_name=config.service_area_name, geography_type="Service area", state_fips=config.state_fips, county_fips=config.county_fips, year_or_period="not pulled this run", source_latest_year_or_period="not pulled this run", estimate=None, moe=None, numerator=None, denominator=None, source_variable_label=f"{d.source_name}: catalog indicator existed, but no observation was returned by current adapter.", stratification="Total", comparison_available="No", data_quality_note="No estimate was returned for this indicator in this run. Backfill row prevents silent data loss.", source_url_or_endpoint=d.source_name))
    return observations + backfill


def summarize_latest_by_source(observations: list[Observation]) -> dict[str, list[str]]:
    summary: dict[str, set[str]] = {}
    for o in observations:
        key = f"{o.source_name} ({o.source_agency})"
        summary.setdefault(key, set()).add(o.source_latest_year_or_period)
    return {k: sorted(v) for k, v in summary.items()}


def config_for_metadata(config: RunConfig) -> dict[str, Any]:
    payload = asdict(config)
    payload["census_api_key"] = bool(config.census_api_key)
    return payload


def write_citation_appendix() -> None:
    lines = ["# Citation Appendix", "", "| Citation ID | Label | Source | Agency/Author | Date | Reference | Notes |", "|---|---|---|---|---|---|---|"]
    for c in build_citation_registry().values():
        lines.append(f"| {c.citation_id} | {c.citation_label} | {c.source_title} | {c.source_agency_or_author} | {c.publication_date} | {c.url_or_file_reference} | {c.notes} |")
    (OUTPUT_DIR / "citation_appendix.md").write_text("\n".join(lines), encoding="utf-8")


def write_data_availability(catalog: list[IndicatorCatalogRow], observations: list[Observation]) -> None:
    by_id: dict[str, list[Observation]] = {}
    for o in observations:
        by_id.setdefault(o.indicator_id, []).append(o)
    lines = ["# Data Availability", "", "| Domain | Indicator | Summary | Units | Unit source | Source | Citation | Latest logic | Latest year/period found | Filled? | Limitation | Follow-up |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in catalog:
        obs_rows = by_id.get(r.indicator_id, [])
        latest = "; ".join(sorted({o.source_latest_year_or_period for o in obs_rows})) if obs_rows else ""
        filled = "No"
        if obs_rows:
            filled = "Yes" if any(o.estimate is not None for o in obs_rows) else "Partial"
        lines.append(f"| {r.needs_assessment_domain} | {r.indicator_name} | {r.indicator_summary} | {r.units} | {r.unit_source} | {r.source_name} | {r.source_citation_id} | {r.latest_logic} | {latest} | {filled} | {r.limitation} | {r.recommended_qualitative_followup_question} |")
    (OUTPUT_DIR / "data_availability.md").write_text("\n".join(lines), encoding="utf-8")


def write_api_errors(errors: list[str], notes: list[str]) -> None:
    lines = ["# API Errors and Data Limitations", "", "## General limitations", "", "- HUD PIT now always emits one annual row for each requested HUD year for total, sheltered, and unsheltered homelessness.", "- When HUD CoC parsing fails or no CoC codes are supplied, the script uses state-level fallback rows when possible.", "- Medicaid coverage is computed from ACS C27007 Medicaid/means-tested public coverage, not from public coverage.", "- HRSA, SAMHSA, and NCES are now automated best-effort pulls. Review parsed rows before publication.", "- Internal client, utilization, staffing, and qualitative data cannot be pulled from government APIs.", "", "## Run notes", ""]
    lines.extend([f"- {note}" for note in notes] if notes else ["No run notes captured."])
    lines.extend(["", "## Captured errors", ""])
    lines.extend([f"- {e}" for e in errors] if errors else ["No API errors captured."])
    (OUTPUT_DIR / "api_errors_and_limitations.md").write_text("\n".join(lines), encoding="utf-8")


def write_readme(config: RunConfig) -> None:
    lines = ["# Needs Assessment Data Outputs", "", f"Service area: {config.service_area_name}", f"County: {config.county_name}", f"State FIPS: {config.state_fips}", f"County FIPS: {config.county_fips}", f"Comparison geographies: {', '.join(config.comparison_geographies)}", f"Census API key provided: {bool(config.census_api_key)}", f"Annual series years: {config.annual_years}", f"HUD PIT year: {config.hud_year}", f"HUD CoC codes: {', '.join(config.hud_coc_codes) if config.hud_coc_codes else 'None; state-level HUD matching attempted'}", "", "## Key outputs", "", "- HUD PIT annual series for total, sheltered, and unsheltered homelessness.", "- CDC MIVO annual series for suicide and drug overdose death counts and mortality rates.", "- ACS Medicaid/means-tested public coverage rate.", "- HRSA mental health HPSA count/status.", "- SAMHSA MH/SUD facility counts.", "- NCES CCD enrollment best-effort.", "- Annual BLS unemployment and ACS uninsured data; no YoY percent-change rows."]
    (OUTPUT_DIR / "README.md").write_text("\n".join(lines), encoding="utf-8")


def write_outputs(config: RunConfig, catalog: list[IndicatorCatalogRow], observations: list[Observation], errors: list[str], notes: list[str]) -> None:
    ensure_dirs()
    pd.DataFrame([asdict(r) for r in catalog]).to_csv(OUTPUT_DIR / "indicator_catalog.csv", index=False)
    pd.DataFrame([asdict(o) for o in observations]).to_csv(OUTPUT_DIR / "needs_assessment_data_long.csv", index=False)
    metadata = {"retrieved_at": now_iso(), "run_config": config_for_metadata(config), "citations": [asdict(c) for c in build_citation_registry().values()], "notes": notes, "errors": errors, "source_latest_summary": summarize_latest_by_source(observations)}
    (OUTPUT_DIR / "source_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    write_citation_appendix()
    write_data_availability(catalog, observations)
    write_api_errors(errors, notes)
    write_readme(config)


# --------------------------------------------------------------------------------------
# CLI and orchestration
# --------------------------------------------------------------------------------------

def parse_args() -> RunConfig:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-fips", required=True)
    parser.add_argument("--county-fips", required=True)
    parser.add_argument("--state-abbr", required=True)
    parser.add_argument("--county-name", required=True)
    parser.add_argument("--service-area-name", required=True)
    parser.add_argument("--latest", action="store_true")
    parser.add_argument("--years", nargs="*", type=int, default=[])
    parser.add_argument("--census-api-key", default=os.getenv("CENSUS_API_KEY") or os.getenv("CENSUS_KEY"))
    parser.add_argument("--comparison-geographies", nargs="*", default=["county", "state", "us"], choices=["county", "state", "us"])
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--annual-years", type=int, default=10)
    parser.add_argument("--bls-target-year", type=int, default=2025)
    parser.add_argument("--hud-year", type=int, default=2025)
    parser.add_argument("--hud-coc-codes", nargs="*", default=[])
    parser.add_argument("--hud-pit-file", default=None)
    parser.add_argument("--hud-pit-url", default=None)
    parser.add_argument("--hrsa-hpsa-file", default=None)
    parser.add_argument("--hrsa-hpsa-url", default=None)
    parser.add_argument("--samhsa-lat", type=float, default=None)
    parser.add_argument("--samhsa-lon", type=float, default=None)
    parser.add_argument("--samhsa-radius-miles", type=float, default=50.0)
    parser.add_argument("--nces-year", type=int, default=None)
    args = parser.parse_args()
    return RunConfig(state_fips=normalize_fips(args.state_fips, 2), county_fips=normalize_fips(args.county_fips, 3), state_abbr=args.state_abbr.upper(), county_name=args.county_name, service_area_name=args.service_area_name, latest=True if args.latest or not args.years else False, years=args.years, census_api_key=args.census_api_key, comparison_geographies=args.comparison_geographies, verbose=args.verbose, annual_years=args.annual_years, bls_target_year=args.bls_target_year, hud_year=args.hud_year, hud_coc_codes=[c.upper() for c in args.hud_coc_codes], hud_pit_file=args.hud_pit_file, hud_pit_url=args.hud_pit_url, hrsa_hpsa_file=args.hrsa_hpsa_file, hrsa_hpsa_url=args.hrsa_hpsa_url, samhsa_lat=args.samhsa_lat, samhsa_lon=args.samhsa_lon, samhsa_radius_miles=args.samhsa_radius_miles, nces_year=args.nces_year)


def run(config: RunConfig) -> None:
    setup_logging(config.verbose)
    log_step("Starting needs assessment data pull")
    ensure_dirs()
    errors: list[str] = []
    notes: list[str] = []
    catalog = build_indicator_catalog()
    observations: list[Observation] = []
    observations.extend(extract_acs(config, errors, notes))
    observations.extend(extract_bls_laus(config, errors))
    observations.extend(extract_cdc_places(config, errors))
    observations.extend(extract_cdc_mortality(config, errors))
    observations.extend(extract_hud_homelessness(config, errors, notes))
    observations.extend(extract_hrsa_hpsa(config, errors, notes))
    observations.extend(extract_samhsa_facilities(config, errors, notes))
    observations.extend(extract_nces_enrollment(config, errors, notes))
    observations.extend(add_internal_placeholders(config))
    observations = add_missing_indicator_backfill_rows(config, catalog, observations)
    write_outputs(config, catalog, observations, errors, notes)
    extracted_ids = {o.indicator_id for o in observations if o.estimate is not None}
    all_ids = {r.indicator_id for r in catalog}
    print("Done.")
    print(f"Output directory: {OUTPUT_DIR.resolve()}")
    print(f"Observation rows: {len(observations)}")
    print(f"Indicators in catalog: {len(all_ids)}")
    print(f"Indicators with numeric estimates: {len(extracted_ids)}")
    print(f"Indicators without numeric estimates: {len(sorted(all_ids - extracted_ids))}")
    print(f"Run notes captured: {len(notes)}")
    print(f"Errors captured: {len(errors)}")


if __name__ == "__main__":
    run(parse_args())
