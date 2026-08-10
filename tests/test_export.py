import pysrt

from sinhalasub import subtitle_export as ex


def _subs(items=None):
    items = items or [("පළමු පේළිය", 0, 2), ("දෙවන\nපේළිය", 2.5, 4)]
    f = pysrt.SubRipFile()
    for i, (text, s, e) in enumerate(items):
        f.append(pysrt.SubRipItem(index=i + 1,
                                  start=pysrt.SubRipTime(seconds=s),
                                  end=pysrt.SubRipTime(seconds=e), text=text))
    return f


# ----- format registry -------------------------------------------------------

def test_formats_include_the_tv_friendly_set():
    keys = [f["key"] for f in ex.FORMATS]
    for expected in ("srt", "vtt", "ass", "sub", "txt"):
        assert expected in keys


def test_every_format_declares_an_extension_and_label():
    for f in ex.FORMATS:
        assert f["ext"].startswith(".")
        assert f["label"]


# ----- SubRip ----------------------------------------------------------------

def test_srt_round_trips(tmp_path):
    out = str(tmp_path / "a.srt")
    ex.write(_subs(), out, "srt")
    back = pysrt.open(out)
    assert len(back) == 2
    assert back[0].text == "පළමු පේළිය"


def test_srt_bom_encoding_writes_a_bom(tmp_path):
    out = str(tmp_path / "b.srt")
    ex.write(_subs(), out, "srt", encoding="utf-8-sig")
    assert open(out, "rb").read(3) == b"\xef\xbb\xbf"


def test_srt_without_bom_has_none(tmp_path):
    out = str(tmp_path / "c.srt")
    ex.write(_subs(), out, "srt", encoding="utf-8")
    assert open(out, "rb").read(3) != b"\xef\xbb\xbf"


# ----- WebVTT ----------------------------------------------------------------

def test_vtt_has_header_and_dot_milliseconds(tmp_path):
    out = str(tmp_path / "a.vtt")
    ex.write(_subs(), out, "vtt")
    body = open(out, encoding="utf-8").read()
    assert body.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:02.000" in body


# ----- ASS / SSA -------------------------------------------------------------

def test_ass_has_required_sections(tmp_path):
    out = str(tmp_path / "a.ass")
    ex.write(_subs(), out, "ass")
    body = open(out, encoding="utf-8").read()
    assert "[Script Info]" in body
    assert "[V4+ Styles]" in body
    assert "[Events]" in body
    assert "Dialogue:" in body


def test_ass_newlines_become_hard_breaks(tmp_path):
    out = str(tmp_path / "b.ass")
    ex.write(_subs(), out, "ass")
    assert "දෙවන\\Nපේළිය" in open(out, encoding="utf-8").read()


def test_ass_uses_the_requested_colour(tmp_path):
    out = str(tmp_path / "c.ass")
    ex.write(_subs(), out, "ass", colour="#FFD700")
    # ASS uses &HAABBGGRR, so #FFD700 becomes 00D7FF
    assert "&H0000D7FF" in open(out, encoding="utf-8").read()


# ----- MicroDVD .sub (frame based, for very old players) ---------------------

def test_sub_uses_frame_numbers(tmp_path):
    out = str(tmp_path / "a.sub")
    ex.write(_subs(), out, "sub", fps=25.0)
    first = open(out, encoding="utf-8").read().splitlines()[0]
    # 0s -> frame 0, 2s at 25fps -> frame 50
    assert first.startswith("{0}{50}")


def test_sub_newlines_become_pipes(tmp_path):
    out = str(tmp_path / "b.sub")
    ex.write(_subs(), out, "sub", fps=25.0)
    assert "දෙවන|පේළිය" in open(out, encoding="utf-8").read()


# ----- plain text ------------------------------------------------------------

def test_txt_is_only_the_dialogue(tmp_path):
    out = str(tmp_path / "a.txt")
    ex.write(_subs(), out, "txt")
    body = open(out, encoding="utf-8").read()
    assert "-->" not in body
    assert "පළමු පේළිය" in body


# ----- markup stripping ------------------------------------------------------

def test_font_tags_are_stripped_for_formats_that_cannot_show_them(tmp_path):
    subs = _subs([('<font color="#FFD700">වර්ණ</font>', 0, 2)])
    out = str(tmp_path / "a.sub")
    ex.write(subs, out, "sub", fps=25.0)
    body = open(out, encoding="utf-8").read()
    assert "font" not in body
    assert "වර්ණ" in body


def test_unknown_format_raises(tmp_path):
    try:
        ex.write(_subs(), str(tmp_path / "x.zzz"), "zzz")
        assert False, "expected ValueError"
    except ValueError:
        pass
