"""Behavioural depth on the complaint parser: what the staged pipeline actually
understands, and where it is still wrong.

Everything here runs the OFFLINE rules pipeline (rules_parse / force_rules=True).
That is deliberate on two counts: it is the path that has to work with no API key
and no Wi-Fi, and it is the only path that is deterministic enough to assert on —
an LLM's answer is not a fixture. conftest strips the provider keys so nothing in
this file can accidentally reach the network.

Four tests are marked xfail(strict) and tagged `defect`. Each documents a real
misparse with the precise mechanism, so the bug stays visible in every test run
instead of being quietly absent from the suite. When one is fixed the strict
marker turns the XPASS into a failure that names the test to un-mark.
"""
from __future__ import annotations

import pytest

from backend.parser import (VALID_ZONES, detect_retraction, fuzzy_hit, parse,
                            rules_parse)
from sim.twin import ZONE_IDS


def p(text):
    """Parse one message through the offline pipeline."""
    return rules_parse(text)


# --------------------------------------------------------------------------
# 1 · the five issue families
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,issue,zone", [
    ("It's way too hot in Conference Room B", "too_hot", "zone_b"),
    ("freezing in cabin c", "too_cold", "zone_c"),
    ("so humid in the cafeteria", "humid", "zone_e"),
    ("It's really stuffy in Conference Room B", "stuffy", "zone_b"),
    ("cold air blowing right on me in the lobby", "drafty", "zone_d"),
    ("there's a draft at my desk in the open office", "drafty", "zone_a"),
    ("the canteen is muggy", "humid", "zone_e"),
    ("bullpen is chilly", "too_cold", "zone_a"),
])
def test_each_issue_family_is_recognised(text, issue, zone):
    out = p(text)
    assert out.is_comfort_complaint is True, f"{text!r} was not read as a complaint"
    assert out.issue == issue, f"{text!r} -> {out.issue}, expected {issue}"
    assert out.zone_ids == [zone], f"{text!r} -> {out.zone_ids}, expected [{zone}]"


def test_drafty_beats_too_cold_because_it_is_more_specific():
    """"cold air blowing on me" is a draft complaint, not a temperature one —
    the cascade order in INTENT_TABLE is what makes that true."""
    assert p("cold air blowing right on me in the lobby").issue == "drafty"
    assert p("its cold in the lobby").issue == "too_cold"


# --------------------------------------------------------------------------
# 2 · multi-zone
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,zones", [
    ("The lobby and cafeteria are too hot", ["zone_d", "zone_e"]),
    ("conference room b and cabin c are both freezing", ["zone_b", "zone_c"]),
    ("open office, lobby and cafeteria are all stuffy", ["zone_a", "zone_d", "zone_e"]),
])
def test_multi_zone_complaint_lists_every_zone_in_mention_order(text, zones):
    out = p(text)
    assert out.zone_ids == zones, f"{text!r} -> {out.zone_ids}"
    assert out.is_comfort_complaint is True


def test_zone_id_always_tracks_the_head_of_zone_ids():
    """Back-compat: older callers read the scalar zone_id and must never see a
    different room from the one zone_ids leads with."""
    for text in ("The lobby and cafeteria are too hot", "freezing in cabin c",
                 "it's hot in narnia", "the projector in room b is broken"):
        out = p(text)
        assert out.zone_id == (out.zone_ids[0] if out.zone_ids else None), text


def test_repeated_zone_mentions_collapse_to_one_entry():
    out = p("the lobby is hot, seriously the lobby is so hot")
    assert out.zone_ids == ["zone_d"], f"duplicate mentions fanned out: {out.zone_ids}"


# --------------------------------------------------------------------------
# 3 · Hinglish and Tamil-English
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,issue,zone", [
    ("conference room me bahut garmi hai", "too_hot", "zone_b"),
    ("cabin c me bahut thand lag rahi hai", "too_cold", "zone_c"),
    ("lobby me dum ghut raha hai", "stuffy", "zone_d"),
    ("cafeteria me thoda garam hai", "too_hot", "zone_e"),
])
def test_hinglish_complaints_parse(text, issue, zone):
    out = p(text)
    assert out.is_comfort_complaint is True, text
    assert out.issue == issue, f"{text!r} -> {out.issue}"
    assert out.zone_ids == [zone]
    assert out.language != "en", f"{text!r} was labelled plain English"


@pytest.mark.parametrize("text,issue,zone", [
    ("cafeteria romba soodu irukku", "too_hot", "zone_e"),
    ("conference room la romba kulir irukku", "too_cold", "zone_b"),
    ("lobby la kaatru illa", "stuffy", "zone_d"),
    ("cabin c la AC kammi pannunga", "too_cold", "zone_c"),
])
def test_tamil_english_complaints_parse(text, issue, zone):
    """Tamil transliteration resolves to the same issue set.

    NOTE ON THE LABEL: contracts.LANGUAGES is ("en", "hinglish", "mixed") — there
    is no "tamil" literal — and the parser merges both transliteration lexicons
    into INDIC_LEXICON, so a Tanglish message is correctly labelled non-English
    but reports "hinglish"/"mixed". That is the pinned vocabulary, not a bug, so
    this asserts "not English" rather than a specific Indic label.
    """
    out = p(text)
    assert out.is_comfort_complaint is True, text
    assert out.issue == issue, f"{text!r} -> {out.issue}"
    assert out.zone_ids == [zone]
    assert out.language in ("hinglish", "mixed"), f"{text!r} -> {out.language}"


def test_composed_hinglish_draft_rule_fires_without_a_phrase_entry():
    """"moving air" x "aimed at me" composes into a draft with no phrase in the
    lexicon for that exact sentence."""
    out = p("AC ki hawa seedha muh pe aa rahi hai desk pe")
    assert out.issue == "drafty"
    assert out.reasoning.startswith("Rules[composite]")


# --------------------------------------------------------------------------
# 4 · typos
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,issue,zone", [
    ("the lobbby is boilling", "too_hot", "zone_d"),
    ("conferance room b is friezing", "too_cold", "zone_b"),
    ("its stufffy in the cafeteria", "stuffy", "zone_e"),
    ("cabin c is too hott", "too_hot", "zone_c"),
])
def test_typos_still_resolve_zone_and_issue(text, issue, zone):
    out = p(text)
    assert out.is_comfort_complaint is True, text
    assert out.issue == issue, f"{text!r} -> {out.issue}"
    assert out.zone_ids == [zone], f"{text!r} -> {out.zone_ids}"


def test_fuzzy_matching_never_fires_on_a_real_english_word():
    """The FUZZ_STOPWORDS guard. Without it "meeting" is one edit from "melting"
    and every calendar message becomes a heat complaint."""
    assert fuzzy_hit("meeting", "melting") is False
    assert fuzzy_hit("thanks", "thand") is False
    assert fuzzy_hit("coffee", "cold") is False
    for text in ("we have a meeting in room b", "thanks for fixing room b",
                 "printing in room b"):
        assert p(text).is_comfort_complaint is False, f"{text!r} misread as a complaint"


def test_typo_tolerance_is_budgeted_by_word_length():
    """Short lexicon words get a zero edit budget on purpose: at four characters
    almost every other four-letter word is one edit away."""
    from backend.parser import fuzzy_tolerance
    assert fuzzy_tolerance("cold") == 0
    assert fuzzy_tolerance("stuffy") == 1
    assert fuzzy_tolerance("refrigerat") == 2


# --------------------------------------------------------------------------
# 5 · sarcasm, metaphor, emoji
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,issue", [
    ("it's basically a sauna in the cafeteria", "too_hot"),
    ("conference room b is an icebox", "too_cold"),
    ("I need a winter coat in conference room b", "too_cold"),
    ("i need a jacket in room b", "too_cold"),
    ("might as well bring a blanket to the lobby", "too_cold"),
])
def test_metaphor_and_indirect_phrasing_are_understood(text, issue):
    out = p(text)
    assert out.is_comfort_complaint is True, text
    assert out.issue == issue, f"{text!r} -> {out.issue}"


@pytest.mark.parametrize("text,issue", [
    ("room b \U0001F975", "too_hot"),
    ("cabin c \U0001F976", "too_cold"),
    ("lobby \U0001F4A6", "humid"),
    ("cafeteria \U0001F927", "stuffy"),
])
def test_emoji_only_message_infers_the_issue(text, issue):
    out = p(text)
    assert out.is_comfort_complaint is True, text
    assert out.issue == issue
    assert out.zone_ids, "the zone was named in words and must still resolve"


@pytest.mark.defect
@pytest.mark.xfail(strict=True, reason=(
    "DEFECT: prefix matching cancels the garment/appliance sarcasm lexicon. "
    "_match_rule() accepts `tok.startswith(w)`, so the token 'sweater' matches "
    "the too_hot word 'sweat' and 'heater' matches the too_hot word 'heat'. Both "
    "words are ALSO the too_cold inferred signals ('sweater' in the garment list, "
    "'space heater' in its phrase list), so every such message asserts hot and "
    "cold at once, detect_intent()'s collision branch fires, and the issue is "
    "downgraded to 'other' with confidence ~0.52. Result: the two lexicon entries "
    "written specifically to catch this sarcasm can never win. 'jacket' and "
    "'blanket' work because no too_hot word is a prefix of them."))
@pytest.mark.parametrize("text", [
    "room b needs a sweater",
    "should I bring a space heater to room b",
])
def test_garment_sarcasm_that_collides_with_a_heat_word(text):
    out = p(text)
    assert out.issue == "too_cold", f"{text!r} -> {out.issue} (conf {out.confidence})"


# --------------------------------------------------------------------------
# 6 · comparisons
# --------------------------------------------------------------------------

def test_comparison_against_another_zone_still_names_both_zones():
    """Even where the issue is misread (see the xfail below), the zone extraction
    must not lose a room the occupant named."""
    out = p("the lobby is hotter than the cafeteria")
    assert out.zone_ids == ["zone_d", "zone_e"], out.zone_ids


@pytest.mark.defect
@pytest.mark.xfail(strict=True, reason=(
    "DEFECT: the English word 'than' is read as the Hinglish word 'thand' (cold). "
    "'thand' is 5 characters so fuzzy_tolerance gives it an edit budget of 1, the "
    "token 'than' is exactly 1 edit away, it is 4 characters so the `len(token) < 4` "
    "guard does not apply, and 'than' is absent from FUZZ_STOPWORDS — so "
    "fuzzy_hit('than','thand') is True. Every 'hotter than X' therefore asserts "
    "too_hot AND too_cold, detect_intent()'s collision branch downgrades the issue "
    "to 'other', requires_clarification goes true and confidence drops from ~0.94 "
    "to ~0.56. Comparisons phrased the cold way ('colder than') survive only "
    "because the false match happens to agree with the real one. Fix: add 'than' "
    "to FUZZ_STOPWORDS."))
@pytest.mark.parametrize("text", [
    "the lobby is hotter than the cafeteria",
    "warmer than usual in room b",
    "hotter than yesterday in the lobby",
])
def test_hot_comparisons_are_read_as_heat_complaints(text):
    out = p(text)
    assert out.issue == "too_hot", f"{text!r} -> {out.issue} (conf {out.confidence})"
    assert out.requires_clarification is False


# --------------------------------------------------------------------------
# 7 · follow-ups
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,issue,sev_min", [
    ("still hot in room b", "too_hot", 1),
    ("b is stuffy again", "stuffy", 1),
    ("third time this week the lobby is freezing", "too_cold", 3),
])
def test_follow_up_phrasing_files_a_fresh_report(text, issue, sev_min):
    out = p(text)
    assert out.is_comfort_complaint is True, text
    assert out.issue == issue
    assert out.severity >= sev_min, f"{text!r} severity {out.severity}"
    assert out.zone_ids, f"{text!r} lost its zone"


def test_contrast_marker_beats_an_all_clear_phrase():
    """"all good but the cabin is hot" contains a retraction phrase AND an ongoing
    marker. The complaint must win, or the occupant's actual problem is discarded."""
    assert detect_retraction("all good but the cabin is hot") is False
    out = p("all good but the cabin is hot")
    assert out.is_comfort_complaint is True
    assert (out.issue, out.zone_ids) == ("too_hot", ["zone_c"])


# --------------------------------------------------------------------------
# 8 · negatives: outdoors and appliances
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "it's boiling outside today",
    "chilly out there this morning",
    "monsoon is making everything sticky outside",
    "the weather is horrible, 42 degrees",
])
def test_outdoor_statements_are_not_complaints(text):
    out = p(text)
    assert out.is_comfort_complaint is False, f"{text!r} filed a complaint"
    assert out.zone_ids == [], f"{text!r} kept a zone: {out.zone_ids}"


def test_an_indoor_anchor_overrides_the_outdoor_veto():
    """"hotter in here than outside" mentions the outdoors but is about the room."""
    from backend.parser import detect_intent, normalize
    intent = detect_intent(normalize("its hot in here in room b compared to outside"))
    assert intent.veto == "", "an explicit indoor anchor was still vetoed as weather"


@pytest.mark.parametrize("text", [
    "the coffee machine is boiling hot",
    "my laptop is burning hot",
    "the microwave in the pantry is burning hot",
    "the fridge in the cafeteria is not cold",
])
def test_appliance_subjects_are_not_complaints(text):
    out = p(text)
    assert out.is_comfort_complaint is False, f"{text!r} filed a complaint"


def test_equipment_fault_is_not_a_comfort_complaint():
    out = p("the projector in room b is broken")
    assert out.is_comfort_complaint is False
    assert out.issue == "other"


def test_an_oven_metaphor_is_still_a_room_complaint():
    """APPLIANCE_NOUNS deliberately omits "oven": it is the commonest metaphor for
    a hot room, and vetoing it would drop real complaints."""
    out = p("this room is like an oven, conference room b")
    assert out.is_comfort_complaint is True
    assert out.issue == "too_hot"


# --------------------------------------------------------------------------
# 9 · retractions
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "room b is fine now, thanks",
    "all good in the lobby and cafeteria now",
    "the heat issue in the lobby is fixed now",
    "cabin c theek ho gaya",
    "conference room b is comfortable now",
])
def test_retractions_are_detected_and_file_nothing(text):
    assert detect_retraction(text) is True, f"{text!r} was not read as an all-clear"
    out = p(text)
    assert out.is_comfort_complaint is False, f"{text!r} filed a fresh complaint"


def test_a_retraction_keeps_its_zones_so_the_store_can_be_cleared():
    """app.handle_complaint clears exactly the zones the all-clear names, so the
    parser must still report them even though nothing is filed."""
    out = p("all good in the lobby and cafeteria now")
    assert out.zone_ids == ["zone_d", "zone_e"], out.zone_ids
    assert out.is_comfort_complaint is False


@pytest.mark.parametrize("text", [
    "no longer stuffy in reception",
    "no longer too hot in room b",
    "the lobby isnt stuffy anymore",
    "its not cold in cabin c anymore",
    "not hot anymore in room b",
    "room b is no longer freezing",
    "stopped being humid in the cafeteria",
    "the heat is gone in room b",
])
def test_negated_ongoing_form_is_a_retraction(text):
    """Regression for defect D3 (fixed in parser.py by _negated_state_re).

    The negated-discomfort family — "no longer X", "not X anymore", "stopped being
    X", "the X is gone" — cancels a complaint without containing a single positive
    all-clear phrase. Before the fix these fell through to detect_intent(), which
    saw the issue word and filed a BRAND NEW complaint against the zone the
    occupant had just said was fine; "room b is no longer freezing" filed a
    severity-3 too_cold constraint and HEATED a comfortable room. Locked in here so
    it cannot regress.
    """
    assert detect_retraction(text) is True, f"{text!r} not detected as an all-clear"
    assert p(text).is_comfort_complaint is False, f"{text!r} filed a new complaint"


@pytest.mark.parametrize("text", [
    "no longer hot but still stuffy in room b",
    "not cold, freezing in room b",
])
def test_negation_handling_does_not_over_reach(text):
    """The two ways the negated-state rule must NOT fire: a contrast marker means
    the discomfort continues, and a bare negation with no temporal tail is a
    self-correction ("not cold, freezing"), not an all-clear."""
    assert detect_retraction(text) is False, f"{text!r} wrongly cancelled a complaint"
    assert p(text).is_comfort_complaint is True, f"{text!r} dropped a live complaint"


# --------------------------------------------------------------------------
# 10 · ambiguity -> requires_clarification
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "is it hot or cold in room b, cant tell",
    "it's too warm. or maybe too cold. cant decide",
    "please adjust the temperature in the lobby",
    "maybe kind of warm somewhere?",
])
def test_hedged_or_directionless_messages_ask_for_clarification(text):
    out = p(text)
    assert out.requires_clarification is True, f"{text!r} was acted on without asking"


def test_a_control_request_with_no_direction_is_not_filed_as_a_complaint():
    out = p("please adjust the temperature in the lobby")
    assert out.is_comfort_complaint is False
    assert out.requires_clarification is True
    assert "no direction" in out.reasoning


def test_an_unresolvable_zone_forces_clarification():
    out = p("it's hot in narnia")
    assert out.zone_ids == []
    assert out.requires_clarification is True, \
        "a complaint with no addressable zone must be sent back for clarification"


def test_a_clear_single_zone_complaint_does_not_ask_for_clarification():
    assert p("It's way too hot in Conference Room B").requires_clarification is False


@pytest.mark.defect
@pytest.mark.xfail(strict=True, reason=(
    "DEFECT: an acute distress signal with a named zone but no direction word is "
    "dropped in SILENCE. detect_severity()'s _SEV3 table already treats "
    "'unbearable', 'cant work', 'dying' and 'ridiculous' as level-3 evidence, but "
    "no INTENT_TABLE rule lists them, so detect_intent() returns issue=None; "
    "rules_parse then only sets requires_clarification when intent.ambiguous is "
    "True (i.e. a CONTROL_VERBS request), which these are not. So "
    "'CONFERENCE ROOM B IS UNBEARABLE' comes back is_comfort_complaint=False AND "
    "requires_clarification=False: the operator is never told somebody is in "
    "distress in a room the parser successfully identified. The minimal honest fix "
    "is to flag it for clarification, not to guess a direction."))
@pytest.mark.parametrize("text", [
    "CONFERENCE ROOM B IS UNBEARABLE, I'm dying",
    "the lobby is unbearable",
    "cabin c is ridiculous",
])
def test_severe_distress_without_a_direction_asks_for_clarification(text):
    out = p(text)
    assert out.zone_ids, f"{text!r} did not even resolve a zone"
    assert out.requires_clarification is True, \
        f"{text!r} was dropped silently (cc={out.is_comfort_complaint})"


# --------------------------------------------------------------------------
# 11 · severity and confidence
# --------------------------------------------------------------------------

def test_severity_ladder_is_ordered():
    mild = p("warm in room b").severity
    clear = p("really warm in room b").severity
    urgent = p("conference room b is so hot i cant work").severity
    assert mild < clear < urgent, f"severity ladder collapsed: {mild}/{clear}/{urgent}"
    assert (mild, urgent) == (1, 3)


def test_shouting_and_elongation_raise_severity():
    assert p("SO WARM IN ROOM B!!").severity == 3
    assert p("sooo hot in room b").severity > p("hot in room b").severity


def test_a_dampener_pulls_severity_back_to_mild():
    assert p("slightly warm in room b").severity == 1
    assert p("thoda garam hai cafeteria me").severity == 1
    assert p("konjam soodu irukku cafeteria la").severity == 1


def test_confidence_is_monotone_in_signal_strength():
    """An explicit, zoned, intensified complaint must outscore a vague one."""
    strong = p("It's way too hot in Conference Room B").confidence
    plain = p("the cafeteria is warm").confidence
    vague = p("maybe kind of warm somewhere?").confidence
    assert strong > plain > vague, f"confidence not ordered: {strong}/{plain}/{vague}"
    assert vague < 0.5, "a hedged, zoneless guess should not clear the 0.45 clarify bar"


def test_a_named_zone_is_worth_more_confidence_than_none():
    with_zone = p("it's stuffy in conference room b").confidence
    without = p("it's stuffy in narnia").confidence
    assert with_zone > without, f"{with_zone} vs {without}"


def test_a_fuzzy_match_is_trusted_less_than_an_exact_one():
    exact = p("conference room b is freezing").confidence
    fuzzy = p("conferance room b is friezing").confidence
    assert exact > fuzzy, f"typo'd message scored as high as clean text: {exact}/{fuzzy}"


def test_confidence_stays_inside_zero_to_one_across_the_corpus():
    corpus = ["It's way too hot in Conference Room B", "maybe warm?", "",
              "the coffee machine is boiling hot", "cafeteria romba soodu irukku",
              "all good now", "zone_q is freezing", "\U0001F975\U0001F975\U0001F975"]
    for text in corpus:
        out = p(text)
        assert 0.0 <= out.confidence <= 1.0, f"{text!r} -> {out.confidence}"
        assert 1 <= out.severity <= 3, f"{text!r} -> severity {out.severity}"


# --------------------------------------------------------------------------
# 12 · the anti-hallucination guarantee
# --------------------------------------------------------------------------

INVENTED = ["narnia", "mordor annex", "the batcave", "wakanda hall", "hogwarts wing",
            "atlantis floor", "the death star", "shangri la suite", "el dorado room",
            "valhalla lounge", "gotham deck", "xanadu bay", "brigadoon nook",
            "roof helipad", "car park level 2", "utility closet 7", "the bunker",
            "quidditch pitch", "diagon alley", "sub basement 4"]
TEMPLATES = ["it's too hot in %s", "%s is freezing", "so stuffy in %s",
             "%s feels humid", "drafty in %s"]


@pytest.mark.parametrize("name", INVENTED)
def test_parser_never_invents_a_zone_for_a_room_that_does_not_exist(name):
    """100 fuzz cases in total. A hallucinated zone id would move a real setpoint
    in a real room, which is the single worst failure this parser can have."""
    for template in TEMPLATES:
        text = template % name
        out = p(text)
        assert set(out.zone_ids) <= VALID_ZONES, f"{text!r} -> {out.zone_ids}"
        assert out.zone_ids == [], f"{text!r} invented {out.zone_ids}"
        assert out.zone_id is None
        if out.is_comfort_complaint:
            assert out.requires_clarification is True, \
                f"{text!r} would be applied with no addressable zone"


def test_a_zone_id_typed_verbatim_is_not_trusted_either():
    """"zone_q" looks like our own id format. It still must not resolve."""
    out = p("zone_q is freezing")
    assert out.zone_ids == []
    assert out.requires_clarification is True


def test_every_alias_in_the_vocabulary_maps_into_the_real_zone_set():
    from sim.twin import ZONES
    for z in ZONES:
        for alias in z.aliases:
            out = p(f"it's too hot in the {alias}")
            assert set(out.zone_ids) <= set(ZONE_IDS), f"{alias!r} -> {out.zone_ids}"
            assert out.zone_ids, f"alias {alias!r} of {z.id} resolved to nothing"


# --------------------------------------------------------------------------
# 13 · determinism and the parse() wrapper
# --------------------------------------------------------------------------

def test_rules_parse_is_deterministic():
    text = "The lobby and cafeteria are unbearably hot, i cant work"
    first = p(text).model_dump()
    for _ in range(3):
        assert p(text).model_dump() == first, "rules_parse is not deterministic"


def test_force_rules_reports_the_offline_source_and_a_latency():
    out, source, ms = parse("It's way too hot in Conference Room B", force_rules=True)
    assert source == "rules"
    assert isinstance(ms, int) and ms >= 0
    assert out.is_comfort_complaint is True


def test_parse_without_a_provider_key_also_takes_the_offline_path():
    """conftest strips the keys, so this proves the graceful-degradation path is
    what a keyless deployment actually gets — no exception, no network."""
    out, source, _ms = parse("freezing in cabin c")
    assert source == "rules"
    assert out.issue == "too_cold"


def test_empty_and_whitespace_input_never_raise():
    for text in ("", "   ", "\n", "!!!", "12345"):
        out = p(text)
        assert out.is_comfort_complaint is False, f"{text!r} filed a complaint"
        assert out.zone_ids == []
