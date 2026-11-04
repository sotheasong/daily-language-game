# Known footprint issues

Parked on 2026-11-02 so weights could start. Each one is real, checked by eye or
measured, and none of them block weighting. Work them on the
`footprint-refinements` branch.

## Geometry accuracy

**Mainland Southeast Asia bleeds across borders.** Asher draws these as broad
generalised areas and they overlap neighbours heavily.

| Entry | Footprint spans | Country spans | Note |
|---|---|---|---|
| Khmer | 99.0–109.9°E | Cambodia 102.3–107.6°E | NE Thailand is Northern Khmer, about 1.4M speakers, and the Mekong Delta is Khmer Krom, about 1M. Both legitimate. The western lobe reaching toward the Gulf of Thailand is not. |
| Thai | 93.9–107.1°E | Thailand 97.3–105.6°E | Runs west into Myanmar. |

Fix by clipping each footprint against the countries Glottolog lists for the
entry, which `tools/check_countries.py` already computes. That turns an
over-extended blob into the intersection of the atlas area and the plausible
countries, without hand-drawing anything.

**Asher's shapes are generalised throughout.** Kabyle is a single smooth blob
rather than the Kabylie massif. Fine for scoring by distance, generous for
containment. Worth a pass over tier 1, where containment decides most rounds.

**Serbian excludes Republika Srpska.** Genuinely Serbian-speaking, left out
because admin-0 Bosnia would bring the Federation with it. Needs a sub-national
cut, which Natural Earth admin-1 can do.

## Rendering, done 2026-11-03

**`tools/preview.py` now supersamples.** Everything is drawn at 3x the final
size and downsampled with Lanczos, so coastlines are not staircases. It also
uses Natural Earth 10m borders, the finest free set, instead of 50m.

**The density plot no longer reads as bins.** Three changes, and each fixed a
different cause:

- A city's population used to land in one cell, making a bright square. It is
  now a Gaussian kernel whose width grows with the city, normalised over the
  land it reaches, so a coastal city still delivers all of its people onto land.
- The grid went from about 2,400 cells per entry to 7,000.
- The field is drawn at grid resolution and upscaled bicubically, then masked by
  the footprint. Smooth inside, hard edge at the border.

**The country step is deliberate and stays.** A share of speakers belongs to a
country, so smoothing across a border would bleed Cambodia into Thailand, which
is the thing the weights exist to prevent. The kernel runs inside the clip.

**Scalable to a better population source.** Natural Earth gives 7,342 towns and
one flat rural figure per country, so there is no detail below a country to
resolve and a finer grid would only interpolate the same information. A gridded
source (WorldPop, GPW, Kontur) drops in behind the same cell interface: it would
replace `rural_per_km2` and the city splat with a real per-cell population, and
`TARGET_CELLS` could then go up by an order of magnitude and show something new.

## Scoring consequences, for when weights land

**Scattered footprints barely trigger containment.** Yiddish is now small
patches on three continents and Erzya is 53 parts across the middle Volga, so
almost every pin is scored by distance rather than by being inside. That may be
correct, but check what it does to the difficulty of those entries before
shipping them.

**The antimeridian.** North American English spans −178° to 180° because of
Alaska. The reviewer unwraps it for display. Distance and containment both have
to do the same, or Anchorage reads as the far side of the planet.

**Simplification is a single tolerance.** 0.01° for everything, so Spanish is
719 KB while the median entry is 10 KB. A per-entry tolerance targeting a size
budget would even that out, and only the reveal ships geometry anyway.

## Occitan's footprint is a sliver

Found 2026-11-03 while checking weights. Asher's polygon for `occi1239` spans
lon 6.8–7.7, lat 44.1–46.0, which is the Occitan Valleys in Piedmont, not
southern France. Glottolog gives Occitan 30 dialect nodes (Gascon, Provençal,
Languedocian, Auvergnat, Limousin and the rest) and **none of them are drawn by
any Glottography dataset**, so there is no better source to fall back to.

Needs a hand-drawn footprint from French admin-1: Occitanie, Nouvelle-Aquitaine
south of the Garonne, Provence-Alpes-Côte d'Azur, and the southern edge of
Auvergne-Rhône-Alpes. Weights currently read FR 97 / IT 3, which is the right
share applied to the wrong shape.

## Polygon refinement, done 2026-11-03

**Footprints are now clipped to land.** Measured first: the median footprint is
only 0.16% sea, and just 8 of 136 are above 5%, so a general polygon-boolean
engine was not worth building. The sea is concentrated in island entries, where
Asher's smooth blob encloses the water between them: Faroese 25.7%, Somali
25.6%, Maltese 17.2%, Irish English 16.7%, Scottish Gaelic 12.9%.

Fixed in two places rather than by rewriting the geometry:

- `scoring.is_inside` requires the pin to be on land as well as in the polygon.
  The density grid already knows, since a cell exists only where the footprint
  meets a country, so a zero weight there is open water.
- Both renderers clip the field by land as well as by the footprint. In canvas
  two `clip()` calls intersect; in the preview the two masks are multiplied.

**Small footprints get a real grid.** The minimum cell went from 1.3 km to
0.2 km. Malta is 313 km2 and about 27 km across, so at the old floor it had 20
cells to its name and drew as a blur.

## The blur, fixed 2026-11-03

Two causes, found from a screenshot of Russian.

**The kernel was narrower than the grid.** Russia's cells are 25 km wide and a
small town's catchment is 10 km, which is 0.39 of a cell; even Moscow was only
1.13. A kernel that fits inside one cell cannot spread, so the city drew as a
single bright square and bicubic upscaling turned the square into a blur.
`effective_sigma_km` now takes the wider of the city's catchment and 1.5 cells,
so the field is smooth at any grid resolution.

**Outside cells were painted at the bottom of the ramp.** Both renderers
interpolate between cells, so a zero just outside the border dragged every edge
into a false gradient a cell or two wide, which reads as a halo. `extend_edges`
grows the field outward by two cells before upscaling. The clip removes the
extension, so it is sampled and never shown.

**The squares were the border, not the kernel.** Each cell was assigned wholly
to one country, so the Russia/Kazakhstan boundary drew as a staircase of 25 by
47 km blocks: measured, the weight dropped 0.345 to 0.019 across a single cell,
18 to 1. A finer grid only makes finer stairs. `country_fractions` samples each
cell 4 by 4 and splits it between the countries it straddles, so the share steps
over the real border line instead of over a row of cells. Fractions are
renormalised to sum to 1, since half a cell in the sea should not halve its
weight and would otherwise darken every coastline.

Not a bug: the pale block in south-central Russia is northern Kazakhstan at a 5%
share, correctly lighter than Russia's 92%.

**Cost.** Sub-sampling took a full rebuild from 7 to 21 seconds. Worth it, and
the lever if it ever matters is `BORDER_SUBSAMPLE`.

## Khmer does not cover eastern Cambodia

Measured 2026-11-03: the footprint covers **70.5% of Cambodia**, and 79% of what
is missing lies east of 105.5°E. Meanwhile 23.4% of the footprint is outside
Cambodia, in Isan and the Mekong Delta.

This is the atlas convention again, not a bug. Asher draws nine other languages
in eastern Cambodia: Brao, Central and Southern Mnong, Tampuan, Kuy, Chrau,
Eastern Cham, Jehai and Kaco'. Khmer's polygon stops where another vernacular
community begins, even though Khmer is the national language spoken throughout.

Same shape as Portuguese having no Angola. A gridded or official-language source
fixes it; clipping to Glottolog's country list would remove the Thai lobe but
would not add eastern Cambodia back.
