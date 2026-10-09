from app.sourcing.company_list import CompanySpec

# Hardcoded fallback list, same CompanySpec shape as company_list.py,
# consulted ONLY if Brave Search discovery yields no usable postings (no API
# key, Brave failure, zero results, or every discovered guess failed to
# resolve). Disjoint from KNOWN_COMPANIES so the fallback adds new companies.
# Slugs verified against the live public APIs on 2026-10-06.
SEED_COMPANIES: list[CompanySpec] = [
    CompanySpec(name="Figma", platform="greenhouse", slug="figma"),
    CompanySpec(name="GitLab", platform="greenhouse", slug="gitlab"),
    CompanySpec(name="Cloudflare", platform="greenhouse", slug="cloudflare"),
    CompanySpec(name="Match Group", platform="lever", slug="matchgroup"),
    CompanySpec(name="Zoox", platform="lever", slug="zoox"),
    CompanySpec(name="Notion", platform="ashby", slug="notion"),
    CompanySpec(name="Ramp", platform="ashby", slug="ramp"),
    CompanySpec(name="Cursor", platform="ashby", slug="cursor"),
]
