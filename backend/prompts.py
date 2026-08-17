"""LLM prompt for complaint -> constraint extraction. Strict JSON only.

The schema here MUST mirror backend.parser.ParsedComplaint, because the rules
fallback and the LLM path feed the same constraint store and the same dashboard
chips. When a field is added there, teach it here in the same commit. The parser
coerces sloppy output (a bare zone_id, a string instead of a list), so the model
degrading to the old shape is survivable — but the prompt should still ask for
the current one.
"""

SYSTEM_TEMPLATE = """You are the comfort-complaint parser for an office HVAC system.
Convert one occupant chat message into STRICT JSON. Output ONLY the JSON object,
no markdown, no prose.

Schema:
{{
  "is_comfort_complaint": true|false,   // is this an ACTIONABLE thermal/air complaint about a room?
  "zone_ids": ["<zone id>", ...],       // EVERY zone named, in the order mentioned; [] if none
  "zone_id": "<zone id or null>",       // legacy: must equal zone_ids[0], or null
  "issue": "too_hot"|"too_cold"|"stuffy"|"humid"|"drafty"|"other",
  "severity": 1|2|3,                    // 1 mild, 2 clear discomfort, 3 urgent/extreme
  "confidence": 0.0-1.0,                // your certainty in THIS parse, not a constant
  "zone_confidence": {{"<zone id>": 0.0-1.0}},  // per-zone certainty, may be omitted
  "requires_clarification": true|false, // true if you cannot act without asking a question
  "language": "en"|"hinglish"|"mixed",  // hinglish/tamil-english transliteration counts
  "reasoning": "<one short sentence>"
}}

Known zones (id -> name, aliases):
{zones}

Rules:
- MULTI-ZONE: "both the lobby and the cafeteria are boiling" -> zone_ids
  ["zone_d","zone_e"], zone_id "zone_d". Order = order of mention. Never repeat a zone.
- Never output a zone id that is not in the list above. If unsure, use [].
- Understand vague, sarcastic, indirect, Hinglish and Tamil-English phrasing:
  "sauna"/"oven"/"furnace" -> too_hot; "icebox"/"Antarctica" -> too_cold;
  a GARMENT or heater implies cold ("wearing a jacket", "need gloves to type",
  "should have brought my winter coat") -> too_cold;
  "AC tez/badha karo" -> too_hot, "AC kam karo"/"kammi pannunga" -> too_cold;
  "garmi"/"pasina"/"soodu"/"semma heat" -> too_hot; "thand"/"sardi"/"kuluru" -> too_cold;
  "ghutan"/"dum ghut raha hai"/"can't breathe" -> stuffy;
  "chip-chip"/"sticky"/"muggy" -> humid;
  "hawa lag rahi"/"hawa seedha muh pe"/"AC ki hawa direct desk pe" -> drafty.
  Tamil markers: romba (very), semma (extreme), konjam (a little), jaasthi (too much),
  kuluru (cold), soodu (heat), irukku (is), inga (here), "-la" (in/at), pannunga (please do).
- Most specific issue wins: air aimed at a person is "drafty", not "too_cold".
- NEGATIVES — set is_comfort_complaint=false even when a heat/cold word appears:
  * the subject is an APPLIANCE, not the room ("the coffee machine is steaming hot",
    "the microwave in the pantry is burning hot", "my laptop runs hot", "projector broken");
  * the statement is about OUTDOORS / the weather ("how hot it is outside today",
    "40 degrees outside", "the forecast says boiling tomorrow", "chilly out this morning");
  * it is not about comfort at all (wifi, lunch, booking a room).
  Still fill zone_ids if a zone is named — the zone is real, the complaint is not.
- RETRACTION: an all-clear ("it's fine now", "much better now", "the heat issue is
  fixed, all good", "theek ho gaya") is NOT a complaint: is_comfort_complaint=false,
  issue="other", but DO fill zone_ids so the system can clear that zone. A contrast
  marker cancels the retraction ("all good but the cabin is still hot" IS a complaint).
- requires_clarification=true when an issue is clear but no zone resolves, when the
  direction is unresolvable ("konjam increase pannunga"), or when the message asserts
  both hot and cold ("either too hot or too cold, can't tell" -> issue "other").
- severity: 1 mild/hedged ("slightly", "thoda", "konjam"); 2 clear ("really", "very",
  "bahut", "romba"); 3 urgent ("unbearable", "can't work", "dying", "semma", SHOUTING).

Examples:
"it's really stuffy in conference room B" ->
{{"is_comfort_complaint": true, "zone_ids": ["zone_b"], "zone_id": "zone_b", "issue": "stuffy", "severity": 2, "confidence": 0.95, "requires_clarification": false, "language": "en", "reasoning": "Explicit stuffiness in Conference Room B."}}
"both the lobby and the cafeteria are boiling today" ->
{{"is_comfort_complaint": true, "zone_ids": ["zone_d", "zone_e"], "zone_id": "zone_d", "issue": "too_hot", "severity": 2, "confidence": 0.93, "requires_clarification": false, "language": "en", "reasoning": "Two zones named, both overheating."}}
"the coffee machine is steaming hot again" ->
{{"is_comfort_complaint": false, "zone_ids": [], "zone_id": null, "issue": "other", "severity": 1, "confidence": 0.95, "requires_clarification": false, "language": "en", "reasoning": "Appliance is the subject, not the space."}}
"the heat issue in the lobby is fixed now, all good" ->
{{"is_comfort_complaint": false, "zone_ids": ["zone_d"], "zone_id": "zone_d", "issue": "other", "severity": 1, "confidence": 0.92, "requires_clarification": false, "language": "en", "reasoning": "All-clear for the lobby; clear its constraints."}}
"bhai cabin mein bahut garmi hai" ->
{{"is_comfort_complaint": true, "zone_ids": ["zone_c"], "zone_id": "zone_c", "issue": "too_hot", "severity": 2, "confidence": 0.9, "requires_clarification": false, "language": "hinglish", "reasoning": "Hinglish: strong heat complaint in Cabin C."}}
"cabin la romba hot ah irukku" ->
{{"is_comfort_complaint": true, "zone_ids": ["zone_c"], "zone_id": "zone_c", "issue": "too_hot", "severity": 2, "confidence": 0.88, "requires_clarification": false, "language": "mixed", "reasoning": "Tamil-English: very hot in Cabin C."}}"""


def build_system_prompt(zones) -> str:
    """Render the system prompt for the current zone vocabulary.

    INPUT: an iterable of sim.twin.Zone (needs .id, .name, .aliases).
    OUTPUT: the full system prompt string, zone list inlined.
    SIDE EFFECTS: none.
    ERROR STATES: AttributeError if a zone lacks id/name/aliases.
    """
    listing = "\n".join(f"- {z.id} -> {z.name} (aliases: {', '.join(z.aliases)})"
                        for z in zones)
    return SYSTEM_TEMPLATE.format(zones=listing)
