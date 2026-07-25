import subtitle_text as st


# ----- markup unwrap / rewrap ----------------------------------------------

def test_plain_text_passes_through():
    core, rebuild = st.unwrap("Hello there")
    assert core == "Hello there"
    assert rebuild("හලෝ") == "හලෝ"


def test_italic_tags_are_preserved_not_translated():
    core, rebuild = st.unwrap("<i>Hello there</i>")
    assert core == "Hello there"
    assert rebuild("හලෝ") == "<i>හලෝ</i>"


def test_leading_speaker_dash_is_preserved():
    core, rebuild = st.unwrap("- Get down!")
    assert core == "Get down!"
    assert rebuild("පහළට") == "- පහළට"


def test_music_notes_are_preserved_around_lyrics():
    core, rebuild = st.unwrap("♪ I will always love you ♪")
    assert core == "I will always love you"
    assert rebuild("මම ඔබට") == "♪ මම ඔබට ♪"


def test_dash_and_italics_combined():
    core, rebuild = st.unwrap("- <i>Run now!</i>")
    assert core == "Run now!"
    assert rebuild("දුවන්න") == "- <i>දුවන්න</i>"


# ----- ALL-CAPS normalisation ----------------------------------------------

def test_shouted_line_is_normalised_for_the_translator():
    assert st.normalise_caps("GET OUT OF HERE!") == "Get out of here!"


def test_short_acronyms_are_left_alone():
    assert st.normalise_caps("FBI") == "FBI"
    assert st.normalise_caps("OK") == "OK"


def test_mixed_case_is_untouched():
    assert st.normalise_caps("Hello There") == "Hello There"


# ----- sentence grouping across cues ---------------------------------------

def test_sentence_split_across_two_cues_is_grouped():
    texts = ["I was going to", "tell you the truth."]
    assert st.group_sentences(texts) == [[0, 1]]


def test_complete_sentences_are_not_grouped():
    texts = ["I told you already.", "Now leave."]
    assert st.group_sentences(texts) == [[0], [1]]


def test_grouping_stops_at_three_cues():
    texts = ["one two", "three four", "five six", "seven eight"]
    groups = st.group_sentences(texts)
    assert all(len(g) <= 3 for g in groups)
    assert [i for g in groups for i in g] == [0, 1, 2, 3]  # nothing lost


def test_bracket_cues_are_never_grouped():
    texts = ["I was going to", "[door slams]", "tell you later."]
    assert st.group_sentences(texts) == [[0], [1], [2]]


def test_every_index_appears_exactly_once():
    texts = ["a b", "c d.", "e f", "g h", "Done.", "next one"]
    groups = st.group_sentences(texts)
    flat = [i for g in groups for i in g]
    assert sorted(flat) == list(range(len(texts)))


# ----- splitting a joined translation back across cues ---------------------

def test_split_translation_matches_cue_count():
    parts = st.split_translation("එක දෙක තුන හතර පහ හය", [2, 4])
    assert len(parts) == 2
    assert all(p.strip() for p in parts)
    # all words preserved, in order
    assert (parts[0] + " " + parts[1]).split() == "එක දෙක තුන හතර පහ හය".split()


def test_split_translation_single_group_returns_whole():
    assert st.split_translation("හලෝ ලෝකය", [3]) == ["හලෝ ලෝකය"]


def test_split_translation_handles_fewer_words_than_cues():
    parts = st.split_translation("එක", [2, 3])
    assert len(parts) == 2  # never drops a cue


# ----- glossary -------------------------------------------------------------

def test_glossary_applies_preferred_terms():
    g = {"Marseille": "මාර්සෙයි"}
    assert st.apply_glossary("We land in Marseille tonight.", g) == \
        "We land in මාර්සෙයි tonight."


def test_glossary_is_case_insensitive_and_whole_word():
    g = {"john": "ජෝන්"}
    assert st.apply_glossary("John and johnny", g) == "ජෝන් and johnny"


def test_empty_glossary_is_a_no_op():
    assert st.apply_glossary("Nothing changes", {}) == "Nothing changes"


# ----- safer splitting -------------------------------------------------------

def test_never_merges_more_than_two_cues():
    texts = ["one two", "three four", "five six", "seven eight"]
    assert all(len(g) <= 2 for g in st.group_sentences(texts))


def test_split_prefers_a_comma_boundary():
    # proportional split would cut mid-phrase; the comma is the natural break
    parts = st.split_translation("එක දෙක, තුන හතර පහ", [2, 3])
    assert parts[0].endswith(",")
    assert parts[1] == "තුන හතර පහ"


def test_split_still_works_without_punctuation():
    parts = st.split_translation("එක දෙක තුන හතර", [2, 2])
    assert len(parts) == 2
    assert (parts[0] + " " + parts[1]).split() == "එක දෙක තුන හතර".split()
