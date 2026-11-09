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
import math
import os
import shutil
import subprocess
import sys

# A source utterance below this is a word, not a sample of a language.
MIN_VOICED_S = 2.0

# What a finished round clip should hold.
TARGET_VOICED_S = 8.0

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


def plan_splice(takes, speaker, target_s=TARGET_VOICED_S, min_voiced_s=MIN_VOICED_S):
    """Choose utterances from one speaker to reach about `target_s` voiced.

    Returns an empty list rather than a short clip. A clip that falls short of
    the target is worse than no clip: the round still gets played, and the
    player gets less to go on than the format promises.
    """
    usable = [t for t in takes
              if t["speaker"] == speaker and t["voiced_s"] >= min_voiced_s]
    # Longest first, so a clip is made of as few pieces as will do. Every join
    # is a place the splice can be heard.
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


def decode(path, rate=DECODE_RATE):
    """Read any audio file as mono 16-bit samples at `rate`."""
    require_ffmpeg()
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path),
         "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
        check=True, stdout=subprocess.PIPE,
    ).stdout
    samples = array.array("h")
    samples.frombytes(out[: len(out) - (len(out) % 2)])
    return samples


def splice(paths, out_path, rate=PUBLISH_RATE, lufs=PUBLISH_LUFS):
    """Join takes end to end, normalise loudness, strip metadata."""
    require_ffmpeg()
    args = ["ffmpeg", "-v", "error", "-y"]
    for path in paths:
        args += ["-i", str(path)]
    joined = "".join(f"[{i}:a]" for i in range(len(paths)))
    args += [
        "-filter_complex",
        f"{joined}concat=n={len(paths)}:v=0:a=1[a];"
        f"[a]loudnorm=I={lufs}:TP=-1.5:LRA=11[out]",
        "-map", "[out]", "-ac", "1", "-ar", str(rate),
        "-map_metadata", "-1", str(out_path),
    ]
    subprocess.run(args, check=True)
    return out_path


def main():
    ap = argparse.ArgumentParser(
        description="Report voiced seconds for audio files, which is what the "
                    "clip filter turns on.",
    )
    ap.add_argument("paths", nargs="+", help="audio files, or directories of them")
    ap.add_argument("--min-voiced", type=float, default=MIN_VOICED_S)
    args = ap.parse_args()

    files = []
    for path in args.paths:
        if os.path.isdir(path):
            for root, _dirs, names in os.walk(path):
                files += [os.path.join(root, n) for n in sorted(names)
                          if not n.startswith(".")]
        else:
            files.append(path)

    kept = 0
    for path in files:
        try:
            samples = decode(path)
        except subprocess.CalledProcessError:
            sys.stderr.write(f"  {os.path.basename(path):<40} not audio\n")
            continue
        total = len(samples) / DECODE_RATE
        voiced = voiced_seconds(samples, DECODE_RATE)
        verdict = "ok" if voiced >= args.min_voiced else "too thin"
        kept += voiced >= args.min_voiced
        sys.stderr.write(
            f"  {os.path.basename(path):<40} {total:>6.1f}s file  "
            f"{voiced:>6.1f}s voiced  {voiced / total if total else 0:>5.0%}  {verdict}\n"
        )
    sys.stderr.write(f"\n{kept} of {len(files)} clear {args.min_voiced}s voiced\n")
    return 0 if kept else 1


if __name__ == "__main__":
    sys.exit(main())
