from sinhalasub import colorize


def test_plain_colour_wraps_whole_line():
    assert colorize.colour_line("හලෝ", "#FFD700") == '<font color="#FFD700">හලෝ</font>'


def test_plain_colour_empty_hex_returns_text_unchanged():
    assert colorize.colour_line("හලෝ", "") == "හලෝ"


# ----- detection -------------------------------------------------------------

def test_detects_sound_and_music_cues():
    assert colorize.classify("[door slams]") == "sound"
    assert colorize.classify("♪♪") == "sound"
    assert colorize.classify("♪ la la la ♪") == "sound"


def test_detects_shouting_and_italic_emphasis():
    assert colorize.classify("GET OUT NOW!") == "emphasis"
    assert colorize.classify("<i>whispering</i>") == "emphasis"


def test_detects_multi_speaker_dialogue():
    assert colorize.classify("- Yes.\n- No.") == "dialogue"


def test_plain_dialogue_is_normal():
    assert colorize.classify("Just a normal line.") == "normal"


# ----- name detection --------------------------------------------------------

def test_finds_proper_names_mid_sentence():
    names = colorize.find_names(["We met John near the harbour.",
                                 "John left for Marseille.",
                                 "The harbour was quiet."])
    assert "John" in names
    assert "Marseille" in names
    # sentence-initial words are not names just because they are capitalised
    assert "We" not in names and "The" not in names


def test_common_words_are_not_treated_as_names():
    names = colorize.find_names(["I said No and then I said Yes."])
    assert "No" not in names and "Yes" not in names


# ----- applying colours ------------------------------------------------------

def test_auto_colour_colours_sound_cues():
    out = colorize.auto_colour_line("[door slams]", scheme={"sound": "#888888"},
                                    names=set())
    assert out == '<font color="#888888">[door slams]</font>'


def test_auto_colour_highlights_a_name_inside_a_line():
    out = colorize.auto_colour_line("Call John now.", scheme={"name": "#00FFFF"},
                                    names={"John"})
    assert out == 'Call <font color="#00FFFF">John</font> now.'


def test_auto_colour_gives_each_speaker_its_own_colour():
    out = colorize.auto_colour_line("- Yes.\n- No.",
                                    scheme={"speaker1": "#FF0000",
                                            "speaker2": "#00FF00"},
                                    names=set())
    assert '<font color="#FF0000">- Yes.</font>' in out
    assert '<font color="#00FF00">- No.</font>' in out


def test_auto_colour_leaves_plain_line_untouched_when_no_rule_matches():
    assert colorize.auto_colour_line("Just talking.", scheme={}, names=set()) == \
        "Just talking."


def test_base_colour_applies_to_lines_with_no_special_rule():
    out = colorize.auto_colour_line("Just talking.", scheme={"normal": "#FFFFFF"},
                                    names=set())
    assert out == '<font color="#FFFFFF">Just talking.</font>'
