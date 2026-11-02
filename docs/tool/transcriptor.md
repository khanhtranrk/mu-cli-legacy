# Transcriptor Tool

The `mu tool transcriptor` commands evaluate WhisperX transcription results against reference lyrics and convert aligned transcripts to subtitle formats (SRT, ASS, or karaoke ASS).

## Overview

WhisperX provides word-level timestamps for speech transcription. This tool aligns those timestamps with known lyrics to:

1. **Evaluate** the quality of transcription and alignment
2. **Convert** the aligned result to timed subtitle files

This is particularly useful for music videos, karaoke, and lyric synchronization projects.

## Commands

### `mu tool transcriptor evaluate`

Compare a WhisperX JSON output against reference lyrics and report alignment quality.

#### Usage

```bash
mu tool transcriptor evaluate <transcript.json> <lyrics.json> [options]
```

#### Arguments

- `transcript_path`: Path to WhisperX output JSON file (must contain `segments[]` with word-level timestamps)
- `lyrics_path`: Path to lyrics JSON file (must contain a `lyrics` string field)

#### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--output-type` | string | `pretty` | Output format: `pretty`, `plaintext`, `json`, or `yaml` |
| `--overall-min` | float [0-1] | `0.85` | Overall similarity threshold for quality gate |
| `--local-min` | float [0-1] | `0.60` | Local window coverage threshold for quality gate |
| `--window` | int ≥ 1 | `25` | Window size (in tokens) for local coverage check |
| `--step` | int ≥ 1 | `5` | Step size for sliding window analysis |
| `--line-min` | float [0-1] | `0.50` | Minimum coverage threshold per line |

#### Output Formats

- **pretty**: Colorized terminal output with per-line diff (green=match, red=missing, yellow=extra)
- **plaintext**: Same analysis without ANSI color codes
- **json**: Structured JSON object with metrics
- **yaml**: Structured YAML object with metrics

#### Quality Gate

The evaluation passes when all three conditions are met:

1. Overall similarity ≥ `--overall-min` (default 85%)
2. Worst sliding window coverage ≥ `--local-min` (default 60%)
3. No line below `--line-min` coverage (default 50%)

#### Example

```bash
# Basic evaluation with pretty output
mu tool transcriptor evaluate whisperx_output.json lyrics.json

# JSON output for programmatic use
mu tool transcriptor evaluate whisperx_output.json lyrics.json --output-type json

# Stricter thresholds
mu tool transcriptor evaluate whisperx_output.json lyrics.json \
  --overall-min 0.90 --local-min 0.70 --line-min 0.60
```

#### Transcript JSON Format

```json
{
  "segments": [
    {
      "id": 0,
      "start": 0.0,
      "end": 2.5,
      "text": "hello world",
      "words": [
        {"word": "hello", "start": 0.0, "end": 1.0},
        {"word": "world", "start": 1.0, "end": 2.5}
      ]
    }
  ]
}
```

#### Lyrics JSON Format

```json
{
  "lyrics": "hello world\nthis is a test"
}
```

**Note:** Section headers like `[Verse]` or `[Chorus]` are automatically ignored during alignment.

---

### `mu tool transcriptor convert`

Convert a WhisperX transcript and lyrics to a subtitle file with word-level timing.

#### Usage

```bash
mu tool transcriptor convert <transcript.json> <lyrics.json> [options]
```

#### Arguments

- `transcript_path`: Path to WhisperX output JSON file
- `lyrics_path`: Path to lyrics JSON file

#### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--target-type` | string | `k-ass` | Target format: `k-ass` (karaoke), `ass`, or `srt` |
| `--output` | path | auto | Output file path (default: `karaoke.ass`, `lyrics.ass`, or `lyrics.srt`) |
| `--min-timing-coverage` | float [0-1] | `0.50` | Minimum timing coverage required per line |

#### Target Formats

- **k-ass**: Karaoke ASS with per-word karaoke timing (`\k` and `\kf` tags)
- **ass**: Standard ASS subtitle without karaoke effects
- **srt**: Standard SRT subtitle format

#### Karaoke Effect Details

The `k-ass` format highlights each word as it's sung using ASS karaoke tags:

- `\k<duration>`: Simple fill effect
- `\kf<duration>`: Fade fill effect (more subtle)

For CJK languages (Chinese, Japanese), character timestamps are interpolated from WhisperX word spans since WhisperX doesn't provide character-level timing.

#### Examples

```bash
# Convert to karaoke ASS (default)
mu tool transcriptor convert whisperx_output.json lyrics.json

# Convert to standard SRT
mu tool transcriptor convert whisperx_output.json lyrics.json --target-type srt

# Convert to standard ASS
mu tool transcriptor convert whisperx_output.json lyrics.json --target-type ass

# Specify custom output path
mu tool transcriptor convert whisperx_output.json lyrics.json \
  --target-type k-ass --output my_song.ass

# Stricter timing coverage threshold
mu tool transcriptor convert whisperx_output.json lyrics.json \
  --min-timing-coverage 0.70
```

#### Output Information

The command reports:

- Output file path
- Number of lines written vs. total lines
- List of skipped lines (if any) due to low timing coverage or unaligned text

---

## Language Support

The tool automatically detects language mode:

- **Word mode**: For most languages (English, Vietnamese, etc.), tokenization splits on word boundaries
- **Character mode**: For CJK languages (Chinese, Japanese), tokenization operates on individual characters

Character mode detection checks for Unicode ranges:
- Hiragana/Katakana: `\u3040`–`\u30ff`
- CJK Unified Ideographs: `\u3400`–`\u9fff`
- CJK Compatibility Ideographs: `\uf900`–`\ufaff`

---

## Workflow

A typical workflow for music video lyric synchronization:

1. **Transcribe** audio with WhisperX to get word-level timestamps
2. **Evaluate** alignment quality against known lyrics
3. **Review** the evaluation report and adjust if needed
4. **Convert** to desired subtitle format
5. **Import** subtitle file into video editor (e.g., Premiere, DaVinci Resolve)

```bash
# Step 1: Run WhisperX (external tool)
whisperx audio.mp3 --output_format json --output_dir ./transcripts

# Step 2: Evaluate alignment
mu tool transcriptor evaluate transcripts/audio.json lyrics.json

# Step 3: Convert to karaoke
mu tool transcriptor convert transcripts/audio.json lyrics.json \
  --target-type k-ass --output karaoke.ass
```

---

## Technical Details

### Alignment Algorithm

The tool uses Python's `difflib.SequenceMatcher` to align tokens from the lyrics with tokens from the transcript:

1. Tokenize both lyrics and transcript (word or character mode based on language)
2. Compute optimal alignment using SequenceMatcher
3. Map each lyric token to the best-matching transcript token
4. Assign WhisperX word-level timestamps to aligned lyric tokens

### Timing Interpolation

When a WhisperX word contains multiple lyric tokens (e.g., syllables in karaoke), timestamps are linearly interpolated within the word span.

For CJK character mode, all characters within a WhisperX word span are assigned interpolated timestamps since WhisperX doesn't provide character-level timing.

### Coverage Thresholds

Lines are skipped during conversion if:

- No timing information is available for any tokens in the line
- Timing coverage (`timed_tokens / total_tokens`) is below `--min-timing-coverage`

---

## Troubleshooting

### "WhisperX JSON must contain segments[]"

Ensure your WhisperX output includes the `segments` array with word-level timing. Run WhisperX with word alignment enabled:

```bash
whisperx audio.mp3 --output_format json --word_align True
```

### "Song JSON must contain a lyrics string"

The lyrics JSON must have a top-level `lyrics` field containing the full lyrics as a string:

```json
{
  "lyrics": "Line 1\nLine 2\nLine 3"
}
```

### Low coverage or many skipped lines

This usually indicates:

- Poor transcription quality
- Mismatched lyrics vs. actual audio
- WhisperX failed to align words properly

Try:
- Lowering `--min-timing-coverage` (but review output manually)
- Improving audio quality or using a better Whisper model
- Checking that lyrics match the actual sung words

### CJK characters show interpolated timing warning

This is expected behavior. WhisperX provides word-level timing, not character-level. For CJK, character timestamps are mathematically interpolated from word spans. For precise character timing, you would need a character-level speech recognition model.

---

## See Also

- [WhisperX Documentation](https://github.com/m-bain/whisperX)
- [ASS Subtitle Format](https://github.com/libass/libass)
- [SRT Subtitle Format](https://en.wikipedia.org/wiki/SubRip)
