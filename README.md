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
    --lock data/glottocodes.lock.tsv

# 2. Glottocodes -> which language-area polygons exist, and which do not
python3 -m pipeline.coverage data/resolved.tsv -o data/coverage.tsv
```

Both exit non-zero when anything needs a human, so either can gate a build.

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

Current state, 120 entries: **117 direct, 3 none**. The three are Galician,
Asturian and Aragonese, absent from every registered atlas because Asher's world
atlas folds the Iberian minority languages into Spanish and Portuguese. Each maps
onto a Spanish autonomous community, so admin boundaries will fill them. The gate
stays red until they are settled, which is the point.

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
No module hardcodes 120, the tier is a free-text column, and an entry absent from
the lock resolves normally and gets locked at the next sign-off. Accent varieties
reuse their base language's Glottocode with a suffix, so they cost no new
resolution work.

**Every join is a Glottocode.** Glottolog, Glottography, WALS and Grambank all key
on it, which is why the resolver gates the whole pipeline and why the lock exists.
A dataset that cannot be keyed to a Glottocode needs a mapping table of its own
before it earns a registry row.

## Tests

Stdlib `unittest`, no dependencies.

```bash
python3 -m unittest discover tests -v
```

Tested seams: the `resolve()` matching contract, the drift check, the footprint
quality check, and each module's exit-code gate. Not tested: the contents of
Glottolog or Glottography, which is upstream data rather than this code's
behaviour, and which changes whenever a dataset is added.
