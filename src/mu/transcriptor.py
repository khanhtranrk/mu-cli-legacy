"""Transcriptor tool for evaluating and converting WhisperX transcripts to lyrics."""
import json
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

WORD_RE = re.compile(r"[^\W_]+(?:[''][^\W_]+)*", re.UNICODE)
SECTION_RE = re.compile(r'^\s*\[[^\]]*\]\s*$')
ASS_FORMAT = 'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text'


def is_character_language(text):
    """Use character comparison for Japanese and Chinese, not Hangul-only Korean."""
    return any(('\u3040' <= c <= '\u30ff') or ('\u3400' <= c <= '\u9fff')
               or ('\uf900' <= c <= '\ufaff') for c in text)


def normalize(text):
    return unicodedata.normalize('NFKC', str(text)).casefold()


def tokenize(text, char_mode):
    normalized = normalize(text)
    if char_mode:
        return [c for c in normalized if c.isalnum() or '\u3040' <= c <= '\u30ff']
    return WORD_RE.findall(normalized)


def lyric_lines(song):
    if not isinstance(song.get('lyrics'), str):
        raise ValueError('Song JSON must contain a lyrics string')
    return [line.strip() for line in song['lyrics'].splitlines()
            if line.strip() and not SECTION_RE.fullmatch(line)]


def transcript_segments(transcript):
    segments = transcript.get('segments')
    if not isinstance(segments, list):
        raise ValueError('WhisperX JSON must contain segments[]')
    return segments


def compare_segments(segments, char_mode):
    warnings = 0
    for idx, segment in enumerate(segments, 1):
        text = tokenize(segment.get('text', ''), char_mode)
        words = [part for w in segment.get('words', []) for part in tokenize(w.get('word', ''), char_mode)]
        if text != words:
            warnings += 1
            print(f"⚠ Segment {idx}: text/words differ ({len(text)} vs {len(words)} tokens)")
    print(f'Segments: {len(segments)}, content warnings: {warnings}')
    return warnings


@dataclass
class Alignment:
    lines: list
    line_tokens: list
    expected: list
    actual: list
    char_mode: bool
    opcodes: list
    matches: dict
    heard: dict
    insertions: dict
    timed: list
    timing: dict


def make_timed_tokens(segments, char_mode):
    result = []
    for segment in segments:
        for word in segment.get('words', []):
            start, end = word.get('start'), word.get('end')
            if start is None or end is None:
                continue
            try:
                start, end = float(start), float(end)
            except (ValueError, TypeError):
                continue
            if end <= start:
                continue
            parts = tokenize(word.get('word', ''), char_mode)
            for i, part in enumerate(parts):
                a = start + (end - start) * i / len(parts)
                b = start + (end - start) * (i + 1) / len(parts)
                result.append((part, a, b))
    return result


def build_alignment(transcript, song):
    lines = lyric_lines(song)
    char_mode = is_character_language('\n'.join(lines))
    line_tokens = [tokenize(line, char_mode) for line in lines]
    expected = [token for row in line_tokens for token in row]
    segments = transcript_segments(transcript)
    actual = [token for seg in segments for token in tokenize(seg.get('text', ''), char_mode)]
    matcher = SequenceMatcher(None, expected, actual, autojunk=False)
    opcodes = matcher.get_opcodes()
    matches, heard, insertions = {}, {}, {}
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == 'equal':
            for i, j in zip(range(i1, i2), range(j1, j2)):
                matches[i] = j
                heard[i] = [actual[j]]
        elif tag == 'insert':
            insertions.setdefault(i1, []).extend(actual[j1:j2])
        elif tag == 'replace':
            for offset, j in enumerate(range(j1, j2)):
                i = i1 + offset
                if i < i2:
                    heard.setdefault(i, []).append(actual[j])
                else:
                    insertions.setdefault(i2, []).append(actual[j])
    timed = make_timed_tokens(segments, char_mode)
    timed_matcher = SequenceMatcher(None, expected, [t[0] for t in timed], autojunk=False)
    timing = {}
    for tag, i1, i2, j1, j2 in timed_matcher.get_opcodes():
        if tag == 'equal':
            for i, j in zip(range(i1, i2), range(j1, j2)):
                timing[i] = timed[j][1:]
    return Alignment(lines, line_tokens, expected, actual, char_mode,
                     opcodes, matches, heard, insertions, timed, timing)


def line_ranges(alignment):
    offset = 0
    for n, (line, row) in enumerate(zip(alignment.lines, alignment.line_tokens), 1):
        yield n, line, row, offset, offset + len(row)
        offset += len(row)


def check(alignment, color=True, overall_min=0.85, local_min=0.60, window=25, step=5, line_min=0.50):
    green, red, yellow, reset = ('\033[32m', '\033[31m', '\033[33m', '\033[0m') if color else ('', '', '', '')
    unit = 'characters' if alignment.char_mode else 'words'
    print(f'\nLyrics: {len(alignment.lines)} lines, {len(alignment.expected)} {unit} | '
          f'Transcript: {len(alignment.actual)} {unit}')
    print('Legend: green=match, red=missing/different, yellow=extra\n')
    line_stats = []
    for number, line, row, start, end in line_ranges(alignment):
        line_matched = sum(i in alignment.matches for i in range(start, end))
        line_coverage = line_matched / len(row) if row else 1.0
        line_stats.append((number, line_matched, len(row), line_coverage))
        lyric_parts, heard_parts = [], []
        for i in range(start, end):
            equal = i in alignment.matches
            lyric_parts.append((green if equal else red) + alignment.expected[i] + reset)
            words = alignment.heard.get(i, [])
            heard_parts.extend((green if equal else red) + w + reset for w in words)
            if not words:
                heard_parts.append(red + '∅' + reset)
            heard_parts.extend(yellow + w + reset for w in alignment.insertions.get(i + 1, []))
        if start == 0:
            heard_parts[:0] = [yellow + w + reset for w in alignment.insertions.get(0, [])]
        joiner = '' if alignment.char_mode else ' '
        print(f'{number:02d} Lyrics:     {joiner.join(lyric_parts)}  [{line_coverage:.1%} | {line_matched}/{len(row)}]')
        print(f'   Transcript: {joiner.join(heard_parts)}\n')
    matched = len(alignment.matches)
    expected_len, actual_len = len(alignment.expected), len(alignment.actual)
    similarity = (2 * matched / (expected_len + actual_len)) if expected_len + actual_len else 1.0
    coverage = matched / expected_len if expected_len else 1.0
    missing = expected_len - matched
    extra = actual_len - matched
    windows = []
    if expected_len:
        size = min(window, expected_len)
        starts = sorted(set(range(0, expected_len - size + 1, step)) | {expected_len - size})
        windows = [(s, s + size, sum(i in alignment.matches for i in range(s, s + size)) / size)
                   for s in starts]
    worst = min((score for _, _, score in windows), default=1.0)
    bad_lines = [(n, m, total, score) for n, m, total, score in line_stats if score < line_min]
    passed = similarity >= overall_min and worst >= local_min and not bad_lines
    print('=== SUMMARY ===')
    print(f'Matched: {matched}/{expected_len} ({coverage:.1%})')
    print(f'Similarity: {similarity:.1%}')
    print(f'Missing/different lyrics {unit}: {missing}')
    print(f'Extra/different transcript {unit}: {extra}')
    print(f'Exact match: {missing == 0 and extra == 0}')
    print(f'Lines below {line_min:.0%}: {len(bad_lines)}/{len(line_stats)}')
    if bad_lines:
        print('Manual review lines: ' + ', '.join(f'{n} ({score:.1%}, {m}/{total})' for n, m, total, score in bad_lines))
    print(f'Worst {min(window, expected_len)}-{unit} window coverage: {worst:.1%}')
    print(f'Quality gate: {"PASS" if passed else "FAIL"} '
          f'(similarity >= {overall_min:.0%}; local coverage >= {local_min:.0%}; '
          f'no line below {line_min:.0%})')
    if windows and worst < local_min:
        worst_windows = [(s, e) for s, e, score in windows if score == worst]
        print(f'Worst window token offsets (0-based): {worst_windows[:5]}')
    
    return {
        'matched': matched,
        'expected_len': expected_len,
        'actual_len': actual_len,
        'similarity': similarity,
        'coverage': coverage,
        'missing': missing,
        'extra': extra,
        'exact_match': missing == 0 and extra == 0,
        'bad_lines': len(bad_lines),
        'total_lines': len(line_stats),
        'worst_window': worst,
        'passed': passed,
        'unit': unit,
    }


def subtitle_blocks(alignment, min_coverage=0.5):
    blocks, skipped = [], []
    for number, line, row, start_idx, end_idx in line_ranges(alignment):
        entries = [(i, alignment.timing[i]) for i in range(start_idx, end_idx) if i in alignment.timing]
        coverage = len(entries) / len(row) if row else 0
        if not entries or coverage < min_coverage:
            skipped.append(number)
            continue
        times = [t for _, t in entries]
        start, end = min(t[0] for t in times), max(t[1] for t in times)
        if end <= start:
            skipped.append(number)
            continue
        blocks.append((number, start, end, line, entries, start_idx))
    result = []
    for index, block in enumerate(blocks):
        number, start, end, line, entries, offset = block
        if index + 1 < len(blocks):
            end = min(end, blocks[index + 1][1])
        if end <= start:
            skipped.append(number)
            continue
        result.append((number, start, end, line, entries, offset))
    return result, sorted(set(skipped))


def srt_time(seconds):
    ms = max(0, round(seconds * 1000))
    hours, ms = divmod(ms, 3600000)
    minutes, ms = divmod(ms, 60000)
    sec, ms = divmod(ms, 1000)
    return f'{hours:02d}:{minutes:02d}:{sec:02d},{ms:03d}'


def ass_time(seconds):
    cs = max(0, round(seconds * 100))
    hours, cs = divmod(cs, 360000)
    minutes, cs = divmod(cs, 6000)
    sec, cs = divmod(cs, 100)
    return f'{hours}:{minutes:02d}:{sec:02d}.{cs:02d}'


def ass_escape(text):
    return text.replace('\\', r'\\').replace('{', r'\{').replace('}', r'\}')


def ass_header(karaoke=False):
    primary = '&H0000FFFF' if karaoke else '&H00FFFFFF'
    secondary = '&H00FFFFFF' if karaoke else '&H0000FFFF'
    return f'''[Script Info]
Title: Lyrics
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,54,{primary},{secondary},&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,60,60,55,1

[Events]
{ASS_FORMAT}
'''


def karaoke_text(line, entries, offset, char_mode, start, end):
    matches = ([m for m in re.finditer(r'[^\W_]', line, re.UNICODE) if tokenize(m.group(), True)]
               if char_mode else list(WORD_RE.finditer(line)))
    by_idx = {i - offset: timing for i, timing in entries}
    result, cursor, previous = [], 0, start
    for idx, match in enumerate(matches):
        result.append(ass_escape(line[cursor:match.start()]))
        value = ass_escape(match.group())
        timing = by_idx.get(idx)
        if timing:
            ws, we = timing
            ws, we = max(ws, start, previous), min(we, end)
            if ws > previous:
                result.append(r'{\k%d}' % max(1, round((ws - previous) * 100)))
            if we > ws:
                result.append(r'{\kf%d}' % max(1, round((we - ws) * 100)) + value)
                previous = we
            else:
                result.append(value)
        else:
            result.append(value)
        cursor = match.end()
    result.append(ass_escape(line[cursor:]))
    return ''.join(result)


def export(alignment, kind, output, min_coverage=0.5):
    blocks, skipped = subtitle_blocks(alignment, min_coverage)
    rows = []
    for _, start, end, line, entries, offset in blocks:
        if kind == 'srt':
            rows.append(f'{len(rows)+1}\n{srt_time(start)} --> {srt_time(end)}\n{line}')
        else:
            content = (karaoke_text(line, entries, offset, alignment.char_mode, start, end)
                       if kind == 'karaoke' else ass_escape(line))
            rows.append(f'Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{content}')
    content = ('\n\n'.join(rows) + ('\n' if rows else '') if kind == 'srt'
               else ass_header(kind == 'karaoke') + '\n'.join(rows) + ('\n' if rows else ''))
    Path(output).write_text(content, encoding='utf-8-sig' if kind != 'srt' else 'utf-8')
    print(f'{kind.upper()}: {output} | lines: {len(rows)}/{len(alignment.lines)}')
    if skipped:
        print(f'Skipped low-confidence/unaligned lines: {skipped}')
    if kind == 'karaoke' and alignment.char_mode:
        print('Note: CJK character timestamps are interpolated from WhisperX word spans.')


def evaluate(
    transcript_path: Path,
    lyrics_path: Path,
    output_type: Literal['pretty', 'plaintext', 'json', 'yaml'] = 'pretty',
    overall_min: float = 0.85,
    local_min: float = 0.60,
    window: int = 25,
    step: int = 5,
    line_min: float = 0.50,
) -> dict:
    """Evaluate WhisperX transcript against lyrics."""
    transcript = json.loads(Path(transcript_path).read_text(encoding='utf-8-sig'))
    song = json.loads(Path(lyrics_path).read_text(encoding='utf-8-sig'))
    alignment = build_alignment(transcript, song)
    compare_segments(transcript_segments(transcript), alignment.char_mode)
    
    if output_type == 'pretty':
        check(alignment, color=True, overall_min=overall_min, local_min=local_min,
              window=window, step=step, line_min=line_min)
    elif output_type == 'plaintext':
        check(alignment, color=False, overall_min=overall_min, local_min=local_min,
              window=window, step=step, line_min=line_min)
    elif output_type == 'json':
        import io
        import sys
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            result = check(alignment, color=False, overall_min=overall_min, local_min=local_min,
                          window=window, step=step, line_min=line_min)
            sys.stdout = old_stdout
            print(json.dumps(result, indent=2))
        finally:
            sys.stdout = old_stdout
    elif output_type == 'yaml':
        import io
        import sys
        import yaml
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            result = check(alignment, color=False, overall_min=overall_min, local_min=local_min,
                          window=window, step=step, line_min=line_min)
            sys.stdout = old_stdout
            print(yaml.dump(result, default_flow_style=False))
        finally:
            sys.stdout = old_stdout
    
    return alignment


def convert(
    transcript_path: Path,
    lyrics_path: Path,
    target_type: Literal['k-ass', 'ass', 'srt'] = 'k-ass',
    output: Path | None = None,
    min_timing_coverage: float = 0.50,
) -> None:
    """Convert WhisperX transcript to subtitle format."""
    transcript = json.loads(Path(transcript_path).read_text(encoding='utf-8-sig'))
    song = json.loads(Path(lyrics_path).read_text(encoding='utf-8-sig'))
    alignment = build_alignment(transcript, song)
    
    kind_map = {'k-ass': 'karaoke', 'ass': 'ass', 'srt': 'srt'}
    kind = kind_map[target_type]
    
    if output is None:
        output_map = {'k-ass': 'karaoke.ass', 'ass': 'lyrics.ass', 'srt': 'lyrics.srt'}
        output = Path(output_map[target_type])
    
    export(alignment, kind, output, min_coverage=min_timing_coverage)
