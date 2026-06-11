# Needs Assessment Data Outputs

Service area: Cook County, Illinois
County: IL
State FIPS: 17
County FIPS: 031

## Files

- `indicator_catalog.csv`: indicator inventory with summary, detailed description, units, and citation metadata.
- `needs_assessment_data_long.csv`: extracted and placeholder observations with summaries, descriptions, source labels, units, and citations.
- `source_metadata.json`: run configuration, citation registry, latest source summary, and API errors.
- `data_availability.md`: readable data availability table.
- `citation_appendix.md`: citation registry.
- `api_errors_and_limitations.md`: errors and known limitations.

## Unit logic

- ACS count variables are reported as people or households.
- ACS dollar variables are reported as dollars.
- ACS percentage variables are reported as percent.
- Script-calculated percentages use numerator / denominator * 100.
- BLS LAUS unemployment rate is reported as percent.
- CDC PLACES units are taken from API metadata and normalized where needed.
- Manual/download placeholders use the expected unit for that indicator.

## Important coverage note

- PUBLIC_COVERAGE_RATE is ACS public insurance coverage.
- MEDICAID_COVERAGE_RATE is not derived from PUBLIC_COVERAGE_RATE; it requires a Medicaid-specific source.