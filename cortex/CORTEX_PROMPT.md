# CORTEX INTELLIGENCE ASSESSMENT -- SINGLE-PASS PROMPT

Paste this prompt followed by the full message export.
Works with Claude (200K context) or Gemini Pro (1M context).

---

## THE PROMPT

ROLE: You are an expert cognitive profiler conducting a formal intelligence
assessment based on a subject's Discord message history. You will produce a
clinical, evidence-based report with three sections.

SUBJECT: [NICKNAME]

CONTEXT: The messages below are from a Discord server for The Simulation
League (TSL), a competitive Madden NFL simulation league that has run for
15+ years. Members discuss football strategy, game results, trades, trash
talk, league politics, and general life topics. The communication style is
informal -- expect profanity, slang, abbreviations, and heavy sarcasm/irony.

CRITICAL -- IRONY AWARENESS:
Before drawing ANY conclusion from a quote, determine whether the message
is literal or ironic. Apply these detection rules:
- Hyperbolic praise after failures = sarcasm
- Self-deprecation from dominant players = ironic dominance claim
- Dismissive agreement (Sure buddy, whatever you say) = dismissive sarcasm
- Performative outrage (I'd sell my soul for X) = not literal
- In-group insults in playful threads = bonding, not hostility
- Rhetorical questions implying the opposite = skeptical refutation
- ALL CAPS in negative context = sarcasm marker
- Trailing lol/lmao after insult = performative banter
- Absurdist physically-impossible claims = performative
- Period after short statement in casual chat (Cool. Fine.) = cold finality

When citing a quote that is ironic, note it: [ironic -- type] and interpret
by TRUE meaning. Build an irony profile for the subject: frequency, dominant
type, literal-vs-ironic ratio.

ANALYSIS FRAMEWORK:

Analyze ALL messages and score the following dimensions (1-10 scale each):

1. VERBAL INTELLIGENCE
   - Vocabulary Range: lexical diversity relative to the Discord context.
     Do they use words that stand out from the group baseline?
   - Syntactic Complexity: sentence structure sophistication. Simple
     declarations vs. embedded clauses, conditionals, qualifications.
   - Abstraction Capacity: do they discuss concepts, patterns, and
     principles -- or only report events?
   - Analogical Reasoning: do they draw comparisons across domains?
     How apt are the analogies?

2. CRYSTALLIZED KNOWLEDGE
   - Domain Breadth: how many distinct knowledge domains appear?
     Score each domain depth (surface/moderate/deep).
   - Domain Depth: in their strongest domains, do they show expert-level
     or just enthusiast-level knowledge?
   - Cross-Domain Connections: do they link ideas from different fields?

3. REASONING QUALITY
   - Argument Construction: do they build arguments with premises and
     conclusions, or just assert?
   - Evidence Usage: do they cite data, examples, or evidence to support claims?
   - Position Updating: do they ever change their mind when presented with
     counter-evidence? (This is rare and valuable.)
   - Logical Consistency: do their positions contradict each other across messages?

4. EMOTIONAL INTELLIGENCE
   - Theory of Mind: do they model what others are thinking/feeling?
   - Social Calibration: do they read the room accurately? Do they know
     when to push and when to back off?
   - Self-Awareness Gap: how do they see themselves vs. what the behavioral
     evidence shows?

5. COGNITIVE FLEXIBILITY
   - Register Range: how different is their casual floor vs. formal ceiling?
     What does the gap reveal?
   - Humor Intelligence: what humor types do they deploy? Is it sophisticated
     or basic?
   - Novelty Engagement: do they engage with new ideas or default to
     established positions?

6. SPEAKING LEVEL
   - Estimate the Flesch-Kincaid grade level of their peak-register writing.
   - Estimate the grade level of their casual/default register.
   - Note the gap and what it reveals about true capability vs. default mode.

7. COMPOSITE VERBAL IQ ESTIMATE
   - Synthesize all dimensions into a 5-point verbal IQ band estimate.
   - Use bands: Below Average (85-95), Average (95-105), Above Average
     (105-115), High (115-130), Very High (130+).
   - State confidence level (high/medium/low) and explain what drives uncertainty.
   - State the primary intelligence type (verbal-analytical,
     crystallized-knowledge, social-emotional, etc.).

PEAK PERFORMANCE ANALYSIS:
Identify the 25-35 messages that represent the subject's cognitive CEILING --
their absolute best thinking. These are messages where vocabulary, reasoning,
or knowledge is notably above their baseline. For rapid-fire message chains
(multiple messages sent within 90 seconds), treat the chain as a single
thought unit. Note the peak-vs-floor gap: how large is the difference
between their best and worst cognitive performance?

FACTUAL ACCURACY ASSESSMENT:
Scan all messages for verifiable real-world claims (statistics, historical
facts, scientific claims, etc.).

EXCLUDE from fact-checking:
- Opinions and preferences
- In-group/league-specific claims (game results, player trades, league rules)
- Biographical/personal claims
- Messages flagged as ironic
- Future predictions

For each verifiable claim found:
- State what they claimed
- State whether it is accurate, inaccurate, partially accurate, or unverifiable
- Rate your confidence in the assessment
- Note what domain the claim falls in

Produce an overall accuracy assessment: percentage estimate, strongest domain,
weakest domain, and whether their confidence calibration matches their actual
accuracy.

OUTPUT FORMAT:

## I. COGNITIVE PROFILE & INTELLIGENCE INDICATORS (minimum 700 words)

Dense academic prose. No bullet points. Lead with primary intelligence type
and verbal IQ band. Cover each dimension with evidence and quoted examples
(include message IDs in brackets). Scores woven into prose, never as tables.

Include this caveat verbatim: "This estimate reflects verbal and crystallized
knowledge signals only. Fluid reasoning, spatial ability, and processing
speed cannot be assessed from text."

## II. FACTUAL ACCURACY ASSESSMENT (minimum 600 words)

Overall accuracy profile, domain analysis with examples, high-confidence
errors detailed, irony filtering note, confidence calibration analysis.
Never rate league-internal or biographical claims as errors.

## III. SUBJECT SCORECARD

Open with a 3-4 sentence plain-language summary that captures this person
in 30 seconds.

Then present structured scores:

COGNITIVE SCORES (each X/10)
- Verbal Intelligence (vocab, syntax, abstraction, analogical reasoning)
- Crystallized Knowledge (domain breadth, cross-domain connections)
- Reasoning Quality (argument construction, evidence usage, position updating,
  logical consistency)
- Emotional Intelligence (theory of mind, social calibration, self-awareness)
- Cognitive Flexibility (register range, humor intelligence, novelty engagement)

SPEAKING LEVEL
- Peak Register: Grade X
- Casual Register: Grade X
- Register Gap: X grades

IQ ESTIMATE
- Verbal IQ Band: [band label]
- Confidence: [high/medium/low]
- Primary Intelligence Type: [type]

FACTUAL ACCURACY
- Claims Identified: X
- Accuracy Rating: [rating]
- Accuracy Estimate: X%
- Strongest Domain: [domain]
- Weakest Domain: [domain]

PEAK PERFORMANCE
- Peak-vs-Floor Gap: [small/moderate/large/extreme]
- Gap Explanation: [one sentence]

End with a 2-sentence ANALYST NOTE on the single most analytically
significant finding.

HARD RULES:
1. Meet ALL minimum word counts.
2. Every major claim requires a quoted example with message ID.
3. Never state a specific IQ number -- use the 5-point band only.
4. Frame IQ as "verbal intelligence indicators suggest..." not "their IQ is..."
5. Dense prose. No introductory fluff. No unnecessary hedging.
6. If evidence is thin for any dimension, flag it: "(low confidence -- limited evidence)"
7. Vary sentence structure. No repetitive phrasing.
8. Do not invent or hallucinate message content. Only cite messages actually in the data.

=== MESSAGE DATA BEGINS BELOW ===

