# Status

A daily audio map game. Five rounds, each one clip and one pin, scored against
a weighted footprint of where that language is spoken.

The design record lives in the Obsidian vault at
`Second Brain/03-Projects/daily-language-game/`. This file is the engineering
view: what is built, what is being built, what is next.

Last updated 2026-11-09.

---

## Branches

One branch per feature, from 2026-11-09. `sothea_mac` is the integration line
and should always be green; work lands there by merge, not by committing
straight to it.

| Branch | Holds |
|---|---|
| `sothea_mac` | integration. The pool, footprints, weights, scoring, the reviewer. |
| `clip-pipeline` | voiced seconds, splice planning, the corpus fetches and audit |
| `schedule-generator` | the draft session generator, branched off `clip-pipeline` |
| `footprint-refinements` | parked map work, see `REFINEMENTS.md` |

Start one with `git switch -c <feature>` off `sothea_mac`. Merge back when the
feature is finished and the suite is green.

`main` is stale at an early commit and is not used.

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

1. **Clip pipeline**, on branch `clip-pipeline`. It now produces audio.

   ```bash
   python3 tools/fetch_edacc.py --per-split 264
   python3 -m pipeline.clips .cache/edacc --manifest .cache/edacc/manifest.tsv \
       --build .cache/clips --per-label 4
   ```

   Measures voiced seconds, screens transcripts, picks takes from one speaker,
   trims each to the target, splices, normalises loudness to -18 LUFS, strips
   metadata, and names each clip by a hash of its contents. Provenance goes to
   `clips.tsv`, which matters because EdAcc is CC BY-SA.

   **Clips exist.** 80 across 12 entries as of 2026-11-10: 52 from EdAcc over
   9 accents, and 10 Hebrew, 10 Greek and 8 Turkish from Common Voice. One of
   the 80 holds under 8 voiced seconds, a Southeastern English clip with no
   longer take available from that speaker.

   Three things were wrong and are fixed, all found by pulling a locale that
   was not Latin script:

   **The transcript screen only knew Latin letters.** It tokenised on
   `[A-Za-z']+`, so a Hebrew sentence produced zero words and all 80 Hebrew
   takes were binned as backchannels. Turkish had passed only because Turkish
   is Latin script, and most of the 100 locales are not. Transcripts now
   tokenise on any letter in any script. Khmer, Thai, Chinese and Japanese
   write without spaces, so a sentence is one token and no token count clears
   the floor; those are recognised by one long run of letters instead.

   **`clips.tsv` recorded what the sources held, not what the clip holds.** It
   summed each take's voiced seconds before trimming, so a 13.2 second take
   cut to a 9 second budget was filed as 13.2. The undershoot tuned on
   2026-11-09 was measured against that column, which is why raising the plan
   target never closed it. Two honest numbers now: `planned_s`, the speech
   inside the trimmed spans, and `voiced_s`, read back off the encoded file.

   **The publish step loses voiced seconds, by 0.34 to 1.78 per clip.**
   loudnorm raises the gain on a quiet source, lifting the noise floor along
   with the speech, and the detector's threshold is relative to that floor.
   The loss tracks how noisy the source was, so no constant absorbs it. The
   builder encodes, measures, and retries up to two seconds wider when the
   file came up short. Hebrew went from 4 of 10 short to 0 of 10, with 4 clips
   needing the wider budget.

   **Clips are labelled by entry now.** The builder grouped by whatever string
   the corpus used, which produced clips filed as `tr` and
   `Mainstream US English`. EdAcc was worse: it tags by self-described accent
   and mixes nativised varieties with first-language backgrounds, so an
   unfiltered run built clips labelled `Italian`, `Spanish` and `Don't know`.
   `data/edacc-accents.tsv` maps the 9 nativised varieties and names every
   label it drops with its count and the reason. Common Voice reuses
   `data/cv-locales.tsv` through `--label-map-cols`, since that file already
   pairs locale with entry.

   **Still needed:** more Common Voice locales. 12 of 136 entries have audio.

   **MDC meters downloads by volume, somewhere near 1 to 3 GB a day.** Revised
   2026-11-10. On 11-09 one 3 GB locale went through and everything for the
   next hour was refused, 18 retries, none succeeding, which read as one
   locale per window. On 11-10 Hebrew (151 MB) and Greek (810 MB) both landed
   within six minutes of each other, so the limit is not a count of locales.
   Pull smallest first and expect roughly a gigabyte a day. Korean 219 MB and
   Vietnamese 485 are the next cheap ones; Spanish alone is 52 GB.

   Terms accepted: tr nl sv-SE el hi vi he ko. Downloaded: tr he el. Korean's
   terms were accepted on 2026-11-10 and its download still failed, through
   four backoffs totalling 15 minutes, with RateLimitError. Accepting terms
   and having quota are separate things, and the backoff cannot bridge the
   second: 954 MB landed that day before everything after it was refused.

   **`--check` used to report every refusal as a terms problem.** It returned
   a boolean, so anything short of success was printed under "still need their
   terms accepted", when only `PermissionError` means that. A rate limit on
   six small locales was reported as six datasets needing terms. This was the
   second time the same conflation cost time; the first was a Cloudflare 403,
   code 1010, from a hand-rolled request. Three states now, ready, terms and
   unknown, with the classification tested on all three error shapes.

2. **The schedule generator, done 2026-11-10**, on branch `schedule-generator`.

   ```
   python3 -m pipeline.schedule --days 30 --start 2026-12-01 --dry-run
   ```

   Enforces the ramp from `game-design.md`, the 110 forbidden pairs in
   `data/overlaps.tsv`, and a 21 day rest on entries and clips alike, counted
   across the whole run and not just against history. Accents carry their own
   rule, since theirs is not a geographic conflict: Irish and Scottish English
   pins sit 400 km apart and `overlaps.tsv` records nothing between them, but
   two English varieties in one session read as the same round twice. Every
   day comes out a draft with a hash over its content, for the gate to sign.

   Filling is backtracking rather than greedy, so a reported failure proves no
   legal session existed.

   **It proposes zero days today, correctly.** Ten of the 12 entries with
   audio are accents and a session holds one, so rounds 3, 4 and 5 have
   nothing else to offer. The three languages that do have audio, Greek,
   Hebrew and Turkish, are all tier 1. `why_stuck()` says this in a sentence,
   because the per-round counts on their own read as a near miss.

   **Runway is set by how many entries have audio, not by the generator.**
   Measured 2026-11-10 by giving synthetic audio to N languages per tier:

   | Languages with audio, per tier | Days of runway |
   |---|---|
   | 8 | 6 |
   | 12 | 9 |
   | 16 | 11 |
   | 21 (every tier 1 language) | 14 |
   | all 119 | 120+ |

   The window is the other half of that. With every language carrying audio a
   21 day window sustains 120 days, while 27 collapses to 25, because round 1
   then needs more tier 1 entries than exist. 21 is deliberate, not a default.

   **What unblocks it: small tier 2 and tier 3 locales.** Common Voice has
   several that are tiny. Lao 9 MB, Icelandic 16, Khmer 21 (tier 2); Ossetian
   31, Sardinian 78, Turkmen 157 (tier 3). All six is 312 MB, inside one day's
   quota, and takes the generator from 0 days to 2. Terms are not yet accepted
   on any of them.

3. **The approval gate and review page**, so no session goes live unreviewed.
4. **Name the game.** Open since the third design pass. It gates the domain.

Parked on `footprint-refinements`: the map work. Outlines, borders and the
density plot are accurate enough to build on, and the remaining items are in
`REFINEMENTS.md`.

Dropped: clipping over-extended footprints against Glottolog's country lists.
Measured 2026-11-09, it does nothing for Khmer or Thai, whose lists already
include the countries they spill into, and it would delete Hong Kong from
Cantonese and South Ossetia from Ossetian. See `REFINEMENTS.md`.

---

## Decisions waiting on you

- Whether the rules should say "pin where it is spoken natively". Both Glottolog
  and Asher record mother tongues, so Portuguese has no Angola and French has no
  African country at all. That convention needs to be stated or overridden.
- The 28 rows in `data/shares.tsv` are estimates, not a cited source. They are
  good to about ten percent. Worth a skim on the reveal map.
