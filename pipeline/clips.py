#!/usr/bin/env python3
"""Pick and assemble the audio for a round.

**Voiced seconds, not file duration.** Common Voice utterances run 3 to 7
seconds against a spec of 8 to 12, and a 3 second clip can hold a single word
surrounded by room tone. Measuring the file tells you nothing about whether
there is enough speech to recognise a language from. So every decision here
turns on how much of a recording is actually someone talking.

A round's clip is built by splicing utterances from one speaker until there are
about 8 voiced seconds. Same speaker, because a change of voice mid-clip reads
as a cut and tells the player the clip is assembled. Unrelated sentences are an
advantage: more phonetic variety per ten seconds, and far less chance that the
content gives the language away.

Decoding is not here. ffmpeg does that at the edge, and everything below works
on plain integer samples, so it is testable without audio files.

Stdlib only. No pip install.
"""

import argparse
import array
import io
import math
import os
import re
import hashlib
import json
import shutil
import subprocess
import sys
import wave

from pipeline import sources

# A source utterance below this is a word, not a sample of a language.
MIN_VOICED_S = 2.0

# What a finished round clip should hold.
TARGET_VOICED_S = 8.0

# What to aim for when planning one. Measured over 67 built clips: the median
# lands at 8.2 voiced seconds against an 8.0 target, so there is no systematic
# shortfall, but 17 of 67 came in under and the shortest was 6.3. The voiced
# measurement is taken before the loudness pass and the encode, and both move it
# a little either way. Aiming a second high lifts the tail over the spec.
PLAN_VOICED_S = 9.0

FRAME_MS = 20
# Speech sits this far above the room tone. Measured against a noise floor
# rather than an absolute level, because recording volumes vary wildly.
SPEECH_OVER_NOISE = 4.0
NOISE_PERCENTILE = 0.2
# A pause between words is not the end of speech. Without this every utterance
# shatters into syllables.
HANGOVER_S = 0.35
MIN_SEGMENT_S = 0.12


def frame_energies(samples, rate, frame_ms=FRAME_MS):
    """Root mean square per frame, which is loudness over a short window."""
    size = max(1, int(rate * frame_ms / 1000))
    out = []
    for start in range(0, len(samples) - size + 1, size):
        total = 0
        for i in range(start, start + size):
            total += samples[i] * samples[i]
        out.append(math.sqrt(total / size))
    return out


def noise_floor(energies, percentile=NOISE_PERCENTILE):
    """The level the quiet parts sit at, taken as a low percentile."""
    if not energies:
        return 0.0
    ordered = sorted(energies)
    return ordered[min(len(ordered) - 1, int(len(ordered) * percentile))]


def voiced_segments(samples, rate, frame_ms=FRAME_MS,
                    over_noise=SPEECH_OVER_NOISE, hangover_s=HANGOVER_S,
                    min_segment_s=MIN_SEGMENT_S):
    """Return [(start_s, end_s)] for the stretches that are someone talking."""
    energies = frame_energies(samples, rate, frame_ms)
    if not energies:
        return []
    floor = noise_floor(energies)
    threshold = max(floor * over_noise, 1.0)
    step = frame_ms / 1000.0

    segments = []
    start = None
    for i, level in enumerate(energies):
        if level >= threshold and start is None:
            start = i
        elif level < threshold and start is not None:
            segments.append((start * step, i * step))
            start = None
    if start is not None:
        segments.append((start * step, len(energies) * step))

    merged = []
    for seg in segments:
        if merged and seg[0] - merged[-1][1] <= hangover_s:
            merged[-1] = (merged[-1][0], seg[1])
        else:
            merged.append(seg)
    return [s for s in merged if s[1] - s[0] >= min_segment_s]


def voiced_seconds(samples, rate, **kwargs):
    """How much of a recording is speech."""
    return sum(end - start for start, end in voiced_segments(samples, rate, **kwargs))


# Transcript markers that make a take unusable. The first three would break
# the game rather than merely waste a listen.
FATAL_TAGS = {
    "<FOREIGN": "code switching, another language is in the clip",
    "<OVERLAP": "two people talking at once, and the other voice is a different accent",
    "<DTMF": "phone tones over the speech",
}
NON_SPEECH = ("IGNORE_TIME_SEGMENT_IN_SCORING",)
# A real language sample needs some lexical variety. "MM HMM MM HMM" clears a
# duration floor and teaches a player nothing.
MIN_DISTINCT_WORDS = 4
# Khmer, Thai, Chinese and Japanese write without spaces, so a whole sentence
# arrives as one token and no amount of token counting will clear the floor.
# Those scripts are recognised by a single long unbroken run of letters, which
# a backchannel never produces: "MM HMM MM" is five letters at its longest.
MIN_RUN_LETTERS = 12

# Natural Earth writes its place names in Latin script, so giveaways are
# extracted with a Latin pattern.
_WORD = re.compile(r"[A-Za-z']+")
# Transcripts are not. Any letter in any script counts, with contractions kept
# whole so a giveaway cannot hide behind an apostrophe.
_TOKEN = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)*")


def screen(text, giveaways=()):
    """Return why a take is unusable, or None to keep it.

    EdAcc ships transcripts, so this costs nothing and spares the curator from
    auditioning clips that were never going to work.
    """
    raw = (text or "").strip()
    if not raw:
        return "no transcript"
    upper = raw.upper()
    for marker in NON_SPEECH:
        if marker in upper:
            return "not speech"
    for tag, reason in FATAL_TAGS.items():
        if tag in upper:
            # Name the tag, so a curator can find it in the transcript.
            return f"{tag.lstrip('<').lower()}: {reason}"

    words = [w.lower() for w in _TOKEN.findall(raw)]
    if (len(set(words)) < MIN_DISTINCT_WORDS
            and max((len(w) for w in words), default=0) < MIN_RUN_LETTERS):
        return "a backchannel, not a language sample"

    hits = sorted(set(words) & {g.lower() for g in giveaways})
    if hits:
        return f"names {', '.join(hits)}, which gives the answer away"
    return None


def load_giveaways(min_city_pop=300_000):
    """Words that would hand a player the answer, built from the game's own data.

    Entry names, country names and demonyms, and the larger cities. Derived
    rather than hand-listed, so it stays in step with the pool: add a language
    and its name is screened for from the next run.

    Small towns are left out on purpose. Nobody placing a language from the
    sound of it is helped by hearing a village named, and every extra term is
    another chance to reject a take for nothing.
    """
    terms = set()

    entries = os.path.join(sources.DATA_DIR, "entries.tsv")
    if os.path.exists(entries):
        with open(entries, encoding="utf-8") as fh:
            for line in fh:
                if line.strip() and not line.startswith("#"):
                    for word in _WORD.findall(line.split("\t")[0]):
                        if len(word) > 3:
                            terms.add(word.lower())

    geo = os.path.join(sources.CACHE_DIR, "geo")
    countries = os.path.join(geo, "ne_50m_admin_0_countries.geojson")
    if os.path.exists(countries):
        with open(countries, encoding="utf-8") as fh:
            for feature in json.load(fh)["features"]:
                props = feature["properties"]
                for key in ("NAME", "NAME_LONG", "ADMIN"):
                    for word in _WORD.findall(str(props.get(key) or "")):
                        if len(word) > 3:
                            terms.add(word.lower())

    places = os.path.join(geo, "ne_10m_populated_places_simple.geojson")
    if os.path.exists(places):
        with open(places, encoding="utf-8") as fh:
            for feature in json.load(fh)["features"]:
                props = feature["properties"]
                if (props.get("pop_max") or 0) < min_city_pop:
                    continue
                for word in _WORD.findall(str(props.get("name") or "")):
                    if len(word) > 3:
                        terms.add(word.lower())

    # Countries are not enough: Scotland and Wales are admin-1, and so is every
    # region a hand-drawn footprint is cut from.
    regions = os.path.join(geo, "ne_10m_admin_1_states_provinces.geojson")
    if os.path.exists(regions):
        with open(regions, encoding="utf-8") as fh:
            for feature in json.load(fh)["features"]:
                props = feature["properties"]
                for key in ("geonunit", "region"):
                    for word in _WORD.findall(str(props.get(key) or "")):
                        if len(word) > 3:
                            terms.add(word.lower())

    # Somewhere is named after almost every ordinary word. These are the ones
    # whose place sense is vanishingly rare next to their everyday sense, found
    # by screening the EdAcc sample and reading what it threw out: Nice cost six
    # takes for the word "nice", Hạ Long four for "long".
    #
    # The list stays short on purpose. A wrong reject costs one take out of
    # thousands; a wrong accept ships a clip that names its own answer.
    terms -= {
        "most", "south", "north", "east", "west", "central", "union", "general",
        "victoria", "springs", "george", "charlotte", "jordan", "chad", "turkey",
        "china", "nice", "long", "blue", "cook", "upon", "area", "base", "city",
        "great", "heard", "special", "reading", "beach", "english", "mobile",
        "deal", "bath", "hope", "white", "orange", "race", "same", "life",
    }
    return terms


# A little air at each end, so a clip does not start mid-breath.
PAD_S = 0.18


def voiced_span(segments, pad_s=PAD_S, duration=None):
    """The stretch to cut from a take: first speech to last, plus a little air.

    Cutting each voiced segment out separately and butting them together chops
    the rhythm out of a sentence. Keeping the span means the pauses inside stay
    where the speaker put them, and only the dead air at the ends goes.
    """
    if not segments:
        return None
    start = max(0.0, segments[0][0] - pad_s)
    end = segments[-1][1] + pad_s
    if duration is not None:
        end = min(end, duration)
    return (start, end)


def trim_to_voiced(segments, target_s, pad_s=PAD_S, duration=None):
    """Cut a take down to about `target_s` of speech.

    A take longer than the target used whole is what made the first real build
    overshoot: 51 seconds at the worst, against an 8 to 12 second spec. Walk
    the voiced stretches until the budget is spent and end there, so the clip
    stops at a natural point rather than mid-word.
    """
    if not segments:
        return None
    spent = 0.0
    end = segments[-1][1]
    for seg_start, seg_end in segments:
        length = seg_end - seg_start
        if spent + length >= target_s:
            end = seg_start + (target_s - spent)
            break
        spent += length
    start = max(0.0, segments[0][0] - pad_s)
    end = end + pad_s
    if duration is not None:
        end = min(end, duration)
    return (start, end)


def clip_id(parts, length=16):
    """Name a clip for its contents.

    A content hash, so the filename gives nothing away and changes the moment a
    source or a span does. An approval is a signature over content, and it must
    not survive someone swapping a take underneath it.
    """
    digest = hashlib.sha256()
    for source, start, end in parts:
        digest.update(f"{source}|{start:.3f}|{end:.3f}\n".encode("utf-8"))
    return digest.hexdigest()[:length]


# How much wider to plan when the encoded clip came up short. The publish loss
# ran to 1.78s on the first Hebrew batch, so two extra seconds covers it.
RETRY_MARGINS_S = (0.0, 1.0, 2.0)


def pick_best(attempts, target_s=TARGET_VOICED_S):
    """The first attempt that clears `target_s`, else the longest one."""
    if not attempts:
        return None
    for attempt in attempts:
        if attempt["voiced_s"] >= target_s:
            return attempt
    return max(attempts, key=lambda a: a["voiced_s"])


def voiced_within(segments, start, end):
    """Seconds of speech inside [start, end), counting partial segments.

    This is what a clip holds after trimming, as opposed to what its sources
    held before it. The two differ by however much the trim threw away.
    """
    return sum(max(0.0, min(e, end) - max(s, start)) for s, e in segments)


def plan_splice(takes, speaker, target_s=PLAN_VOICED_S, min_voiced_s=MIN_VOICED_S):
    """Choose utterances from one speaker to reach about `target_s` voiced.

    Returns an empty list rather than a short clip. A clip that falls short of
    the target is worse than no clip: the round still gets played, and the
    player gets less to go on than the format promises.
    """
    usable = [t for t in takes
              if t["speaker"] == speaker and t["voiced_s"] >= min_voiced_s]
    if not usable:
        return []

    # One take that fits beats a much longer one. A 20 second take and a 9
    # second take both clear an 8 second target, and taking the 20 produced a
    # 51 second clip. Only the extra beyond the target gets trimmed away, so
    # the tightest fit wastes the least.
    enough = [t for t in usable if t["voiced_s"] >= target_s]
    if enough:
        return [min(enough, key=lambda t: t["voiced_s"])]

    # Nothing is long enough alone, so join. Longest first, since every join is
    # a place the splice can be heard.
    usable.sort(key=lambda t: -t["voiced_s"])

    chosen, total = [], 0.0
    for take in usable:
        if total >= target_s:
            break
        chosen.append(take)
        total += take["voiced_s"]
    return chosen if total >= target_s else []


# ---------------------------------------------------------------------------
# The edge: decoding and encoding. Everything above this line works on samples
# and needs nothing installed.

DECODE_RATE = 16000
# What a published clip is encoded to. Identical specs for every clip, metadata
# stripped, so the file itself gives nothing away. See the vault's audio notes.
PUBLISH_RATE = 24000
PUBLISH_LUFS = -18.0


def require_ffmpeg():
    """ffmpeg is the one thing this module needs that the pipeline does not."""
    if shutil.which("ffmpeg") is None:
        raise SystemExit(
            "ffmpeg not found. It decodes the source audio and does the loudness\n"
            "pass; everything else here is stdlib. Install it with:\n"
            "    brew install ffmpeg"
        )


def decode_wav_bytes(blob):
    """Read a WAV with the stdlib, mixing any channels down to mono.

    EdAcc ships mono 32 kHz 16-bit WAV, so an audit of it needs nothing
    installed. Measuring one channel of a stereo interview would count only
    whichever speaker happened to be on the left.
    """
    with wave.open(io.BytesIO(blob)) as src:
        channels = src.getnchannels()
        width = src.getsampwidth()
        rate = src.getframerate()
        if width != 2:
            raise ValueError(f"expected 16-bit WAV, got {width * 8}-bit")
        frames = src.readframes(src.getnframes())
    samples = array.array("h")
    samples.frombytes(frames[: len(frames) - (len(frames) % (2 * channels))])
    if channels == 1:
        return samples, rate
    mixed = array.array("h", [
        sum(samples[i:i + channels]) // channels
        for i in range(0, len(samples), channels)
    ])
    return mixed, rate


def decode(path, rate=DECODE_RATE):
    """Read any audio file as mono 16-bit samples.

    WAV goes through the stdlib at its own rate; anything else needs ffmpeg.
    """
    with open(path, "rb") as fh:
        head = fh.read(12)
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        with open(path, "rb") as fh:
            return decode_wav_bytes(fh.read())
    require_ffmpeg()
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path),
         "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
        check=True, stdout=subprocess.PIPE,
    ).stdout
    samples = array.array("h")
    samples.frombytes(out[: len(out) - (len(out) % 2)])
    return samples, rate


def splice(parts, out_path, rate=PUBLISH_RATE, lufs=PUBLISH_LUFS):
    """Cut each part to its span, join them, normalise loudness, strip metadata.

    `parts` is [(path, start_s, end_s)]. Every clip is encoded to identical
    specs with metadata removed, so the file itself gives nothing away: no
    duration tell, no bitrate tell, nothing in the tags.
    """
    require_ffmpeg()
    args = ["ffmpeg", "-v", "error", "-y"]
    for path, _start, _end in parts:
        args += ["-i", str(path)]

    chain = []
    for i, (_path, start, end) in enumerate(parts):
        chain.append(
            f"[{i}:a]atrim=start={start:.3f}:end={end:.3f},"
            f"asetpts=PTS-STARTPTS,aresample={rate}[t{i}]"
        )
    joined = "".join(f"[t{i}]" for i in range(len(parts)))
    chain.append(f"{joined}concat=n={len(parts)}:v=0:a=1[c]")
    chain.append(f"[c]loudnorm=I={lufs}:TP=-1.5:LRA=11[out]")

    args += [
        "-filter_complex", ";".join(chain),
        "-map", "[out]", "-ac", "1", "-ar", str(rate),
        "-map_metadata", "-1", str(out_path),
    ]
    subprocess.run(args, check=True)
    return out_path


def stamp_provenance(rows, licence, attribution):
    """Put the corpus licence and attribution on every clip row.

    The pool mixes corpora with different obligations: EdAcc is CC BY-SA 4.0
    and must be attributed, Common Voice is CC0 and need not be. Nothing
    downstream could tell them apart while clips.tsv recorded neither.

    A blank licence raises rather than writing an empty column, because that
    is exactly how an attribution obligation goes missing.
    """
    if not licence or not licence.strip():
        raise ValueError(
            "a clip build needs --licence. One build is one corpus, and a "
            "clip with no recorded licence cannot be published safely.")
    return [dict(row, licence=licence.strip(),
                 attribution=(attribution or "").strip()) for row in rows]


def measure_published(path):
    """Voiced seconds in a finished clip, read back off the encoded file.

    Ground truth, and not the same as the plan. loudnorm raises the gain on a
    quiet source, which lifts the noise floor along with the speech, so the
    detector finds less voicing in the published clip than in the source. On
    the first Hebrew batch that cost 0.4 to 1.8 seconds per clip, enough to
    drop four of ten below spec while the plan said all ten cleared it.
    """
    samples, rate = decode(path)
    return voiced_seconds(samples, rate)


def audit(takes, target_s=PLAN_VOICED_S, min_voiced_s=MIN_VOICED_S):
    """Group takes by label and report how many finished clips each could make.

    The number that matters is not hours of audio but finished clips: a corpus
    of conversational turns is mostly too short to use one take at a time.
    """
    import collections
    import statistics

    grouped = collections.defaultdict(list)
    for take in takes:
        grouped[take.get("label") or ""].append(take)

    out = []
    for label, items in grouped.items():
        usable = [t for t in items if t["voiced_s"] >= min_voiced_s]
        made = 0
        for speaker in {t["speaker"] for t in usable}:
            remaining = [t for t in usable if t["speaker"] == speaker]
            while True:
                plan = plan_splice(remaining, speaker, target_s, min_voiced_s)
                if not plan:
                    break
                made += 1
                used = {t["id"] for t in plan}
                remaining = [t for t in remaining if t["id"] not in used]
        out.append({
            "label": label, "takes": len(items),
            "speakers": len({t["speaker"] for t in items}),
            "median_voiced_s": statistics.median([t["voiced_s"] for t in items]),
            "usable": len(usable), "clips": made,
        })
    return sorted(out, key=lambda r: -r["takes"])


def map_labels(takes, mapping):
    """Rename each take's label to its entry, dropping labels not in the map.

    An empty map passes everything through, for corpora whose labels already
    are entry names. Dropping is the point: a corpus label with no entry has
    nowhere to be played, and passing it through quietly built clips for
    varieties the design had excluded.
    """
    if not mapping:
        return list(takes)
    return [dict(t, label=mapping[t["label"]])
            for t in takes if t["label"] in mapping]


def read_label_map(path, from_col=0, to_col=1):
    """Read a corpus-label-to-entry map out of two columns of a TSV.

    Columns are taken by position because the two maps are laid out
    differently: data/edacc-accents.tsv is written for this (label, entry),
    while data/cv-locales.tsv is the pull list and has the entry first with
    the locale third. Reading it in place beats keeping a second copy.
    """
    out = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            if max(from_col, to_col) >= len(cols):
                continue
            key, value = cols[from_col].strip(), cols[to_col].strip()
            if key and value:
                out[key] = value
    return out


def read_manifest(path, label_field):
    import csv
    with open(path, encoding="utf-8") as fh:
        return [dict(row, label=row.get(label_field, "")) for row in
                csv.DictReader(fh, delimiter="\t")]


def survey(root, manifest, label_field, giveaways):
    """Measure and screen every take named in a manifest."""
    rows = read_manifest(manifest, label_field)
    takes = []
    for row in rows:
        path = os.path.join(root, row["file"])
        if not os.path.exists(path):
            continue
        try:
            samples, rate = decode(path)
        except (subprocess.CalledProcessError, ValueError, EOFError):
            continue
        segments = voiced_segments(samples, rate)
        takes.append({
            "id": row["file"], "path": path, "speaker": row.get("speaker") or row["file"],
            "label": row.get("label", ""), "duration": len(samples) / rate,
            "segments": segments,
            "voiced_s": sum(e - s for s, e in segments),
            "rejected": screen(row.get("text", ""), giveaways),
        })
    return takes


def build_clips(takes, out_dir, target_s=PLAN_VOICED_S, min_voiced_s=MIN_VOICED_S,
                per_label=None):
    """Make finished clips, longest-first within a speaker, and record provenance."""
    import collections

    usable = [t for t in takes if not t["rejected"] and t["voiced_s"] >= min_voiced_s]
    by_label = collections.defaultdict(list)
    for take in usable:
        by_label[take["label"]].append(take)

    os.makedirs(out_dir, exist_ok=True)
    built = []
    for label, items in sorted(by_label.items()):
        # Round robin across speakers rather than draining one before starting
        # the next. Four clips for an entry should be four voices where the
        # corpus allows it: taking them in speaker order gave two speakers and
        # two clips each, which is a voice a regular player could learn.
        pool = {speaker: [t for t in items if t["speaker"] == speaker]
                for speaker in sorted({t["speaker"] for t in items})}
        made = 0
        while per_label is None or made < per_label:
            progressed = False
            for speaker in sorted(pool):
                if per_label is not None and made >= per_label:
                    break
                # Plan, encode, measure the encoded file, and widen if it came
                # up short. The publish loss varies with how noisy the source
                # was, so it cannot be absorbed into one constant.
                attempts, seen = [], set()
                for margin in RETRY_MARGINS_S:
                    plan = plan_splice(pool[speaker], speaker,
                                       target_s + margin, min_voiced_s)
                    if not plan:
                        continue
                    parts, budget, planned = [], target_s + margin, 0.0
                    for take in plan:
                        span = trim_to_voiced(take["segments"], budget,
                                              duration=take["duration"])
                        if span:
                            parts.append((take["path"], span[0], span[1]))
                            planned += voiced_within(
                                take["segments"], span[0], span[1])
                            budget -= take["voiced_s"]
                    if not parts:
                        continue
                    name = clip_id(parts)
                    if name in seen:
                        # A wider budget picked the same takes and the same
                        # spans, so re-encoding would give the same file.
                        continue
                    seen.add(name)
                    path = os.path.join(out_dir, f"{name}.opus")
                    splice(parts, path)
                    attempts.append({
                        "clip": name, "label": label, "speaker": speaker,
                        "parts": len(parts),
                        "voiced_s": round(measure_published(path), 2),
                        "planned_s": round(planned, 2),
                        "sources": " ".join(t["id"] for t in plan),
                        "_used": {t["id"] for t in plan},
                    })
                    if attempts[-1]["voiced_s"] >= TARGET_VOICED_S:
                        break
                keeper = pick_best(attempts)
                if keeper is None:
                    continue
                for attempt in attempts:
                    if attempt is not keeper:
                        os.remove(os.path.join(out_dir, f"{attempt['clip']}.opus"))
                used = keeper.pop("_used")
                built.append(keeper)
                made += 1
                progressed = True
                pool[speaker] = [t for t in pool[speaker] if t["id"] not in used]
            if not progressed:
                break
    return built


def main():
    ap = argparse.ArgumentParser(
        description="Measure, screen and assemble clips from a corpus.")
    ap.add_argument("root", help="directory of audio")
    ap.add_argument("--manifest", required=True,
                    help="TSV with file, speaker, text and a label column")
    ap.add_argument("--label", default="accent", help="manifest column to group by")
    ap.add_argument("--label-map", metavar="TSV",
                    help="map corpus labels to entry names, dropping unmapped "
                         "ones (e.g. data/edacc-accents.tsv)")
    ap.add_argument("--label-map-cols", default="0,1", metavar="FROM,TO",
                    help="which columns of --label-map hold the corpus label "
                         "and the entry name (default 0,1; use 2,0 for "
                         "data/cv-locales.tsv)")
    ap.add_argument("--build", metavar="DIR",
                    help="assemble clips into DIR instead of only reporting")
    ap.add_argument("--per-label", type=int, default=None,
                    help="stop after this many clips per label")
    ap.add_argument("--min-voiced", type=float, default=MIN_VOICED_S)
    ap.add_argument("--target", type=float, default=PLAN_VOICED_S,
                    help="voiced seconds to plan for; a little above what a clip must hold")
    ap.add_argument("--licence", default=None,
                    help="the corpus licence, recorded on every clip "
                         "(e.g. 'CC BY-SA 4.0'). Required with --build.")
    ap.add_argument("--attribution", default="",
                    help="who to credit, for licences that require it")
    ap.add_argument("--no-screen", action="store_true",
                    help="skip the transcript screen")
    args = ap.parse_args()

    # Checked before any decoding, because finding out after the splices have
    # run wastes the whole build.
    if args.build and not (args.licence or "").strip():
        sys.stderr.write(
            "--build needs --licence, recorded on every clip. One build is one\n"
            "corpus, and the pool mixes licences with different obligations:\n"
            "    EdAcc        --licence 'CC BY-SA 4.0' --attribution '...'\n"
            "    Common Voice --licence 'CC0 1.0'\n")
        return 2

    giveaways = () if args.no_screen else load_giveaways()
    takes = survey(args.root, args.manifest, args.label, giveaways)
    if not takes:
        sys.stderr.write("no audio read\n")
        return 1

    if args.label_map:
        from_col, to_col = (int(c) for c in args.label_map_cols.split(","))
        mapping = read_label_map(args.label_map, from_col, to_col)
        before = len(takes)
        takes = map_labels(takes, mapping)
        sys.stderr.write(
            f"{before - len(takes)} of {before} takes dropped, their label is "
            f"not an entry in {args.label_map}\n")
        if not takes:
            sys.stderr.write("no takes left after mapping\n")
            return 1

    duration = sum(t["duration"] for t in takes)
    voiced = sum(t["voiced_s"] for t in takes)
    screened = sum(1 for t in takes if t["rejected"])
    sys.stderr.write(
        f"\n{len(takes)} takes, {len({t['speaker'] for t in takes})} speakers, "
        f"{duration / 60:.0f} min audio, {voiced / 60:.0f} min voiced "
        f"({voiced / duration:.0%}), {screened} screened out\n\n"
    )

    rows = audit([t for t in takes if not t["rejected"]], args.target, args.min_voiced)
    sys.stderr.write(
        f"  {'label':<30}{'n':>4}{'spk':>5}{'med voiced':>12}{'usable':>8}{'clips':>7}\n")
    for row in rows:
        sys.stderr.write(
            f"  {row['label'][:29]:<30}{row['takes']:>4}{row['speakers']:>5}"
            f"{row['median_voiced_s']:>11.1f}s{row['usable'] / row['takes']:>8.0%}"
            f"{row['clips']:>7}\n")
    sys.stderr.write(f"\n{sum(r['clips'] for r in rows)} clips available at "
                     f"{args.target:.0f} voiced seconds\n")

    if args.build:
        import csv
        built = build_clips(takes, args.build, args.target, args.min_voiced,
                            args.per_label)
        record = os.path.join(args.build, "clips.tsv")
        built = stamp_provenance(built, args.licence, args.attribution)
        fields = ["clip", "label", "speaker", "parts", "voiced_s", "planned_s",
                  "licence", "attribution", "sources"]
        # Merge rather than overwrite. One run covers one corpus, and rewriting
        # the file left the clips from every earlier run sitting in the
        # directory with no provenance. Clip ids are content hashes, so a
        # rebuilt clip replaces its own row and nothing else.
        rows = {}
        if os.path.exists(record):
            with open(record, encoding="utf-8") as fh:
                for row in csv.DictReader(fh, delimiter="\t"):
                    rows[row["clip"]] = row
        for row in built:
            rows[row["clip"]] = row
        with open(record, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields, delimiter="\t",
                                    lineterminator="\n", extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows[k] for k in sorted(rows))
        short = [r for r in built if float(r["voiced_s"]) < TARGET_VOICED_S]
        sys.stderr.write(f"\nbuilt {len(built)} clips into {args.build}\n")
        sys.stderr.write(
            f"{len(short)} of {len(built)} hold under {TARGET_VOICED_S}s voiced "
            f"once encoded\n")
        sys.stderr.write(f"provenance in {record} ({len(rows)} clips)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
