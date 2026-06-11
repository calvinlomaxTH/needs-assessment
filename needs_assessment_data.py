#!/usr/bin/env python3
"""
needs_assessment_data.py

Single-file government-data extraction script for CCBHC community needs assessments.

Primary use:
    python3 needs_assessment_data.py \
      --state-fips 08 \
      --county-fips 067 \
      --state-abbr CO \
      --county-name "La Plata County" \
      --service-area-name "La Plata County, Colorado" \
      --latest \
      --census-api-key 4a550c8493494c363ee0afc87c2af4e97d4a169b

Outputs:
    outputs/indicator_catalog.csv
    outputs/needs_assessment_data_long.csv
    outputs/source_metadata.json
    outputs/data_availability.md
    outputs/api_errors_and_limitations.md
    outputs/citation_appendix.md

Dependencies:
    pip install requests pandas

Notes:
    - This script uses government APIs where feasible.
    - Some government data sources are not reliably API-accessible at the county level.
      Those are included as structured placeholders in the indicator catalog and limitations file.
    - The script never fabricates estimates.
    - If data are unavailable, suppressed, or not county-level, it emits null estimates with notes.
    - Every extracted observation includes citation metadata.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import requests


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

OUTPUT_DIR = Path("outputs")
CACHE_DIR = Path("data/raw")
REQUEST_TIMEOUT = 45
REQUEST_SLEEP_SECONDS = 0.25
MAX_RETRIES = 3


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
    census_api_key: Optional[str] = None


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
class Observation:
    indicator_id: str
    indicator_name: str
    needs_assessment_domain: str
    source_name: str
    source_agency: str
    api_or_download: str
    geography_name: str
    geography_type: str
    state_fips: str
    county_fips: str
    year_or_period: str
    estimate: Optional[float]
    moe: Optional[float]
    numerator: Optional[float]
    denominator: Optional[float]
    units: str
    stratification: str
    comparison_available: str
    data_quality_note: str
    source_url_or_endpoint: str
    source_citation_id: str
    source_citation_text: str
    retrieved_at: str


@dataclass
class IndicatorCatalogRow:
    indicator_id: str
    indicator_name: str
    needs_assessment_domain: str
    source_name: str
    source_agency: str
    api_or_download: str
    expected_geography_level: str
    expected_update_frequency: str
    government_api_available: str
    public_download_available: str
    internal_or_qualitative_required: str
    source_citation_id: str
    source_citation_text: str
    limitation: str
    recommended_qualitative_followup_question: str


# --------------------------------------------------------------------------------------
# Basic utilities
# --------------------------------------------------------------------------------------

def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    s = str(value).strip()
    if s in {"", "null", "None", "NaN", "nan", "-", "**", "***", "N/A"}:
        return None

    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def ensure_dirs() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def normalize_fips(value: str, width: int) -> str:
    return str(value).zfill(width)


def cache_key(url: str, params: Optional[dict[str, Any]]) -> str:
    payload = json.dumps({"url": url, "params": params or {}}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cached_get_json(
    source_slug: str,
    url: str,
    params: Optional[dict[str, Any]] = None,
    errors: Optional[list[str]] = None,
) -> Optional[Any]:
    """
    GET JSON with simple caching, retries, and error collection.
    """
    params = params or {}
    source_dir = CACHE_DIR / source_slug / dt.date.today().isoformat()
    source_dir.mkdir(parents=True, exist_ok=True)
    path = source_dir / f"{cache_key(url, params)}.json"

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

    msg = f"Failed GET JSON after {MAX_RETRIES} tries: {url} params={params} error={last_error}"
    if errors is not None:
        errors.append(msg)
    return None


def cached_get_text(
    source_slug: str,
    url: str,
    params: Optional[dict[str, Any]] = None,
    errors: Optional[list[str]] = None,
) -> Optional[str]:
    """
    GET text with simple caching, retries, and error collection.
    """
    params = params or {}
    source_dir = CACHE_DIR / source_slug / dt.date.today().isoformat()
    source_dir.mkdir(parents=True, exist_ok=True)
    path = source_dir / f"{cache_key(url, params)}.txt"

    if path.exists():
        return path.read_text(encoding="utf-8")

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            time.sleep(REQUEST_SLEEP_SECONDS)
            response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)

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

    msg = f"Failed GET text after {MAX_RETRIES} tries: {url} params={params} error={last_error}"
    if errors is not None:
        errors.append(msg)
    return None


# --------------------------------------------------------------------------------------
# Citation registry
# --------------------------------------------------------------------------------------

def build_citation_registry() -> dict[str, Citation]:
    """
    Citation registry used by the indicator catalog and output files.

    Keep this separate from live API metadata:
    - citation_id explains why the source belongs in a CCBHC needs assessment.
    - source_url_or_endpoint on each Observation records the exact API endpoint used.
    """
    citations = [
        Citation(
            citation_id="CNA_CRITERIA_2023",
            citation_label="CCBHC Community Needs Assessment Criteria",
            source_title="Certified Community Behavioral Health Clinic (CCBHC) Requirements for Community Needs Assessment",
            source_type="Uploaded project reference",
            source_agency_or_author="Third Horizon Strategies / SAMHSA criteria summary",
            publication_date="2023-11-06",
            url_or_file_reference="CCBHC Community Needs Assessment Criteria.pdf",
            notes=(
                "Defines required CNA elements: service area, prevalence of mental health and SUD needs, "
                "economic factors and SDOH, cultures and languages, underserved populations, staffing-plan "
                "alignment, update plan, and stakeholder input."
            ),
        ),
        Citation(
            citation_id="AXIS_2026_CNA",
            citation_label="Axis 2026 CCBHC CNA",
            source_title="Certified Community Behavioral Health Clinic (CCBHC) Community Needs Assessment, La Plata County, Colorado",
            source_type="Uploaded needs assessment example",
            source_agency_or_author="Third Horizon for Axis Health System",
            publication_date="2026-03-23",
            url_or_file_reference="Axis Needs Assessment March 26 Final.pdf",
            notes=(
                "Example of a completed CNA using ACS, BRFSS, CDC/NVSS/WONDER, CDC PLACES, HRSA, "
                "NCES, Census, SAMHSA Buprenorphine Practitioner Locator, and N-SUMHSS."
            ),
        ),
        Citation(
            citation_id="CCBHC_TOOLKIT_2024",
            citation_label="CCBHC CNA Toolkit",
            source_title="CCBHC Community Needs Assessment Toolkit",
            source_type="Uploaded toolkit",
            source_agency_or_author="National Council for Mental Wellbeing / CCBHC-E NTTAC",
            publication_date="2024-01",
            url_or_file_reference="CCBHC-Needs-Assessment-Toolkit.pdf",
            notes=(
                "Describes the CNA as the foundation of the CCBHC model and emphasizes mixed quantitative "
                "and qualitative data, stakeholder engagement, staffing, services, partnerships, and CQI."
            ),
        ),
        Citation(
            citation_id="CENSUS_ACS_API",
            citation_label="U.S. Census ACS API",
            source_title="American Community Survey API",
            source_type="Government API",
            source_agency_or_author="U.S. Census Bureau",
            publication_date="Ongoing",
            url_or_file_reference="https://api.census.gov/data.html",
            notes=(
                "Primary source for population, demographics, language, poverty, housing, insurance, "
                "transportation, and SDOH indicators."
            ),
        ),
        Citation(
            citation_id="BLS_LAUS_API",
            citation_label="BLS Public Data API",
            source_title="Local Area Unemployment Statistics via BLS Public Data API",
            source_type="Government API",
            source_agency_or_author="U.S. Bureau of Labor Statistics",
            publication_date="Ongoing",
            url_or_file_reference="https://api.bls.gov/publicAPI/v2/timeseries/data/",
            notes="Source for unemployment indicators where county series IDs can be constructed or supplied.",
        ),
        Citation(
            citation_id="CDC_PLACES_API",
            citation_label="CDC PLACES API",
            source_title="CDC PLACES Local Data for Better Health",
            source_type="Government API / Socrata",
            source_agency_or_author="Centers for Disease Control and Prevention",
            publication_date="Ongoing",
            url_or_file_reference="https://data.cdc.gov/",
            notes=(
                "Source for modeled county/local health indicators such as frequent mental distress, "
                "depression, binge drinking, and smoking."
            ),
        ),
        Citation(
            citation_id="CDC_WONDER_NVSS",
            citation_label="CDC WONDER / NVSS",
            source_title="CDC WONDER, National Vital Statistics System Mortality",
            source_type="Government system / manual download fallback",
            source_agency_or_author="Centers for Disease Control and Prevention",
            publication_date="Ongoing",
            url_or_file_reference="https://wonder.cdc.gov/",
            notes=(
                "Source for mortality indicators such as suicide and drug overdose; programmatic access "
                "may require manual query/download fallback."
            ),
        ),
        Citation(
            citation_id="HRSA_DATA_WAREHOUSE",
            citation_label="HRSA Data Warehouse",
            source_title="HRSA Data Warehouse and Area Health Resource Files",
            source_type="Government API / public files",
            source_agency_or_author="Health Resources and Services Administration",
            publication_date="Ongoing",
            url_or_file_reference="https://data.hrsa.gov/",
            notes=(
                "Source for HPSA, MUA/P, provider shortage, health center, and workforce/resource indicators."
            ),
        ),
        Citation(
            citation_id="SAMHSA_FINDTREATMENT",
            citation_label="SAMHSA FindTreatment / N-SUMHSS",
            source_title="SAMHSA Behavioral Health Treatment Locator and N-SUMHSS",
            source_type="Government locator / public files",
            source_agency_or_author="Substance Abuse and Mental Health Services Administration",
            publication_date="Ongoing",
            url_or_file_reference="https://findtreatment.gov/",
            notes=(
                "Source for treatment facility availability, MOUD-related provider/facility data, and "
                "behavioral health service infrastructure."
            ),
        ),
        Citation(
            citation_id="NCES_CCD",
            citation_label="NCES Common Core of Data",
            source_title="Common Core of Data",
            source_type="Government data files / API where available",
            source_agency_or_author="National Center for Education Statistics",
            publication_date="Ongoing",
            url_or_file_reference="https://nces.ed.gov/ccd/",
            notes=(
                "Source for school enrollment, district demographics, and school meal poverty proxy indicators."
            ),
        ),
        Citation(
            citation_id="HUD_PIT_HIC",
            citation_label="HUD PIT/HIC",
            source_title="Point-in-Time Count and Housing Inventory Count",
            source_type="Government public files",
            source_agency_or_author="U.S. Department of Housing and Urban Development",
            publication_date="Annual",
            url_or_file_reference="https://www.hudexchange.info/programs/hdx/pit-hic/",
            notes=(
                "Source for homelessness indicators, often available by Continuum of Care rather than county."
            ),
        ),
        Citation(
            citation_id="CMS_MEDICAID_DATA",
            citation_label="CMS Medicaid Data",
            source_title="Data.Medicaid.gov and CMS Medicaid data resources",
            source_type="Government API / public files",
            source_agency_or_author="Centers for Medicare & Medicaid Services",
            publication_date="Ongoing",
            url_or_file_reference="https://data.medicaid.gov/",
            notes=(
                "Potential source for Medicaid-related indicators, depending on geography and dataset availability."
            ),
        ),
        Citation(
            citation_id="INTERNAL_PLACEHOLDER",
            citation_label="Internal organization data placeholder",
            source_title="Internal CCBHC data placeholder",
            source_type="Internal data placeholder",
            source_agency_or_author="Client organization",
            publication_date="Project-specific",
            url_or_file_reference="Not available through government API",
            notes=(
                "Used for EHR/client demographics, service utilization, staffing, wait times, referrals, "
                "patient satisfaction, and qualitative findings."
            ),
        ),
    ]

    return {c.citation_id: c for c in citations}


def citation_text(citation_id: str, registry: Optional[dict[str, Citation]] = None) -> str:
    registry = registry or build_citation_registry()
    c = registry[citation_id]
    return (
        f"{c.citation_label}: {c.source_title}. "
        f"{c.source_agency_or_author}. {c.publication_date}. "
        f"{c.url_or_file_reference}."
    )


# --------------------------------------------------------------------------------------
# Indicator catalog
# --------------------------------------------------------------------------------------

def build_indicator_catalog() -> list[IndicatorCatalogRow]:
    citations = build_citation_registry()
    rows: list[IndicatorCatalogRow] = []

    def add(
        indicator_id: str,
        indicator_name: str,
        domain: str,
        source_name: str,
        source_agency: str,
        api_or_download: str,
        geography: str,
        update_frequency: str,
        api_available: str,
        download_available: str,
        internal_required: str,
        citation_id: str,
        limitation: str,
        followup: str,
    ) -> None:
        rows.append(
            IndicatorCatalogRow(
                indicator_id=indicator_id,
                indicator_name=indicator_name,
                needs_assessment_domain=domain,
                source_name=source_name,
                source_agency=source_agency,
                api_or_download=api_or_download,
                expected_geography_level=geography,
                expected_update_frequency=update_frequency,
                government_api_available=api_available,
                public_download_available=download_available,
                internal_or_qualitative_required=internal_required,
                source_citation_id=citation_id,
                source_citation_text=citation_text(citation_id, citations),
                limitation=limitation,
                recommended_qualitative_followup_question=followup,
            )
        )

    # Core CNA requirements
    add(
        "CNA_REQUIREMENTS",
        "CCBHC CNA required elements",
        "Data Gaps / Qualitative Follow-up Needed",
        "CCBHC Criteria Summary",
        "SAMHSA / Third Horizon",
        "Project reference",
        "Service area",
        "Every 3 years or sooner if conditions change",
        "No",
        "No",
        "Yes",
        "CNA_CRITERIA_2023",
        "Not an extractable indicator; used to structure the CNA data model.",
        "Which required CNA elements need additional qualitative or internal data?",
    )

    # Service area and demographics
    add(
        "POP_TOTAL",
        "Total population",
        "Service Area and Population",
        "ACS 5-year",
        "U.S. Census Bureau",
        "API",
        "County, state, U.S.",
        "Annual",
        "Yes",
        "Yes",
        "No",
        "CENSUS_ACS_API",
        "ACS estimates are period estimates and include margins of error.",
        "Do community partners perceive population growth or decline in particular subareas?",
    )
    add(
        "POP_AGE_UNDER_18",
        "Population under age 18",
        "Demographics",
        "ACS 5-year",
        "U.S. Census Bureau",
        "API",
        "County, state, U.S.",
        "Annual",
        "Yes",
        "Yes",
        "No",
        "CENSUS_ACS_API",
        "Age bands may need to be mapped to CCBHC lifespan categories.",
        "Which child and youth populations are experiencing the greatest access barriers?",
    )
    add(
        "POP_AGE_65_PLUS",
        "Population age 65 and older",
        "Demographics",
        "ACS 5-year",
        "U.S. Census Bureau",
        "API",
        "County, state, U.S.",
        "Annual",
        "Yes",
        "Yes",
        "No",
        "CENSUS_ACS_API",
        "Age bands may need to be mapped to CCBHC lifespan categories.",
        "Which older adult needs are most visible to providers and caregivers?",
    )
    add(
        "POP_RACE_ETHNICITY",
        "Race and ethnicity",
        "Demographics",
        "ACS 5-year",
        "U.S. Census Bureau",
        "API",
        "County, state, U.S.",
        "Annual",
        "Yes",
        "Yes",
        "No",
        "CENSUS_ACS_API",
        "Small groups may have large margins of error.",
        "Which racial or ethnic communities are underrepresented in current services?",
    )
    add(
        "HISPANIC_LATINX",
        "Hispanic or Latinx population",
        "Demographics",
        "ACS 5-year",
        "U.S. Census Bureau",
        "API",
        "County, state, U.S.",
        "Annual",
        "Yes",
        "Yes",
        "No",
        "CENSUS_ACS_API",
        "Small-area estimates may have margins of error.",
        "Are Hispanic/Latinx residents proportionately represented in behavioral health services?",
    )
    add(
        "AIAN_POPULATION",
        "American Indian and Alaska Native population",
        "Demographics",
        "ACS 5-year",
        "U.S. Census Bureau",
        "API",
        "County, state, U.S.",
        "Annual",
        "Yes",
        "Yes",
        "No",
        "CENSUS_ACS_API",
        "Small population estimates may be unstable.",
        "What tribal or Indigenous-serving partners should be engaged?",
    )
    add(
        "VETERAN_POPULATION",
        "Veteran population",
        "Underserved Populations",
        "ACS 5-year",
        "U.S. Census Bureau",
        "API",
        "County, state, U.S.",
        "Annual",
        "Yes",
        "Yes",
        "No",
        "CENSUS_ACS_API",
        "Does not capture all veteran-specific behavioral health needs.",
        "Are veterans able to access timely behavioral health and SUD care locally?",
    )
    add(
        "DISABILITY_STATUS",
        "Population with a disability",
        "Underserved Populations",
        "ACS 5-year",
        "U.S. Census Bureau",
        "API",
        "County, state, U.S.",
        "Annual",
        "Yes",
        "Yes",
        "No",
        "CENSUS_ACS_API",
        "Disability categories may not map exactly to behavioral health need.",
        "What physical, cognitive, and accessibility barriers affect care?",
    )
    add(
        "LANGUAGE_LEP",
        "Language spoken at home and limited English proficiency",
        "Culture and Language",
        "ACS 5-year",
        "U.S. Census Bureau",
        "API",
        "County, state, U.S.",
        "Annual",
        "Yes",
        "Yes",
        "No",
        "CENSUS_ACS_API",
        "Detailed language tables can be unstable for small counties.",
        "Which language-assistance or translation needs are most urgent?",
    )

    # SDOH
    add(
        "POVERTY_RATE",
        "Population below poverty level",
        "Economic Stability",
        "ACS 5-year",
        "U.S. Census Bureau",
        "API",
        "County, state, U.S.",
        "Annual",
        "Yes",
        "Yes",
        "No",
        "CENSUS_ACS_API",
        "ACS poverty estimates are period estimates and may lag rapid local changes.",
        "How is economic hardship affecting access to behavioral health care?",
    )
    add(
        "MEDIAN_HOUSEHOLD_INCOME",
        "Median household income",
        "Economic Stability",
        "ACS 5-year",
        "U.S. Census Bureau",
        "API",
        "County, state, U.S.",
        "Annual",
        "Yes",
        "Yes",
        "No",
        "CENSUS_ACS_API",
        "Median income may mask affordability pressures in high-cost rural or tourism areas.",
        "Are income trends aligned with what residents report about affordability?",
    )
    add(
        "UNEMPLOYMENT_RATE",
        "Unemployment rate",
        "Economic Stability",
        "BLS LAUS",
        "U.S. Bureau of Labor Statistics",
        "API",
        "County, state, U.S.",
        "Monthly / annual",
        "Yes",
        "Yes",
        "No",
        "BLS_LAUS_API",
        "County series construction may require validation against BLS series metadata.",
        "Are employment barriers contributing to behavioral health or treatment access needs?",
    )
    add(
        "UNINSURED_RATE",
        "Uninsured rate",
        "Insurance Coverage",
        "ACS 5-year",
        "U.S. Census Bureau",
        "API",
        "County, state, U.S.",
        "Annual",
        "Yes",
        "Yes",
        "No",
        "CENSUS_ACS_API",
        "Insurance coverage does not equal access to available or affordable services.",
        "Do uninsured residents know where they can receive behavioral health care?",
    )
    add(
        "MEDICAID_COVERAGE",
        "Medicaid coverage",
        "Insurance Coverage",
        "ACS 5-year / CMS Medicaid",
        "U.S. Census Bureau / CMS",
        "API / public files",
        "County, state, U.S.; CMS varies by dataset",
        "Annual / ongoing",
        "Partial",
        "Yes",
        "No",
        "CENSUS_ACS_API",
        "ACS Medicaid coverage differs from administrative enrollment and may lag.",
        "Are Medicaid members able to find timely specialty behavioral health care?",
    )
    add(
        "NO_VEHICLE",
        "Households with no vehicle available",
        "Transportation",
        "ACS 5-year",
        "U.S. Census Bureau",
        "API",
        "County, state, U.S.",
        "Annual",
        "Yes",
        "Yes",
        "No",
        "CENSUS_ACS_API",
        "Does not directly measure public transit availability or travel distance to care.",
        "Where do transportation barriers most affect access to services?",
    )
    add(
        "INTERNET_ACCESS",
        "Household internet access",
        "Transportation / Access to Care",
        "ACS 5-year",
        "U.S. Census Bureau",
        "API",
        "County, state, U.S.",
        "Annual",
        "Yes",
        "Yes",
        "No",
        "CENSUS_ACS_API",
        "Internet access is a proxy for telehealth readiness and does not measure digital literacy.",
        "Are telehealth services accessible to people with limited internet or devices?",
    )
    add(
        "SNAP_HOUSEHOLDS",
        "Households receiving SNAP",
        "Food/Nutrition",
        "ACS 5-year",
        "U.S. Census Bureau",
        "API",
        "County, state, U.S.",
        "Annual",
        "Yes",
        "Yes",
        "No",
        "CENSUS_ACS_API",
        "SNAP is a proxy and does not fully measure food insecurity.",
        "Do residents report food insecurity even when SNAP participation appears low?",
    )
    add(
        "RENT_BURDEN",
        "Gross rent as percentage of household income",
        "Housing Stability",
        "ACS 5-year",
        "U.S. Census Bureau",
        "API",
        "County, state, U.S.",
        "Annual",
        "Yes",
        "Yes",
        "No",
        "CENSUS_ACS_API",
        "Rent burden does not capture homelessness or informal housing instability.",
        "How is housing affordability affecting behavioral health and recovery?",
    )
    add(
        "MEDIAN_HOME_VALUE",
        "Median home value",
        "Housing Stability",
        "ACS 5-year",
        "U.S. Census Bureau",
        "API",
        "County, state, U.S.",
        "Annual",
        "Yes",
        "Yes",
        "No",
        "CENSUS_ACS_API",
        "Home value does not measure rental availability or homelessness.",
        "Are housing costs contributing to workforce shortages and client instability?",
    )
    add(
        "HOMELESSNESS_PIT",
        "People experiencing homelessness",
        "Housing Stability",
        "HUD PIT/HIC",
        "U.S. Department of Housing and Urban Development",
        "Public download",
        "Continuum of Care; county only where crosswalked",
        "Annual",
        "Partial",
        "Yes",
        "No",
        "HUD_PIT_HIC",
        "Often reported by CoC rather than county; county allocation may require a crosswalk.",
        "What local housing instability is not captured in PIT counts?",
    )

    # Behavioral health prevalence and outcomes
    add(
        "SUICIDE_MORTALITY",
        "Suicide deaths and age-adjusted suicide mortality rate",
        "Mental Health Prevalence and Outcomes",
        "CDC WONDER / NVSS",
        "Centers for Disease Control and Prevention",
        "Manual download fallback",
        "County, state, U.S.; suppression possible",
        "Annual",
        "Partial",
        "Yes",
        "No",
        "CDC_WONDER_NVSS",
        "Programmatic access may be limited; small counts may be suppressed.",
        "What local suicide-prevention needs are not visible in mortality data?",
    )
    add(
        "OVERDOSE_MORTALITY",
        "Drug overdose deaths and age-adjusted overdose mortality rate",
        "Substance Use Prevalence and Outcomes",
        "CDC WONDER / NVSS",
        "Centers for Disease Control and Prevention",
        "Manual download fallback",
        "County, state, U.S.; suppression possible",
        "Annual",
        "Partial",
        "Yes",
        "No",
        "CDC_WONDER_NVSS",
        "Small counties may require multi-year aggregation due to suppression.",
        "Which substances and overdose risks are most visible to local providers?",
    )
    add(
        "FREQUENT_MENTAL_DISTRESS",
        "Frequent mental distress",
        "Mental Health Prevalence and Outcomes",
        "CDC PLACES",
        "Centers for Disease Control and Prevention",
        "API",
        "County, place, census tract depending on release",
        "Annual",
        "Yes",
        "Yes",
        "No",
        "CDC_PLACES_API",
        "Modeled estimate; should be labeled separately from observed survey or mortality data.",
        "Does modeled distress align with stakeholder experience?",
    )
    add(
        "DEPRESSION_PREVALENCE",
        "Depression prevalence",
        "Mental Health Prevalence and Outcomes",
        "CDC PLACES",
        "Centers for Disease Control and Prevention",
        "API",
        "County, place, census tract depending on release",
        "Annual",
        "Yes",
        "Yes",
        "No",
        "CDC_PLACES_API",
        "Modeled estimate; not a direct service-utilization measure.",
        "Are depression-related needs presenting differently across subpopulations?",
    )
    add(
        "BINGE_DRINKING",
        "Binge drinking",
        "Substance Use Prevalence and Outcomes",
        "CDC PLACES / BRFSS",
        "Centers for Disease Control and Prevention",
        "API / public files",
        "County modeled; state observed",
        "Annual",
        "Partial",
        "Yes",
        "No",
        "CDC_PLACES_API",
        "County estimates may be modeled; BRFSS observed data may be state or region level.",
        "How are alcohol-related needs showing up in local systems?",
    )
    add(
        "CURRENT_SMOKING",
        "Current smoking",
        "Physical Health and Co-occurring Conditions",
        "CDC PLACES",
        "Centers for Disease Control and Prevention",
        "API",
        "County, place, census tract depending on release",
        "Annual",
        "Yes",
        "Yes",
        "No",
        "CDC_PLACES_API",
        "Modeled estimate.",
        "Are tobacco and co-occurring physical health needs addressed in integrated care?",
    )

    # Workforce, access, and treatment infrastructure
    add(
        "MENTAL_HEALTH_HPSA",
        "Mental Health Professional Shortage Area status",
        "Workforce and Provider Availability",
        "HRSA Data Warehouse",
        "Health Resources and Services Administration",
        "API / public files",
        "County, tract, facility, or service area depending on designation",
        "Ongoing",
        "Partial",
        "Yes",
        "No",
        "HRSA_DATA_WAREHOUSE",
        "HPSA boundaries may not perfectly align with county boundaries.",
        "Which workforce shortages most affect access, staffing, and care continuity?",
    )
    add(
        "PRIMARY_CARE_HPSA",
        "Primary Care Professional Shortage Area status",
        "Workforce and Provider Availability",
        "HRSA Data Warehouse",
        "Health Resources and Services Administration",
        "API / public files",
        "County, tract, facility, or service area depending on designation",
        "Ongoing",
        "Partial",
        "Yes",
        "No",
        "HRSA_DATA_WAREHOUSE",
        "HPSA boundaries may not perfectly align with county boundaries.",
        "Are primary care shortages affecting behavioral health integration?",
    )
    add(
        "TREATMENT_FACILITIES_MH",
        "Mental health treatment facilities",
        "Treatment Facilities and Service Infrastructure",
        "SAMHSA FindTreatment / N-SUMHSS",
        "Substance Abuse and Mental Health Services Administration",
        "Locator / public files",
        "Address-level; can aggregate to county",
        "Annual / ongoing",
        "Partial",
        "Yes",
        "No",
        "SAMHSA_FINDTREATMENT",
        "Locator completeness and service details should be validated locally.",
        "Which listed services are actually available, accepting referrals, and accessible?",
    )
    add(
        "TREATMENT_FACILITIES_SUD",
        "SUD treatment facilities and MOUD availability",
        "Treatment Facilities and Service Infrastructure",
        "SAMHSA FindTreatment / N-SUMHSS",
        "Substance Abuse and Mental Health Services Administration",
        "Locator / public files",
        "Address-level; can aggregate to county",
        "Annual / ongoing",
        "Partial",
        "Yes",
        "No",
        "SAMHSA_FINDTREATMENT",
        "Facility listings may not reflect real-time capacity or payer acceptance.",
        "Where are the biggest gaps in SUD treatment levels of care and MOUD access?",
    )

    # Youth and schools
    add(
        "SCHOOL_ENROLLMENT",
        "School enrollment and student demographics",
        "Children, Youth, and Families",
        "NCES Common Core of Data",
        "National Center for Education Statistics",
        "API / public files",
        "School, district, county crosswalk where available",
        "Annual",
        "Partial",
        "Yes",
        "No",
        "NCES_CCD",
        "County aggregation may require district/school geocoding or crosswalks.",
        "Which school partners should be engaged for youth behavioral health needs?",
    )
    add(
        "FREE_REDUCED_LUNCH",
        "Free/reduced-price lunch or school poverty proxy",
        "Food/Nutrition",
        "NCES Common Core of Data",
        "National Center for Education Statistics",
        "API / public files",
        "School, district, county crosswalk where available",
        "Annual",
        "Partial",
        "Yes",
        "Yes",
        "NCES_CCD",
        "Program rule changes can affect comparability over time.",
        "Do schools report increasing student and family economic stress?",
    )

    # Internal and qualitative placeholders
    add(
        "CLIENT_DEMOGRAPHICS",
        "CCBHC client demographics",
        "Underserved Populations",
        "Internal EHR / client data",
        "Client organization",
        "Internal file",
        "Client/service area",
        "Project-specific",
        "No",
        "No",
        "Yes",
        "INTERNAL_PLACEHOLDER",
        "Not available through government APIs.",
        "Which populations are represented in the community but underrepresented among clients?",
    )
    add(
        "SERVICE_UTILIZATION",
        "Service utilization, referrals, wait times, no-shows",
        "Access to Care",
        "Internal EHR / operations data",
        "Client organization",
        "Internal file",
        "Client/service area",
        "Project-specific",
        "No",
        "No",
        "Yes",
        "INTERNAL_PLACEHOLDER",
        "Not available through government APIs.",
        "Where do operational data show access barriers or service bottlenecks?",
    )
    add(
        "STAFFING_PLAN",
        "Staffing plan, FTEs, credentials, turnover, training",
        "Staffing Implications",
        "Internal staffing plan",
        "Client organization",
        "Internal file",
        "Client/service area",
        "Project-specific",
        "No",
        "No",
        "Yes",
        "INTERNAL_PLACEHOLDER",
        "Government APIs cannot determine organization-specific staffing alignment.",
        "What staffing changes are needed to address identified community needs?",
    )
    add(
        "QUALITATIVE_THEMES",
        "Interview, focus group, advisory board, and survey themes",
        "Qualitative Findings",
        "Primary qualitative research",
        "Client organization / consultant",
        "Internal file",
        "Service area",
        "Project-specific",
        "No",
        "No",
        "Yes",
        "CCBHC_TOOLKIT_2024",
        "Qualitative findings are not statistically representative unless designed as such.",
        "What explains the quantitative patterns, gaps, and outliers?",
    )

    return rows


# --------------------------------------------------------------------------------------
# Observation helper
# --------------------------------------------------------------------------------------

def make_observation(
    *,
    indicator_id: str,
    indicator_name: str,
    domain: str,
    source_name: str,
    source_agency: str,
    api_or_download: str,
    geography_name: str,
    geography_type: str,
    state_fips: str,
    county_fips: str,
    year_or_period: str,
    estimate: Optional[float],
    units: str,
    source_url_or_endpoint: str,
    source_citation_id: str,
    moe: Optional[float] = None,
    numerator: Optional[float] = None,
    denominator: Optional[float] = None,
    stratification: str = "Total",
    comparison_available: str = "No",
    data_quality_note: str = "",
) -> Observation:
    registry = build_citation_registry()
    return Observation(
        indicator_id=indicator_id,
        indicator_name=indicator_name,
        needs_assessment_domain=domain,
        source_name=source_name,
        source_agency=source_agency,
        api_or_download=api_or_download,
        geography_name=geography_name,
        geography_type=geography_type,
        state_fips=state_fips,
        county_fips=county_fips,
        year_or_period=year_or_period,
        estimate=estimate,
        moe=moe,
        numerator=numerator,
        denominator=denominator,
        units=units,
        stratification=stratification,
        comparison_available=comparison_available,
        data_quality_note=data_quality_note,
        source_url_or_endpoint=source_url_or_endpoint,
        source_citation_id=source_citation_id,
        source_citation_text=citation_text(source_citation_id, registry),
        retrieved_at=now_iso(),
    )


# --------------------------------------------------------------------------------------
# Census ACS extraction
# --------------------------------------------------------------------------------------

ACS_PROFILE_VARS = {
    "POP_TOTAL": {
        "estimate": "DP05_0001E",
        "moe": "DP05_0001M",
        "name": "Total population",
        "domain": "Service Area and Population",
        "units": "people",
    },
    "POP_AGE_UNDER_18": {
        "estimate": "DP05_0019E",
        "moe": "DP05_0019M",
        "name": "Population under age 18",
        "domain": "Demographics",
        "units": "percent",
    },
    "POP_AGE_65_PLUS": {
        "estimate": "DP05_0024E",
        "moe": "DP05_0024M",
        "name": "Population age 65 and older",
        "domain": "Demographics",
        "units": "percent",
    },
    "HISPANIC_LATINX": {
        "estimate": "DP05_0071E",
        "moe": "DP05_0071M",
        "name": "Hispanic or Latinx population",
        "domain": "Demographics",
        "units": "percent",
    },
    "POVERTY_RATE": {
        "estimate": "DP03_0128PE",
        "moe": "DP03_0128PM",
        "name": "Population below poverty level",
        "domain": "Economic Stability",
        "units": "percent",
    },
    "MEDIAN_HOUSEHOLD_INCOME": {
        "estimate": "DP03_0062E",
        "moe": "DP03_0062M",
        "name": "Median household income",
        "domain": "Economic Stability",
        "units": "dollars",
    },
    "UNINSURED_RATE": {
        "estimate": "DP03_0099PE",
        "moe": "DP03_0099PM",
        "name": "Uninsured rate",
        "domain": "Insurance Coverage",
        "units": "percent",
    },
    "MEDICAID_COVERAGE": {
        "estimate": "DP03_0096PE",
        "moe": "DP03_0096PM",
        "name": "Medicaid coverage",
        "domain": "Insurance Coverage",
        "units": "percent",
    },
    "NO_VEHICLE": {
        "estimate": "DP04_0058PE",
        "moe": "DP04_0058PM",
        "name": "Households with no vehicle available",
        "domain": "Transportation",
        "units": "percent",
    },
    "SNAP_HOUSEHOLDS": {
        "estimate": "DP03_0074PE",
        "moe": "DP03_0074PM",
        "name": "Households receiving SNAP",
        "domain": "Food/Nutrition",
        "units": "percent",
    },
    "MEDIAN_HOME_VALUE": {
        "estimate": "DP04_0089E",
        "moe": "DP04_0089M",
        "name": "Median home value",
        "domain": "Housing Stability",
        "units": "dollars",
    },
}


ACS_DETAILED_VARS = {
    "VETERAN_POPULATION": {
        "estimate": "B21001_002E",
        "moe": "B21001_002M",
        "denominator": "B21001_001E",
        "name": "Veteran population",
        "domain": "Underserved Populations",
        "units": "people",
    },
    "DISABILITY_STATUS": {
        "estimate": "B18101_004E",
        "moe": "B18101_004M",
        "denominator": "B18101_001E",
        "name": "Population with a disability",
        "domain": "Underserved Populations",
        "units": "people",
    },
    "AIAN_POPULATION": {
        "estimate": "B02001_004E",
        "moe": "B02001_004M",
        "denominator": "B02001_001E",
        "name": "American Indian and Alaska Native population",
        "domain": "Demographics",
        "units": "people",
    },
    "INTERNET_ACCESS": {
        "estimate": "B28002_004E",
        "moe": "B28002_004M",
        "denominator": "B28002_001E",
        "name": "Households without an internet subscription",
        "domain": "Transportation / Access to Care",
        "units": "households",
    },
}


ACS_SUBJECT_VARS = {
    "RENT_BURDEN": {
        "estimate": "S2503_C01_028E",
        "moe": "S2503_C01_028M",
        "name": "Gross rent 30 percent or more of household income",
        "domain": "Housing Stability",
        "units": "percent",
    },
}


def get_latest_acs_year(errors: list[str]) -> int:
    """
    Tries recent ACS years newest to oldest.
    """
    for year in range(dt.date.today().year - 1, dt.date.today().year - 8, -1):
        url = f"https://api.census.gov/data/{year}/acs/acs5/profile"
        data = cached_get_json("census_acs_metadata", url, params={"get": "NAME", "for": "us:1"}, errors=None)
        if isinstance(data, list) and len(data) > 1:
            return year

    errors.append("Could not detect latest ACS 5-year profile year; defaulting to 2023.")
    return 2023


def census_get(
    *,
    year: int,
    dataset: str,
    variables: list[str],
    geography: str,
    config: RunConfig,
    errors: list[str],
) -> Optional[list[dict[str, Any]]]:
    base = f"https://api.census.gov/data/{year}/acs/acs5/{dataset}".rstrip("/")
    params = {
        "get": ",".join(["NAME"] + variables),
    }

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

    data = cached_get_json("census_acs", base, params=params, errors=errors)
    if not data or not isinstance(data, list) or len(data) < 2:
        return None

    header = data[0]
    records = []
    for row in data[1:]:
        records.append(dict(zip(header, row)))

    return records


def extract_acs_profile(config: RunConfig, year: int, errors: list[str]) -> list[Observation]:
    observations: list[Observation] = []

    variables = sorted(
        set(
            v
            for spec in ACS_PROFILE_VARS.values()
            for v in [spec.get("estimate"), spec.get("moe")]
            if v
        )
    )

    for geography in ["county", "state", "us"]:
        records = census_get(
            year=year,
            dataset="profile",
            variables=variables,
            geography=geography,
            config=config,
            errors=errors,
        )

        if not records:
            continue

        rec = records[0]
        geo_name = rec.get("NAME", geography)
        geo_type = {"county": "County", "state": "State", "us": "United States"}[geography]

        for indicator_id, spec in ACS_PROFILE_VARS.items():
            est = safe_float(rec.get(spec["estimate"]))
            moe = safe_float(rec.get(spec["moe"]))

            observations.append(
                make_observation(
                    indicator_id=indicator_id,
                    indicator_name=spec["name"],
                    domain=spec["domain"],
                    source_name="ACS 5-year Data Profile",
                    source_agency="U.S. Census Bureau",
                    api_or_download="API",
                    geography_name=geo_name,
                    geography_type=geo_type,
                    state_fips=config.state_fips if geography != "us" else "",
                    county_fips=config.county_fips if geography == "county" else "",
                    year_or_period=str(year),
                    estimate=est,
                    moe=moe,
                    numerator=None,
                    denominator=None,
                    units=spec["units"],
                    stratification="Total",
                    comparison_available="Yes",
                    data_quality_note="ACS 5-year period estimate; review margins of error.",
                    source_url_or_endpoint=f"https://api.census.gov/data/{year}/acs/acs5/profile",
                    source_citation_id="CENSUS_ACS_API",
                )
            )

    return observations


def extract_acs_detailed(config: RunConfig, year: int, errors: list[str]) -> list[Observation]:
    observations: list[Observation] = []

    variables = sorted(
        set(
            v
            for spec in ACS_DETAILED_VARS.values()
            for v in [spec.get("estimate"), spec.get("moe"), spec.get("denominator")]
            if v
        )
    )

    for geography in ["county", "state", "us"]:
        records = census_get(
            year=year,
            dataset="",
            variables=variables,
            geography=geography,
            config=config,
            errors=errors,
        )

        if not records:
            continue

        rec = records[0]
        geo_name = rec.get("NAME", geography)
        geo_type = {"county": "County", "state": "State", "us": "United States"}[geography]

        for indicator_id, spec in ACS_DETAILED_VARS.items():
            est = safe_float(rec.get(spec["estimate"]))
            moe = safe_float(rec.get(spec["moe"]))

            denominator_key = spec.get("denominator")
            denominator = safe_float(rec.get(denominator_key)) if denominator_key else None

            observations.append(
                make_observation(
                    indicator_id=indicator_id,
                    indicator_name=spec["name"],
                    domain=spec["domain"],
                    source_name="ACS 5-year Detailed Tables",
                    source_agency="U.S. Census Bureau",
                    api_or_download="API",
                    geography_name=geo_name,
                    geography_type=geo_type,
                    state_fips=config.state_fips if geography != "us" else "",
                    county_fips=config.county_fips if geography == "county" else "",
                    year_or_period=str(year),
                    estimate=est,
                    moe=moe,
                    numerator=est,
                    denominator=denominator,
                    units=spec["units"],
                    stratification="Total",
                    comparison_available="Yes",
                    data_quality_note="ACS 5-year period estimate; review margins of error.",
                    source_url_or_endpoint=f"https://api.census.gov/data/{year}/acs/acs5",
                    source_citation_id="CENSUS_ACS_API",
                )
            )

    return observations


def extract_acs_subject(config: RunConfig, year: int, errors: list[str]) -> list[Observation]:
    observations: list[Observation] = []

    variables = sorted(
        set(
            v
            for spec in ACS_SUBJECT_VARS.values()
            for v in [spec.get("estimate"), spec.get("moe")]
            if v
        )
    )

    for geography in ["county", "state", "us"]:
        records = census_get(
            year=year,
            dataset="subject",
            variables=variables,
            geography=geography,
            config=config,
            errors=errors,
        )

        if not records:
            continue

        rec = records[0]
        geo_name = rec.get("NAME", geography)
        geo_type = {"county": "County", "state": "State", "us": "United States"}[geography]

        for indicator_id, spec in ACS_SUBJECT_VARS.items():
            est = safe_float(rec.get(spec["estimate"]))
            moe = safe_float(rec.get(spec["moe"]))

            observations.append(
                make_observation(
                    indicator_id=indicator_id,
                    indicator_name=spec["name"],
                    domain=spec["domain"],
                    source_name="ACS 5-year Subject Tables",
                    source_agency="U.S. Census Bureau",
                    api_or_download="API",
                    geography_name=geo_name,
                    geography_type=geo_type,
                    state_fips=config.state_fips if geography != "us" else "",
                    county_fips=config.county_fips if geography == "county" else "",
                    year_or_period=str(year),
                    estimate=est,
                    moe=moe,
                    numerator=None,
                    denominator=None,
                    units=spec["units"],
                    stratification="Total",
                    comparison_available="Yes",
                    data_quality_note="ACS 5-year period estimate; review margins of error.",
                    source_url_or_endpoint=f"https://api.census.gov/data/{year}/acs/acs5/subject",
                    source_citation_id="CENSUS_ACS_API",
                )
            )

    return observations


def extract_language_lep(config: RunConfig, year: int, errors: list[str]) -> list[Observation]:
    """
    ACS table C16001:
    Language spoken at home for population 5 years and over.
    This function gets Spanish speakers who speak English less than very well as a practical LEP proxy.
    """
    observations: list[Observation] = []

    variables = [
        "C16001_001E",
        "C16001_001M",
        "C16001_005E",
        "C16001_005M",
    ]

    for geography in ["county", "state", "us"]:
        records = census_get(
            year=year,
            dataset="",
            variables=variables,
            geography=geography,
            config=config,
            errors=errors,
        )

        if not records:
            continue

        rec = records[0]
        geo_name = rec.get("NAME", geography)
        geo_type = {"county": "County", "state": "State", "us": "United States"}[geography]

        lep_spanish = safe_float(rec.get("C16001_005E"))
        lep_spanish_moe = safe_float(rec.get("C16001_005M"))
        denominator = safe_float(rec.get("C16001_001E"))

        observations.append(
            make_observation(
                indicator_id="LANGUAGE_LEP",
                indicator_name="Spanish speakers who speak English less than very well",
                domain="Culture and Language",
                source_name="ACS 5-year Detailed Table C16001",
                source_agency="U.S. Census Bureau",
                api_or_download="API",
                geography_name=geo_name,
                geography_type=geo_type,
                state_fips=config.state_fips if geography != "us" else "",
                county_fips=config.county_fips if geography == "county" else "",
                year_or_period=str(year),
                estimate=lep_spanish,
                moe=lep_spanish_moe,
                numerator=lep_spanish,
                denominator=denominator,
                units="people",
                stratification="Spanish; speaks English less than very well",
                comparison_available="Yes",
                data_quality_note=(
                    "ACS language table. This is a specific LEP proxy, not all limited-English-proficiency residents."
                ),
                source_url_or_endpoint=f"https://api.census.gov/data/{year}/acs/acs5",
                source_citation_id="CENSUS_ACS_API",
            )
        )

    return observations


# --------------------------------------------------------------------------------------
# CDC PLACES extraction
# --------------------------------------------------------------------------------------

def extract_cdc_places(config: RunConfig, errors: list[str]) -> list[Observation]:
    """
    Attempts to query CDC Socrata API using common PLACES endpoint patterns.

    CDC PLACES datasets change identifiers over time, so this tries several candidate datasets.
    If the dataset schemas change, the script records a limitation rather than fabricating data.
    """
    observations: list[Observation] = []

    # Common Socrata endpoint candidates. If one stops working, try another.
    candidate_endpoints = [
        "https://data.cdc.gov/resource/cwsq-ngmh.json",
        "https://data.cdc.gov/resource/swc5-untb.json",
        "https://data.cdc.gov/resource/duw2-7jbt.json",
    ]

    measures = {
        "FREQUENT_MENTAL_DISTRESS": {
            "keywords": ["Frequent mental distress", "Mental health not good"],
            "name": "Frequent mental distress",
            "domain": "Mental Health Prevalence and Outcomes",
        },
        "DEPRESSION_PREVALENCE": {
            "keywords": ["Depression", "Current depression"],
            "name": "Depression prevalence",
            "domain": "Mental Health Prevalence and Outcomes",
        },
        "BINGE_DRINKING": {
            "keywords": ["Binge drinking"],
            "name": "Binge drinking",
            "domain": "Substance Use Prevalence and Outcomes",
        },
        "CURRENT_SMOKING": {
            "keywords": ["Current smoking"],
            "name": "Current smoking",
            "domain": "Physical Health and Co-occurring Conditions",
        },
    }

    # Try broad query by county FIPS.
    county_fips_full = f"{config.state_fips}{config.county_fips}"

    for endpoint in candidate_endpoints:
        data = cached_get_json(
            "cdc_places",
            endpoint,
            params={
                "$limit": 5000,
                "$where": (
                    f"locationid='{county_fips_full}' OR "
                    f"locationid='{int(county_fips_full)}'"
                ),
            },
            errors=None,
        )

        if isinstance(data, list) and data:
            for indicator_id, spec in measures.items():
                matching_rows = []
                for row in data:
                    row_text = json.dumps(row).lower()
                    if any(k.lower() in row_text for k in spec["keywords"]):
                        matching_rows.append(row)

                for row in matching_rows[:5]:
                    estimate = None
                    for candidate_field in [
                        "data_value",
                        "datavalue",
                        "estimate",
                        "prevalence",
                        "value",
                    ]:
                        if candidate_field in row:
                            estimate = safe_float(row.get(candidate_field))
                            break

                    year = (
                        row.get("year")
                        or row.get("yearend")
                        or row.get("data_value_year")
                        or row.get("datavaluetypeid")
                        or "latest available"
                    )

                    observations.append(
                        make_observation(
                            indicator_id=indicator_id,
                            indicator_name=spec["name"],
                            domain=spec["domain"],
                            source_name="CDC PLACES",
                            source_agency="Centers for Disease Control and Prevention",
                            api_or_download="API / Socrata",
                            geography_name=row.get("locationname", config.county_name),
                            geography_type=row.get("geographiclevel", "County"),
                            state_fips=config.state_fips,
                            county_fips=config.county_fips,
                            year_or_period=str(year),
                            estimate=estimate,
                            moe=None,
                            numerator=None,
                            denominator=None,
                            units=row.get("data_value_unit", "percent"),
                            stratification=row.get("measure", "Total"),
                            comparison_available="Partial",
                            data_quality_note=(
                                "CDC PLACES modeled estimate. Validate dataset schema and measure label before final reporting."
                            ),
                            source_url_or_endpoint=endpoint,
                            source_citation_id="CDC_PLACES_API",
                        )
                    )

            if observations:
                return observations

    errors.append(
        "CDC PLACES extraction did not return usable records. Dataset endpoint/schema may have changed; "
        "use CDC PLACES download or update Socrata dataset ID."
    )
    return observations


# --------------------------------------------------------------------------------------
# BLS LAUS extraction
# --------------------------------------------------------------------------------------

def build_bls_laus_county_series_id(state_fips: str, county_fips: str) -> str:
    """
    BLS LAUS county unemployment rate series IDs commonly use:
    LAUCN + 5-digit county FIPS + 0000000003

    Example:
      La Plata County, CO = state 08 + county 067 = 08067
      Series = LAUCN080670000000003

    Validate against BLS metadata for production use.
    """
    full_county_fips = f"{state_fips}{county_fips}"
    return f"LAUCN{full_county_fips}0000000003"


def extract_bls_laus(config: RunConfig, errors: list[str]) -> list[Observation]:
    observations: list[Observation] = []

    series_id = build_bls_laus_county_series_id(config.state_fips, config.county_fips)
    end_year = dt.date.today().year
    start_year = end_year - 5

    url = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
    payload = {
        "seriesid": [series_id],
        "startyear": str(start_year),
        "endyear": str(end_year),
    }

    # BLS expects POST. Cache manually.
    source_dir = CACHE_DIR / "bls_laus" / dt.date.today().isoformat()
    source_dir.mkdir(parents=True, exist_ok=True)
    path = source_dir / f"{hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()}.json"

    data = None
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = None

    if data is None:
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                time.sleep(REQUEST_SLEEP_SECONDS)
                response = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
                if response.status_code >= 400:
                    last_error = f"{response.status_code}: {response.text[:500]}"
                    time.sleep(attempt)
                    continue
                data = response.json()
                path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                break
            except Exception as exc:
                last_error = repr(exc)
                time.sleep(attempt)

        if data is None:
            errors.append(f"BLS LAUS API failed for series {series_id}: {last_error}")
            return observations

    try:
        series = data["Results"]["series"][0]["data"]
    except Exception:
        errors.append(f"BLS LAUS response did not include expected data for series {series_id}.")
        return observations

    for row in series:
        # Annual average is period M13 in many BLS datasets.
        if row.get("period") != "M13":
            continue

        estimate = safe_float(row.get("value"))
        year = row.get("year")

        observations.append(
            make_observation(
                indicator_id="UNEMPLOYMENT_RATE",
                indicator_name="Unemployment rate",
                domain="Economic Stability",
                source_name="BLS LAUS",
                source_agency="U.S. Bureau of Labor Statistics",
                api_or_download="API",
                geography_name=config.county_name,
                geography_type="County",
                state_fips=config.state_fips,
                county_fips=config.county_fips,
                year_or_period=str(year),
                estimate=estimate,
                moe=None,
                numerator=None,
                denominator=None,
                units="percent",
                stratification="Annual average",
                comparison_available="No",
                data_quality_note=(
                    f"BLS LAUS county series inferred as {series_id}; validate series metadata before final reporting."
                ),
                source_url_or_endpoint=url,
                source_citation_id="BLS_LAUS_API",
            )
        )

    if not observations:
        errors.append(f"BLS LAUS returned no annual average observations for series {series_id}.")

    return observations


# --------------------------------------------------------------------------------------
# Placeholder observations for partially automated sources
# --------------------------------------------------------------------------------------

def add_placeholder_observations(config: RunConfig) -> list[Observation]:
    """
    Emits null observations for important CNA indicators not fully automated in this single-file version.
    """
    placeholders = [
        {
            "indicator_id": "SUICIDE_MORTALITY",
            "indicator_name": "Suicide deaths and age-adjusted suicide mortality rate",
            "domain": "Mental Health Prevalence and Outcomes",
            "source_name": "CDC WONDER / NVSS",
            "source_agency": "Centers for Disease Control and Prevention",
            "api_or_download": "Manual download fallback",
            "units": "deaths per 100,000",
            "endpoint": "https://wonder.cdc.gov/",
            "citation": "CDC_WONDER_NVSS",
            "note": (
                "Manual CDC WONDER query/download recommended. Small county counts may be suppressed; "
                "multi-year aggregation may be needed."
            ),
        },
        {
            "indicator_id": "OVERDOSE_MORTALITY",
            "indicator_name": "Drug overdose deaths and age-adjusted overdose mortality rate",
            "domain": "Substance Use Prevalence and Outcomes",
            "source_name": "CDC WONDER / NVSS",
            "source_agency": "Centers for Disease Control and Prevention",
            "api_or_download": "Manual download fallback",
            "units": "deaths per 100,000",
            "endpoint": "https://wonder.cdc.gov/",
            "citation": "CDC_WONDER_NVSS",
            "note": (
                "Manual CDC WONDER query/download recommended. Use ICD-10 drug overdose cause-of-death "
                "groupings and document suppression."
            ),
        },
        {
            "indicator_id": "HOMELESSNESS_PIT",
            "indicator_name": "People experiencing homelessness",
            "domain": "Housing Stability",
            "source_name": "HUD PIT/HIC",
            "source_agency": "U.S. Department of Housing and Urban Development",
            "api_or_download": "Public download",
            "units": "people",
            "endpoint": "https://www.hudexchange.info/programs/hdx/pit-hic/",
            "citation": "HUD_PIT_HIC",
            "note": (
                "HUD PIT/HIC data are often reported by Continuum of Care rather than county. "
                "Use county/CoC crosswalk or local PIT files where available."
            ),
        },
        {
            "indicator_id": "MENTAL_HEALTH_HPSA",
            "indicator_name": "Mental Health Professional Shortage Area status",
            "domain": "Workforce and Provider Availability",
            "source_name": "HRSA Data Warehouse",
            "source_agency": "Health Resources and Services Administration",
            "api_or_download": "API / public files",
            "units": "designation",
            "endpoint": "https://data.hrsa.gov/",
            "citation": "HRSA_DATA_WAREHOUSE",
            "note": (
                "HRSA HPSA geographies may not align with counties. Download current HPSA file or connect "
                "to HRSA API endpoint for production extraction."
            ),
        },
        {
            "indicator_id": "TREATMENT_FACILITIES_MH",
            "indicator_name": "Mental health treatment facilities",
            "domain": "Treatment Facilities and Service Infrastructure",
            "source_name": "SAMHSA FindTreatment / N-SUMHSS",
            "source_agency": "Substance Abuse and Mental Health Services Administration",
            "api_or_download": "Locator / public files",
            "units": "facilities",
            "endpoint": "https://findtreatment.gov/",
            "citation": "SAMHSA_FINDTREATMENT",
            "note": (
                "Use SAMHSA locator/N-SUMHSS files to count facilities and validate service availability locally."
            ),
        },
        {
            "indicator_id": "TREATMENT_FACILITIES_SUD",
            "indicator_name": "SUD treatment facilities and MOUD availability",
            "domain": "Treatment Facilities and Service Infrastructure",
            "source_name": "SAMHSA FindTreatment / N-SUMHSS",
            "source_agency": "Substance Abuse and Mental Health Services Administration",
            "api_or_download": "Locator / public files",
            "units": "facilities",
            "endpoint": "https://findtreatment.gov/",
            "citation": "SAMHSA_FINDTREATMENT",
            "note": (
                "Use SAMHSA locator/N-SUMHSS files to count SUD facilities and MOUD availability; "
                "validate capacity, payer acceptance, and referral pathways locally."
            ),
        },
        {
            "indicator_id": "SCHOOL_ENROLLMENT",
            "indicator_name": "School enrollment and student demographics",
            "domain": "Children, Youth, and Families",
            "source_name": "NCES Common Core of Data",
            "source_agency": "National Center for Education Statistics",
            "api_or_download": "API / public files",
            "units": "students",
            "endpoint": "https://nces.ed.gov/ccd/",
            "citation": "NCES_CCD",
            "note": (
                "County aggregation may require a school/district-to-county crosswalk or state education files."
            ),
        },
        {
            "indicator_id": "FREE_REDUCED_LUNCH",
            "indicator_name": "Free/reduced-price lunch or school poverty proxy",
            "domain": "Food/Nutrition",
            "source_name": "NCES Common Core of Data",
            "source_agency": "National Center for Education Statistics",
            "api_or_download": "API / public files",
            "units": "students or percent",
            "endpoint": "https://nces.ed.gov/ccd/",
            "citation": "NCES_CCD",
            "note": (
                "Program rule changes can affect comparability over time. Use local district/state files when possible."
            ),
        },
        {
            "indicator_id": "CLIENT_DEMOGRAPHICS",
            "indicator_name": "CCBHC client demographics",
            "domain": "Underserved Populations",
            "source_name": "Internal EHR / client data",
            "source_agency": "Client organization",
            "api_or_download": "Internal file",
            "units": "clients",
            "endpoint": "Not available through government API",
            "citation": "INTERNAL_PLACEHOLDER",
            "note": (
                "Internal EHR/client demographics are required to compare client population to service-area population."
            ),
        },
        {
            "indicator_id": "SERVICE_UTILIZATION",
            "indicator_name": "Service utilization, referrals, wait times, no-shows",
            "domain": "Access to Care",
            "source_name": "Internal EHR / operations data",
            "source_agency": "Client organization",
            "api_or_download": "Internal file",
            "units": "varies",
            "endpoint": "Not available through government API",
            "citation": "INTERNAL_PLACEHOLDER",
            "note": "Operational data are required to understand real access, utilization, and service gaps.",
        },
        {
            "indicator_id": "STAFFING_PLAN",
            "indicator_name": "Staffing plan, FTEs, credentials, turnover, training",
            "domain": "Staffing Implications",
            "source_name": "Internal staffing plan",
            "source_agency": "Client organization",
            "api_or_download": "Internal file",
            "units": "FTE / roles / credentials",
            "endpoint": "Not available through government API",
            "citation": "INTERNAL_PLACEHOLDER",
            "note": "Staffing plan alignment is a CNA requirement but cannot be filled through government APIs.",
        },
        {
            "indicator_id": "QUALITATIVE_THEMES",
            "indicator_name": "Interview, focus group, advisory board, and survey themes",
            "domain": "Qualitative Findings",
            "source_name": "Primary qualitative research",
            "source_agency": "Client organization / consultant",
            "api_or_download": "Internal file",
            "units": "themes",
            "endpoint": "Not available through government API",
            "citation": "CCBHC_TOOLKIT_2024",
            "note": "Qualitative data explain the why behind quantitative patterns and identify gaps not captured by APIs.",
        },
    ]

    observations: list[Observation] = []

    for p in placeholders:
        observations.append(
            make_observation(
                indicator_id=p["indicator_id"],
                indicator_name=p["indicator_name"],
                domain=p["domain"],
                source_name=p["source_name"],
                source_agency=p["source_agency"],
                api_or_download=p["api_or_download"],
                geography_name=config.service_area_name,
                geography_type="Service area",
                state_fips=config.state_fips,
                county_fips=config.county_fips,
                year_or_period="latest available / project-specific",
                estimate=None,
                moe=None,
                numerator=None,
                denominator=None,
                units=p["units"],
                stratification="Total",
                comparison_available="No",
                data_quality_note=p["note"],
                source_url_or_endpoint=p["endpoint"],
                source_citation_id=p["citation"],
            )
        )

    return observations


# --------------------------------------------------------------------------------------
# Output writers
# --------------------------------------------------------------------------------------

def write_indicator_catalog(rows: list[IndicatorCatalogRow]) -> None:
    df = pd.DataFrame([asdict(r) for r in rows])
    df.to_csv(OUTPUT_DIR / "indicator_catalog.csv", index=False)


def write_observations(observations: list[Observation]) -> None:
    df = pd.DataFrame([asdict(o) for o in observations])
    df.to_csv(OUTPUT_DIR / "needs_assessment_data_long.csv", index=False)


def write_source_metadata(config: RunConfig, errors: list[str]) -> None:
    registry = build_citation_registry()
    payload = {
        "retrieved_at": now_iso(),
        "run_config": asdict(config),
        "citations": [asdict(c) for c in registry.values()],
        "errors": errors,
    }
    (OUTPUT_DIR / "source_metadata.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_citation_appendix() -> None:
    registry = build_citation_registry()

    lines = [
        "# Citation Appendix",
        "",
        "This appendix lists the source references used by the indicator catalog and extracted observations.",
        "",
        "| Citation ID | Label | Source | Agency/Author | Date | Reference | Notes |",
        "|---|---|---|---|---|---|---|",
    ]

    for c in registry.values():
        lines.append(
            "| {citation_id} | {label} | {title} | {agency} | {date} | {ref} | {notes} |".format(
                citation_id=c.citation_id,
                label=c.citation_label.replace("|", "/"),
                title=c.source_title.replace("|", "/"),
                agency=c.source_agency_or_author.replace("|", "/"),
                date=c.publication_date.replace("|", "/"),
                ref=c.url_or_file_reference.replace("|", "/"),
                notes=c.notes.replace("|", "/"),
            )
        )

    (OUTPUT_DIR / "citation_appendix.md").write_text("\n".join(lines), encoding="utf-8")


def write_data_availability(catalog: list[IndicatorCatalogRow], observations: list[Observation]) -> None:
    obs_by_indicator: dict[str, list[Observation]] = {}

    for obs in observations:
        obs_by_indicator.setdefault(obs.indicator_id, []).append(obs)

    lines = [
        "# Data Availability for CCBHC Community Needs Assessment",
        "",
        "This file summarizes which needs-assessment indicators were filled from government APIs, public downloads, or placeholders.",
        "",
        "The indicator domains are aligned to CCBHC needs assessment requirements, including service area, behavioral health/SUD prevalence, SDOH, cultures and languages, underserved populations, staffing alignment, and update planning.",
        "",
        "| Data element | Indicator | Source | Citation | API/download status | Geography level | Filled? | Limitation | Recommended qualitative follow-up |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    for row in catalog:
        filled = "No"

        if row.indicator_id in obs_by_indicator:
            any_estimate = any(o.estimate is not None for o in obs_by_indicator[row.indicator_id])
            filled = "Yes" if any_estimate else "Partial"

        lines.append(
            "| {domain} | {indicator} | {source} | {citation} | {status} | {geo} | {filled} | {limitation} | {followup} |".format(
                domain=row.needs_assessment_domain.replace("|", "/"),
                indicator=row.indicator_name.replace("|", "/"),
                source=f"{row.source_name} ({row.source_agency})".replace("|", "/"),
                citation=row.source_citation_id,
                status=row.api_or_download.replace("|", "/"),
                geo=row.expected_geography_level.replace("|", "/"),
                filled=filled,
                limitation=row.limitation.replace("|", "/"),
                followup=row.recommended_qualitative_followup_question.replace("|", "/"),
            )
        )

    lines.extend(
        [
            "",
            "## Notes on citations",
            "",
            "- `source_citation_id` links each indicator and observation back to `citation_appendix.md`.",
            "- `source_url_or_endpoint` in `needs_assessment_data_long.csv` records the specific endpoint or source location used for each extracted observation.",
            "- Government API outputs should still be reviewed for recency, suppression, modeled-vs-observed status, and geography limitations before insertion into a final CNA report.",
        ]
    )

    (OUTPUT_DIR / "data_availability.md").write_text("\n".join(lines), encoding="utf-8")


def write_api_errors_and_limitations(errors: list[str]) -> None:
    lines = [
        "# API Errors and Data Limitations",
        "",
        "This file records API errors and known limitations relevant to the needs assessment.",
        "",
        "## General limitations",
        "",
        "- Some county-level behavioral health prevalence indicators are modeled estimates rather than observed survey estimates.",
        "- CDC WONDER/NVSS mortality queries may require manual download fallback and may suppress small counts.",
        "- HUD homelessness data may be reported by Continuum of Care rather than county.",
        "- NCES school data may require school/district-to-county crosswalks.",
        "- HRSA HPSA and MUA/P geographies may not align exactly with county boundaries.",
        "- SAMHSA treatment locator data should be validated locally for capacity, payer acceptance, and service availability.",
        "- Internal client, service utilization, staffing, referral, satisfaction, interview, and focus group data are not available through government APIs.",
        "- Qualitative findings should be interpreted as illustrative unless the study design supports statistical generalization.",
        "",
        "## API errors captured during this run",
        "",
    ]

    if not errors:
        lines.append("No API errors captured.")
    else:
        for err in errors:
            lines.append(f"- {err}")

    (OUTPUT_DIR / "api_errors_and_limitations.md").write_text("\n".join(lines), encoding="utf-8")


def write_readme(config: RunConfig) -> None:
    lines = [
        "# Needs Assessment Data Extraction",
        "",
        "This folder contains outputs from `needs_assessment_data.py`.",
        "",
        "## Run configuration",
        "",
        f"- Service area: {config.service_area_name}",
        f"- County: {config.county_name}",
        f"- State abbreviation: {config.state_abbr}",
        f"- State FIPS: {config.state_fips}",
        f"- County FIPS: {config.county_fips}",
        f"- Years: {'latest' if config.latest else ', '.join(str(y) for y in config.years)}",
        "",
        "## Output files",
        "",
        "- `indicator_catalog.csv`: all indicators, including government API indicators, public download indicators, and internal/qualitative placeholders.",
        "- `needs_assessment_data_long.csv`: extracted and placeholder observations in long format.",
        "- `source_metadata.json`: run configuration, citation registry, and API errors.",
        "- `data_availability.md`: readable availability table.",
        "- `api_errors_and_limitations.md`: errors and known limitations.",
        "- `citation_appendix.md`: citation registry.",
        "",
        "## Important review steps",
        "",
        "1. Validate all ACS estimates and margins of error.",
        "2. Confirm whether CDC PLACES measures are modeled estimates and label them as such.",
        "3. Download CDC WONDER/NVSS suicide and overdose mortality data manually if needed.",
        "4. Validate treatment facility availability with local partners.",
        "5. Load internal client demographics, service utilization, staffing, and qualitative data separately.",
    ]

    (OUTPUT_DIR / "README.md").write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------------------
# Main orchestration
# --------------------------------------------------------------------------------------

def parse_args() -> RunConfig:
    parser = argparse.ArgumentParser(
        description="Extract government API data for CCBHC community needs assessments."
    )

    parser.add_argument("--state-fips", required=True, help="Two-digit state FIPS, e.g., 08.")
    parser.add_argument("--county-fips", required=True, help="Three-digit county FIPS, e.g., 067.")
    parser.add_argument("--state-abbr", required=True, help="State abbreviation, e.g., CO.")
    parser.add_argument("--county-name", required=True, help='County name, e.g., "La Plata County".')
    parser.add_argument(
        "--service-area-name",
        required=True,
        help='Service area name, e.g., "La Plata County, Colorado".',
    )
    parser.add_argument("--latest", action="store_true", help="Use latest available year where supported.")
    parser.add_argument(
        "--years",
        nargs="*",
        type=int,
        default=[],
        help="Specific years to query where supported, e.g., --years 2022 2023 2024.",
    )
    parser.add_argument(
        "--census-api-key",
        default=os.getenv("CENSUS_API_KEY"),
        help="Optional Census API key. Can also be set as CENSUS_API_KEY environment variable.",
    )

    args = parser.parse_args()

    state_fips = normalize_fips(args.state_fips, 2)
    county_fips = normalize_fips(args.county_fips, 3)

    if not args.latest and not args.years:
        args.latest = True

    return RunConfig(
        state_fips=state_fips,
        county_fips=county_fips,
        state_abbr=args.state_abbr.upper(),
        county_name=args.county_name,
        service_area_name=args.service_area_name,
        latest=args.latest,
        years=args.years,
        census_api_key=args.census_api_key,
    )


def run(config: RunConfig) -> None:
    ensure_dirs()

    errors: list[str] = []
    catalog = build_indicator_catalog()
    observations: list[Observation] = []

    if config.latest:
        acs_years = [get_latest_acs_year(errors)]
    else:
        acs_years = config.years

    for year in acs_years:
        observations.extend(extract_acs_profile(config, year, errors))
        observations.extend(extract_acs_detailed(config, year, errors))
        observations.extend(extract_acs_subject(config, year, errors))
        observations.extend(extract_language_lep(config, year, errors))

    observations.extend(extract_bls_laus(config, errors))
    observations.extend(extract_cdc_places(config, errors))
    observations.extend(add_placeholder_observations(config))

    write_indicator_catalog(catalog)
    write_observations(observations)
    write_source_metadata(config, errors)
    write_citation_appendix()
    write_data_availability(catalog, observations)
    write_api_errors_and_limitations(errors)
    write_readme(config)

    print("Done.")
    print(f"Wrote outputs to: {OUTPUT_DIR.resolve()}")
    print(f"Observations: {len(observations)}")
    print(f"Errors/limitations captured: {len(errors)}")


def main() -> None:
    config = parse_args()
    run(config)


if __name__ == "__main__":
    main()