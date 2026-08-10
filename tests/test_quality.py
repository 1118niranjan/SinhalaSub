import pysrt

from sinhalasub import quality


def _cue(text, start_s, end_s, index=1):
    return pysrt.SubRipItem(
        index=index,
        start=pysrt.SubRipTime(seconds=start_s),
        end=pysrt.SubRipTime(seconds=end_s),
        text=text)


def _subs(items):
    f = pysrt.SubRipFile()
    for it in items:
        f.append(it)
    return f


# ----- reading speed ---------------------------------------------------------

def test_comfortable_line_has_no_issue():
    subs = _subs([_cue("කොහොමද?", 0, 2)])
    assert quality.check(subs) == []


def test_too_fast_to_read_is_flagged():
    long_text = "මේක ගොඩක් දිග වගේ පේන පේළියක් වන අතර එය කියවීමට ඉතා අඩු කාලයක් ඇත"
    subs = _subs([_cue(long_text, 0, 0.5)])
    kinds = [i.kind for i in quality.check(subs)]
    assert "reading_speed" in kinds


def test_reading_speed_reports_cps():
    long_text = "අකුරු ගොඩක් ඇති පේළියක් මෙතන තිබේ එය කියවීමට කාලය නැත"
    issue = [i for i in quality.check(_subs([_cue(long_text, 0, 0.4)]))
             if i.kind == "reading_speed"][0]
    assert issue.value > quality.MAX_CPS


# ----- line length / count ---------------------------------------------------

def test_over_long_single_line_is_flagged():
    subs = _subs([_cue("අ" * 60, 0, 5)])
    assert "line_length" in [i.kind for i in quality.check(subs)]


def test_three_display_lines_are_flagged():
    subs = _subs([_cue("එක\nදෙක\nතුන", 0, 5)])
    assert "line_count" in [i.kind for i in quality.check(subs)]


def test_two_short_lines_are_fine():
    subs = _subs([_cue("එක\nදෙක", 0, 4)])
    assert quality.check(subs) == []


# ----- untranslated detection ------------------------------------------------

def test_english_left_in_output_is_flagged():
    subs = _subs([_cue("This line was never translated", 0, 4)])
    assert "untranslated" in [i.kind for i in quality.check(subs)]


def test_sound_cue_is_not_reported_as_untranslated():
    subs = _subs([_cue("[door slams]", 0, 2)])
    assert quality.check(subs) == []


def test_empty_cue_is_flagged():
    subs = _subs([_cue("   ", 0, 2)])
    assert "empty" in [i.kind for i in quality.check(subs)]


# ----- timing sanity ---------------------------------------------------------

def test_overlapping_cues_are_flagged():
    subs = _subs([_cue("පළමු", 0, 3, 1), _cue("දෙවන", 2, 5, 2)])
    assert "overlap" in [i.kind for i in quality.check(subs)]


def test_zero_duration_is_flagged():
    subs = _subs([_cue("කෙටි", 4, 4)])
    assert "zero_duration" in [i.kind for i in quality.check(subs)]


# ----- summary ---------------------------------------------------------------

def test_summary_counts_by_kind():
    subs = _subs([_cue("Plain english here", 0, 4, 1),
                  _cue("අ" * 60, 5, 9, 2)])
    text = quality.summarise(quality.check(subs), len(subs))
    assert "untranslated" in text and "line_length" in text


def test_summary_reports_a_clean_file():
    assert "No problems" in quality.summarise([], 10)
