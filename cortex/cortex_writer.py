"""
cortex_writer.py - ATLAS Cortex Writer
=======================================
Synthesizes three narrative report sections via Gemini 2.5 Pro.
Reads structured signals from cortex_analyst.py and produces a
standalone Cortex Intelligence Report.

Sections:
  I.   Cognitive Profile & Intelligence Indicators  (min 700 words)
  II.  Factual Accuracy Assessment                  (min 600 words)
  III. Subject Scorecard                            (structured + 200w prose)

Usage:
  from cortex_writer import CortexWriter
  writer = CortexWriter()
  report = writer.write_report(subject, signals)
  writer.save_markdown(report, filename)
"""

import os
import json
import time
from datetime import datetime
from google import genai
from google.genai import types
from tenacity import retry, stop_after_attempt, wait_exponential

PRO_MODEL  = "gemini-2.5-pro"
PRO_DELAY  = 4  # seconds between Pro calls


class CortexWriter:

    def __init__(self):
        self.client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=4, max=20))
    def _synthesize(self, subject, section_title,
                    signals, rubric, irony_profile=None):
        """Single Pro synthesis call for one report section."""
        print(f"[Cortex Writer] Synthesizing: {section_title}...")

        irony_context = ""
        if irony_profile:
            irony_context = f"""
IRONY AWARENESS (apply to ALL quote interpretation in this section):
- Irony baseline: {irony_profile.get('baseline_irony_frequency', 'unknown')} frequency
- Literal vs ironic ratio: {irony_profile.get('literal_vs_ironic_ratio', 'unknown')}
- Dominant irony type: {irony_profile.get('dominant_irony_type', 'unknown')}
- Signature ironic phrases (never interpret literally): {irony_profile.get('signature_ironic_phrases', [])}
When citing quotes, check whether they are ironic before drawing conclusions.
If a quote is ironic, interpret by its TRUE meaning and note it: [ID: 1234] [ironic -- dismissive sarcasm].
"""

        prompt = f"""
ROLE: Expert cognitive profiler writing one section of a formal intelligence assessment.
SUBJECT: {subject}
SECTION: {section_title}

TASK: Write a highly detailed, authoritative narrative section based STRICTLY on the
extracted signals below. This is a clinical intelligence report -- be precise, cite evidence,
and do not hedge unnecessarily.

{irony_context}

EXTRACTED SIGNALS:
{signals}

RUBRIC & REQUIREMENTS:
{rubric}

HARD RULES:
1. Meet the minimum word count. Non-negotiable.
2. Every major claim requires a quoted example and message ID in brackets.
3. Do not state a specific IQ number -- always use the 5-point band provided.
4. Frame IQ estimates as "verbal intelligence indicators suggest..." not "their IQ is..."
5. The IQ caveat must appear verbatim: "This estimate reflects verbal and crystallized
   knowledge signals only. Fluid reasoning, spatial ability, and processing speed
   cannot be assessed from text."
6. Dense academic prose. No introductory fluff. No bullet points in narrative sections.
7. If evidence is thin, flag explicitly with "(low confidence -- limited evidence)".
8. Vary sentence structure. Avoid repetitive phrasing.
"""

        response = self.client.models.generate_content(
            model=PRO_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=8192,
            ),
        )
        return f"## {section_title}\n\n{response.text}\n"

    def write_report(self, subject, signals):
        """
        Full three-section Cortex report.
        signals = output of CortexAnalyst.run_all_passes()
        """
        cognitive  = signals.get("cognitive", {})
        fact_check = signals.get("fact_check", {})
        peak_pass  = signals.get("peak_pass", {})
        tone_map   = signals.get("tone_map", {})
        pack_stats = signals.get("pack_stats", {})

        irony_profile = tone_map.get("subject_irony_profile", {}) if tone_map else {}
        ironic_count  = sum(
            1 for t in tone_map.get("tone_classifications", []) if t.get("is_ironic")
        ) if tone_map else 0

        # Report header
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        total_msgs = pack_stats.get("total_messages", 0)
        total_msgs_str = f"{total_msgs:,}" if isinstance(total_msgs, int) else str(total_msgs)
        long_count = pack_stats.get("long_count", "?")
        medium_count = pack_stats.get("medium_count", "?")
        chains_total = pack_stats.get("chains_total", "?")
        report   = [
            f"# ATLAS CORTEX -- INTELLIGENCE ASSESSMENT: {subject}\n",
            f"*Generated: {date_str}*\n",
            f"*Evidence base: {total_msgs_str} total messages | "
            f"{long_count} long | "
            f"{medium_count} medium | "
            f"{chains_total} chains detected*\n",
            f"*Irony pre-pass: {ironic_count} messages flagged | "
            f"Baseline: {irony_profile.get('baseline_irony_frequency', 'unknown')} frequency*\n",
            f"*Peak items identified: {len(peak_pass.get('peak_items', []))} | "
            f"Peak-vs-floor gap: {peak_pass.get('peak_vs_floor_gap', 'unknown')}*\n",
            "---\n",
        ]

        # -- SECTION I: Cognitive Profile --------------------------------------
        sec1_signals = {
            "cognitive_signals":         cognitive,
            "peak_performance_pre_pass": peak_pass,
            "irony_profile":             irony_profile,
        }
        sec1_rubric = f"""
Minimum 700 words. Write a comprehensive cognitive profile.

Lead with the subject's PRIMARY intelligence type and estimated verbal IQ band
({cognitive.get('composite', {}).get('verbal_iq_band_label', 'see signals')})
with explicit confidence level and rationale.

Cover each dimension in flowing prose with evidence and quoted examples:

1. VERBAL INTELLIGENCE: vocabulary range relative to Discord context, syntactic
   complexity, abstraction capacity. Does their language reveal conceptual thinking
   or just event reporting? Cite the best example quote with ID.

2. CRYSTALLIZED KNOWLEDGE: what do they actually know well? Where does expertise
   appear genuine vs surface-level? Domain depth scores with evidence.
   Cross-domain connection ability. Cite best example.

3. REASONING QUALITY: do they reason or assert? Evidence usage, position updating
   (do they ever change their mind?), logical consistency. Cite best example.

4. EMOTIONAL INTELLIGENCE: theory of mind, social calibration accuracy,
   self-awareness gap -- how they see themselves vs behavioral evidence.

5. COGNITIVE FLEXIBILITY: register range (casual floor to formal ceiling -- how big
   is the gap and what does it reveal?), humor intelligence, novelty engagement.

6. SPEAKING LEVEL: state the grade level estimate
   ({cognitive.get('speaking_level', {}).get('grade_level', '?')} --
   {cognitive.get('speaking_level', {}).get('grade_equivalent', '')}),
   what it means in plain terms, and note that casual register scores
   {cognitive.get('speaking_level', {}).get('casual_register_grade', '?')} grades lower.
   Explain what that gap reveals.

7. COMPOSITE ASSESSMENT: state the 5-point verbal IQ band with confidence.
   Describe the peak-vs-floor gap finding. State the single most impressive
   cognitive performance in the dataset with the quote and ID.
   Include the required caveat verbatim.

Scores inline in prose, never as a table or bullet list.
"""

        report.append(
            self._synthesize(subject, "I. COGNITIVE PROFILE & INTELLIGENCE INDICATORS",
                             sec1_signals, sec1_rubric, irony_profile)
        )
        time.sleep(PRO_DELAY)

        # -- SECTION II: Factual Accuracy Assessment ---------------------------
        accuracy_overall = fact_check.get("overall_accuracy_assessment", {})
        n_claims  = accuracy_overall.get("claims_found", 0)
        acc_pct   = accuracy_overall.get("accuracy_percentage_estimate", "unknown")

        sec2_signals = {
            "fact_check_signals": fact_check,
            "irony_profile":      irony_profile,
        }
        sec2_rubric = f"""
Minimum 600 words. Write a factual accuracy assessment.

Context: {n_claims} verifiable real-world claims were identified across the message archive.
Overall accuracy: {acc_pct}

Cover the following in flowing prose:

1. OVERALL ACCURACY PROFILE: What is the subject's general relationship with factual
   accuracy? Are they careful about citing facts, or do they assert confidently with
   frequent errors? Note the overall accuracy percentage and what it means.

2. DOMAIN ANALYSIS: In which domains does the subject show strong factual accuracy?
   In which domains do errors cluster? For each notable domain (strong or weak),
   cite specific examples with message IDs.

3. HIGH-CONFIDENCE ERRORS: Detail each high-confidence factual error found.
   State what the subject claimed, what is actually correct, and whether the error
   pattern reveals overconfidence, domain-specific gaps, or carelessness.

4. IRONY FILTERING NOTE: Note how many claims were screened as potentially ironic
   and therefore excluded from accuracy assessment. This matters for interpretation --
   a high-irony communicator may appear to make more factual errors than they actually do.

5. CONFIDENCE CALIBRATION: Does the subject's stated confidence match their accuracy?
   Do they hedge when they should, or do they assert with equal confidence regardless
   of whether they are correct?

6. ANALYTICAL NOTES: Flag where accuracy assessment was limited by training knowledge
   gaps. Note claims rated "unverifiable" and why. Be explicit about the limits
   of this assessment.

Never rate TSL-internal claims, biographical claims, or ironic statements as errors.
Scope is real-world verifiable claims only.
"""

        report.append(
            self._synthesize(subject, "II. FACTUAL ACCURACY ASSESSMENT",
                             sec2_signals, sec2_rubric, irony_profile)
        )
        time.sleep(PRO_DELAY)

        # -- SECTION III: Scorecard --------------------------------------------
        comp      = cognitive.get("composite", {})
        spk       = cognitive.get("speaking_level", {})
        vi        = cognitive.get("verbal_intelligence", {})
        ck        = cognitive.get("crystallized_knowledge", {})
        rq        = cognitive.get("reasoning_quality", {})
        ei        = cognitive.get("emotional_intelligence", {})
        cf        = cognitive.get("cognitive_flexibility", {})

        sec3_signals = {
            "cognitive":  cognitive,
            "fact_check": fact_check,
            "peak_pass":  peak_pass,
        }
        sec3_rubric = f"""
This is the Cortex scorecard. Structure is required here -- it is a reference artifact.

Write a 3-4 sentence OPENING PARAGRAPH that captures the subject in plain language --
the paragraph someone would read first if they had 30 seconds. Make it specific and
direct, not generic.

Then present the full scorecard:

COGNITIVE SCORES
Verbal Intelligence:         {vi.get('vocabulary_range_score', '?')}/10
  - Vocabulary Range:        {vi.get('vocabulary_range_score', '?')}/10
  - Syntactic Complexity:    {vi.get('syntactic_complexity_score', '?')}/10
  - Abstraction Capacity:    {vi.get('abstraction_capacity_score', '?')}/10
  - Analogical Reasoning:    {vi.get('analogical_reasoning_score', '?')}/10
Crystallized Knowledge:      {ck.get('domain_breadth_score', '?')}/10
  - Domain Breadth:          {ck.get('domain_breadth_score', '?')}/10
  - Cross-Domain Connections:{ck.get('cross_domain_connection_score', '?')}/10
Reasoning Quality:           {rq.get('argument_construction_score', '?')}/10
  - Argument Construction:   {rq.get('argument_construction_score', '?')}/10
  - Evidence Usage:          {rq.get('evidence_usage_score', '?')}/10
  - Position Updating:       {rq.get('position_updating_score', '?')}/10
  - Logical Consistency:     {rq.get('logical_consistency_score', '?')}/10
Emotional Intelligence:      {ei.get('theory_of_mind_score', '?')}/10
  - Theory of Mind:          {ei.get('theory_of_mind_score', '?')}/10
  - Social Calibration:      {ei.get('social_calibration_score', '?')}/10
  - Self-Awareness:          {ei.get('self_awareness_score', '?')}/10
Cognitive Flexibility:       {cf.get('register_range_score', '?')}/10
  - Register Range:          {cf.get('register_range_score', '?')}/10
  - Humor Intelligence:      {cf.get('humor_intelligence_score', '?')}/10
  - Novelty Engagement:      {cf.get('novelty_engagement_score', '?')}/10

SPEAKING LEVEL
  Peak Register:             Grade {spk.get('grade_level', '?')} ({spk.get('grade_equivalent', '?')})
  Casual Register:           Grade {spk.get('casual_register_grade', '?')} (estimated)
  Register Gap:              {int(spk.get('grade_level', 0) or 0) - int(spk.get('casual_register_grade', 0) or 0)} grades

IQ ESTIMATE
  Verbal IQ Band:            {comp.get('verbal_iq_band_label', '?')}
  Confidence:                {comp.get('iq_confidence', '?')}
  Primary Intelligence Type: {comp.get('primary_intelligence_type', '?')}
  Note: Verbal and crystallized knowledge signals only.

FACTUAL ACCURACY
  Claims Identified:         {accuracy_overall.get('claims_found', '?')}
  Accuracy Rating:           {accuracy_overall.get('accuracy_rating', '?')}
  Accuracy Estimate:         {accuracy_overall.get('accuracy_percentage_estimate', '?')}
  Strongest Domain:          {accuracy_overall.get('strongest_domain', '?')}
  Weakest Domain:            {accuracy_overall.get('weakest_domain', '?')}

PEAK PERFORMANCE
  Peak-vs-Floor Gap:         {peak_pass.get('peak_vs_floor_gap', '?')}
  Gap Explanation:           [summarize in one sentence]

End with a 2-sentence ANALYST NOTE on the most analytically significant finding
in this report -- the one thing that most changes how you would interpret this person.
"""

        report.append(
            self._synthesize(subject, "III. SUBJECT SCORECARD",
                             sec3_signals, sec3_rubric, irony_profile)
        )

        return "\n".join(report)

    def save_markdown(self, text, filename):
        """Save report to output/cortex/ folder."""
        out_dir = os.path.join("output", "cortex")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def save_json_signals(self, signals, filename):
        """Save raw JSON signals to output/cortex/ for debugging."""
        out_dir = os.path.join("output", "cortex")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(signals, f, indent=2, default=str)
        return path
