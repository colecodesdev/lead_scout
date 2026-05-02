# LeadScout — Project Overview

## What This Is

A Python CLI tool that finds local restaurants without websites (or with bad ones), audits their web presence, scores them as leads, and outputs a ranked list for freelance web design cold outreach. Built for Cole Codes targeting the 30A/Santa Rosa Beach, FL area.

## Pipeline

```
[Google Places API] → Search for restaurants in area
        ↓
[Google Custom Search API] → Find websites for businesses missing one
        ↓
[URL Classification] → official site / social media / directory listing / none
        ↓
[PageSpeed Insights + Playwright] → Audit sites that exist
        ↓
[Scoring] → Rank leads by what they're missing
        ↓
[JSON output] → Ranked lead list with deficiencies
```

## Constraints

- All APIs stay within free tiers ($0/mo target)
- No database, JSON files only
- No web frontend, CLI only
- No outreach generation, Cole writes pitches manually
- Idempotent: re-running updates, doesn't duplicate

## Full Design Doc

See `docs/leadscout_design_doc.md` for the complete design document including data schema, key decisions, dependency costs, and definition of done.
