# daily-language-game

Pipeline and game code. The design record lives in the Obsidian vault at
`Second Brain/03-Projects/daily-language-game/`, not here.

The game plays in rounds. Each round plays a clip and the player drops one pin on
a map, scored by distance against a weighted multipolygon of where the language is
spoken. Every entry therefore needs two things the pipeline has to produce and a
human has to approve: a Glottocode, and a footprint.

## Pipeline

```bash
# 1. entry names -> Glottocodes, checked against the approval lock
python3 -m pipeline.glottocodes data/entries.tsv -o data/resolved.tsv \
    --local-ids data/local-ids.tsv --lock data/glottocodes.lock.tsv

# 2. Glottocodes -> which language-area polygons exist, and which do not
python3 -m pipeline.coverage data/resolved.tsv -o data/coverage.tsv

# 3. which entries share too much ground to sit in one session
python3 -m pipeline.overlaps data/resolved.tsv -o data/overlaps.tsv

# 4. one simplified footprint per entry, Glottography plus Natural Earth
python3 -m pipeline.footprints data/resolved.tsv
```

Stages 1 and 2 exit non-zero when anything needs a human, so either can gate a
build. Stage 3 is a report, because overlap is scheduled around rather than
fixed.

The pool is **136 entries**, 119 languages and 17 English accents.

### 1. Glottocode resolver

Matches entry names against the [Glottolog CLDF](https://github.com/glottolog/glottolog-cldf)
language table, one 2.4 MB CSV cached on first run.

| Status | Meaning |
|---|---|
| `ok` | Unique exact match |
| `ambiguous` | Several exact matches, pick one by hand |
| `alias_broken` | Our alias points at a name the table does not have |
| `unresolved` | No match, with fuzzy candidates listed |
| `drifted` | Resolves to a different code than the lock approved |
| `local` | No Glottolog node exists, using a recorded local id |

**`local` is the escape hatch for varieties Glottolog does not model.** Scottish
English is the only one so far. Glottolog has Scots as its own language and Ulster
English as a dialect, but no node for Scottish Standard English, and borrowing
Scots' code would file Glaswegian-accented English as a different language.
Instead `data/local-ids.tsv` records an id of the form `stan1293-scotland` with a
written reason. That counts as approved, since a human wrote the row, so it does
not hold the build shut.

**It never auto-accepts a fuzzy match.** On the first real run, fuzzy matching
offered Greek to Creek and Sakha to Yaaku, two wrong languages on two wrong
continents. Candidates are reported for a human to choose from, never applied.

**The lock is the approval record.** `data/glottocodes.lock.tsv` holds the
Glottocodes a human has checked. Once approved, a code is the join key under every
clip, polygon and puzzle, so an upstream re-code is reported as `drifted` rather
than adopted. After reviewing a change, sign it off:

```bash
python3 -m pipeline.glottocodes data/entries.tsv -o data/resolved.tsv \
    --lock data/glottocodes.lock.tsv --write-lock
```

Entries not yet in the lock pass untouched, so the pool can grow without tripping
the gate.

### 2. Area coverage

Answers, per entry, whether a language-area polygon exists and where, reading only
the small `languages.csv` index from each [Glottography](https://github.com/Glottography)
dataset. That is about 2.4 MB against roughly 400 MB of geojson, so coverage is
settled before any polygon is downloaded.

| Coverage | Meaning |
|---|---|
| `direct` | A polygon is drawn for this exact Glottocode |
| `via_dialects` | Drawn only as its dialects, union them |
| `via_parent` | Drawn on the parent language, a wider area |
| `none` | No polygon anywhere, needs an admin boundary or removal |

A separate `country_shape_only` column flags footprints resting entirely on
`wikipedia2024officiallang`, which draws national outlines rather than speech
communities. Right for a politically defined standard, wrong for most entries.

Two more columns flag footprints that exist but should not be trusted.
`country_shape_only` marks footprints resting entirely on
`wikipedia2024officiallang`, which draws national outlines rather than speech
communities. `inherited_point` marks entries whose Glottolog coordinate is just
their parent's: all 114 English dialect nodes carry English's own point, 53.0 by
-1.0, which puts Jamaican English's fallback centre in Derbyshire.

**An accent is never allowed to inherit its base language's area.** Glottography
draws English, so without that rule 15 of the 16 accents would report as covered
while scoring a London pin as perfect for Jamaican English. An accent exists
because its footprint is not the base language's.

Current state, 136 entries: **117 direct, 19 none**. The 19 break down as three
Iberian languages (Galician, Asturian, Aragonese, which Asher's world atlas folds
into Spanish and Portuguese) and 16 accents. Irish English is the one accent with
a drawn area, from the Britain and Ireland atlas. Every one of the 19 has a row in
`data/footprints.tsv` giving its territory and which boundary set to cut it from.
The gate stays red until the polygons exist, which is the point.

### 3. Session collisions

Two entries whose footprints coincide ask the player to pin the same place twice.
That is a scheduling constraint, not a reason to drop an entry. Hindi and Indian
English both belong in the pool, they just must not come up on the same day.

Territory comes from Glottolog's country list, overridden by `data/footprints.tsv`
where Glottolog is empty or too coarse. The score is the share of the two
territory lists that coincides, so entries covering identical ground score 1.0
while a small language inside a much larger range scores low, because the correct
pins still differ. Subdivisions are handled properly: `GB` contains `GB-SCT`, but
`GB-SCT` and `GB-WLS` do not touch.

This is a first pass at country granularity, which is all there is before
polygons are drawn. It cannot tell Madrid from Barcelona. Replace the score with
real polygon intersection once footprints exist.

**15 of the 17 accents collide with a language of the same country**, which
is unavoidable: Indian English and Telugu are both India. Eight accents collide
with nothing at all: Southeastern, Jamaican, Australian, Singlish, New Zealand,
Kenyan, Hong Kong and North American English.

### 4. Footprints

Assembles `data/footprints.geojson`, one feature per entry. **136 of 136 have
geometry**, 5.1 MB total, median 10 KB per entry, simplified at build time with
Douglas-Peucker so the Worker never simplifies per request.

| Source | Entries |
|---|---|
| asher2007world/contemporary | 99 |
| Natural Earth admin-0 | 13 |
| carling2025diacl | 11 |
| Natural Earth admin-1 | 6 |
| others | 7 |

**Contemporary beats traditional, and it was 5 decisions rather than 99.**
Measured 2026-11-02: the two asher layers are identical for 94 of the 99 entries
it draws. The five that differ are all colonial spread, and contemporary is right
for all five.

| Entry | Contemporary | Traditional |
|---|---|---|
| Portuguese | 7.7M km² | 97k km² |
| Spanish | 8.0M km² | 503k km² |
| Danish | 246k km² | 43k km² |
| French | 2.0M km² | 574k km² |
| Dutch | 156k km² | 50k km² |

Traditional Portuguese is Portugal alone, which marks a Brazil pin wrong when 200
of 260 million speakers are Brazilian.

**A dataset splits its polygons by languoid level**, and its `languages.csv` says
which file holds each entry in `Speaker_Area`. Serbian, Croatian and Irish
English are drawn as dialects, so loading `languages.geojson` alone left them
silently without geometry. Families are skipped on purpose, since Indo-European
is not a footprint anyone can pin at.

**Natural Earth admin-1 is province level**, so there is no `GB-SCT` or `ES-GA`
feature to look up. Scotland is 32 council areas sharing `gu_a3=SCT` and Galicia
is 4 provinces sharing `region=Galicia`. `data/footprints.tsv` therefore carries
a selector expression per entry rather than a subdivision code.

**Not done: weights.** Every part currently counts the same, so the density term
in the scoring formula has nothing to read. Spanish has 275 parts and pinning
rural Patagonia scores like pinning Mexico City.

## The accent layer

17 English varieties, added 2026-11-01. One of them, Southeastern English
[sout3285], replaced the bare "English" entry: once accents became separate
entries, bare English covered 34 territories and could not be scored. Glottolog models most English accents as
dialect nodes with their own Glottocodes, so they resolve through the same path as
a language and need no suffix mechanism.

Only nativised varieties are included. A learner's accent, say German-accented or
Spanish-accented English, is excluded on purpose: guessing the speaker's first
language duplicates the language pool exactly and has no footprint of its own.

| Tier | Entries |
|---|---|
| 1 | Southeastern, Irish, Scottish, Indian, Jamaican, Australian |
| 2 | Nigerian, Southern African, Philippine, Singlish, New Zealand, Welsh |
| 3 | Kenyan, Ghanaian, Hong Kong, Malaysian, North American |

North American English sits in tier 3 despite being the most recognisable of the
lot, because tier is geographic distinctiveness and its footprint is a continent.

Audio comes from [EdAcc](https://datashare.ed.ac.uk/handle/10283/8983), the
Edinburgh International Accents of English Corpus, CC BY-SA 4.0, 40 hours of
spontaneous dyadic conversation at 32 kHz with a linguist-standardised accent
label per speaker. Spontaneous conversation suits this game better than
Common Voice's read prompts, and the transcripts ship with it, so clips that name
their own location can be screened out automatically.

## Scaling this up

The pipeline was built for 120 entries and 2 datasets, and is meant to grow past
both. Three rules keep that cheap.

**Adding a dataset is adding a row.** `data/sources/*.tsv` holds the registry.
Nothing in the code names a dataset, and `pipeline/sources.py` owns the fetch,
the cache and the size guard, so a new source reuses all of it. The `kind` column
carries what the dataset actually draws, which is how `country-shape` data is kept
from passing as a speech area. Next ones expected through this door: WALS,
Grambank, and the Common Voice clip manifests.

**Adding languages is adding a line.** `data/entries.tsv` is read, never counted.
No module hardcodes a pool size, tier and kind are free-text columns, and an entry
absent from the lock resolves normally and gets locked at the next sign-off. This
was tested rather than assumed: adding the 16 accents took the pool from 120 to
136 with zero drift reported on the original 120.

**Every join is a Glottocode.** Glottolog, Glottography, WALS and Grambank all key
on it, which is why the resolver gates the whole pipeline and why the lock exists.
A dataset that cannot be keyed to a Glottocode needs a mapping table of its own
before it earns a registry row.

## Tests

Stdlib `unittest`, no dependencies.

```bash
python3 -m unittest discover tests -v
```

Tested seams: the `resolve()` matching contract, the drift and local-id checks,
the footprint quality and inherited-coordinate checks, the territory comparison
and collision scoring, and each module's exit-code gate. Not tested: the contents of
Glottolog or Glottography, which is upstream data rather than this code's
behaviour, and which changes whenever a dataset is added.
