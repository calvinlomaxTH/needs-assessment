# API Errors and Data Limitations

This file records API errors and known limitations relevant to the needs assessment.

## General limitations

- Some county-level behavioral health prevalence indicators are modeled estimates rather than observed survey estimates.
- CDC WONDER/NVSS mortality queries may require manual download fallback and may suppress small counts.
- HUD homelessness data may be reported by Continuum of Care rather than county.
- NCES school data may require school/district-to-county crosswalks.
- HRSA HPSA and MUA/P geographies may not align exactly with county boundaries.
- SAMHSA treatment locator data should be validated locally for capacity, payer acceptance, and service availability.
- Internal client, service utilization, staffing, referral, satisfaction, interview, and focus group data are not available through government APIs.
- Qualitative findings should be interpreted as illustrative unless the study design supports statistical generalization.

## API errors captured during this run

- Could not detect latest ACS 5-year profile year; defaulting to 2023.
- BLS LAUS returned no annual average observations for series LAUCN080670000000003.