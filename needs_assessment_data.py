#!/usr/bin/env python3
"""
needs_assessment_data.py

Single-file, citation-aware CCBHC community needs assessment data script.

Fixes included:
- ACS variables are chunked so the Census API does not silently fail from too many variables.
- ACS detailed-table rows are preserved and merged across chunks.
- Disability is split into count and prevalence, with correct units.
- Medicaid/public coverage issue is fixed:
  - PUBLIC_COVERAGE_RATE uses ACS DP03_0098PE.
  - MEDICAID_COVERAGE_RATE remains as a placeholder because ACS DP03 does not isolate Medicaid cleanly.
- CDC PLACES units are normalized from "%" to "percent."
- BLS LAUS parsing is safe and falls back from annual average to latest monthly if needed.
- Every catalog indicator gets a row in the output, even if the estimate is null.
- Every row includes summary, detailed description, unit definition, unit source, variable label, and citation.

Install:
    pip install requests pandas

Example:
    python needs_assessment_data.py \
      --state-fips 17 \
      --county-fips 031 \
      --state-abbr IL \
      --county-name "Cook County" \
      --service-area-name "Cook County, Illinois" \
      --latest

Optional:
    export CENSUS_API_KEY="your_key"
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import requests


OUTPUT_DIR = Path("outputs")
CACHE_DIR = Path("data/raw")
REQUEST_TIMEOUT = 45
REQUEST_SLEEP_SECONDS = 0.25
MAX_RETRIES = 3
CENSUS_MAX_GET_VARS = 45


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
        return None if math.isnan(f) else f

    s = str(value).strip()
    if s in {"", "null", "None", "NaN", "nan", "-", "**", "***", "N/A", "NA"}:
        return None

    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


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


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def cache_key(url: str, params: Optional[dict[str, Any]] = None, body: Optional[dict[str, Any]] = None) -> str:
    payload = json.dumps({"url": url, "params": params or {}, "body": body or {}}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cached_get_json(
    source_slug: str,
    url: str,
    params: Optional[dict[str, Any]] = None,
    errors: Optional[list[str]] = None,
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
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            time.sleep(REQUEST_SLEEP_SECONDS)
            response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)

            if response.status_code >= 400:
                last_error = f"{response.status_code}: {response.text[:500]}"
                time.sleep(attempt)
                continue

            data = response.json()
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return data

        except Exception as exc:
            last_error = repr(exc)
            time.sleep(attempt)

    if errors is not None:
        errors.append(f"GET JSON failed: {url} params={params} error={last_error}")
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

            data = response.json()
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return data

        except Exception as exc:
            last_error = repr(exc)
            time.sleep(attempt)

    if errors is not None:
        errors.append(f"POST JSON failed: {url} body={body} error={last_error}")
    return None


# --------------------------------------------------------------------------------------
# Citations
# --------------------------------------------------------------------------------------

def build_citation_registry() -> dict[str, Citation]:
    citations = [
        Citation(
            "CNA_CRITERIA_2023",
            "CCBHC Community Needs Assessment Criteria",
            "Certified Community Behavioral Health Clinic Requirements for Community Needs Assessment",
            "Uploaded project reference",
            "Third Horizon Strategies / SAMHSA criteria summary",
            "2023-11-06",
            "CCBHC Community Needs Assessment Criteria.pdf",
            "Defines required CNA elements: service area, mental health/SUD prevalence, SDOH, cultures/languages, underserved populations, staffing alignment, update plans, and stakeholder input.",
        ),
        Citation(
            "CCBHC_TOOLKIT_2024",
            "CCBHC CNA Toolkit",
            "CCBHC Community Needs Assessment Toolkit",
            "Uploaded toolkit",
            "National Council for Mental Wellbeing / CCBHC-E NTTAC",
            "2024-01",
            "CCBHC-Needs-Assessment-Toolkit.pdf",
            "Guidance for quantitative data, qualitative data, engagement, services, staffing, partnerships, and CQI.",
        ),
        Citation(
            "AXIS_2026_CNA",
            "Axis 2026 CCBHC CNA",
            "Certified Community Behavioral Health Clinic Community Needs Assessment, La Plata County, Colorado",
            "Uploaded needs assessment example",
            "Third Horizon for Axis Health System",
            "2026-03-23",
            "Axis Needs Assessment March 26 Final.pdf",
            "Example of a completed CNA using ACS, BRFSS, CDC/NVSS/WONDER, CDC PLACES, HRSA, NCES, Census, SAMHSA Buprenorphine Practitioner Locator, and N-SUMHSS.",
        ),
        Citation(
            "CENSUS_ACS_API",
            "U.S. Census ACS API",
            "American Community Survey API",
            "Government API",
            "U.S. Census Bureau",
            "Ongoing",
            "https://api.census.gov/data.html",
            "Source for population, demographics, language, poverty, housing, insurance, transportation, and SDOH indicators.",
        ),
        Citation(
            "BLS_LAUS_API",
            "BLS Public Data API",
            "Local Area Unemployment Statistics via BLS Public Data API",
            "Government API",
            "U.S. Bureau of Labor Statistics",
            "Ongoing",
            "https://api.bls.gov/publicAPI/v2/timeseries/data/",
            "Source for county labor force and unemployment indicators.",
        ),
        Citation(
            "CDC_PLACES_API",
            "CDC PLACES API",
            "CDC PLACES Local Data for Better Health",
            "Government API / Socrata",
            "Centers for Disease Control and Prevention",
            "Ongoing",
            "https://data.cdc.gov/",
            "Source for modeled county/local health indicators such as frequent mental distress, depression, binge drinking, and smoking.",
        ),
        Citation(
            "CDC_WONDER_NVSS",
            "CDC WONDER / NVSS",
            "CDC WONDER, National Vital Statistics System Mortality",
            "Government system / manual download fallback",
            "Centers for Disease Control and Prevention",
            "Ongoing",
            "https://wonder.cdc.gov/",
            "Source for suicide and drug overdose mortality; programmatic access may require manual query/download fallback.",
        ),
        Citation(
            "HRSA_DATA_WAREHOUSE",
            "HRSA Data Warehouse",
            "HRSA Data Warehouse and Area Health Resource Files",
            "Government API / public files",
            "Health Resources and Services Administration",
            "Ongoing",
            "https://data.hrsa.gov/",
            "Source for HPSA, MUA/P, health center, workforce, and resource indicators.",
        ),
        Citation(
            "SAMHSA_FINDTREATMENT",
            "SAMHSA FindTreatment / N-SUMHSS",
            "SAMHSA Behavioral Health Treatment Locator and N-SUMHSS",
            "Government locator / public files",
            "Substance Abuse and Mental Health Services Administration",
            "Ongoing",
            "https://findtreatment.gov/",
            "Source for behavioral health treatment facility availability and service infrastructure.",
        ),
        Citation(
            "NCES_CCD",
            "NCES Common Core of Data",
            "Common Core of Data",
            "Government data files / API where available",
            "National Center for Education Statistics",
            "Ongoing",
            "https://nces.ed.gov/ccd/",
            "Source for school enrollment, district demographics, and school poverty proxy indicators.",
        ),
        Citation(
            "HUD_PIT_HIC",
            "HUD PIT/HIC",
            "Point-in-Time Count and Housing Inventory Count",
            "Government public files",
            "U.S. Department of Housing and Urban Development",
            "Annual",
            "https://www.hudexchange.info/programs/hdx/pit-hic/",
            "Source for homelessness indicators, often available by Continuum of Care rather than county.",
        ),
        Citation(
            "INTERNAL_PLACEHOLDER",
            "Internal organization data placeholder",
            "Internal CCBHC data placeholder",
            "Internal data placeholder",
            "Client organization",
            "Project-specific",
            "Not available through government API",
            "Used for EHR/client demographics, service utilization, staffing, wait times, referrals, patient satisfaction, and qualitative findings.",
        ),
    ]
    return {c.citation_id: c for c in citations}


def citation_text(citation_id: str) -> str:
    c = build_citation_registry()[citation_id]
    return f"{c.citation_label}: {c.source_title}. {c.source_agency_or_author}. {c.publication_date}. {c.url_or_file_reference}."


# --------------------------------------------------------------------------------------
# Indicator definitions
# --------------------------------------------------------------------------------------

def build_indicator_definitions() -> dict[str, IndicatorDefinition]:
    defs: dict[str, IndicatorDefinition] = {}

    def add(
        indicator_id: str,
        indicator_name: str,
        domain: str,
        summary: str,
        detail: str,
        units: str,
        unit_definition: str,
        unit_source: str,
        source_name: str,
        source_agency: str,
        mode: str,
        geography: str,
        update: str,
        latest_logic: str,
        api_available: str,
        download_available: str,
        internal_required: str,
        citation_id: str,
        limitation: str,
        followup: str,
    ) -> None:
        defs[indicator_id] = IndicatorDefinition(
            indicator_id=indicator_id,
            indicator_name=indicator_name,
            needs_assessment_domain=domain,
            indicator_summary=summary,
            indicator_detailed_description=detail,
            units=units,
            unit_definition=unit_definition,
            unit_source=unit_source,
            source_name=source_name,
            source_agency=source_agency,
            api_or_download=mode,
            expected_geography_level=geography,
            expected_update_frequency=update,
            latest_logic=latest_logic,
            government_api_available=api_available,
            public_download_available=download_available,
            internal_or_qualitative_required=internal_required,
            source_citation_id=citation_id,
            limitation=limitation,
            recommended_qualitative_followup_question=followup,
        )

    latest_acs = "Probe ACS 5-year endpoints newest-to-oldest and use newest valid ACS source year."
    latest_places = "Try candidate CDC PLACES Socrata endpoints and select newest matching county row returned."
    latest_manual = "Track as current-source placeholder; requires current download/API connector and source-specific schema handling."

    add("POP_TOTAL", "Total population", "Service Area and Population", "Total number of residents in the service area.", "Establishes the denominator for rate calculations, client-population comparison, staffing scale, and service capacity planning.", "people", "Number of people residing in the geography.", "Static indicator definition based on ACS count variable.", "ACS 5-year Detailed Tables", "U.S. Census Bureau", "API", "County, state, U.S.", "Annual", latest_acs, "Yes", "Yes", "No", "CENSUS_ACS_API", "ACS 5-year period estimate.", "Do partners perceive recent population changes not yet reflected in ACS?")
    add("POP_AGE_UNDER_18_COUNT", "Population under age 18", "Demographics", "Number of children and youth under 18.", "Supports assessment of youth treatment, school partnerships, and family-support needs.", "people", "Number of people under age 18.", "Static indicator definition based on ACS count variable.", "ACS 5-year Detailed Tables", "U.S. Census Bureau", "API", "County, state, U.S.", "Annual", latest_acs, "Yes", "Yes", "No", "CENSUS_ACS_API", "Does not directly measure behavioral health need.", "Which youth groups have the largest unmet need?")
    add("POP_AGE_UNDER_18_PCT", "Population under age 18 percentage", "Demographics", "Share of residents under 18.", "Supports comparison of youth share of community versus youth share of clients served.", "percent", "Under-18 population divided by total population, multiplied by 100.", "Calculated by script from ACS numerator and denominator.", "ACS 5-year Detailed Tables", "U.S. Census Bureau", "API / calculated", "County, state, U.S.", "Annual", latest_acs, "Yes", "Yes", "No", "CENSUS_ACS_API", "Derived percentage MOE is not calculated.", "Are youth represented in services proportionally?")
    add("POP_AGE_65_PLUS_COUNT", "Population age 65 and older", "Demographics", "Number of residents age 65 and older.", "Supports assessment of older adult behavioral health, disability, social isolation, and care coordination needs.", "people", "Number of people age 65 and older.", "Calculated by script from ACS age-by-sex count cells.", "ACS 5-year Detailed Tables", "U.S. Census Bureau", "API / calculated", "County, state, U.S.", "Annual", latest_acs, "Yes", "Yes", "No", "CENSUS_ACS_API", "MOE for sum is approximated.", "Which older adult needs are most visible?")
    add("POP_AGE_65_PLUS_PCT", "Population age 65 and older percentage", "Demographics", "Share of residents age 65 and older.", "Helps determine whether services, hours, transportation, and care coordination align to older adult needs.", "percent", "Age 65+ population divided by total population, multiplied by 100.", "Calculated by script from ACS numerator and denominator.", "ACS 5-year Detailed Tables", "U.S. Census Bureau", "API / calculated", "County, state, U.S.", "Annual", latest_acs, "Yes", "Yes", "No", "CENSUS_ACS_API", "Derived percentage MOE is not calculated.", "Are older adults represented in services proportionally?")
    add("HISPANIC_LATINX_COUNT", "Hispanic or Latinx population", "Demographics", "Number of residents who identify as Hispanic or Latinx.", "Supports culturally and linguistically appropriate outreach, bilingual staffing, and disparity analysis.", "people", "Number of people identifying as Hispanic or Latinx, any race.", "Static indicator definition based on ACS count variable.", "ACS 5-year Detailed Tables", "U.S. Census Bureau", "API", "County, state, U.S.", "Annual", latest_acs, "Yes", "Yes", "No", "CENSUS_ACS_API", "Small-area MOEs may be large.", "Are Hispanic/Latinx residents proportionately served?")
    add("HISPANIC_LATINX_PCT", "Hispanic or Latinx population percentage", "Demographics", "Share of residents who identify as Hispanic or Latinx.", "Helps compare community demographics with client demographics and language capacity.", "percent", "Hispanic/Latinx population divided by total population, multiplied by 100.", "Calculated by script from ACS numerator and denominator.", "ACS 5-year Detailed Tables", "U.S. Census Bureau", "API / calculated", "County, state, U.S.", "Annual", latest_acs, "Yes", "Yes", "No", "CENSUS_ACS_API", "Derived percentage MOE is not calculated.", "What cultural or linguistic barriers affect access?")
    add("AIAN_POPULATION_COUNT", "American Indian and Alaska Native population", "Demographics", "Number of residents identifying as American Indian or Alaska Native alone.", "Supports assessment of Indigenous community needs, tribal partnerships, and cultural responsiveness.", "people", "Number of people identifying as American Indian or Alaska Native alone.", "Static indicator definition based on ACS count variable.", "ACS 5-year Detailed Tables", "U.S. Census Bureau", "API", "County, state, U.S.", "Annual", latest_acs, "Yes", "Yes", "No", "CENSUS_ACS_API", "Does not capture tribal affiliation or multiracial identity.", "What tribal or Indigenous-serving partners should be engaged?")
    add("AIAN_POPULATION_PCT", "American Indian and Alaska Native population percentage", "Demographics", "Share of residents identifying as American Indian or Alaska Native alone.", "Supports comparison of community population and served-client demographics.", "percent", "AIAN-alone population divided by total population, multiplied by 100.", "Calculated by script from ACS numerator and denominator.", "ACS 5-year Detailed Tables", "U.S. Census Bureau", "API / calculated", "County, state, U.S.", "Annual", latest_acs, "Yes", "Yes", "No", "CENSUS_ACS_API", "Derived percentage MOE is not calculated.", "Are Indigenous residents represented in services and partnerships?")
    add("VETERAN_POPULATION_COUNT", "Veteran population", "Underserved Populations", "Number of civilian veterans.", "Supports veteran outreach, trauma-informed care, suicide prevention, and coordination with VA/community veteran resources.", "people", "Number of civilian veterans age 18 and older.", "Static indicator definition based on ACS count variable.", "ACS 5-year Detailed Tables", "U.S. Census Bureau", "API", "County, state, U.S.", "Annual", latest_acs, "Yes", "Yes", "No", "CENSUS_ACS_API", "Does not directly measure veteran BH need or VA eligibility.", "Are veterans able to access timely care?")
    add("VETERAN_POPULATION_PCT", "Veteran population percentage", "Underserved Populations", "Share of adult civilian population that is veteran.", "Helps compare veteran community size with veteran representation among clients.", "percent", "Veteran population divided by civilian population age 18+, multiplied by 100.", "Calculated by script from ACS numerator and denominator.", "ACS 5-year Detailed Tables", "U.S. Census Bureau", "API / calculated", "County, state, U.S.", "Annual", latest_acs, "Yes", "Yes", "No", "CENSUS_ACS_API", "Derived percentage MOE is not calculated.", "Are veteran-specific needs visible in referrals or crisis contacts?")
    add("DISABILITY_COUNT", "Population with a disability", "Underserved Populations", "Number of residents with a disability.", "Supports assessment of accessibility, care coordination, physical access, telehealth access, and integrated care needs.", "people", "Number of civilian noninstitutionalized people with a disability.", "Static indicator definition based on ACS subject count variable.", "ACS 5-year Subject Tables", "U.S. Census Bureau", "API", "County, state, U.S.", "Annual", latest_acs, "Yes", "Yes", "No", "CENSUS_ACS_API", "ACS disability categories may not map directly to behavioral health need.", "What accessibility barriers affect care?")
    add("DISABILITY_PREVALENCE", "Disability prevalence", "Underserved Populations", "Share of residents with a disability.", "Supports comparison of disability prevalence across county, state, and U.S.", "percent", "Percentage of civilian noninstitutionalized population with a disability.", "Static indicator definition based on ACS subject percent variable.", "ACS 5-year Subject Tables", "U.S. Census Bureau", "API", "County, state, U.S.", "Annual", latest_acs, "Yes", "Yes", "No", "CENSUS_ACS_API", "ACS disability categories may not map directly to behavioral health need.", "What accessibility barriers affect care?")
    add("LANGUAGE_LEP_SPANISH_COUNT", "Spanish speakers who speak English less than very well", "Culture and Language", "Number of Spanish-speaking residents with limited English proficiency.", "Supports bilingual staff, interpretation, translation, outreach materials, and culturally responsive access planning.", "people", "Number of people age 5+ who speak Spanish at home and speak English less than very well.", "Static indicator definition based on ACS count variable.", "ACS 5-year Detailed Tables", "U.S. Census Bureau", "API", "County, state, U.S.", "Annual", latest_acs, "Yes", "Yes", "No", "CENSUS_ACS_API", "Spanish LEP proxy only, not all LEP residents.", "Which language supports are most urgent?")
    add("LANGUAGE_LEP_SPANISH_PCT", "Spanish limited-English-proficiency percentage", "Culture and Language", "Share of residents age 5+ who speak Spanish at home and speak English less than very well.", "Helps size Spanish-language assistance needs relative to population age 5+.", "percent", "Spanish LEP population divided by population age 5+, multiplied by 100.", "Calculated by script from ACS numerator and denominator.", "ACS 5-year Detailed Tables", "U.S. Census Bureau", "API / calculated", "County, state, U.S.", "Annual", latest_acs, "Yes", "Yes", "No", "CENSUS_ACS_API", "Derived percentage MOE is not calculated.", "Do staffing and interpretation resources match language needs?")
    add("POVERTY_RATE", "Population below poverty level", "Economic Stability", "Percentage of residents below the federal poverty level.", "Supports assessment of economic hardship as a barrier to behavioral health access, insurance, transportation, housing, nutrition, and ability to pay.", "percent", "Percentage of people for whom poverty status is determined who are below the federal poverty level.", "Static indicator definition based on ACS profile percentage variable.", "ACS 5-year Data Profile", "U.S. Census Bureau", "API", "County, state, U.S.", "Annual", latest_acs, "Yes", "Yes", "No", "CENSUS_ACS_API", "ACS poverty estimates may lag rapid local changes.", "How is economic hardship affecting access?")
    add("MEDIAN_HOUSEHOLD_INCOME", "Median household income", "Economic Stability", "Median household income in dollars.", "Provides broad household economic context for affordability, transportation, housing, and access barriers.", "dollars", "Median household income in inflation-adjusted dollars for the ACS period.", "Static indicator definition based on ACS dollar variable.", "ACS 5-year Detailed Tables", "U.S. Census Bureau", "API", "County, state, U.S.", "Annual", latest_acs, "Yes", "Yes", "No", "CENSUS_ACS_API", "Median may mask high-cost local pressures.", "Are income trends aligned with lived experience?")
    add("UNINSURED_RATE", "Uninsured rate", "Insurance Coverage", "Percentage of residents without health insurance coverage.", "Supports assessment of safety-net role, charity/sliding-fee needs, and outreach to uninsured residents.", "percent", "Percentage of civilian noninstitutionalized population with no health insurance coverage.", "Static indicator definition based on ACS profile percentage variable.", "ACS 5-year Data Profile", "U.S. Census Bureau", "API", "County, state, U.S.", "Annual", latest_acs, "Yes", "Yes", "No", "CENSUS_ACS_API", "Insurance coverage does not equal access to affordable care.", "Do uninsured residents know where to receive care?")
    add("PUBLIC_COVERAGE_RATE", "Public health insurance coverage rate", "Insurance Coverage", "Percentage of residents with public health insurance coverage.", "Supports payer-mix and safety-net analysis. This is public coverage, not Medicaid-only.", "percent", "Percentage of civilian noninstitutionalized population with public health insurance coverage.", "Static indicator definition based on ACS profile percentage variable DP03_0098PE.", "ACS 5-year Data Profile", "U.S. Census Bureau", "API", "County, state, U.S.", "Annual", latest_acs, "Yes", "Yes", "No", "CENSUS_ACS_API", "This ACS profile variable is public coverage, not Medicaid-only.", "Are publicly insured residents able to access timely BH/SUD care?")
    add("MEDICAID_COVERAGE_RATE", "Medicaid coverage rate", "Insurance Coverage", "Percentage of residents covered by Medicaid.", "Medicaid-specific coverage may require ACS detailed tables or CMS/state administrative data. This script does not fabricate it from public-coverage data.", "percent", "Medicaid-covered population divided by relevant population, multiplied by 100.", "Placeholder; requires Medicaid-specific table or administrative source.", "CMS Medicaid / ACS detailed Medicaid table", "CMS / U.S. Census Bureau", "Manual/API extension needed", "County, state, U.S. depending source", "Varies", latest_manual, "Partial", "Yes", "No", "CENSUS_ACS_API", "Do not substitute public insurance coverage for Medicaid-only coverage.", "Are Medicaid members able to access timely behavioral health care?")
    add("NO_VEHICLE_HOUSEHOLDS_COUNT", "Households with no vehicle available", "Transportation", "Number of households with no vehicle available.", "Supports transportation-barrier assessment and mobile/telehealth planning.", "households", "Number of households reporting no vehicle available.", "Static indicator definition based on ACS count variable.", "ACS 5-year Detailed Tables", "U.S. Census Bureau", "API", "County, state, U.S.", "Annual", latest_acs, "Yes", "Yes", "No", "CENSUS_ACS_API", "Does not measure public transit or distance to care.", "Where do transportation barriers most affect care?")
    add("NO_VEHICLE_HOUSEHOLDS_PCT", "Households with no vehicle available percentage", "Transportation", "Share of households with no vehicle available.", "Quantifies transportation vulnerability relative to all households.", "percent", "No-vehicle households divided by total households, multiplied by 100.", "Calculated by script from ACS numerator and denominator.", "ACS 5-year Detailed Tables", "U.S. Census Bureau", "API / calculated", "County, state, U.S.", "Annual", latest_acs, "Yes", "Yes", "No", "CENSUS_ACS_API", "Derived percentage MOE is not calculated.", "Are transit, rides, telehealth, or mobile services needed?")
    add("SNAP_HOUSEHOLDS_COUNT", "Households receiving SNAP", "Food/Nutrition", "Number of households receiving SNAP.", "Proxy for food/economic insecurity and partnership needs.", "households", "Number of households receiving Food Stamps/SNAP in the past 12 months.", "Static indicator definition based on ACS count variable.", "ACS 5-year Detailed Tables", "U.S. Census Bureau", "API", "County, state, U.S.", "Annual", latest_acs, "Yes", "Yes", "No", "CENSUS_ACS_API", "SNAP participation is not full food insecurity.", "Do residents report food insecurity?")
    add("SNAP_HOUSEHOLDS_PCT", "Households receiving SNAP percentage", "Food/Nutrition", "Share of households receiving SNAP.", "Shows SNAP participation relative to all households.", "percent", "SNAP households divided by total households, multiplied by 100.", "Calculated by script from ACS numerator and denominator.", "ACS 5-year Detailed Tables", "U.S. Census Bureau", "API / calculated", "County, state, U.S.", "Annual", latest_acs, "Yes", "Yes", "No", "CENSUS_ACS_API", "Derived percentage MOE is not calculated.", "Are food needs appearing in behavioral health settings?")
    add("RENT_BURDENED_HOUSEHOLDS_COUNT", "Rent-burdened households", "Housing Stability", "Number of renter households spending 30% or more of income on rent.", "Supports housing affordability and recovery-stability analysis.", "households", "Renter households with gross rent equal to 30% or more of income.", "Calculated by script from ACS rent-burden category counts.", "ACS 5-year Detailed Tables", "U.S. Census Bureau", "API / calculated", "County, state, U.S.", "Annual", latest_acs, "Yes", "Yes", "No", "CENSUS_ACS_API", "MOE for sum approximated.", "How is housing affordability affecting recovery?")
    add("RENT_BURDENED_HOUSEHOLDS_PCT", "Rent-burdened households percentage", "Housing Stability", "Share of renter households spending 30% or more of income on rent.", "Quantifies housing affordability pressure among renters.", "percent", "Rent-burdened renter households divided by renter households with rent-burden data, multiplied by 100.", "Calculated by script from ACS numerator and denominator.", "ACS 5-year Detailed Tables", "U.S. Census Bureau", "API / calculated", "County, state, U.S.", "Annual", latest_acs, "Yes", "Yes", "No", "CENSUS_ACS_API", "Derived percentage MOE is not calculated.", "How often does housing cost interfere with treatment?")
    add("MEDIAN_HOME_VALUE", "Median home value", "Housing Stability", "Median value of owner-occupied housing units.", "Supports interpretation of local cost pressures that may affect clients and workforce.", "dollars", "Median value in dollars for owner-occupied housing units.", "Static indicator definition based on ACS dollar variable.", "ACS 5-year Detailed Tables", "U.S. Census Bureau", "API", "County, state, U.S.", "Annual", latest_acs, "Yes", "Yes", "No", "CENSUS_ACS_API", "Does not measure rental availability or homelessness.", "Are housing costs contributing to workforce or client instability?")
    add("UNEMPLOYMENT_RATE", "Unemployment rate", "Economic Stability", "Annual average unemployment rate.", "Labor-market context for economic hardship and employment-related stress.", "percent", "Unemployed labor force divided by total labor force, multiplied by 100.", "Static source definition based on BLS LAUS unemployment-rate series.", "BLS LAUS", "U.S. Bureau of Labor Statistics", "API", "County", "Monthly / annual", "Query recent years and select newest annual average M13 returned; fall back to latest monthly.", "Yes", "Yes", "No", "BLS_LAUS_API", "County series ID is inferred and should be validated.", "Are employment barriers contributing to treatment access needs?")
    add("FREQUENT_MENTAL_DISTRESS", "Frequent mental distress", "Mental Health Prevalence and Outcomes", "Estimated prevalence of frequent mental distress.", "Estimates adults reporting frequent mental distress and helps identify population-level mental health burden.", "percent", "CDC PLACES data value unit, usually percent of adults.", "API metadata when available; normalized to percent.", "CDC PLACES", "Centers for Disease Control and Prevention", "API / Socrata", "County", "Annual release", latest_places, "Partial", "Yes", "No", "CDC_PLACES_API", "Modeled estimate; validate release and measure.", "Does modeled distress align with stakeholder experience?")
    add("DEPRESSION_PREVALENCE", "Depression prevalence", "Mental Health Prevalence and Outcomes", "Estimated prevalence of depression.", "Estimates adults reporting depression/current depression depending on PLACES measure returned.", "percent", "CDC PLACES data value unit, usually percent of adults.", "API metadata when available; normalized to percent.", "CDC PLACES", "Centers for Disease Control and Prevention", "API / Socrata", "County", "Annual release", latest_places, "Partial", "Yes", "No", "CDC_PLACES_API", "Modeled estimate; label should be reviewed.", "Are depression needs presenting differently by population?")
    add("BINGE_DRINKING", "Binge drinking", "Substance Use Prevalence and Outcomes", "Estimated prevalence of binge drinking.", "Estimates alcohol-related risk and supports substance-use prevention/treatment planning.", "percent", "CDC PLACES data value unit, usually percent of adults.", "API metadata when available; normalized to percent.", "CDC PLACES", "Centers for Disease Control and Prevention", "API / Socrata", "County", "Annual release", latest_places, "Partial", "Yes", "No", "CDC_PLACES_API", "Modeled estimate; not SUD diagnosis.", "How are alcohol-related needs showing up locally?")
    add("CURRENT_SMOKING", "Current smoking", "Physical Health and Co-occurring Conditions", "Estimated prevalence of current smoking.", "Supports integrated care assessment for co-occurring physical health risks.", "percent", "CDC PLACES data value unit, usually percent of adults.", "API metadata when available; normalized to percent.", "CDC PLACES", "Centers for Disease Control and Prevention", "API / Socrata", "County", "Annual release", latest_places, "Partial", "Yes", "No", "CDC_PLACES_API", "Modeled estimate.", "Are tobacco and co-occurring needs addressed in integrated care?")
    add("SUICIDE_MORTALITY", "Suicide deaths and age-adjusted suicide mortality rate", "Mental Health Prevalence and Outcomes", "Suicide mortality burden.", "Should include deaths and age-adjusted mortality rate once CDC WONDER/NVSS data are downloaded.", "deaths per 100,000", "Age-adjusted suicide deaths per 100,000 population.", "Static indicator definition for CDC WONDER/NVSS mortality output.", "CDC WONDER / NVSS", "Centers for Disease Control and Prevention", "Manual download fallback", "County, state, U.S.", "Annual", latest_manual, "Partial", "Yes", "No", "CDC_WONDER_NVSS", "Programmatic access may be limited; small counts suppressed.", "What suicide-prevention needs are not visible in mortality data?")
    add("OVERDOSE_MORTALITY", "Drug overdose deaths and age-adjusted overdose mortality rate", "Substance Use Prevalence and Outcomes", "Drug overdose mortality burden.", "Should include deaths and age-adjusted mortality rate once CDC WONDER/NVSS data are downloaded.", "deaths per 100,000", "Age-adjusted drug overdose deaths per 100,000 population.", "Static indicator definition for CDC WONDER/NVSS mortality output.", "CDC WONDER / NVSS", "Centers for Disease Control and Prevention", "Manual download fallback", "County, state, U.S.", "Annual", latest_manual, "Partial", "Yes", "No", "CDC_WONDER_NVSS", "Programmatic access may be limited; small counts suppressed.", "Which substances and overdose risks are visible locally?")
    add("MENTAL_HEALTH_HPSA", "Mental Health HPSA status", "Workforce and Provider Availability", "Mental health professional shortage designation.", "Identifies whether the service area or portions of it are designated as having mental health workforce shortages.", "designation", "Categorical shortage-area designation or related HPSA status/score.", "Static indicator definition for HRSA HPSA output.", "HRSA Data Warehouse", "Health Resources and Services Administration", "API / public files", "County, tract, facility, or service area", "Ongoing", latest_manual, "Partial", "Yes", "No", "HRSA_DATA_WAREHOUSE", "Boundaries may not align with county.", "Which workforce shortages most affect access?")
    add("TREATMENT_FACILITIES_MH", "Mental health treatment facilities", "Treatment Facilities and Service Infrastructure", "Mental health treatment facility availability.", "Counts or lists mental health treatment facilities serving the geography, validated locally for access and capacity.", "facilities", "Count of facilities or facility records.", "Static indicator definition for SAMHSA facility files.", "SAMHSA FindTreatment / N-SUMHSS", "Substance Abuse and Mental Health Services Administration", "Locator / public files", "Address-level; can aggregate to county", "Annual / ongoing", latest_manual, "Partial", "Yes", "No", "SAMHSA_FINDTREATMENT", "Listings may not reflect capacity or payer acceptance.", "Which listed services are actually available?")
    add("TREATMENT_FACILITIES_SUD", "SUD treatment facilities and MOUD availability", "Treatment Facilities and Service Infrastructure", "SUD treatment and MOUD service availability.", "Counts/lists SUD treatment facilities and MOUD capacity to identify SUD continuum gaps.", "facilities", "Count of facilities or facility records.", "Static indicator definition for SAMHSA facility files.", "SAMHSA FindTreatment / N-SUMHSS", "Substance Abuse and Mental Health Services Administration", "Locator / public files", "Address-level; can aggregate to county", "Annual / ongoing", latest_manual, "Partial", "Yes", "No", "SAMHSA_FINDTREATMENT", "Listings may not reflect real-time capacity.", "Where are SUD level-of-care gaps?")
    add("HOMELESSNESS_PIT", "People experiencing homelessness", "Housing Stability", "Homelessness count.", "Uses HUD PIT/HIC or local PIT data to estimate people experiencing homelessness.", "people", "Number of people counted as experiencing homelessness.", "Static indicator definition for HUD PIT/HIC output.", "HUD PIT/HIC", "U.S. Department of Housing and Urban Development", "Public download", "CoC; county if crosswalked", "Annual", latest_manual, "No", "Yes", "No", "HUD_PIT_HIC", "Often reported by CoC rather than county.", "What housing instability is missed by PIT counts?")
    add("SCHOOL_ENROLLMENT", "School enrollment and student demographics", "Children, Youth, and Families", "Student enrollment.", "Supports youth population context, school partnerships, and school-based behavioral health access planning.", "students", "Number of enrolled students.", "Static indicator definition for NCES CCD output.", "NCES Common Core of Data", "National Center for Education Statistics", "API / public files", "School, district, county crosswalk", "Annual", latest_manual, "Partial", "Yes", "No", "NCES_CCD", "County aggregation may require crosswalk.", "Which school partners should be engaged?")
    add("CLIENT_DEMOGRAPHICS", "CCBHC client demographics", "Underserved Populations", "Client demographic profile.", "Internal indicator needed to compare served population with service area population.", "clients", "Number or percentage of clients, depending on internal file.", "Internal data placeholder.", "Internal EHR / client data", "Client organization", "Internal file", "Client/service area", "Project-specific", "Load from internal file.", "No", "No", "Yes", "INTERNAL_PLACEHOLDER", "Not available through government APIs.", "Which populations are underrepresented among clients?")
    add("SERVICE_UTILIZATION", "Service utilization, referrals, wait times, no-shows", "Access to Care", "Service access and operational utilization.", "Captures actual access patterns and bottlenecks.", "varies", "Depends on internal field: visits, clients, days, referrals, or percent.", "Internal data placeholder.", "Internal operations data", "Client organization", "Internal file", "Client/service area", "Project-specific", "Load from internal file.", "No", "No", "Yes", "INTERNAL_PLACEHOLDER", "Not available through government APIs.", "Where do operational data show bottlenecks?")
    add("STAFFING_PLAN", "Staffing plan, FTEs, credentials, turnover, training", "Staffing Implications", "Staffing capacity and alignment.", "Links CNA findings to staffing changes, roles, credentials, language capacity, peer roles, and care coordination.", "FTE / roles", "Full-time equivalents, positions, credentials, vacancies, turnover, or training counts.", "Internal data placeholder.", "Internal staffing plan", "Client organization", "Internal file", "Client/service area", "Project-specific", "Load from internal file.", "No", "No", "Yes", "INTERNAL_PLACEHOLDER", "Not available through government APIs.", "What staffing changes address identified needs?")
    add("QUALITATIVE_THEMES", "Interview, focus group, advisory board, and survey themes", "Qualitative Findings", "Themes from primary qualitative research.", "Captures stakeholder, client, partner, and community perspectives explaining quantitative trends.", "themes", "Qualitative themes, coded findings, quotes, or theme counts.", "Internal qualitative analysis placeholder.", "Primary qualitative research", "Client organization / consultant", "Internal file", "Service area", "Project-specific", "Analyze qualitative files.", "No", "No", "Yes", "CCBHC_TOOLKIT_2024", "Not statistically representative unless designed that way.", "What explains the quantitative patterns?")

    return defs


def definition(indicator_id: str) -> IndicatorDefinition:
    return build_indicator_definitions()[indicator_id]


# --------------------------------------------------------------------------------------
# Observation helper
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
    units = units_override or d.units
    unit_definition = unit_definition_override or d.unit_definition
    unit_source = unit_source_override or d.unit_source

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
        units=units,
        unit_definition=unit_definition,
        unit_source=unit_source,
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
# ACS extraction
# --------------------------------------------------------------------------------------

def detect_latest_acs_year(config: RunConfig, errors: list[str]) -> int:
    for year in range(dt.date.today().year, dt.date.today().year - 10, -1):
        url = f"https://api.census.gov/data/{year}/acs/acs5"
        params = {"get": "NAME", "for": "us:1"}
        if config.census_api_key:
            params["key"] = config.census_api_key

        data = cached_get_json("census_acs_detect", url, params=params, errors=None)
        if isinstance(data, list) and len(data) > 1:
            return year

    errors.append("Could not detect latest ACS 5-year year; defaulted to 2024.")
    return 2024


def fetch_census_variable_metadata(
    year: int,
    dataset_suffix: str,
    errors: list[str],
) -> dict[str, dict[str, str]]:
    url = f"https://api.census.gov/data/{year}/acs/acs5/{dataset_suffix}/variables.json".rstrip("/")
    data = cached_get_json("census_acs_variables", url, params={}, errors=errors)

    if not isinstance(data, dict):
        return {}

    variables = data.get("variables")
    if not isinstance(variables, dict):
        return {}

    out: dict[str, dict[str, str]] = {}
    for var, meta in variables.items():
        if isinstance(meta, dict):
            out[var] = {
                "label": str(meta.get("label", "")),
                "concept": str(meta.get("concept", "")),
                "predicateType": str(meta.get("predicateType", "")),
            }
    return out


def label_for_vars(metadata: dict[str, dict[str, str]], vars_used: list[str]) -> str:
    pieces = []
    for v in vars_used:
        meta = metadata.get(v, {})
        label = meta.get("label") or ""
        concept = meta.get("concept") or ""
        if label and concept:
            pieces.append(f"{v}: {concept} - {label}")
        elif label:
            pieces.append(f"{v}: {label}")
        else:
            pieces.append(v)
    return "; ".join(pieces)


def census_request(
    config: RunConfig,
    year: int,
    dataset_suffix: str,
    variables: list[str],
    geography: str,
    errors: list[str],
) -> Optional[dict[str, Any]]:
    """
    Requests ACS variables and chunks them so the Census API variable limit is not exceeded.
    Returns a merged record across chunks.
    """
    url = f"https://api.census.gov/data/{year}/acs/acs5/{dataset_suffix}".rstrip("/")
    merged: dict[str, Any] = {}

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

        data = cached_get_json("census_acs", url, params=params, errors=errors)
        if not isinstance(data, list) or len(data) < 2:
            continue

        row = dict(zip(data[0], data[1]))
        merged.update(row)

    return merged if merged else None


def extract_acs(config: RunConfig, errors: list[str]) -> list[Observation]:
    observations: list[Observation] = []

    year = detect_latest_acs_year(config, errors)
    source_latest = str(year)

    meta_detailed = fetch_census_variable_metadata(year, "", errors)
    meta_profile = fetch_census_variable_metadata(year, "profile", errors)
    meta_subject = fetch_census_variable_metadata(year, "subject", errors)

    geos = {
        "county": ("County", config.state_fips, config.county_fips),
        "state": ("State", config.state_fips, ""),
        "us": ("United States", "", ""),
    }

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
        "B25070_007E", "B25070_007M",
        "B25070_008E", "B25070_008M",
        "B25070_009E", "B25070_009M",
        "B25070_010E", "B25070_010M",
        "B25077_001E", "B25077_001M",
        "B19013_001E", "B19013_001M",
        "B01001_020E", "B01001_020M",
        "B01001_021E", "B01001_021M",
        "B01001_022E", "B01001_022M",
        "B01001_023E", "B01001_023M",
        "B01001_024E", "B01001_024M",
        "B01001_025E", "B01001_025M",
        "B01001_044E", "B01001_044M",
        "B01001_045E", "B01001_045M",
        "B01001_046E", "B01001_046M",
        "B01001_047E", "B01001_047M",
        "B01001_048E", "B01001_048M",
        "B01001_049E", "B01001_049M",
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

    for geo, (geo_type, st, co) in geos.items():
        rec = census_request(config, year, "", detailed_vars, geo, errors)
        if rec:
            geo_name = rec.get("NAME", geo)
            endpoint = f"https://api.census.gov/data/{year}/acs/acs5"
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
                source_variable_label=label_for_vars(meta_detailed, ["B01001_001E", "B01001_001M"]),
                source_url_or_endpoint=endpoint,
                note="Most recent ACS 5-year detailed table detected independently for ACS source.",
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
                source_variable_label=label_for_vars(meta_detailed, ["B09001_001E", "B01001_001E"]),
                source_url_or_endpoint=endpoint,
                note="Most recent ACS 5-year detailed table detected independently for ACS source.",
            )

            age65_vars = [
                "B01001_020", "B01001_021", "B01001_022", "B01001_023", "B01001_024", "B01001_025",
                "B01001_044", "B01001_045", "B01001_046", "B01001_047", "B01001_048", "B01001_049",
            ]
            age65_count = sum_values([safe_float(rec.get(f"{v}E")) for v in age65_vars])
            age65_moe = sum_moes([safe_float(rec.get(f"{v}M")) for v in age65_vars])

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
                source_variable_label=label_for_vars(meta_detailed, [f"{v}E" for v in age65_vars] + ["B01001_001E"]),
                source_url_or_endpoint=endpoint,
                note="65+ count calculated from ACS B01001 age/sex cells. Sum MOE approximated.",
            )

            hispanic = safe_float(rec.get("B03003_003E"))
            add_count_and_percent_observations(
                observations,
                count_indicator_id="HISPANIC_LATINX_COUNT",
                pct_indicator_id="HISPANIC_LATINX_PCT",
                numerator=hispanic,
                numerator_moe=safe_float(rec.get("B03003_003M")),
                denominator=total,
                geography_name=geo_name,
                geography_type=geo_type,
                state_fips=st,
                county_fips=co,
                year=year,
                source_latest=source_latest,
                source_variable_label=label_for_vars(meta_detailed, ["B03003_003E", "B01001_001E"]),
                source_url_or_endpoint=endpoint,
                note="Most recent ACS 5-year detailed table detected independently for ACS source.",
            )

            aian = safe_float(rec.get("B02001_004E"))
            add_count_and_percent_observations(
                observations,
                count_indicator_id="AIAN_POPULATION_COUNT",
                pct_indicator_id="AIAN_POPULATION_PCT",
                numerator=aian,
                numerator_moe=safe_float(rec.get("B02001_004M")),
                denominator=total,
                geography_name=geo_name,
                geography_type=geo_type,
                state_fips=st,
                county_fips=co,
                year=year,
                source_latest=source_latest,
                source_variable_label=label_for_vars(meta_detailed, ["B02001_004E", "B01001_001E"]),
                source_url_or_endpoint=endpoint,
                note="Most recent ACS 5-year detailed table detected independently for ACS source.",
            )

            veteran = safe_float(rec.get("B21001_002E"))
            veteran_denominator = safe_float(rec.get("B21001_001E"))
            add_count_and_percent_observations(
                observations,
                count_indicator_id="VETERAN_POPULATION_COUNT",
                pct_indicator_id="VETERAN_POPULATION_PCT",
                numerator=veteran,
                numerator_moe=safe_float(rec.get("B21001_002M")),
                denominator=veteran_denominator,
                geography_name=geo_name,
                geography_type=geo_type,
                state_fips=st,
                county_fips=co,
                year=year,
                source_latest=source_latest,
                source_variable_label=label_for_vars(meta_detailed, ["B21001_002E", "B21001_001E"]),
                source_url_or_endpoint=endpoint,
                note="Veteran percentage uses ACS B21001 civilian population age 18+ denominator.",
            )

            lep = safe_float(rec.get("C16001_005E"))
            lep_denominator = safe_float(rec.get("C16001_001E"))
            add_count_and_percent_observations(
                observations,
                count_indicator_id="LANGUAGE_LEP_SPANISH_COUNT",
                pct_indicator_id="LANGUAGE_LEP_SPANISH_PCT",
                numerator=lep,
                numerator_moe=safe_float(rec.get("C16001_005M")),
                denominator=lep_denominator,
                geography_name=geo_name,
                geography_type=geo_type,
                state_fips=st,
                county_fips=co,
                year=year,
                source_latest=source_latest,
                source_variable_label=label_for_vars(meta_detailed, ["C16001_005E", "C16001_001E"]),
                source_url_or_endpoint=endpoint,
                note="Spanish LEP uses ACS C16001 population age 5+ language table.",
            )

            no_vehicle = safe_float(rec.get("B08201_002E"))
            no_vehicle_denominator = safe_float(rec.get("B08201_001E"))
            add_count_and_percent_observations(
                observations,
                count_indicator_id="NO_VEHICLE_HOUSEHOLDS_COUNT",
                pct_indicator_id="NO_VEHICLE_HOUSEHOLDS_PCT",
                numerator=no_vehicle,
                numerator_moe=safe_float(rec.get("B08201_002M")),
                denominator=no_vehicle_denominator,
                geography_name=geo_name,
                geography_type=geo_type,
                state_fips=st,
                county_fips=co,
                year=year,
                source_latest=source_latest,
                source_variable_label=label_for_vars(meta_detailed, ["B08201_002E", "B08201_001E"]),
                source_url_or_endpoint=endpoint,
                note="No-vehicle percentage uses ACS B08201 household denominator.",
            )

            snap = safe_float(rec.get("B22010_002E"))
            snap_denominator = safe_float(rec.get("B22010_001E"))
            add_count_and_percent_observations(
                observations,
                count_indicator_id="SNAP_HOUSEHOLDS_COUNT",
                pct_indicator_id="SNAP_HOUSEHOLDS_PCT",
                numerator=snap,
                numerator_moe=safe_float(rec.get("B22010_002M")),
                denominator=snap_denominator,
                geography_name=geo_name,
                geography_type=geo_type,
                state_fips=st,
                county_fips=co,
                year=year,
                source_latest=source_latest,
                source_variable_label=label_for_vars(meta_detailed, ["B22010_002E", "B22010_001E"]),
                source_url_or_endpoint=endpoint,
                note="SNAP percentage uses ACS B22010 household denominator.",
            )

            rent_burden_vars = ["B25070_007", "B25070_008", "B25070_009", "B25070_010"]
            rent_burdened = sum_values([safe_float(rec.get(f"{v}E")) for v in rent_burden_vars])
            rent_burdened_moe = sum_moes([safe_float(rec.get(f"{v}M")) for v in rent_burden_vars])
            renter_denominator = safe_float(rec.get("B25070_001E"))

            add_count_and_percent_observations(
                observations,
                count_indicator_id="RENT_BURDENED_HOUSEHOLDS_COUNT",
                pct_indicator_id="RENT_BURDENED_HOUSEHOLDS_PCT",
                numerator=rent_burdened,
                numerator_moe=rent_burdened_moe,
                denominator=renter_denominator,
                geography_name=geo_name,
                geography_type=geo_type,
                state_fips=st,
                county_fips=co,
                year=year,
                source_latest=source_latest,
                source_variable_label=label_for_vars(meta_detailed, [f"{v}E" for v in rent_burden_vars] + ["B25070_001E"]),
                source_url_or_endpoint=endpoint,
                note="Rent-burdened households calculated from ACS B25070 categories for 30% or more of income. Sum MOE approximated.",
            )

            observations.append(
                make_observation(
                    indicator_id="MEDIAN_HOME_VALUE",
                    geography_name=geo_name,
                    geography_type=geo_type,
                    state_fips=st,
                    county_fips=co,
                    year_or_period=str(year),
                    source_latest_year_or_period=source_latest,
                    estimate=safe_float(rec.get("B25077_001E")),
                    moe=safe_float(rec.get("B25077_001M")),
                    numerator=None,
                    denominator=None,
                    source_variable_label=label_for_vars(meta_detailed, ["B25077_001E", "B25077_001M"]),
                    stratification="Owner-occupied housing units",
                    comparison_available="Yes",
                    data_quality_note="Most recent ACS 5-year detailed table detected independently for ACS source.",
                    source_url_or_endpoint=endpoint,
                )
            )

            observations.append(
                make_observation(
                    indicator_id="MEDIAN_HOUSEHOLD_INCOME",
                    geography_name=geo_name,
                    geography_type=geo_type,
                    state_fips=st,
                    county_fips=co,
                    year_or_period=str(year),
                    source_latest_year_or_period=source_latest,
                    estimate=safe_float(rec.get("B19013_001E")),
                    moe=safe_float(rec.get("B19013_001M")),
                    numerator=None,
                    denominator=None,
                    source_variable_label=label_for_vars(meta_detailed, ["B19013_001E", "B19013_001M"]),
                    stratification="Households",
                    comparison_available="Yes",
                    data_quality_note="Most recent ACS 5-year detailed table detected independently for ACS source.",
                    source_url_or_endpoint=endpoint,
                )
            )

        profile_rec = census_request(config, year, "profile", profile_vars, geo, errors)
        if profile_rec:
            geo_name = profile_rec.get("NAME", geo)
            endpoint = f"https://api.census.gov/data/{year}/acs/acs5/profile"

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
                        source_variable_label=label_for_vars(meta_profile, [estimate_var, moe_var]),
                        stratification="Total",
                        comparison_available="Yes",
                        data_quality_note="Most recent ACS 5-year profile endpoint detected independently for ACS source.",
                        source_url_or_endpoint=endpoint,
                    )
                )

        subject_rec = census_request(config, year, "subject", subject_vars, geo, errors)
        if subject_rec:
            geo_name = subject_rec.get("NAME", geo)
            endpoint = f"https://api.census.gov/data/{year}/acs/acs5/subject"

            observations.append(
                make_observation(
                    indicator_id="DISABILITY_COUNT",
                    geography_name=geo_name,
                    geography_type=geo_type,
                    state_fips=st,
                    county_fips=co,
                    year_or_period=str(year),
                    source_latest_year_or_period=source_latest,
                    estimate=safe_float(subject_rec.get("S1810_C02_001E")),
                    moe=safe_float(subject_rec.get("S1810_C02_001M")),
                    numerator=safe_float(subject_rec.get("S1810_C02_001E")),
                    denominator=None,
                    source_variable_label=label_for_vars(meta_subject, ["S1810_C02_001E", "S1810_C02_001M"]),
                    stratification="Civilian noninstitutionalized population",
                    comparison_available="Yes",
                    data_quality_note="Most recent ACS 5-year subject endpoint detected independently for ACS source.",
                    source_url_or_endpoint=endpoint,
                )
            )

            observations.append(
                make_observation(
                    indicator_id="DISABILITY_PREVALENCE",
                    geography_name=geo_name,
                    geography_type=geo_type,
                    state_fips=st,
                    county_fips=co,
                    year_or_period=str(year),
                    source_latest_year_or_period=source_latest,
                    estimate=safe_float(subject_rec.get("S1810_C03_001E")),
                    moe=safe_float(subject_rec.get("S1810_C03_001M")),
                    numerator=None,
                    denominator=None,
                    source_variable_label=label_for_vars(meta_subject, ["S1810_C03_001E", "S1810_C03_001M"]),
                    stratification="Civilian noninstitutionalized population",
                    comparison_available="Yes",
                    data_quality_note="Most recent ACS 5-year subject endpoint detected independently for ACS source.",
                    source_url_or_endpoint=endpoint,
                )
            )

    return observations


# --------------------------------------------------------------------------------------
# BLS LAUS extraction
# --------------------------------------------------------------------------------------

def bls_laus_county_series_id(config: RunConfig) -> str:
    return f"LAUCN{config.state_fips}{config.county_fips}0000000003"


def extract_bls_laus(config: RunConfig, errors: list[str]) -> list[Observation]:
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

    status = data.get("status")
    if status and str(status).upper() != "REQUEST_SUCCEEDED":
        errors.append(f"BLS LAUS request status for {series_id}: {status}; message={data.get('message')}")

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

    annual_rows = [r for r in rows if isinstance(r, dict) and r.get("period") == "M13"]
    annual_rows.sort(key=lambda r: safe_int(r.get("year")) or 0, reverse=True)

    if annual_rows:
        latest_row = annual_rows[0]
        latest_period_label = "Annual average"
    else:
        monthly_rows = [
            r for r in rows
            if isinstance(r, dict)
            and isinstance(r.get("period"), str)
            and str(r.get("period")).startswith("M")
            and r.get("period") != "M13"
        ]
        monthly_rows.sort(
            key=lambda r: (
                safe_int(r.get("year")) or 0,
                safe_int(str(r.get("period", "M00")).replace("M", "")) or 0,
            ),
            reverse=True,
        )

        if not monthly_rows:
            errors.append(f"BLS LAUS returned no annual or monthly rows for {series_id}.")
            return observations

        latest_row = monthly_rows[0]
        latest_period_label = latest_row.get("periodName") or latest_row.get("period") or "Latest monthly"

    latest_year = str(latest_row.get("year", "latest returned"))
    latest_period = f"{latest_year} {latest_period_label}".strip()

    observations.append(
        make_observation(
            indicator_id="UNEMPLOYMENT_RATE",
            geography_name=config.county_name,
            geography_type="County",
            state_fips=config.state_fips,
            county_fips=config.county_fips,
            year_or_period=latest_period,
            source_latest_year_or_period=latest_period,
            estimate=safe_float(latest_row.get("value")),
            moe=None,
            numerator=None,
            denominator=None,
            source_variable_label=f"{series_id}: LAUS county unemployment rate, {latest_period_label}.",
            stratification=latest_period_label,
            comparison_available="No",
            data_quality_note=(
                f"Most recent BLS LAUS row returned for inferred county LAUS unemployment-rate series {series_id}. "
                "Annual average M13 is preferred; latest monthly row is used only if M13 is unavailable. "
                "Validate series metadata before publication."
            ),
            source_url_or_endpoint=url,
        )
    )

    return observations


# --------------------------------------------------------------------------------------
# CDC PLACES extraction
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
    observations: list[Observation] = []
    county_fips_full = f"{config.state_fips}{config.county_fips}"

    for endpoint in CDC_PLACES_ENDPOINTS:
        params = {
            "$limit": 50000,
            "$where": f"locationid='{county_fips_full}' OR locationid='{int(county_fips_full)}'",
        }
        data = cached_get_json("cdc_places", endpoint, params, errors=None)

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
            return observations

    errors.append("CDC PLACES returned no usable records from candidate endpoints. Update endpoint ID or use current PLACES download.")
    return observations


# --------------------------------------------------------------------------------------
# Placeholders and backfill
# --------------------------------------------------------------------------------------

def add_current_source_placeholders(config: RunConfig) -> list[Observation]:
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
                data_quality_note=(
                    "This indicator requires a source-specific connector, manual download, Medicaid-specific table, "
                    "or internal data. No estimate was fabricated."
                ),
                source_url_or_endpoint=endpoints[indicator_id],
            )
        )
    return observations


def add_missing_indicator_backfill_rows(
    config: RunConfig,
    catalog: list[IndicatorCatalogRow],
    observations: list[Observation],
) -> list[Observation]:
    existing_ids = {o.indicator_id for o in observations}
    backfill: list[Observation] = []

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
                data_quality_note=(
                    "No estimate was returned for this indicator in this run. "
                    "This backfill row prevents silent data loss. Check API errors, endpoint changes, "
                    "source availability, or whether the indicator requires manual/internal data."
                ),
                source_url_or_endpoint=d.source_name,
            )
        )

    return observations + backfill


# --------------------------------------------------------------------------------------
# Catalog and outputs
# --------------------------------------------------------------------------------------

def build_indicator_catalog() -> list[IndicatorCatalogRow]:
    rows: list[IndicatorCatalogRow] = []
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


def summarize_latest_by_source(observations: list[Observation]) -> dict[str, list[str]]:
    summary: dict[str, set[str]] = {}
    for o in observations:
        key = f"{o.source_name} ({o.source_agency})"
        summary.setdefault(key, set()).add(o.source_latest_year_or_period)
    return {k: sorted(v) for k, v in summary.items()}


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


def write_api_errors(errors: list[str]) -> None:
    lines = [
        "# API Errors and Data Limitations",
        "",
        "## General limitations",
        "",
        "- Each source determines its own most recent available data; years will not necessarily match across sources.",
        "- ACS requests are chunked to prevent missing data from the Census API variable limit.",
        "- ACS estimates are period estimates and include margins of error.",
        "- ACS calculated percentages do not include derived margins of error.",
        "- ACS sum margins of error are approximated using square root of sum of squared MOEs.",
        "- PUBLIC_COVERAGE_RATE is not Medicaid-only coverage.",
        "- MEDICAID_COVERAGE_RATE is left as a placeholder unless a Medicaid-specific source is connected.",
        "- CDC PLACES values are modeled estimates.",
        "- BLS LAUS county series IDs are inferred and should be validated before publication.",
        "- CDC WONDER/NVSS mortality data may suppress small counts and may require manual downloads.",
        "- HRSA, SAMHSA, HUD, and NCES source schemas may require source-specific download handling or crosswalks.",
        "- Internal and qualitative data cannot be filled from government APIs.",
        "",
        "## Captured errors",
        "",
    ]

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
        "",
        "## Files",
        "",
        "- `indicator_catalog.csv`: indicator inventory with summary, detailed description, units, and citation metadata.",
        "- `needs_assessment_data_long.csv`: extracted and placeholder observations with summaries, descriptions, source labels, units, and citations.",
        "- `source_metadata.json`: run configuration, citation registry, latest source summary, and API errors.",
        "- `data_availability.md`: readable data availability table.",
        "- `citation_appendix.md`: citation registry.",
        "- `api_errors_and_limitations.md`: errors and known limitations.",
        "",
        "## Unit logic",
        "",
        "- ACS count variables are reported as people or households.",
        "- ACS dollar variables are reported as dollars.",
        "- ACS percentage variables are reported as percent.",
        "- Script-calculated percentages use numerator / denominator * 100.",
        "- BLS LAUS unemployment rate is reported as percent.",
        "- CDC PLACES units are taken from API metadata and normalized where needed.",
        "- Manual/download placeholders use the expected unit for that indicator.",
        "",
        "## Important coverage note",
        "",
        "- PUBLIC_COVERAGE_RATE is ACS public insurance coverage.",
        "- MEDICAID_COVERAGE_RATE is not derived from PUBLIC_COVERAGE_RATE; it requires a Medicaid-specific source.",
    ]
    (OUTPUT_DIR / "README.md").write_text("\n".join(lines), encoding="utf-8")


def write_outputs(config: RunConfig, catalog: list[IndicatorCatalogRow], observations: list[Observation], errors: list[str]) -> None:
    ensure_dirs()

    pd.DataFrame([asdict(r) for r in catalog]).to_csv(OUTPUT_DIR / "indicator_catalog.csv", index=False)
    pd.DataFrame([asdict(o) for o in observations]).to_csv(OUTPUT_DIR / "needs_assessment_data_long.csv", index=False)

    metadata = {
        "retrieved_at": now_iso(),
        "run_config": asdict(config),
        "citations": [asdict(c) for c in build_citation_registry().values()],
        "errors": errors,
        "source_latest_summary": summarize_latest_by_source(observations),
        "unit_logic_summary": {
            "ACS counts": "people or households, based on ACS table definition",
            "ACS dollars": "dollars, based on ACS median dollar variable",
            "ACS source percentages": "percent, based on ACS PE percentage variables",
            "ACS calculated percentages": "percent = numerator / denominator * 100",
            "BLS LAUS unemployment": "percent, based on LAUS unemployment-rate series",
            "CDC PLACES": "API-provided unit normalized where needed",
            "Manual placeholders": "expected units from indicator definition",
        },
    }
    (OUTPUT_DIR / "source_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    write_citation_appendix()
    write_data_availability(catalog, observations)
    write_api_errors(errors)
    write_readme(config)


# --------------------------------------------------------------------------------------
# CLI
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
    parser.add_argument("--census-api-key", default=os.getenv("CENSUS_API_KEY"))

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
    )


def run(config: RunConfig) -> None:
    ensure_dirs()
    errors: list[str] = []

    catalog = build_indicator_catalog()
    observations: list[Observation] = []

    observations.extend(extract_acs(config, errors))
    observations.extend(extract_bls_laus(config, errors))
    observations.extend(extract_cdc_places(config, errors))
    observations.extend(add_current_source_placeholders(config))

    observations = add_missing_indicator_backfill_rows(config, catalog, observations)

    write_outputs(config, catalog, observations, errors)

    extracted_ids = {o.indicator_id for o in observations if o.estimate is not None}
    all_ids = {r.indicator_id for r in catalog}
    missing_estimates = sorted(all_ids - extracted_ids)

    print("Done.")
    print(f"Output directory: {OUTPUT_DIR.resolve()}")
    print(f"Observation rows: {len(observations)}")
    print(f"Indicators in catalog: {len(all_ids)}")
    print(f"Indicators with numeric estimates: {len(extracted_ids)}")
    print(f"Indicators without numeric estimates: {len(missing_estimates)}")
    print(f"Errors/limitations captured: {len(errors)}")


if __name__ == "__main__":
    run(parse_args())