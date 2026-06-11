#!/usr/bin/env python3
"""
needs_assessment_data.py

Final single-file, citation-aware CCBHC community needs assessment data script.

Key fixes:
- Census ACS data calls now require a Census API key in this environment.
- If no Census key is provided, ACS is skipped cleanly with one run note, not dozens of errors.
- Census key is masked in logs and metadata.
- Census missing-key HTML is detected immediately and not retried repeatedly.
- ACS latest-year detection probes variables.json and avoids future-year retry spam.
- ACS URLs are built without double slashes.
- ACS requests are chunked.
- ACS special values such as -555555555 are converted to None.
- ACS MOE variables are treated as optional, avoiding false "unavailable variable" errors.
- PUBLIC_COVERAGE_RATE is separate from MEDICAID_COVERAGE_RATE.
- DISABILITY_COUNT is separate from DISABILITY_PREVALENCE.
- BLS LAUS prefers annual average, otherwise calculates a complete-year average, otherwise labels monthly fallback.
- CDC PLACES units are normalized.
- Stepwise progress logging is included.
- Missing indicators are backfilled with null rows so indicators are never silently dropped.

Install:
    pip3 install requests pandas

Recommended:
    export CENSUS_API_KEY="4a550c8493494c363ee0afc87c2af4e97d4a169b"

Example:
    python3 needs_assessment_data.py \
      --state-fips 17 \
      --county-fips 031 \
      --state-abbr IL \
      --county-name "Cook County" \
      --service-area-name "Cook County, Illinois" \
      --latest \
      --comparison-geographies county state us \
      --verbose

County-only:
    python3 needs_assessment_data.py \
      --state-fips 17 \
      --county-fips 031 \
      --state-abbr IL \
      --county-name "Cook County" \
      --service-area-name "Cook County, Illinois" \
      --latest \
      --comparison-geographies county
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import math
import os
import time
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

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


def mask_body(body: dict[str, Any]) -> dict[str, Any]:
    masked = dict(body)
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


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None

    if isinstance(value, (int, float)):
        f = float(value)
    else:
        s = str(value).strip()
        if s in {"", "null", "None", "NaN", "nan", "-", "**", "***", "N/A", "NA"}:
            return None
        try:
            f = float(s.replace(",", ""))
        except ValueError:
            return None

    if math.isnan(f):
        return None

    if int(f) in ACS_SPECIAL_VALUES:
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
    if not values:
        return None
    return sum(values) / len(values)


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def cache_key(
    url: str,
    params: Optional[dict[str, Any]] = None,
    body: Optional[dict[str, Any]] = None,
) -> str:
    payload = json.dumps({"url": url, "params": params or {}, "body": body or {}}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def census_base_url(year: int, dataset_suffix: str = "") -> str:
    base = f"https://api.census.gov/data/{year}/acs/acs5"
    suffix = dataset_suffix.strip("/")
    if suffix:
        return f"{base}/{suffix}"
    return base


def looks_like_missing_key_html(text: str) -> bool:
    lowered = text.lower()
    return (
        "missing key" in lowered
        or "invalid key" in lowered
        or "a valid <em>key</em>" in lowered
        or "key_signup.html" in lowered
    )


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
            log_debug(f"Cache hit: {source_slug} {url}")
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log_warn(f"Could not read cache file; refetching: {path}")

    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            log_debug(f"GET attempt {attempt}: {url} params={mask_params(params)}")
            time.sleep(REQUEST_SLEEP_SECONDS)
            response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT, allow_redirects=True)

            if response.status_code >= 400:
                last_error = f"{response.status_code}: {response.text[:500]}"
                log_debug(f"GET non-success attempt {attempt}: {last_error}")
                time.sleep(attempt)
                continue

            if looks_like_missing_key_html(response.text):
                last_error = (
                    "Census API key missing or invalid. "
                    "Provide --census-api-key or set CENSUS_API_KEY."
                )
                log_debug(last_error)
                break

            try:
                data = response.json()
            except json.JSONDecodeError:
                last_error = (
                    f"Non-JSON response from {url}; status={response.status_code}; "
                    f"content_type={response.headers.get('content-type', '')}; body={response.text[:500]}"
                )
                log_debug(last_error)
                time.sleep(attempt)
                continue

            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return data

        except Exception as exc:
            last_error = repr(exc)
            log_debug(f"GET exception attempt {attempt}: {last_error}")
            time.sleep(attempt)

    if errors is not None and record_error:
        errors.append(f"GET JSON failed: {url} params={mask_params(params)} error={last_error}")
    return None


def cached_post_json(
    source_slug: str,
    url: str,
    body: dict[str, Any],
    errors: Optional[list[str]] = None,
) -> Optional[Any]:
    source_dir = CACHE_DIR / source_slug / dt.date.today().isoformat()
    source_dir.mkdir(parents=True, exist_ok=True)
    path = source_dir / f"{cache_key(url, body=body)}.json"

    if path.exists():
        try:
            log_debug(f"Cache hit: {source_slug} {url}")
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log_warn(f"Could not read cache file; refetching: {path}")

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log_debug(f"POST attempt {attempt}: {url} body={mask_body(body)}")
            time.sleep(REQUEST_SLEEP_SECONDS)
            response = requests.post(url, json=body, timeout=REQUEST_TIMEOUT)

            if response.status_code >= 400:
                last_error = f"{response.status_code}: {response.text[:500]}"
                log_debug(f"POST non-success attempt {attempt}: {last_error}")
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
        errors.append(f"POST JSON failed: {url} body={mask_body(body)} error={last_error}")
    return None


# --------------------------------------------------------------------------------------
# Citations and indicator definitions
# --------------------------------------------------------------------------------------

def build_citation_registry() -> dict[str, Citation]:
    citations = [
        Citation("CENSUS_ACS_API", "U.S. Census ACS API", "American Community Survey API", "Government API", "U.S. Census Bureau", "Ongoing", "https://api.census.gov/data.html", "Source for population, demographics, language, poverty, housing, insurance, transportation, and SDOH indicators."),
        Citation("CMS_MEDICAID_DATA", "CMS Medicaid Data", "Data.Medicaid.gov and Medicaid administrative data", "Government API / public files", "Centers for Medicare & Medicaid Services", "Ongoing", "https://data.medicaid.gov/", "Potential source for Medicaid administrative indicators."),
        Citation("BLS_LAUS_API", "BLS Public Data API", "Local Area Unemployment Statistics via BLS Public Data API", "Government API", "U.S. Bureau of Labor Statistics", "Ongoing", "https://api.bls.gov/publicAPI/v2/timeseries/data/", "Source for county labor force and unemployment indicators."),
        Citation("CDC_PLACES_API", "CDC PLACES API", "CDC PLACES Local Data for Better Health", "Government API / Socrata", "Centers for Disease Control and Prevention", "Ongoing", "https://data.cdc.gov/", "Source for modeled county/local health indicators."),
        Citation("CDC_WONDER_NVSS", "CDC WONDER / NVSS", "CDC WONDER, National Vital Statistics System Mortality", "Government system / manual download fallback", "Centers for Disease Control and Prevention", "Ongoing", "https://wonder.cdc.gov/", "Source for suicide and drug overdose mortality."),
        Citation("HRSA_DATA_WAREHOUSE", "HRSA Data Warehouse", "HRSA Data Warehouse and Area Health Resource Files", "Government API / public files", "Health Resources and Services Administration", "Ongoing", "https://data.hrsa.gov/", "Source for HPSA, MUA/P, health center, workforce, and resource indicators."),
        Citation("SAMHSA_FINDTREATMENT", "SAMHSA FindTreatment / N-SUMHSS", "SAMHSA Behavioral Health Treatment Locator and N-SUMHSS", "Government locator / public files", "Substance Abuse and Mental Health Services Administration", "Ongoing", "https://findtreatment.gov/", "Source for behavioral health treatment facility availability."),
        Citation("NCES_CCD", "NCES Common Core of Data", "Common Core of Data", "Government data files / API where available", "National Center for Education Statistics", "Ongoing", "https://nces.ed.gov/ccd/", "Source for school enrollment and district demographics."),
        Citation("HUD_PIT_HIC", "HUD PIT/HIC", "Point-in-Time Count and Housing Inventory Count", "Government public files", "U.S. Department of Housing and Urban Development", "Annual", "https://www.hudexchange.info/programs/hdx/pit-hic/", "Source for homelessness indicators."),
        Citation("INTERNAL_PLACEHOLDER", "Internal organization data placeholder", "Internal CCBHC data placeholder", "Internal data placeholder", "Client organization", "Project-specific", "Not available through government API", "Used for EHR, staffing, utilization, and qualitative findings."),
    ]
    return {c.citation_id: c for c in citations}


def citation_text(citation_id: str) -> str:
    c = build_citation_registry()[citation_id]
    return f"{c.citation_label}: {c.source_title}. {c.source_agency_or_author}. {c.publication_date}. {c.url_or_file_reference}."


def indicator_def(
    indicator_id: str,
    name: str,
    domain: str,
    summary: str,
    units: str,
    unit_definition: str,
    unit_source: str,
    source_name: str,
    source_agency: str,
    api_or_download: str,
    geo: str,
    update: str,
    latest_logic: str,
    citation_id: str,
    limitation: str,
    followup: str,
    government_api_available: str = "Yes",
    public_download_available: str = "Yes",
    internal_or_qualitative_required: str = "No",
) -> IndicatorDefinition:
    return IndicatorDefinition(
        indicator_id=indicator_id,
        indicator_name=name,
        needs_assessment_domain=domain,
        indicator_summary=summary,
        indicator_detailed_description=summary,
        units=units,
        unit_definition=unit_definition,
        unit_source=unit_source,
        source_name=source_name,
        source_agency=source_agency,
        api_or_download=api_or_download,
        expected_geography_level=geo,
        expected_update_frequency=update,
        latest_logic=latest_logic,
        government_api_available=government_api_available,
        public_download_available=public_download_available,
        internal_or_qualitative_required=internal_or_qualitative_required,
        source_citation_id=citation_id,
        limitation=limitation,
        recommended_qualitative_followup_question=followup,
    )


def build_indicator_definitions() -> dict[str, IndicatorDefinition]:
    latest_acs = "Use newest ACS 5-year endpoint with a working variables.json file and a valid Census API key."
    latest_bls = "Prefer BLS annual average M13; otherwise complete-year monthly average; otherwise latest monthly row."
    latest_places = "Use newest matching CDC PLACES county row."
    latest_manual = "Manual or source-specific connector needed."

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
        indicator_def("UNINSURED_RATE", "Uninsured rate", "Insurance Coverage", "Percentage without health insurance.", "percent", "Percent with no health insurance.", "ACS profile percent variable.", *acs_profile, "Coverage does not equal access.", "Do uninsured residents know where to get care?"),
        indicator_def("PUBLIC_COVERAGE_RATE", "Public health insurance coverage rate", "Insurance Coverage", "Percentage with public coverage.", "percent", "Percent with public health insurance.", "ACS DP03_0098PE.", *acs_profile, "Public coverage is not Medicaid-only.", "Are publicly insured residents able to access care?"),
        indicator_def("MEDICAID_COVERAGE_RATE", "Medicaid coverage rate", "Insurance Coverage", "Percentage covered by Medicaid.", "percent", "Medicaid coverage rate.", "Placeholder requiring Medicaid-specific source.", "CMS Medicaid / ACS Medicaid-specific data", "CMS / U.S. Census Bureau", "Manual/API extension needed", "County, state, U.S. depending source", "Varies", latest_manual, "CMS_MEDICAID_DATA", "Do not substitute public coverage for Medicaid-only.", "Are Medicaid members able to access care?", "Partial", "Yes", "No"),
        indicator_def("NO_VEHICLE_HOUSEHOLDS_COUNT", "Households with no vehicle available", "Transportation", "Households with no vehicle.", "households", "Households reporting no vehicle.", "ACS count variable.", *acs_detail, "Does not measure transit/distance.", "Where do transportation barriers affect care?"),
        indicator_def("NO_VEHICLE_HOUSEHOLDS_PCT", "Households with no vehicle available percentage", "Transportation", "Share of households with no vehicle.", "percent", "No-vehicle households / total households * 100.", "Calculated from ACS numerator and denominator.", *acs_detail, "Derived percentage MOE not calculated.", "Are transit, rides, or mobile services needed?"),
        indicator_def("SNAP_HOUSEHOLDS_COUNT", "Households receiving SNAP", "Food/Nutrition", "Households receiving SNAP.", "households", "Households receiving SNAP.", "ACS count variable.", *acs_detail, "SNAP is not full food insecurity.", "Do residents report food insecurity?"),
        indicator_def("SNAP_HOUSEHOLDS_PCT", "Households receiving SNAP percentage", "Food/Nutrition", "Share of households receiving SNAP.", "percent", "SNAP households / total households * 100.", "Calculated from ACS numerator and denominator.", *acs_detail, "Derived percentage MOE not calculated.", "Are food needs appearing in BH settings?"),
        indicator_def("RENT_BURDENED_HOUSEHOLDS_COUNT", "Rent-burdened households", "Housing Stability", "Renter households spending 30%+ of income on rent.", "households", "Rent-burdened renter households.", "Calculated from ACS rent categories.", *acs_detail, "MOE for sum approximated.", "How is housing affordability affecting recovery?"),
        indicator_def("RENT_BURDENED_HOUSEHOLDS_PCT", "Rent-burdened households percentage", "Housing Stability", "Share of renter households rent-burdened.", "percent", "Rent-burdened renter households / renter households * 100.", "Calculated from ACS numerator and denominator.", *acs_detail, "Derived percentage MOE not calculated.", "Does housing cost interfere with treatment?"),
        indicator_def("MEDIAN_HOME_VALUE", "Median home value", "Housing Stability", "Median owner-occupied home value.", "dollars", "Median home value in dollars.", "ACS dollar variable.", *acs_detail, "Does not measure rent/homelessness.", "Are housing costs affecting clients/workforce?"),
        indicator_def("UNEMPLOYMENT_RATE", "Unemployment rate", "Economic Stability", "Unemployment rate.", "percent", "Unemployed labor force / labor force * 100.", "BLS LAUS.", "BLS LAUS", "U.S. Bureau of Labor Statistics", "API", "County", "Monthly / annual", "Prefer BLS annual average M13; otherwise complete-year monthly average; otherwise latest monthly row.", "BLS_LAUS_API", "Series ID inferred; validate before publication.", "Are employment barriers affecting access?"),
        indicator_def("FREQUENT_MENTAL_DISTRESS", "Frequent mental distress", "Mental Health Prevalence and Outcomes", "Estimated frequent mental distress prevalence.", "percent", "Percent of adults.", "CDC PLACES metadata.", "CDC PLACES", "Centers for Disease Control and Prevention", "API / Socrata", "County", "Annual release", "Use newest matching CDC PLACES county row.", "CDC_PLACES_API", "Modeled estimate.", "Does modeled distress align with stakeholders?", "Partial", "Yes", "No"),
        indicator_def("DEPRESSION_PREVALENCE", "Depression prevalence", "Mental Health Prevalence and Outcomes", "Estimated depression prevalence.", "percent", "Percent of adults.", "CDC PLACES metadata.", "CDC PLACES", "Centers for Disease Control and Prevention", "API / Socrata", "County", "Annual release", "Use newest matching CDC PLACES county row.", "CDC_PLACES_API", "Modeled estimate.", "Are depression needs presenting differently?", "Partial", "Yes", "No"),
        indicator_def("BINGE_DRINKING", "Binge drinking", "Substance Use Prevalence and Outcomes", "Estimated binge drinking prevalence.", "percent", "Percent of adults.", "CDC PLACES metadata.", "CDC PLACES", "Centers for Disease Control and Prevention", "API / Socrata", "County", "Annual release", "Use newest matching CDC PLACES county row.", "CDC_PLACES_API", "Modeled estimate.", "How are alcohol needs showing up?", "Partial", "Yes", "No"),
        indicator_def("CURRENT_SMOKING", "Current smoking", "Physical Health and Co-occurring Conditions", "Estimated current smoking prevalence.", "percent", "Percent of adults.", "CDC PLACES metadata.", "CDC PLACES", "Centers for Disease Control and Prevention", "API / Socrata", "County", "Annual release", "Use newest matching CDC PLACES county row.", "CDC_PLACES_API", "Modeled estimate.", "Are tobacco needs addressed?", "Partial", "Yes", "No"),
        indicator_def("SUICIDE_MORTALITY", "Suicide deaths and age-adjusted suicide mortality rate", "Mental Health Prevalence and Outcomes", "Suicide mortality burden.", "deaths per 100,000", "Age-adjusted suicide deaths per 100,000.", "CDC WONDER/NVSS.", "CDC WONDER / NVSS", "Centers for Disease Control and Prevention", "Manual download fallback", "County, state, U.S.", "Annual", "Manual or source-specific connector needed.", "CDC_WONDER_NVSS", "Small counts may be suppressed.", "What suicide-prevention needs are not visible?", "Partial", "Yes", "No"),
        indicator_def("OVERDOSE_MORTALITY", "Drug overdose deaths and age-adjusted overdose mortality rate", "Substance Use Prevalence and Outcomes", "Overdose mortality burden.", "deaths per 100,000", "Age-adjusted overdose deaths per 100,000.", "CDC WONDER/NVSS.", "CDC WONDER / NVSS", "Centers for Disease Control and Prevention", "Manual download fallback", "County, state, U.S.", "Annual", "Manual or source-specific connector needed.", "CDC_WONDER_NVSS", "Small counts may be suppressed.", "Which substances and overdose risks are visible?", "Partial", "Yes", "No"),
        indicator_def("MENTAL_HEALTH_HPSA", "Mental Health HPSA status", "Workforce and Provider Availability", "Mental health shortage designation.", "designation", "HPSA designation/score.", "HRSA HPSA.", "HRSA Data Warehouse", "Health Resources and Services Administration", "API / public files", "County, tract, facility, service area", "Ongoing", "Manual or source-specific connector needed.", "HRSA_DATA_WAREHOUSE", "Boundaries may not align with county.", "Which workforce shortages affect access?", "Partial", "Yes", "No"),
        indicator_def("TREATMENT_FACILITIES_MH", "Mental health treatment facilities", "Treatment Facilities and Service Infrastructure", "Mental health facility availability.", "facilities", "Count of facility records.", "SAMHSA locator/files.", "SAMHSA FindTreatment / N-SUMHSS", "Substance Abuse and Mental Health Services Administration", "Locator / public files", "Address-level; aggregate to county", "Annual / ongoing", "Manual or source-specific connector needed.", "SAMHSA_FINDTREATMENT", "Listings may not reflect capacity.", "Which services are actually available?", "Partial", "Yes", "No"),
        indicator_def("TREATMENT_FACILITIES_SUD", "SUD treatment facilities and MOUD availability", "Treatment Facilities and Service Infrastructure", "SUD/MOUD facility availability.", "facilities", "Count of facility records.", "SAMHSA locator/files.", "SAMHSA FindTreatment / N-SUMHSS", "Substance Abuse and Mental Health Services Administration", "Locator / public files", "Address-level; aggregate to county", "Annual / ongoing", "Manual or source-specific connector needed.", "SAMHSA_FINDTREATMENT", "Listings may not reflect capacity.", "Where are SUD level-of-care gaps?", "Partial", "Yes", "No"),
        indicator_def("HOMELESSNESS_PIT", "People experiencing homelessness", "Housing Stability", "Homelessness count.", "people", "People counted as experiencing homelessness.", "HUD PIT/HIC.", "HUD PIT/HIC", "U.S. Department of Housing and Urban Development", "Public download", "CoC; county if crosswalked", "Annual", "Manual or source-specific connector needed.", "HUD_PIT_HIC", "Often CoC-level.", "What housing instability is missed?", "No", "Yes", "No"),
        indicator_def("SCHOOL_ENROLLMENT", "School enrollment and student demographics", "Children, Youth, and Families", "Student enrollment.", "students", "Enrolled students.", "NCES CCD.", "NCES Common Core of Data", "National Center for Education Statistics", "API / public files", "School, district, county crosswalk", "Annual", "Manual or source-specific connector needed.", "NCES_CCD", "County aggregation may require crosswalk.", "Which school partners should be engaged?", "Partial", "Yes", "No"),
        indicator_def("CLIENT_DEMOGRAPHICS", "CCBHC client demographics", "Underserved Populations", "Client demographic profile.", "clients", "Clients or percent of clients.", "Internal data.", "Internal EHR / client data", "Client organization", "Internal file", "Client/service area", "Project-specific", "Manual or source-specific connector needed.", "INTERNAL_PLACEHOLDER", "Not available through APIs.", "Which groups are underrepresented?", "No", "No", "Yes"),
        indicator_def("SERVICE_UTILIZATION", "Service utilization, referrals, wait times, no-shows", "Access to Care", "Service access and operations.", "varies", "Visits, clients, days, referrals, or percent.", "Internal data.", "Internal operations data", "Client organization", "Internal file", "Client/service area", "Project-specific", "Manual or source-specific connector needed.", "INTERNAL_PLACEHOLDER", "Not available through APIs.", "Where are bottlenecks?", "No", "No", "Yes"),
        indicator_def("STAFFING_PLAN", "Staffing plan, FTEs, credentials, turnover, training", "Staffing Implications", "Staffing capacity and alignment.", "FTE / roles", "FTEs, roles, credentials, vacancies.", "Internal data.", "Internal staffing plan", "Client organization", "Internal file", "Client/service area", "Project-specific", "Manual or source-specific connector needed.", "INTERNAL_PLACEHOLDER", "Not available through APIs.", "What staffing changes are needed?", "No", "No", "Yes"),
        indicator_def("QUALITATIVE_THEMES", "Interview, focus group, advisory board, and survey themes", "Qualitative Findings", "Primary qualitative themes.", "themes", "Themes, findings, quotes, or counts.", "Internal qualitative analysis.", "Primary qualitative research", "Client organization / consultant", "Internal file", "Service area", "Project-specific", "Manual or source-specific connector needed.", "INTERNAL_PLACEHOLDER", "Not statistically representative unless designed that way.", "What explains quantitative patterns?", "No", "No", "Yes"),
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


def add_count_and_percent_observations(
    observations: list[Observation],
    *,
    count_indicator_id: str,
    pct_indicator_id: Optional[str],
    numerator: Optional[float],
    numerator_moe: Optional[float],
    denominator: Optional[float],
    geography_name: str,
    geography_type: str,
    state_fips: str,
    county_fips: str,
    year: int,
    source_latest: str,
    source_variable_label: str,
    source_url_or_endpoint: str,
    note: str,
) -> None:
    observations.append(
        make_observation(
            indicator_id=count_indicator_id,
            geography_name=geography_name,
            geography_type=geography_type,
            state_fips=state_fips,
            county_fips=county_fips,
            year_or_period=str(year),
            source_latest_year_or_period=source_latest,
            estimate=numerator,
            moe=numerator_moe,
            numerator=numerator,
            denominator=denominator,
            source_variable_label=source_variable_label,
            stratification="Total",
            comparison_available="Yes",
            data_quality_note=note,
            source_url_or_endpoint=source_url_or_endpoint,
        )
    )

    if pct_indicator_id:
        observations.append(
            make_observation(
                indicator_id=pct_indicator_id,
                geography_name=geography_name,
                geography_type=geography_type,
                state_fips=state_fips,
                county_fips=county_fips,
                year_or_period=str(year),
                source_latest_year_or_period=source_latest,
                estimate=calc_percent(numerator, denominator),
                moe=None,
                numerator=numerator,
                denominator=denominator,
                source_variable_label=source_variable_label,
                stratification="Total",
                comparison_available="Yes",
                data_quality_note=note + " Derived percentage calculated by script; MOE for derived percentage not calculated.",
                source_url_or_endpoint=source_url_or_endpoint,
            )
        )


# --------------------------------------------------------------------------------------
# ACS
# --------------------------------------------------------------------------------------

def get_census_variables(
    year: int,
    dataset_suffix: str,
    errors: Optional[list[str]] = None,
    record_error: bool = True,
) -> dict[str, dict[str, Any]]:
    url = f"{census_base_url(year, dataset_suffix)}/variables.json"
    data = cached_get_json(
        "census_acs_variables",
        url,
        params={},
        errors=errors,
        record_error=record_error,
        max_retries=1,
    )

    if not isinstance(data, dict):
        return {}

    variables = data.get("variables")
    if not isinstance(variables, dict):
        return {}

    return {k: v for k, v in variables.items() if isinstance(v, dict)}


def detect_latest_acs_year(config: RunConfig, notes: list[str]) -> int:
    log_step("Detecting latest usable ACS 5-year year")

    if not config.latest and config.years:
        candidate_years = sorted(config.years, reverse=True)
    else:
        today = dt.date.today()
        start_year = today.year - 1 if today.month >= 12 else today.year - 2
        candidate_years = list(range(start_year, start_year - 10, -1))

    for year in candidate_years:
        variables = get_census_variables(year, "", errors=None, record_error=False)
        if variables and "B01001_001E" in variables:
            log_info(f"Using ACS 5-year year {year}")
            return year
        log_debug(f"ACS {year} not usable or not published yet")

    fallback = 2023
    notes.append(f"Could not detect requested/latest ACS year; falling back to {fallback}.")
    return fallback


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
    all_geos = {
        "county": ("County", config.state_fips, config.county_fips),
        "state": ("State", config.state_fips, ""),
        "us": ("United States", "", ""),
    }
    return {k: v for k, v in all_geos.items() if k in config.comparison_geographies}


def filter_available_vars(
    requested: list[str],
    metadata: dict[str, dict[str, Any]],
    source_name: str,
    notes: list[str],
) -> list[str]:
    """
    Keep MOE variables even if variables.json does not list them.
    Census ACS usually supports M/PM MOE variables when the matching E/PE estimate exists.
    """
    if not metadata:
        notes.append(f"{source_name}: variable metadata unavailable; requesting configured variables anyway.")
        return requested

    available: list[str] = []
    missing_required: list[str] = []

    for var in requested:
        if var in metadata:
            available.append(var)
            continue

        # Keep ACS MOE variables if the matching estimate variable exists.
        if var.endswith("M"):
            matching_estimate = var[:-1] + "E"
            if matching_estimate in metadata:
                available.append(var)
                continue

        if var.endswith("PM"):
            matching_estimate = var[:-2] + "PE"
            if matching_estimate in metadata:
                available.append(var)
                continue

        missing_required.append(var)

    if missing_required:
        notes.append(
            f"{source_name}: skipped {len(missing_required)} unavailable required variables: "
            + ", ".join(missing_required[:20])
            + ("..." if len(missing_required) > 20 else "")
        )

    return available

def census_request(
    config: RunConfig,
    year: int,
    dataset_suffix: str,
    variables: list[str],
    geography: str,
    errors: list[str],
) -> Optional[dict[str, Any]]:
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

        data = cached_get_json("census_acs", url, params=params, errors=errors, record_error=True)

        if not isinstance(data, list) or len(data) < 2:
            continue

        row = dict(zip(data[0], data[1]))
        merged.update(row)

    return merged if merged else None


def extract_acs(config: RunConfig, errors: list[str], notes: list[str]) -> list[Observation]:
    log_step("Starting ACS extraction")
    observations: list[Observation] = []

    if not config.census_api_key:
        notes.append(
            "Census API key was not provided. ACS data requests were skipped to avoid repeated Missing Key HTML responses. "
            "Set CENSUS_API_KEY or pass --census-api-key to pull ACS rows."
        )
        log_warn("No Census API key provided; skipping ACS data requests.")
        return observations

    year = detect_latest_acs_year(config, notes)
    source_latest = str(year)

    detailed_meta = get_census_variables(year, "", errors=errors, record_error=True)
    profile_meta = get_census_variables(year, "profile", errors=errors, record_error=True)
    subject_meta = get_census_variables(year, "subject", errors=errors, record_error=True)

    detailed_vars = [
        "B01001_001E", "B01001_001M",
        "B09001_001E", "B09001_001M",
        "B03003_003E", "B03003_003M",
        "B02001_004E", "B02001_004M",
        "B21001_001E", "B21001_001M", "B21001_002E", "B21001_002M",
        "C16001_001E", "C16001_001M", "C16001_005E", "C16001_005M",
        "B08201_001E", "B08201_001M", "B08201_002E", "B08201_002M",
        "B22010_001E", "B22010_001M", "B22010_002E", "B22010_002M",
        "B25070_001E", "B25070_001M",
        "B25070_007E", "B25070_007M", "B25070_008E", "B25070_008M",
        "B25070_009E", "B25070_009M", "B25070_010E", "B25070_010M",
        "B25077_001E", "B25077_001M",
        "B19013_001E", "B19013_001M",
        "B01001_020E", "B01001_020M", "B01001_021E", "B01001_021M",
        "B01001_022E", "B01001_022M", "B01001_023E", "B01001_023M",
        "B01001_024E", "B01001_024M", "B01001_025E", "B01001_025M",
        "B01001_044E", "B01001_044M", "B01001_045E", "B01001_045M",
        "B01001_046E", "B01001_046M", "B01001_047E", "B01001_047M",
        "B01001_048E", "B01001_048M", "B01001_049E", "B01001_049M",
    ]

    profile_vars = [
        "DP03_0128PE", "DP03_0128PM",
        "DP03_0099PE", "DP03_0099PM",
        "DP03_0098PE", "DP03_0098PM",
    ]

    subject_vars = [
        "S1810_C02_001E", "S1810_C02_001M",
        "S1810_C03_001E", "S1810_C03_001M",
    ]

    detailed_vars = filter_available_vars(detailed_vars, detailed_meta, "ACS detailed", notes)
    profile_vars = filter_available_vars(profile_vars, profile_meta, "ACS profile", notes)
    subject_vars = filter_available_vars(subject_vars, subject_meta, "ACS subject", notes)

    for geo, (geo_type, st, co) in selected_geographies(config).items():
        log_step(f"Pulling ACS detailed tables for {geo}")
        rec = census_request(config, year, "", detailed_vars, geo, errors)

        if rec:
            geo_name = rec.get("NAME", geo)
            endpoint = census_base_url(year, "")
            total = safe_float(rec.get("B01001_001E"))

            add_count_and_percent_observations(
                observations,
                count_indicator_id="POP_TOTAL",
                pct_indicator_id=None,
                numerator=total,
                numerator_moe=safe_float(rec.get("B01001_001M")),
                denominator=None,
                geography_name=geo_name,
                geography_type=geo_type,
                state_fips=st,
                county_fips=co,
                year=year,
                source_latest=source_latest,
                source_variable_label=label_for_vars(detailed_meta, ["B01001_001E", "B01001_001M"]),
                source_url_or_endpoint=endpoint,
                note="Most recent usable ACS 5-year detailed table. ACS special codes converted to null.",
            )

            under18 = safe_float(rec.get("B09001_001E"))
            add_count_and_percent_observations(
                observations,
                count_indicator_id="POP_AGE_UNDER_18_COUNT",
                pct_indicator_id="POP_AGE_UNDER_18_PCT",
                numerator=under18,
                numerator_moe=safe_float(rec.get("B09001_001M")),
                denominator=total,
                geography_name=geo_name,
                geography_type=geo_type,
                state_fips=st,
                county_fips=co,
                year=year,
                source_latest=source_latest,
                source_variable_label=label_for_vars(detailed_meta, ["B09001_001E", "B01001_001E"]),
                source_url_or_endpoint=endpoint,
                note="Under-18 count and percentage from ACS.",
            )

            age65_prefixes = [
                "B01001_020", "B01001_021", "B01001_022", "B01001_023", "B01001_024", "B01001_025",
                "B01001_044", "B01001_045", "B01001_046", "B01001_047", "B01001_048", "B01001_049",
            ]
            age65_count = sum_values([safe_float(rec.get(f"{v}E")) for v in age65_prefixes])
            age65_moe = sum_moes([safe_float(rec.get(f"{v}M")) for v in age65_prefixes])

            add_count_and_percent_observations(
                observations,
                count_indicator_id="POP_AGE_65_PLUS_COUNT",
                pct_indicator_id="POP_AGE_65_PLUS_PCT",
                numerator=age65_count,
                numerator_moe=age65_moe,
                denominator=total,
                geography_name=geo_name,
                geography_type=geo_type,
                state_fips=st,
                county_fips=co,
                year=year,
                source_latest=source_latest,
                source_variable_label=label_for_vars(detailed_meta, [f"{v}E" for v in age65_prefixes] + ["B01001_001E"]),
                source_url_or_endpoint=endpoint,
                note="65+ count calculated from ACS B01001 age/sex cells. Sum MOE approximated.",
            )

            count_pct_specs = [
                ("HISPANIC_LATINX_COUNT", "HISPANIC_LATINX_PCT", "B03003_003E", "B03003_003M", total, ["B03003_003E", "B01001_001E"], "Hispanic/Latinx uses ACS B03003."),
                ("AIAN_POPULATION_COUNT", "AIAN_POPULATION_PCT", "B02001_004E", "B02001_004M", total, ["B02001_004E", "B01001_001E"], "AIAN-alone uses ACS B02001."),
                ("VETERAN_POPULATION_COUNT", "VETERAN_POPULATION_PCT", "B21001_002E", "B21001_002M", safe_float(rec.get("B21001_001E")), ["B21001_002E", "B21001_001E"], "Veteran percentage uses ACS B21001 civilian population age 18+ denominator."),
                ("LANGUAGE_LEP_SPANISH_COUNT", "LANGUAGE_LEP_SPANISH_PCT", "C16001_005E", "C16001_005M", safe_float(rec.get("C16001_001E")), ["C16001_005E", "C16001_001E"], "Spanish LEP uses ACS C16001 population age 5+ table."),
                ("NO_VEHICLE_HOUSEHOLDS_COUNT", "NO_VEHICLE_HOUSEHOLDS_PCT", "B08201_002E", "B08201_002M", safe_float(rec.get("B08201_001E")), ["B08201_002E", "B08201_001E"], "No-vehicle percentage uses ACS B08201 household denominator."),
                ("SNAP_HOUSEHOLDS_COUNT", "SNAP_HOUSEHOLDS_PCT", "B22010_002E", "B22010_002M", safe_float(rec.get("B22010_001E")), ["B22010_002E", "B22010_001E"], "SNAP percentage uses ACS B22010 household denominator."),
            ]

            for count_id, pct_id, num_var, moe_var, denom, label_vars, note in count_pct_specs:
                numerator = safe_float(rec.get(num_var))
                add_count_and_percent_observations(
                    observations,
                    count_indicator_id=count_id,
                    pct_indicator_id=pct_id,
                    numerator=numerator,
                    numerator_moe=safe_float(rec.get(moe_var)),
                    denominator=denom,
                    geography_name=geo_name,
                    geography_type=geo_type,
                    state_fips=st,
                    county_fips=co,
                    year=year,
                    source_latest=source_latest,
                    source_variable_label=label_for_vars(detailed_meta, label_vars),
                    source_url_or_endpoint=endpoint,
                    note=note,
                )

            rent_prefixes = ["B25070_007", "B25070_008", "B25070_009", "B25070_010"]
            rent_burdened = sum_values([safe_float(rec.get(f"{v}E")) for v in rent_prefixes])
            rent_moe = sum_moes([safe_float(rec.get(f"{v}M")) for v in rent_prefixes])
            renter_denominator = safe_float(rec.get("B25070_001E"))

            add_count_and_percent_observations(
                observations,
                count_indicator_id="RENT_BURDENED_HOUSEHOLDS_COUNT",
                pct_indicator_id="RENT_BURDENED_HOUSEHOLDS_PCT",
                numerator=rent_burdened,
                numerator_moe=rent_moe,
                denominator=renter_denominator,
                geography_name=geo_name,
                geography_type=geo_type,
                state_fips=st,
                county_fips=co,
                year=year,
                source_latest=source_latest,
                source_variable_label=label_for_vars(detailed_meta, [f"{v}E" for v in rent_prefixes] + ["B25070_001E"]),
                source_url_or_endpoint=endpoint,
                note="Rent-burdened households calculated from ACS B25070 categories for 30% or more of income. Sum MOE approximated.",
            )

            for indicator_id, estimate_var, moe_var, strat in [
                ("MEDIAN_HOME_VALUE", "B25077_001E", "B25077_001M", "Owner-occupied housing units"),
                ("MEDIAN_HOUSEHOLD_INCOME", "B19013_001E", "B19013_001M", "Households"),
            ]:
                observations.append(
                    make_observation(
                        indicator_id=indicator_id,
                        geography_name=geo_name,
                        geography_type=geo_type,
                        state_fips=st,
                        county_fips=co,
                        year_or_period=str(year),
                        source_latest_year_or_period=source_latest,
                        estimate=safe_float(rec.get(estimate_var)),
                        moe=safe_float(rec.get(moe_var)),
                        numerator=None,
                        denominator=None,
                        source_variable_label=label_for_vars(detailed_meta, [estimate_var, moe_var]),
                        stratification=strat,
                        comparison_available="Yes",
                        data_quality_note="Most recent usable ACS 5-year detailed table.",
                        source_url_or_endpoint=endpoint,
                    )
                )
        else:
            errors.append(f"ACS detailed request returned no rows for geography={geo}, year={year}.")

        log_step(f"Pulling ACS profile tables for {geo}")
        profile_rec = census_request(config, year, "profile", profile_vars, geo, errors)

        if profile_rec:
            geo_name = profile_rec.get("NAME", geo)
            endpoint = census_base_url(year, "profile")

            for indicator_id, estimate_var, moe_var in [
                ("POVERTY_RATE", "DP03_0128PE", "DP03_0128PM"),
                ("UNINSURED_RATE", "DP03_0099PE", "DP03_0099PM"),
                ("PUBLIC_COVERAGE_RATE", "DP03_0098PE", "DP03_0098PM"),
            ]:
                observations.append(
                    make_observation(
                        indicator_id=indicator_id,
                        geography_name=geo_name,
                        geography_type=geo_type,
                        state_fips=st,
                        county_fips=co,
                        year_or_period=str(year),
                        source_latest_year_or_period=source_latest,
                        estimate=safe_float(profile_rec.get(estimate_var)),
                        moe=safe_float(profile_rec.get(moe_var)),
                        numerator=None,
                        denominator=None,
                        source_variable_label=label_for_vars(profile_meta, [estimate_var, moe_var]),
                        stratification="Total",
                        comparison_available="Yes",
                        data_quality_note="Most recent usable ACS 5-year profile table.",
                        source_url_or_endpoint=endpoint,
                    )
                )
        else:
            errors.append(f"ACS profile request returned no rows for geography={geo}, year={year}.")

        log_step(f"Pulling ACS subject tables for {geo}")
        subject_rec = census_request(config, year, "subject", subject_vars, geo, errors)

        if subject_rec:
            geo_name = subject_rec.get("NAME", geo)
            endpoint = census_base_url(year, "subject")

            for indicator_id, estimate_var, moe_var in [
                ("DISABILITY_COUNT", "S1810_C02_001E", "S1810_C02_001M"),
                ("DISABILITY_PREVALENCE", "S1810_C03_001E", "S1810_C03_001M"),
            ]:
                estimate = safe_float(subject_rec.get(estimate_var))
                observations.append(
                    make_observation(
                        indicator_id=indicator_id,
                        geography_name=geo_name,
                        geography_type=geo_type,
                        state_fips=st,
                        county_fips=co,
                        year_or_period=str(year),
                        source_latest_year_or_period=source_latest,
                        estimate=estimate,
                        moe=safe_float(subject_rec.get(moe_var)),
                        numerator=estimate if indicator_id == "DISABILITY_COUNT" else None,
                        denominator=None,
                        source_variable_label=label_for_vars(subject_meta, [estimate_var, moe_var]),
                        stratification="Civilian noninstitutionalized population",
                        comparison_available="Yes",
                        data_quality_note="Most recent usable ACS 5-year subject table.",
                        source_url_or_endpoint=endpoint,
                    )
                )
        else:
            errors.append(f"ACS subject request returned no rows for geography={geo}, year={year}.")

    log_info(f"ACS extraction produced {len(observations)} rows")
    return observations


# --------------------------------------------------------------------------------------
# BLS LAUS
# --------------------------------------------------------------------------------------

def bls_laus_county_series_id(config: RunConfig) -> str:
    return f"LAUCN{config.state_fips}{config.county_fips}0000000003"


def bls_month_num(row: dict[str, Any]) -> int:
    period = str(row.get("period", "M00"))
    if not period.startswith("M"):
        return 0
    return safe_int(period.replace("M", "")) or 0


def extract_bls_laus(config: RunConfig, errors: list[str]) -> list[Observation]:
    log_step("Starting BLS LAUS extraction")
    series_id = bls_laus_county_series_id(config)
    url = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
    end_year = dt.date.today().year
    start_year = end_year - 10
    body = {"seriesid": [series_id], "startyear": str(start_year), "endyear": str(end_year)}

    data = cached_post_json("bls_laus", url, body, errors)
    observations: list[Observation] = []

    if not isinstance(data, dict):
        errors.append(f"BLS LAUS did not return a valid JSON object for {series_id}.")
        return observations

    results = data.get("Results")
    if not isinstance(results, dict):
        errors.append(f"BLS LAUS response did not include Results for {series_id}.")
        return observations

    series_list = results.get("series")
    if not isinstance(series_list, list) or not series_list:
        errors.append(f"BLS LAUS response did not include series rows for {series_id}.")
        return observations

    series0 = series_list[0]
    if not isinstance(series0, dict):
        errors.append(f"BLS LAUS first series row was not an object for {series_id}.")
        return observations

    rows = series0.get("data")
    if not isinstance(rows, list):
        errors.append(f"BLS LAUS first series did not include data rows for {series_id}.")
        return observations

    valid_rows = [r for r in rows if isinstance(r, dict)]
    annual_rows = [r for r in valid_rows if r.get("period") == "M13" and safe_float(r.get("value")) is not None]
    annual_rows.sort(key=lambda r: safe_int(r.get("year")) or 0, reverse=True)

    if annual_rows:
        latest_row = annual_rows[0]
        latest_year = str(latest_row.get("year"))
        estimate = safe_float(latest_row.get("value"))
        label = "Annual average"
        note = "BLS annual average M13 row used."
    else:
        monthly_rows = [
            r for r in valid_rows
            if isinstance(r.get("period"), str)
            and str(r.get("period")).startswith("M")
            and r.get("period") != "M13"
            and safe_float(r.get("value")) is not None
        ]

        by_year: dict[int, list[dict[str, Any]]] = {}
        for r in monthly_rows:
            y = safe_int(r.get("year"))
            if y is not None:
                by_year.setdefault(y, []).append(r)

        complete_years = []
        for y, yrows in by_year.items():
            months = {bls_month_num(r) for r in yrows}
            if set(range(1, 13)).issubset(months):
                complete_years.append(y)

        if complete_years:
            y = max(complete_years)
            yrows = [r for r in by_year[y] if 1 <= bls_month_num(r) <= 12]

            monthly_values: list[float] = []
            for r in yrows:
                value = safe_float(r.get("value"))
                if value is not None:
                    monthly_values.append(value)

            estimate = average(monthly_values)
            latest_year = str(y)
            label = "Calculated annual average from 12 monthly BLS rows"
            note = "BLS M13 annual average was unavailable; script calculated an unweighted average from 12 monthly rows for the latest complete year."
        else:
            monthly_rows.sort(key=lambda r: ((safe_int(r.get("year")) or 0), bls_month_num(r)), reverse=True)

            if not monthly_rows:
                errors.append(f"BLS LAUS returned no annual or monthly rows for {series_id}.")
                return observations

            latest_row = monthly_rows[0]
            latest_year = str(latest_row.get("year", "latest returned"))
            label = str(latest_row.get("periodName") or latest_row.get("period") or "Latest monthly")
            estimate = safe_float(latest_row.get("value"))
            note = "No BLS M13 or complete-year monthly set was available; latest monthly row used and labeled."

    observations.append(
        make_observation(
            indicator_id="UNEMPLOYMENT_RATE",
            geography_name=config.service_area_name,
            geography_type="County",
            state_fips=config.state_fips,
            county_fips=config.county_fips,
            year_or_period=f"{latest_year} {label}".strip(),
            source_latest_year_or_period=f"{latest_year} {label}".strip(),
            estimate=estimate,
            moe=None,
            numerator=None,
            denominator=None,
            source_variable_label=f"{series_id}: LAUS county unemployment rate, {label}.",
            stratification=label,
            comparison_available="No",
            data_quality_note=f"{note} Validate inferred county LAUS series ID before publication.",
            source_url_or_endpoint=url,
        )
    )

    log_info(f"BLS LAUS extraction produced {len(observations)} rows")
    return observations


# --------------------------------------------------------------------------------------
# CDC PLACES
# --------------------------------------------------------------------------------------

CDC_PLACES_ENDPOINTS = [
    "https://data.cdc.gov/resource/swc5-untb.json",
    "https://data.cdc.gov/resource/cwsq-ngmh.json",
    "https://data.cdc.gov/resource/duw2-7jbt.json",
]

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
        log_step(f"Trying CDC PLACES endpoint {endpoint}")
        params = {
            "$limit": 50000,
            "$where": f"locationid='{county_fips_full}' OR locationid='{int(county_fips_full)}'",
        }
        data = cached_get_json("cdc_places", endpoint, params, errors=None, record_error=False)

        if not isinstance(data, list) or not data:
            continue

        for indicator_id, tokens in CDC_PLACES_MEASURES.items():
            matches: list[dict[str, Any]] = []
            for row in data:
                text = json.dumps(row).lower()
                if any(token in text for token in tokens):
                    matches.append(row)

            if not matches:
                continue

            matches.sort(key=cdc_row_year, reverse=True)
            latest = matches[0]
            latest_year = str(cdc_row_year(latest) or latest.get("yearend") or latest.get("year") or "latest returned")
            units, unit_definition, unit_source = cdc_row_units(latest)

            observations.append(
                make_observation(
                    indicator_id=indicator_id,
                    geography_name=latest.get("locationname", config.county_name),
                    geography_type=latest.get("geographiclevel", "County"),
                    state_fips=config.state_fips,
                    county_fips=config.county_fips,
                    year_or_period=latest_year,
                    source_latest_year_or_period=latest_year,
                    estimate=cdc_row_estimate(latest),
                    moe=None,
                    numerator=None,
                    denominator=None,
                    source_variable_label=cdc_row_label(latest),
                    stratification=latest.get("measure", definition(indicator_id).indicator_name),
                    comparison_available="Partial",
                    data_quality_note="Most recent matching CDC PLACES county row returned from tried Socrata endpoint; modeled estimate; validate endpoint/release before final reporting.",
                    source_url_or_endpoint=endpoint,
                    units_override=units,
                    unit_definition_override=unit_definition,
                    unit_source_override=unit_source,
                )
            )

        if any(o.source_url_or_endpoint == endpoint for o in observations):
            log_info(f"CDC PLACES extraction produced {len(observations)} rows")
            return observations

    errors.append("CDC PLACES returned no usable records from candidate endpoints. Update endpoint ID or use current PLACES download.")
    return observations


# --------------------------------------------------------------------------------------
# Placeholders, catalog, outputs
# --------------------------------------------------------------------------------------

def add_current_source_placeholders(config: RunConfig) -> list[Observation]:
    log_step("Adding current-source placeholder rows")
    current = "current source as of retrieval; manual/download connector needed"

    placeholder_ids = [
        "MEDICAID_COVERAGE_RATE",
        "SUICIDE_MORTALITY",
        "OVERDOSE_MORTALITY",
        "MENTAL_HEALTH_HPSA",
        "TREATMENT_FACILITIES_MH",
        "TREATMENT_FACILITIES_SUD",
        "HOMELESSNESS_PIT",
        "SCHOOL_ENROLLMENT",
        "CLIENT_DEMOGRAPHICS",
        "SERVICE_UTILIZATION",
        "STAFFING_PLAN",
        "QUALITATIVE_THEMES",
    ]

    endpoints = {
        "MEDICAID_COVERAGE_RATE": "https://data.medicaid.gov/ or Medicaid-specific ACS detailed table",
        "SUICIDE_MORTALITY": "https://wonder.cdc.gov/",
        "OVERDOSE_MORTALITY": "https://wonder.cdc.gov/",
        "MENTAL_HEALTH_HPSA": "https://data.hrsa.gov/",
        "TREATMENT_FACILITIES_MH": "https://findtreatment.gov/",
        "TREATMENT_FACILITIES_SUD": "https://findtreatment.gov/",
        "HOMELESSNESS_PIT": "https://www.hudexchange.info/programs/hdx/pit-hic/",
        "SCHOOL_ENROLLMENT": "https://nces.ed.gov/ccd/",
        "CLIENT_DEMOGRAPHICS": "Not available through government API",
        "SERVICE_UTILIZATION": "Not available through government API",
        "STAFFING_PLAN": "Not available through government API",
        "QUALITATIVE_THEMES": "Not available through government API",
    }

    observations = []
    for indicator_id in placeholder_ids:
        d = definition(indicator_id)
        observations.append(
            make_observation(
                indicator_id=indicator_id,
                geography_name=config.service_area_name,
                geography_type="Service area",
                state_fips=config.state_fips,
                county_fips=config.county_fips,
                year_or_period=current,
                source_latest_year_or_period=current,
                estimate=None,
                moe=None,
                numerator=None,
                denominator=None,
                source_variable_label=f"{d.source_name}: {d.indicator_detailed_description}",
                stratification="Total",
                comparison_available="No",
                data_quality_note="This indicator requires a source-specific connector, manual download, Medicaid-specific table, or internal data. No estimate was fabricated.",
                source_url_or_endpoint=endpoints[indicator_id],
            )
        )

    return observations


def build_indicator_catalog() -> list[IndicatorCatalogRow]:
    rows = []
    for d in build_indicator_definitions().values():
        rows.append(
            IndicatorCatalogRow(
                indicator_id=d.indicator_id,
                indicator_name=d.indicator_name,
                needs_assessment_domain=d.needs_assessment_domain,
                indicator_summary=d.indicator_summary,
                indicator_detailed_description=d.indicator_detailed_description,
                units=d.units,
                unit_definition=d.unit_definition,
                unit_source=d.unit_source,
                source_name=d.source_name,
                source_agency=d.source_agency,
                api_or_download=d.api_or_download,
                expected_geography_level=d.expected_geography_level,
                expected_update_frequency=d.expected_update_frequency,
                latest_logic=d.latest_logic,
                government_api_available=d.government_api_available,
                public_download_available=d.public_download_available,
                internal_or_qualitative_required=d.internal_or_qualitative_required,
                source_citation_id=d.source_citation_id,
                source_citation_text=citation_text(d.source_citation_id),
                limitation=d.limitation,
                recommended_qualitative_followup_question=d.recommended_qualitative_followup_question,
            )
        )
    return rows


def add_missing_indicator_backfill_rows(
    config: RunConfig,
    catalog: list[IndicatorCatalogRow],
    observations: list[Observation],
) -> list[Observation]:
    log_step("Auditing missing indicators and adding backfill rows")
    existing_ids = {o.indicator_id for o in observations}
    backfill = []

    for row in catalog:
        if row.indicator_id in existing_ids:
            continue

        d = definition(row.indicator_id)
        backfill.append(
            make_observation(
                indicator_id=d.indicator_id,
                geography_name=config.service_area_name,
                geography_type="Service area",
                state_fips=config.state_fips,
                county_fips=config.county_fips,
                year_or_period="not pulled this run",
                source_latest_year_or_period="not pulled this run",
                estimate=None,
                moe=None,
                numerator=None,
                denominator=None,
                source_variable_label=f"{d.source_name}: catalog indicator existed, but no observation was returned by current adapter.",
                stratification="Total",
                comparison_available="No",
                data_quality_note="No estimate was returned for this indicator in this run. Backfill row prevents silent data loss.",
                source_url_or_endpoint=d.source_name,
            )
        )

    log_info(f"Added {len(backfill)} backfill rows")
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
    lines = [
        "# Citation Appendix",
        "",
        "| Citation ID | Label | Source | Agency/Author | Date | Reference | Notes |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in build_citation_registry().values():
        lines.append(
            f"| {c.citation_id} | {c.citation_label} | {c.source_title} | {c.source_agency_or_author} | {c.publication_date} | {c.url_or_file_reference} | {c.notes} |"
        )
    (OUTPUT_DIR / "citation_appendix.md").write_text("\n".join(lines), encoding="utf-8")


def write_data_availability(catalog: list[IndicatorCatalogRow], observations: list[Observation]) -> None:
    by_id: dict[str, list[Observation]] = {}
    for o in observations:
        by_id.setdefault(o.indicator_id, []).append(o)

    lines = [
        "# Data Availability",
        "",
        "| Domain | Indicator | Summary | Units | Unit source | Source | Citation | Latest logic | Latest year/period found | Filled? | Limitation | Follow-up |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]

    for r in catalog:
        obs_rows = by_id.get(r.indicator_id, [])
        latest = "; ".join(sorted({o.source_latest_year_or_period for o in obs_rows})) if obs_rows else ""
        filled = "No"
        if obs_rows:
            filled = "Yes" if any(o.estimate is not None for o in obs_rows) else "Partial"

        lines.append(
            f"| {r.needs_assessment_domain} | {r.indicator_name} | {r.indicator_summary} | {r.units} | {r.unit_source} | {r.source_name} | {r.source_citation_id} | {r.latest_logic} | {latest} | {filled} | {r.limitation} | {r.recommended_qualitative_followup_question} |"
        )

    (OUTPUT_DIR / "data_availability.md").write_text("\n".join(lines), encoding="utf-8")


def write_api_errors(errors: list[str], notes: list[str]) -> None:
    lines = [
        "# API Errors and Data Limitations",
        "",
        "## General limitations",
        "",
        "- Each source determines its own most recent available data; years will not necessarily match across sources.",
        "- ACS data requests require a valid Census API key in this environment.",
        "- The Census API key is masked in logs and metadata.",
        "- ACS latest-year detection probes variables.json and avoids future-year retry spam.",
        "- ACS URLs are built without double slashes.",
        "- ACS requests are chunked to prevent missing data from the Census API variable limit.",
        "- ACS MOE variables are optional; unavailable MOE variables are omitted without failing the indicator.",
        "- ACS special values such as -555555555 are converted to blank/null.",
        "- ACS estimates are period estimates and include margins of error when available.",
        "- ACS calculated percentages do not include derived margins of error.",
        "- ACS sum margins of error are approximated using square root of sum of squared MOEs.",
        "- PUBLIC_COVERAGE_RATE is not Medicaid-only coverage.",
        "- MEDICAID_COVERAGE_RATE is left as a placeholder unless a Medicaid-specific source is connected.",
        "- BLS LAUS annual average is preferred. If M13 is not returned, the script calculates a complete-year monthly average; if that is unavailable, it uses the latest monthly value and labels it.",
        "- CDC PLACES values are modeled estimates.",
        "- Internal and qualitative data cannot be filled from government APIs.",
        "",
        "## Run notes",
        "",
    ]

    if notes:
        lines.extend([f"- {note}" for note in notes])
    else:
        lines.append("No run notes captured.")

    lines.extend(["", "## Captured errors", ""])

    if errors:
        lines.extend([f"- {e}" for e in errors])
    else:
        lines.append("No API errors captured.")

    (OUTPUT_DIR / "api_errors_and_limitations.md").write_text("\n".join(lines), encoding="utf-8")


def write_readme(config: RunConfig) -> None:
    lines = [
        "# Needs Assessment Data Outputs",
        "",
        f"Service area: {config.service_area_name}",
        f"County: {config.county_name}",
        f"State FIPS: {config.state_fips}",
        f"County FIPS: {config.county_fips}",
        f"Comparison geographies: {', '.join(config.comparison_geographies)}",
        f"Census API key provided: {bool(config.census_api_key)}",
        "",
        "## Files",
        "",
        "- `indicator_catalog.csv`",
        "- `needs_assessment_data_long.csv`",
        "- `source_metadata.json`",
        "- `data_availability.md`",
        "- `citation_appendix.md`",
        "- `api_errors_and_limitations.md`",
        "",
        "## Census API key",
        "",
        "To pull ACS indicators, set `CENSUS_API_KEY` or pass `--census-api-key`.",
    ]
    (OUTPUT_DIR / "README.md").write_text("\n".join(lines), encoding="utf-8")


def write_outputs(
    config: RunConfig,
    catalog: list[IndicatorCatalogRow],
    observations: list[Observation],
    errors: list[str],
    notes: list[str],
) -> None:
    log_step("Writing output files")
    ensure_dirs()

    pd.DataFrame([asdict(r) for r in catalog]).to_csv(OUTPUT_DIR / "indicator_catalog.csv", index=False)
    pd.DataFrame([asdict(o) for o in observations]).to_csv(OUTPUT_DIR / "needs_assessment_data_long.csv", index=False)

    metadata = {
        "retrieved_at": now_iso(),
        "run_config": config_for_metadata(config),
        "citations": [asdict(c) for c in build_citation_registry().values()],
        "notes": notes,
        "errors": errors,
        "source_latest_summary": summarize_latest_by_source(observations),
        "unit_logic_summary": {
            "ACS counts": "people or households",
            "ACS dollars": "dollars",
            "ACS percentages": "percent",
            "ACS calculated percentages": "percent = numerator / denominator * 100",
            "ACS special values": "special values converted to null",
            "BLS LAUS unemployment": "annual average preferred",
            "CDC PLACES": "API unit normalized",
        },
    }

    (OUTPUT_DIR / "source_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    write_citation_appendix()
    write_data_availability(catalog, observations)
    write_api_errors(errors, notes)
    write_readme(config)

    log_info(f"Wrote outputs to {OUTPUT_DIR.resolve()}")


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
    parser.add_argument(
        "--census-api-key",
        default=os.getenv("CENSUS_API_KEY") or os.getenv("CENSUS_KEY"),
    )
    parser.add_argument(
        "--comparison-geographies",
        nargs="*",
        default=["county", "state", "us"],
        choices=["county", "state", "us"],
    )
    parser.add_argument("--verbose", action="store_true")

    args = parser.parse_args()

    return RunConfig(
        state_fips=normalize_fips(args.state_fips, 2),
        county_fips=normalize_fips(args.county_fips, 3),
        state_abbr=args.state_abbr.upper(),
        county_name=args.county_name,
        service_area_name=args.service_area_name,
        latest=True if args.latest or not args.years else False,
        years=args.years,
        census_api_key=args.census_api_key,
        comparison_geographies=args.comparison_geographies,
        verbose=args.verbose,
    )


def run(config: RunConfig) -> None:
    setup_logging(config.verbose)
    log_step("Starting needs assessment data pull")
    log_info(f"Service area: {config.service_area_name}")
    log_info(f"Comparison geographies: {', '.join(config.comparison_geographies)}")
    log_info(f"Census API key provided: {bool(config.census_api_key)}")

    ensure_dirs()
    errors: list[str] = []
    notes: list[str] = []

    log_step("Building indicator catalog")
    catalog = build_indicator_catalog()
    observations: list[Observation] = []

    observations.extend(extract_acs(config, errors, notes))
    observations.extend(extract_bls_laus(config, errors))
    observations.extend(extract_cdc_places(config, errors))
    observations.extend(add_current_source_placeholders(config))

    observations = add_missing_indicator_backfill_rows(config, catalog, observations)

    write_outputs(config, catalog, observations, errors, notes)

    extracted_ids = {o.indicator_id for o in observations if o.estimate is not None}
    all_ids = {r.indicator_id for r in catalog}
    missing_estimates = sorted(all_ids - extracted_ids)

    print("Done.")
    print(f"Output directory: {OUTPUT_DIR.resolve()}")
    print(f"Observation rows: {len(observations)}")
    print(f"Indicators in catalog: {len(all_ids)}")
    print(f"Indicators with numeric estimates: {len(extracted_ids)}")
    print(f"Indicators without numeric estimates: {len(missing_estimates)}")
    print(f"Run notes captured: {len(notes)}")
    print(f"Errors captured: {len(errors)}")


if __name__ == "__main__":
    run(parse_args())