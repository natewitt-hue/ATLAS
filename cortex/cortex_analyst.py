"""
cortex_analyst.py - ATLAS Cortex Analyst
==========================================
Runs three Flash extraction passes and one fact-check pass for the
Cortex cognitive intelligence pipeline. Zero dependency on Oracle.

Pass order:
  PRE-PASS A  - Tone map (lightweight irony detection, prevents literal misreads)
  PRE-PASS B  - Peak performance identification (finds cognitive ceiling messages)
  PASS 1      - Cognitive schema extraction (all dimensions, scored against peak only)
  PASS 2      - Fact check extraction (real-world verifiable claims only)

All calls: gemini-2.5-flash, response_mime_type=application/json, temperature=0.1
All calls: MD5 cached to .cortex_cache/
"""

import os
import re
import json
import hashlib
import time
import httpx

import atlas_ai
from atlas_ai import Tier
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
CACHE_DIR   = ".cortex_cache"
PASS_DELAY  = 5  # seconds between Flash calls to avoid 429s


class CortexAnalyst:

    def __init__(self):
        os.makedirs(CACHE_DIR, exist_ok=True)

    # -- Cache helpers ---------------------------------------------------------

    def _cache_get(self, key):
        path = os.path.join(CACHE_DIR, f"{key}.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return None

    def _cache_set(self, key, data):
        path = os.path.join(CACHE_DIR, f"{key}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _make_key(self, *parts):
        pieces = []
        for p in parts:
            if isinstance(p, str):
                pieces.append(p)
            else:
                try:
                    pieces.append(json.dumps(p, sort_keys=True))
                except (TypeError, ValueError):
                    pieces.append(str(p))
        combined = "|".join(pieces)
        return hashlib.md5(combined.encode()).hexdigest()


    def _safe_extract_json(self, raw_text, pass_name="unknown"):
        """Extract JSON from potentially malformed Gemini responses."""
        text = raw_text.strip()
        # Strip BOM
        if text and ord(text[0]) == 0xFEFF:
            text = text[1:]
        # Strip markdown code fences
        if text.startswith("```"):
            first_nl = text.find(chr(10))
            if first_nl > 0:
                text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[:-3].rstrip()
        # Strip comment lines
        text_lines = text.splitlines()
        json_lines = [ln for ln in text_lines if not ln.strip().startswith("#")]
        text = chr(10).join(json_lines).strip()

        # Try direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try to find JSON object/array boundaries
        for sc, ec in [("{", "}"), ("[", "]")]:
            s = text.find(sc)
            e = text.rfind(ec)
            if s >= 0 and e > s:
                try:
                    return json.loads(text[s:e + 1])
                except json.JSONDecodeError:
                    continue

        # Last resort: try adding closing braces
        for suffix in ["}", "}}", "]}}",
                       '"]}']:
            try:
                return json.loads(text + suffix)
            except json.JSONDecodeError:
                continue

        print(f"    [!] WARN: Could not parse JSON for {pass_name}. Returning raw wrapper.")
        return {"_raw": text[:2000], "_parse_failed": True}
    # -- Flash call wrapper ----------------------------------------------------

    @retry(
        retry=retry_if_exception_type((Exception, httpx.TransportError)),
        stop=stop_after_attempt(6),
        wait=wait_exponential(multiplier=3, min=5, max=90),
        before_sleep=lambda rs: print(f"    [!] Retry {rs.attempt_number}/6 after error: {rs.outcome.exception().__class__.__name__} -- waiting {rs.next_action.sleep:.0f}s..."),
    )
    def _call_flash(self, prompt, pass_name="unknown"):
        t0 = time.time()
        result = atlas_ai.generate_sync(
            prompt,
            tier=Tier.HAIKU,
            json_mode=True,
            temperature=0.1,
        )
        raw = result.text
        elapsed = time.time() - t0
        print(f"    -> Flash call ({pass_name}) completed in {elapsed:.1f}s")
        return self._safe_extract_json(raw, pass_name)

    # -- PRE-PASS A: Tone Map --------------------------------------------------

    def build_tone_map(self, subject, tone_pack):
        """
        Lightweight irony/sarcasm pre-pass.
        Prevents downstream passes from misreading ironic statements as literal.
        Uses only 200 messages -- enough to characterize the irony register,
        not meant to be exhaustive.
        """
        print(f"[Cortex] PRE-PASS A: Building tone map for {subject}...")
        cache_key = self._make_key("cortex_tone", subject, tone_pack)
        cached    = self._cache_get(cache_key)
        if cached:
            print(f"    -> Loaded from cache.")
            return cached

        prompt = f"""
You are an expert linguist specializing in irony and sarcasm in informal digital communication.
Analyze messages from '{subject}' and build a tone classification map.

Determine whether SURFACE MEANING matches INTENDED MEANING for each message.
This is used to prevent literal misreads downstream.

IRONY DETECTION RULES:
1. Hyperbolic praise used to mock: "Oh he's SO good at this" after failures = sarcastic
2. Self-deprecation masking dominance: "Just glad I finally lost without outplaying him" = ironic dominance claim
3. Feigned agreement to dismiss: "Sure buddy, whatever you say" = dismissive sarcasm
4. Performative outrage for comedic effect: "I'd perform a satanic ritual to get X" = not literal
5. In-group insults as affection: harsh insults in playful threads = bonding, not genuine hostility
6. Rhetorical questions implying opposite: "Oh really? He's the best?" = skeptical refutation
7. CAPS as sarcasm marker in negative context
8. Trailing lol/lmao after insult = often performative banter register
9. Absurdist physically-impossible claims = performative, not literal
10. Period after short statement in casual chat = cold finality ("Cool." "Fine." "Okay.")

Always evaluate a message against surrounding context before classifying.

Output pure valid JSON only, no markdown:
{{
    "tone_classifications": [
        {{
            "message_id": "id string",
            "surface_sentiment": "positive/negative/neutral",
            "true_sentiment": "positive/negative/neutral",
            "is_ironic": true,
            "irony_type": "sarcasm/hyperbole/self_deprecation/performative_hostility/in_group_bonding/rhetorical/absurdist/none",
            "confidence": "high/med/low",
            "corrected_interpretation": "what this message actually means in plain language"
        }}
    ],
    "subject_irony_profile": {{
        "baseline_irony_frequency": "high/med/low",
        "dominant_irony_type": "string",
        "literal_vs_ironic_ratio": "X% literal / Y% ironic estimate",
        "signature_ironic_phrases": ["list of recurring phrases that are reliably ironic"],
        "banter_vs_genuine_hostility_notes": "how to distinguish genuine anger from performative trash talk"
    }}
}}

MESSAGES TO ANALYZE:
{json.dumps(tone_pack, indent=2)}
"""

        result = self._call_flash(prompt, pass_name="tone_map")
        self._cache_set(cache_key, result)

        profile = result.get("subject_irony_profile", {})
        ironic  = sum(1 for t in result.get("tone_classifications", []) if t.get("is_ironic"))
        print(f"    -> {ironic} ironic msgs flagged | "
              f"Baseline: {profile.get('baseline_irony_frequency', '?')} | "
              f"Ratio: {profile.get('literal_vs_ironic_ratio', '?')}")
        return result

    # -- PRE-PASS B: Peak Performance Identification ---------------------------

    def identify_peak_performance(self, subject, cognitive_pack, tone_map):
        """
        Reads peak_candidates and message_chains and identifies the 25-35
        items that best represent the subject's cognitive CEILING.

        Critical: without this pass, the cognitive schema would score based on
        average/casual messages. This isolates only the highest-capability evidence.
        Chains are treated as single logical units -- the subject writing one thought
        across multiple rapid messages.
        """
        print(f"[Cortex] PRE-PASS B: Identifying cognitive ceiling for {subject}...")

        tone_str = json.dumps(tone_map, sort_keys=True)
        pack_str = json.dumps({
            "peak_candidates": cognitive_pack.get("peak_candidates", [])[:50],
            "message_chains":  cognitive_pack.get("message_chains", [])[:50],
        }, sort_keys=True)
        cache_key = self._make_key("cortex_peak", subject, pack_str, tone_str)
        cached    = self._cache_get(cache_key)
        if cached:
            gap = cached.get("peak_vs_floor_gap", "?")
            print(f"    -> Loaded from cache. Gap: {gap}")
            return cached

        irony_profile = tone_map.get("subject_irony_profile", {})

        prompt = f"""
You are identifying the 25-35 messages or message chains that best represent
the COGNITIVE CEILING of '{subject}' -- not their average register, not their floor.

IRONY CONTEXT (apply before evaluating):
- Baseline irony frequency: {irony_profile.get('baseline_irony_frequency', 'unknown')}
- Literal vs ironic ratio: {irony_profile.get('literal_vs_ironic_ratio', 'unknown')}
- Signature ironic phrases (never interpret literally): {irony_profile.get('signature_ironic_phrases', [])}
Do not penalize ironic messages -- evaluate their cognitive sophistication by their TRUE meaning.

SELECTION CRITERIA -- include items that demonstrate:
- Structured multi-step argument construction
- Complex vocabulary used correctly and precisely
- Abstract concept discussion (not just event reporting)
- Cross-domain connections or analogies
- Explaining something to someone else (highest signal -- requires understanding)
- Sophisticated humor requiring pattern recognition, timing, or wordplay
- Accurate prediction or strategic multi-step thinking
- Numerical reasoning or statistical interpretation

EXCLUDE regardless of length:
- One-word or two-word responses
- Pure insults with no analytical content
- Simple agreements or reactions ("lol", "facts", "exactly")
- Anything requiring no cognitive effort
- Ironic statements that ONLY function as dismissal (no underlying reasoning)

MESSAGE CHAINS: evaluate on combined_text as a single logical unit.
The subject is writing one thought across multiple rapid messages -- treat the
chain as equivalent to a paragraph. A chain showing building argument = high value.

Output pure valid JSON only, no markdown:
{{
    "peak_items": [
        {{
            "source": "individual_message or chain",
            "message_id": "id or chain index as string",
            "content": "full text or combined_text",
            "capability_signal": "specific cognitive skill demonstrated",
            "complexity_score": 8
        }}
    ],
    "capability_ceiling_summary": "2-3 sentence paragraph describing the highest cognitive register observed",
    "peak_vs_floor_gap": "large/medium/small",
    "gap_explanation": "why the gap is what it is -- what the casual register hides vs reveals about this person",
    "most_impressive_single_item": {{
        "content": "the single best message or chain text",
        "why": "what makes this the most impressive cognitive performance in the dataset"
    }}
}}

PEAK CANDIDATES:
{json.dumps(cognitive_pack.get('peak_candidates', []), indent=2)}

MESSAGE CHAINS:
{json.dumps(cognitive_pack.get('message_chains', []), indent=2)}
"""

        result    = self._call_flash(prompt, pass_name="peak_performance")
        self._cache_set(cache_key, result)

        n_items = len(result.get("peak_items", []))
        gap     = result.get("peak_vs_floor_gap", "?")
        print(f"    -> {n_items} peak items identified | Gap: {gap}")
        return result

    # -- PASS 1: Cognitive Schema Extraction -----------------------------------

    def extract_cognitive_signals(self, subject, cognitive_pack,
                                  peak_pass, tone_map):
        """
        Main cognitive assessment pass.
        Scores all dimensions using ONLY peak_items from pre-pass B.
        Uses broad_sample only for vocabulary range baseline.
        """
        print(f"[Cortex] PASS 1: Extracting cognitive signals for {subject}...")

        tone_str  = json.dumps(tone_map, sort_keys=True)
        peak_str  = json.dumps(peak_pass, sort_keys=True)
        broad_str = json.dumps(cognitive_pack.get("broad_sample", [])[:500], sort_keys=True)
        cache_key = self._make_key("cortex_cognitive", subject, peak_str, tone_str, broad_str)
        cached    = self._cache_get(cache_key)
        if cached:
            band = cached.get("composite", {}).get("verbal_iq_band_label", "?")
            grade = cached.get("speaking_level", {}).get("grade_level", "?")
            print(f"    -> Loaded from cache. IQ band: {band} | Speaking grade: {grade}")
            return cached

        irony_profile = tone_map.get("subject_irony_profile", {})

        prompt = f"""
You are performing a cognitive intelligence assessment of '{subject}' based on
their peak-register Discord messages. This is a verbal and crystallized intelligence
assessment only -- not a full IQ test. Frame all estimates accordingly.

CRITICAL SCORING RULES:
1. Score ALL dimensions using ONLY the peak_items list below -- never casual messages
2. Use broad_sample ONLY for vocabulary range baseline (not for scoring)
3. Chains are complete thoughts -- evaluate combined_text as a single unit
4. Never score based on banter, single reactions, or messages with no cognitive content
5. The irony profile below is ground truth -- ironic messages must be evaluated
   by their TRUE meaning, not surface meaning

IRONY CONTEXT:
- Baseline irony frequency: {irony_profile.get('baseline_irony_frequency', 'unknown')}
- Literal vs ironic ratio: {irony_profile.get('literal_vs_ironic_ratio', 'unknown')}
- Signature ironic phrases (always interpret by true meaning): {irony_profile.get('signature_ironic_phrases', [])}

PEAK PERFORMANCE DATA (score against these only):
{json.dumps(peak_pass.get('peak_items', []), indent=2)}

CAPABILITY CEILING SUMMARY FROM PRE-PASS:
{peak_pass.get('capability_ceiling_summary', '')}

BROAD SAMPLE (vocabulary baseline only, do not score):
{json.dumps(cognitive_pack.get('broad_sample', [])[:200], indent=2)}

Output pure valid JSON only, no markdown:
{{
    "verbal_intelligence": {{
        "vocabulary_range_score": 7,
        "vocabulary_notes": "specific observations -- unusual or domain-specific words that appear",
        "syntactic_complexity_score": 6,
        "abstraction_capacity_score": 8,
        "abstraction_notes": "can they discuss concepts not just events -- cite specific example",
        "analogical_reasoning_score": 7,
        "best_example": {{"id": "msg_id", "text": "quote", "why": "what it demonstrates"}}
    }},
    "crystallized_knowledge": {{
        "domain_breadth_score": 7,
        "domains_identified": {{"domain_name": 9}},
        "factual_accuracy_signals": "high/med/low",
        "cross_domain_connection_score": 6,
        "best_example": {{"id": "msg_id", "text": "quote", "why": "what it demonstrates"}}
    }},
    "reasoning_quality": {{
        "argument_construction_score": 7,
        "argument_notes": "do they build multi-step cases or just assert -- cite example",
        "evidence_usage_score": 5,
        "position_updating_score": 4,
        "position_notes": "do they ever change their mind with new evidence",
        "logical_consistency_score": 7,
        "best_example": {{"id": "msg_id", "text": "quote", "why": "what it demonstrates"}}
    }},
    "emotional_intelligence": {{
        "theory_of_mind_score": 6,
        "theory_notes": "do they model how others think and feel -- cite example",
        "social_calibration_score": 7,
        "empathy_signals_score": 4,
        "self_awareness_score": 5,
        "self_awareness_notes": "how accurately do they perceive themselves",
        "best_example": {{"id": "msg_id", "text": "quote", "why": "what it demonstrates"}}
    }},
    "cognitive_flexibility": {{
        "multi_perspective_score": 6,
        "register_range_score": 8,
        "register_notes": "distance between casual floor and formal ceiling",
        "novelty_engagement_score": 6,
        "humor_intelligence_score": 8,
        "humor_notes": "complexity, timing, and originality of humor construction",
        "best_example": {{"id": "msg_id", "text": "quote", "why": "what it demonstrates"}}
    }},
    "speaking_level": {{
        "grade_level": 10,
        "grade_label": "10th Grade",
        "grade_equivalent": "High School (Upper)",
        "flesch_kincaid_estimate": "Grade 10 equivalent",
        "reading_ease_estimate": "Standard (60-70)",
        "vocabulary_tier": "Tier 2 academic/domain vocabulary with Tier 3 domain-specific terms",
        "sentence_structure": "brief description of syntactic patterns in peak register",
        "casual_register_grade": 5,
        "casual_register_notes": "estimated grade level in casual/banter register",
        "gap_significance": "what the gap between peak and casual grade levels reveals"
    }},
    "composite": {{
        "verbal_iq_band_low": 115,
        "verbal_iq_band_high": 120,
        "verbal_iq_band_label": "115-120",
        "iq_confidence": "high/med/low",
        "iq_confidence_rationale": "why estimate is confident or hedged -- sample size, register variation, evidence quality",
        "iq_important_caveat": "This estimate reflects verbal and crystallized knowledge signals only. Fluid reasoning, spatial ability, and processing speed cannot be assessed from text.",
        "peak_vs_floor_gap": "large/medium/small",
        "primary_intelligence_type": "verbal/crystallized/social/strategic/humor",
        "most_impressive_feat": "single best example of high cognitive performance with message id",
        "overall_cognitive_summary": "2-3 sentence synthesis of the full cognitive profile"
    }}
}}
"""

        result    = self._call_flash(prompt, pass_name="cognitive_signals")
        self._cache_set(cache_key, result)

        band  = result.get("composite", {}).get("verbal_iq_band_label", "?")
        grade = result.get("speaking_level", {}).get("grade_level", "?")
        conf  = result.get("composite", {}).get("iq_confidence", "?")
        print(f"    -> IQ band: {band} ({conf} confidence) | Speaking grade: {grade}")
        return result

    # -- PASS 2: Fact Check Extraction -----------------------------------------

    def extract_fact_check_signals(self, subject, factcheck_pack,
                                   tone_map):
        """
        Scans medium/long messages for verifiable real-world factual claims.
        Scopes: sports stats, historical events, public figures, dates, science, law.
        Excludes: TSL-internal claims, biographical/personal claims, opinions.
        Rates accuracy based on Flash training knowledge with explicit confidence.
        """
        print(f"[Cortex] PASS 2: Extracting fact-check signals for {subject}...")

        tone_str = json.dumps(tone_map, sort_keys=True)
        pack_str = json.dumps(factcheck_pack, sort_keys=True)
        cache_key = self._make_key("cortex_factcheck", subject, pack_str, tone_str)
        cached    = self._cache_get(cache_key)
        if cached:
            n_claims = len(cached.get("verifiable_claims", []))
            acc = cached.get("overall_accuracy_assessment", {}).get("accuracy_rating", "?")
            print(f"    -> Loaded from cache. {n_claims} claims found | Rating: {acc}")
            return cached

        irony_profile = tone_map.get("subject_irony_profile", {})

        prompt = f"""
You are a fact-checker analyzing Discord messages from '{subject}' to identify
and evaluate real-world factual claims.

SCOPE -- only include claims in these categories:
  - Sports statistics, records, historical scores, player/team achievements
  - Historical events, dates, sequences (real-world history, not TSL history)
  - Public figures -- what they did, said, or achieved
  - Scientific facts, laws of physics, documented medical facts
  - Legal rules or regulations (real-world, not league rules)
  - Geographic or demographic facts
  - Financial or economic data (general, not personal finances)

EXCLUDE these entirely -- do not flag them:
  - TSL-internal claims ("I went 12-4 last season", "JT has 14 rings")
  - Personal/biographical claims ("I've been doing this 15 years")
  - Opinions, predictions, or preferences ("Brady is the GOAT")
  - Trash talk hyperbole ("he's never won anything")
  - Ironic or sarcastic statements -- check the irony context below first

IRONY CONTEXT -- check before flagging any claim:
- Baseline irony frequency: {irony_profile.get('baseline_irony_frequency', 'unknown')}
- Signature ironic phrases: {irony_profile.get('signature_ironic_phrases', [])}
If a claim appears in an ironic/sarcastic message, evaluate by its TRUE meaning.
An ironic claim stated for comedic effect is not a factual assertion -- skip it.

FOR EACH REAL-WORLD FACTUAL CLAIM:
- State the claim as made
- Assess accuracy: correct / mostly_correct / mostly_incorrect / incorrect / unverifiable
- Explain the correct information if the claim is wrong
- Note your confidence in the assessment (high/med/low)
- Flag if the claim was likely ironic and should not be taken literally

ACCURACY RATING DEFINITIONS:
  correct           = factually accurate as stated
  mostly_correct    = accurate in substance, minor error in detail
  mostly_incorrect  = core claim is wrong, minor element correct
  incorrect         = factually wrong
  unverifiable      = claim cannot be assessed from training knowledge

Output pure valid JSON only, no markdown:
{{
    "verifiable_claims": [
        {{
            "message_id": "id",
            "claim_as_stated": "exact claim made by subject",
            "claim_category": "sports/history/science/law/geography/public_figure/other",
            "accuracy_rating": "correct/mostly_correct/mostly_incorrect/incorrect/unverifiable",
            "correct_information": "what is actually true -- null if claim was correct",
            "confidence": "high/med/low",
            "confidence_rationale": "why you are or are not confident in this assessment",
            "is_ironic": false,
            "irony_note": "null or explanation if claim was ironic/performative"
        }}
    ],
    "overall_accuracy_assessment": {{
        "claims_found": 0,
        "correct_count": 0,
        "mostly_correct_count": 0,
        "incorrect_count": 0,
        "unverifiable_count": 0,
        "accuracy_rating": "high/med/low",
        "accuracy_percentage_estimate": "X% of verifiable claims were correct or mostly correct",
        "strongest_domain": "domain where subject is most accurate",
        "weakest_domain": "domain where subject makes most errors",
        "notable_patterns": "any patterns in how or when errors occur"
    }},
    "high_confidence_errors": [
        {{
            "claim": "the incorrect claim",
            "correct_fact": "what is actually true",
            "significance": "how material this error is"
        }}
    ]
}}

MESSAGES TO ANALYZE:
{json.dumps(factcheck_pack, indent=2)}
"""

        result    = self._call_flash(prompt, pass_name="fact_check")
        self._cache_set(cache_key, result)

        n_claims = len(result.get("verifiable_claims", []))
        acc      = result.get("overall_accuracy_assessment", {}).get("accuracy_rating", "?")
        pct      = result.get("overall_accuracy_assessment", {}).get("accuracy_percentage_estimate", "?")
        print(f"    -> {n_claims} verifiable claims found | Accuracy: {acc} ({pct})")
        return result

    # -- ORCHESTRATOR ----------------------------------------------------------

    def run_all_passes(self, subject: str, packs: dict) -> dict:
        """
        Run all four passes in sequence. Returns complete signals dict
        ready for cortex_writer.py.
        """
        print(f"\n{'='*55}")
        print(f"  ATLAS CORTEX -- SIGNAL EXTRACTION")
        print(f"  Subject: {subject}")
        print(f"{'='*55}")

        # PRE-PASS A: Tone map
        tone_map = self.build_tone_map(subject, packs["tone"])
        time.sleep(PASS_DELAY)

        # PRE-PASS B: Peak performance identification
        peak_pass = self.identify_peak_performance(subject, packs["cognitive"], tone_map)
        time.sleep(PASS_DELAY)

        # PASS 1: Cognitive schema
        cognitive = self.extract_cognitive_signals(
            subject, packs["cognitive"], peak_pass, tone_map
        )
        time.sleep(PASS_DELAY)

        # PASS 2: Fact check
        fact_check = self.extract_fact_check_signals(
            subject, packs["fact_check"], tone_map
        )

        print(f"\n[Cortex Analyst] All passes complete.")

        return {
            "tone_map":   tone_map,
            "peak_pass":  peak_pass,
            "cognitive":  cognitive,
            "fact_check": fact_check,
            "subject":    subject,
            "pack_stats": packs["cognitive"]["stats"],
        }
