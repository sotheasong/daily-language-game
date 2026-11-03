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
