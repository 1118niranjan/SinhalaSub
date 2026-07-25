"""Drag-and-drop payload parsing and movie auto-detection."""
import os

import pysrt

import sinhalasub


# ----- drag and drop payloads ------------------------------------------------

def test_parse_drop_single_path():
    assert sinhalasub.parse_drop("C:/movies/a.srt") == ["C:/movies/a.srt"]


def test_parse_drop_braced_path_with_spaces():
    assert sinhalasub.parse_drop("{C:/my movies/a b.srt}") == ["C:/my movies/a b.srt"]


def test_parse_drop_mixed_multiple():
    assert sinhalasub.parse_drop("{C:/a b/x.srt} C:/y.srt") == \
        ["C:/a b/x.srt", "C:/y.srt"]


def test_parse_drop_empty():
    assert sinhalasub.parse_drop("") == []


# ----- movie auto-detection --------------------------------------------------

def _make(tmp_path, names):
    for n in names:
        (tmp_path / n).write_text("x", encoding="utf-8")


def _srt(tmp_path, name):
    subs = pysrt.SubRipFile()
    subs.append(pysrt.SubRipItem(index=1, start=pysrt.SubRipTime(0, 0, 1),
                                 end=pysrt.SubRipTime(0, 0, 2), text="hi"))
    path = str(tmp_path / name)
    subs.save(path, encoding="utf-8")
    return path


class _Finder(sinhalasub.SinhalaSubApp):
    """Only the detection logic - no Tk window needed."""

    def __init__(self):
        pass


def test_finds_exactly_matching_video(tmp_path):
    srt = _srt(tmp_path, "Movie.2021.srt")
    _make(tmp_path, ["Movie.2021.mkv", "Unrelated.mkv"])
    found = _Finder().find_video_for(srt)
    assert os.path.basename(found) == "Movie.2021.mkv"


def test_prefers_best_matching_video_in_a_season_folder(tmp_path):
    srt = _srt(tmp_path, "Show.S01E04.srt")
    _make(tmp_path, ["Show.S01E01.mkv", "Show.S01E04.mp4", "Show.S01E09.mkv"])
    found = _Finder().find_video_for(srt)
    assert os.path.basename(found) == "Show.S01E04.mp4"


def test_ignores_the_si_suffix_when_matching(tmp_path):
    srt = _srt(tmp_path, "Movie.2021.si.srt")
    _make(tmp_path, ["Movie.2021.mkv"])
    found = _Finder().find_video_for(srt)
    assert os.path.basename(found) == "Movie.2021.mkv"


def test_returns_none_when_no_video_resembles_the_subtitle(tmp_path):
    srt = _srt(tmp_path, "Movie.2021.srt")
    _make(tmp_path, ["zzz.mkv"])
    assert _Finder().find_video_for(srt) is None


def test_returns_none_for_folder_with_no_video(tmp_path):
    srt = _srt(tmp_path, "Movie.2021.srt")
    assert _Finder().find_video_for(srt) is None
