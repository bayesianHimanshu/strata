# NICE guidance HTML fixtures

Representative `nice.org.uk/guidance/{ta}` pages for unit-testing the **pure**
`sources.nice_guidance.parse_guidance` (date + rationale extraction) with no network.

They mirror the real page structure exercised by the parser:
- a published date as a `<time datetime="YYYY-MM-DD">` element and/or `Published: …` text,
- a `1 Recommendations` (or `Recommendations`) section carrying the verdict and the
  `Why the committee made these recommendations` discussion (the gold-bearing text),
- a following numbered section (`2 …`) that bounds the rationale.

Authored offline (the build sandbox has no network). When run open-network, replace
with live-fetched snapshots — the parser is unchanged.
