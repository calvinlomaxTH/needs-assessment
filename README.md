# Needs Assessment Data Puller

This repository contains a Python workflow for building a CCBHC/community needs assessment dataset from public government data sources. The main script, `needs_assessment_data.py`, pulls county, state, and U.S. comparison indicators; caches raw API/download responses; and writes a documented output bundle under `outputs/`.

The current checked output bundle is configured for Cook County, Illinois.

## What It Produces

Running the script creates:

- `outputs/needs_assessment_data_long.csv`: long-form indicator observations by geography, year/period, source, estimate, denominator, units, citation, and data quality note.
- `outputs/indicator_catalog.csv`: indicator definitions, units, source metadata, limitations, and follow-up questions.
- `outputs/data_availability.md`: human-readable summary of which indicators were filled, partially filled, or left as placeholders.
- `outputs/citation_appendix.md`: citation table for all source systems used by the workflow.
- `outputs/api_errors_and_limitations.md`: run notes, API errors, and source-specific limitations.
- `outputs/source_metadata.json`: run configuration, retrieval timestamp, citations, notes, errors, and latest source periods.
- `outputs/README.md`: generated summary for the latest output run.

Raw API responses and downloads are cached in `data/raw/<source>/<date>/` so runs are auditable and easier to troubleshoot.

## Data Sources

The workflow currently uses best-effort pulls from:

- U.S. Census Bureau ACS 5-year APIs for demographics, social drivers of health, insurance, Medicaid/means-tested public coverage, housing, and transportation indicators.
- BLS Local Area Unemployment Statistics for annual unemployment.
- CDC PLACES for modeled behavioral and physical health prevalence indicators.
- CDC MIVO County data for suicide and drug overdose mortality counts/rates.
- HUD AHAR/PIT data for homelessness indicators.
- HRSA Data Warehouse shortage area downloads for Mental Health HPSA indicators.
- SAMHSA FindTreatment API for mental health and substance use treatment facility counts.
- NCES CCD data through the Urban Institute Education Data API for school enrollment.

Internal client demographics, utilization, staffing, and qualitative findings are intentionally written as placeholders because they are not available from public government APIs.

## Setup

Use Python 3.10 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install requests pandas openpyxl pyxlsb
```

A Census API key is recommended. Provide it through an environment variable:

```bash
export CENSUS_API_KEY="your-census-api-key"
```

Do not commit local credential files such as `key.txt`.

## Run Example

Cook County, Illinois:

```bash
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
```

The script prints the output directory and row counts when it finishes.

## Common Options

- `--state-fips`, `--county-fips`, `--state-abbr`, `--county-name`, and `--service-area-name` define the service geography.
- `--comparison-geographies` selects comparison rows from `county`, `state`, and `us`.
- `--latest` asks the script to use the most recent available ACS endpoints.
- `--years` can be used instead of `--latest` for explicit ACS years.
- `--annual-years` controls annual series length for indicators such as HUD PIT, unemployment, and uninsured.
- `--hud-coc-codes` improves HUD PIT matching when the service area maps to known Continuums of Care.
- `--hud-pit-file` or `--hud-pit-url` can supply a PIT file directly if HUD link discovery fails.
- `--hrsa-hpsa-file` or `--hrsa-hpsa-url` can supply HRSA shortage area data directly.
- `--samhsa-lat`, `--samhsa-lon`, and `--samhsa-radius-miles` enable radius-based SAMHSA facility searches.
- `--nces-year` pins the NCES/CCD enrollment year.
- `--verbose` enables detailed logging.

## Output Review Checklist

Before using the outputs in a final needs assessment:

1. Review `outputs/api_errors_and_limitations.md` for failed pulls, fallback behavior, and notes.
2. Review `outputs/data_availability.md` to identify indicators that are partial or require internal data.
3. Validate HUD CoC coverage for the service area, especially if the county spans multiple CoCs.
4. Validate HRSA, SAMHSA, and NCES rows because those adapters rely on best-effort public downloads/API parsing.
5. Treat ACS 5-year estimates as period estimates and interpret overlapping annual trends cautiously.
6. Add internal EHR, staffing, utilization, wait-time, referral, and qualitative findings outside the government API workflow.

## Repository Layout

```text
.
├── needs_assessment_data.py      # Main extraction and output-generation script
├── data/raw/                     # Cached raw API and download responses
├── outputs/                      # Generated CSV, Markdown, and JSON outputs
├── MediRate Instructions.docx    # Project instructions/reference document
└── key.txt                       # Local credential file; keep out of version control
```

## Notes

This workflow does not fabricate unavailable data. When a public source cannot supply an indicator, the output row is marked as partial or not pulled, with a data quality note explaining the gap.
