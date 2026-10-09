from dataclasses import dataclass


@dataclass(frozen=True)
class CompanySpec:
    name: str  # display name, e.g. "Linear"
    platform: str  # "greenhouse" | "lever" | "ashby"
    slug: str  # that platform's board/company slug


# Hand-curated starter list, queried directly on every run (step 1 of the
# fallback order). Every slug below was verified against the live public API
# on 2026-10-06. Note S4.md's examples were wrong for today's boards: Linear
# is on Ashby (not Greenhouse) and Mercury is not on Lever.
KNOWN_COMPANIES: list[CompanySpec] = [
    CompanySpec(name="Discord", platform="greenhouse", slug="discord"),
    CompanySpec(name="Airbnb", platform="greenhouse", slug="airbnb"),
    CompanySpec(name="Robinhood", platform="greenhouse", slug="robinhood"),
    CompanySpec(name="Spotify", platform="lever", slug="spotify"),
    CompanySpec(name="Outreach", platform="lever", slug="outreach"),
    CompanySpec(name="Ro", platform="lever", slug="ro"),
    CompanySpec(name="Linear", platform="ashby", slug="linear"),
    CompanySpec(name="Supabase", platform="ashby", slug="supabase"),
    CompanySpec(name="PostHog", platform="ashby", slug="posthog"),
]
