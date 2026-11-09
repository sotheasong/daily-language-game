# Status

A daily audio map game. Five rounds, each one clip and one pin, scored against
a weighted footprint of where that language is spoken.

The design record lives in the Obsidian vault at
`Second Brain/03-Projects/daily-language-game/`. This file is the engineering
view: what is built, what is being built, what is next.

Last updated 2026-11-03.

---

## The pipeline

Five stages, each one gating the next. Stdlib only, no pip install.

```bash
# 1. entry names -> Glottocodes, checked against the approval lock
python3 -m pipeline.glottocodes data/entries.tsv -o data/resolved.tsv \
    --local-ids data/local-ids.tsv --lock data/glottocodes.lock.tsv

# 2. which language-area polygons exist, and which need drawing
python3 -m pipeline.coverage data/resolved.tsv -o data/coverage.tsv

# 3. which entries share too much ground to sit in one session
python3 -m pipeline.overlaps data/resolved.tsv -o data/overlaps.tsv

# 4. one simplified footprint per entry
python3 -m pipeline.footprints data/resolved.tsv

# 5. a speaker-density grid per entry, for scoring and for the reveal
python3 -m pipeline.weights
```

Stages 1, 2, 4 and 5 exit non-zero when something needs a human. Stage 3 reports.

| | |
|---|---|
| Pool | 136 entries, 119 languages and 17 English accents |
| Resolution | 135 Glottocodes plus 1 local id, all locked |
| Footprints | 136 of 136, 117 from Glottography and 19 from Natural Earth |
| Density | 292,538 cells, about 2,400 per entry |
| Tests | 77, stdlib `unittest` |
| Clips | **none yet.** This is the long pole. |

### What is hand-authored

Everything here is a decision someone made, versioned on purpose.

| File | Holds |
|---|---|
| `data/entries.tsv` | the pool, with tier and kind |
| `data/local-ids.tsv` | entries Glottolog does not model, with a written reason |
| `data/glottocodes.lock.tsv` | the approved Glottocode per entry |
| `data/footprints.tsv` | territory, boundary selector and review sign-off |
| `data/shares.tsv` | share of speakers per country, 28 entries |
| `data/sources/glottography.tsv` | the 30 atlases and what each one draws |
| `data/test-pins.tsv` | worked pin placements for tuning scoring |

### Tools, which need Pillow and are not pipeline

```bash
python3 tools/preview.py Khmer Spanish -o /tmp/check.png   # render footprints
python3 tools/check_countries.py                           # footprint vs country list
python3 tools/tune_scoring.py --sweep                      # score worked pins
python3 tools/export_reviewer.py -o <dir>                  # data for the reviewer
```

Interactive reviewer for all 136: https://claude.ai/artifact/LfMjxMMtC42JmAvjV6PuQm

---

## Done

**Design, passes 1 to 8.** Settled in the vault. The game pivoted mid-design from
name-the-language to place-the-language, which dissolved equivalence sets,
granularity rules and cover terms, and closed a trilateration exploit by giving
each round a single pin.

**Resolution.** All 136 entries carry a Glottocode, or a recorded local id where
Glottolog has no node. Scottish English is the only one of those. A lock file
reports an upstream re-code as drift rather than silently repointing the join
key under every clip and polygon.

**The accent layer.** 17 English varieties, from EdAcc rather than the Common
Voice accent field, whose labels are contributor free text and whose largest
bucket merges India, Pakistan and Sri Lanka into one footprint.

**Coverage and footprints.** 117 entries have a Glottography polygon, 19 are cut
from Natural Earth. Drawing them caught a bug bounding boxes had passed: a
reconstruction atlas was winning 11 entries on alphabetical order, and Yiddish
rendered as a blob in pre-war eastern Poland.

**Weights.** A density grid per entry, shaded by share of speakers per country
rather than by raw population, because population alone shades Vietnam darkest
for Khmer.

**Session collisions.** 110 entry pairs share enough ground that a session should
not hold both.

---

## Now

**Plot refinement, done.** The density plot read as bins, from three separate
causes. A city's population landed in one cell, making a bright square; the grid
was coarse at about 2,400 cells; and the cells were drawn as flat rectangles. It
is now a Gaussian kernel per city whose width grows with its population, a
7,000-cell grid, and a field upscaled bicubically and masked by the footprint.
Smooth inside, hard edge at the border, because the country step is the point.

**Scalable to better data on purpose.** Natural Earth gives 7,342 towns and one
flat rural figure per country, so there is nothing below a country to resolve
yet. A gridded population source drops in behind the same cell interface and
`TARGET_CELLS` can then go up an order of magnitude.

**Footprint size is balanced by tier, decided 2026-11-03.** The largest
footprint is 61,645 times the smallest, and density weighting does not fix that
because it only varies *within* an entry. Tiers carry it instead: the median
footprint is 370,745 km2 at tier 1, 91,283 at tier 2 and 50,064 at tier 3, so a
harder round pays `TIER_WEIGHT` of 1.0, 1.4 and 1.6. That is the fourth root of
the area ratio, not the whole of it, because a player does not pin at random and
paying seven times for a tier 3 round would make the other four pointless.

The weights are scaled within a session so every day totals the same 25,000.
Without that, a tier 3 heavy day would be worth more than a tier 1 day and the
one number players compare would stop meaning anything.

**Done earlier: tuning the scoring curve.** `pipeline/scoring.py` is the reference
implementation and `tools/tune_scoring.py` runs 29 worked pins through the real
footprints and weights. All bands currently come out in order.

```
inside   5000 x (0.60 + 0.40 x weight)
outside  5000 x 0.60 x exp(-km / 1500)
```

Two bugs the pins found, both fixed:

- A coastal city's grid cell has its centre in the sea, so the rasteriser left
  it out and the city's population was **discarded**. Kingston's 1.47 million
  vanished from Jamaica. Cities now snap to the nearest filled cell.
- A pin inside the footprint but in an empty cell scored the bare floor. It now
  borrows the weight of a nearby cell.

One limitation worth knowing: inside a country the weight separates *urban* from
*countryside* but not *biggest city* from *second city*, because the urban boost
is capped. Kingston and Montego Bay score the same for Jamaican English.

---

## Next

3. **Clip the over-extended footprints**, Khmer and Thai first, against the
   countries Glottolog lists. Branch `footprint-refinements`.
4. **Build the clip pipeline.** Voice activity detection measuring voiced
   seconds, same-speaker splicing to about 8 seconds, loudness normalisation.
   This unlocks roughly 20 hours of listening, which is the real cost of the
   project and the only thing on this list that cannot be shortened.
5. **The approval gate and review page**, so no session goes live unreviewed.
6. **Name the game.** Open since the third design pass. It gates the domain.

---

## Decisions waiting on you

- Whether the rules should say "pin where it is spoken natively". Both Glottolog
  and Asher record mother tongues, so Portuguese has no Angola and French has no
  African country at all. That convention needs to be stated or overridden.
- The 28 rows in `data/shares.tsv` are estimates, not a cited source. They are
  good to about ten percent. Worth a skim on the reveal map.
