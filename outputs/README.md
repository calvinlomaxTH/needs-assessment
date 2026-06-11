# Needs Assessment Data Extraction

This folder contains outputs from `needs_assessment_data.py`.

## Run configuration

- Service area: La Plata County, Colorado
- County: La Plata County
- State abbreviation: CO
- State FIPS: 08
- County FIPS: 067
- Years: latest

## Output files

- `indicator_catalog.csv`: all indicators, including government API indicators, public download indicators, and internal/qualitative placeholders.
- `needs_assessment_data_long.csv`: extracted and placeholder observations in long format.
- `source_metadata.json`: run configuration, citation registry, and API errors.
- `data_availability.md`: readable availability table.
- `api_errors_and_limitations.md`: errors and known limitations.
- `citation_appendix.md`: citation registry.

## Important review steps

1. Validate all ACS estimates and margins of error.
2. Confirm whether CDC PLACES measures are modeled estimates and label them as such.
3. Download CDC WONDER/NVSS suicide and overdose mortality data manually if needed.
4. Validate treatment facility availability with local partners.
5. Load internal client demographics, service utilization, staffing, and qualitative data separately.