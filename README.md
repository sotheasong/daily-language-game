# daily-language-game

Pipeline and game code. The design record lives in the Obsidian vault at
`Second Brain/03-Projects/daily-language-game/`, not here.

## Glottocode resolver

Maps entry names to Glottocodes against the [Glottolog CLDF](https://github.com/glottolog/glottolog-cldf)
language table. Downloads one 2.4 MB CSV on first run and caches it.

```bash
python3 -m pipeline.glottocodes data/entries.tsv -o data/resolved.tsv
```

Exits non-zero when any entry needs a human, so it can gate a build.

| Status | Meaning |
|---|---|
| `ok` | Unique exact match |
| `ambiguous` | Several exact matches, pick one by hand |
| `alias_broken` | Our alias points at a name the table does not have |
| `unresolved` | No match, with fuzzy candidates listed |

**It never auto-accepts a fuzzy match.** On the first real run, fuzzy matching
offered Greek to Creek and Sakha to Yaaku, two wrong languages on two wrong
continents. Candidates are reported for a human to choose from, never applied.

## Tests

Stdlib `unittest`, no dependencies.

```bash
python3 -m unittest discover tests -v
```

Tested seams: the `resolve()` matching contract, and the `main()` exit-code gate.
Not tested: the contents of Glottolog itself, which is upstream's data rather
than this code's behaviour.
