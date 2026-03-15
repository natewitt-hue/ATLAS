# HANDOFF: Patient Education Agent — Architecture & Next Steps

> **From:** Witt
> **Date:** 2026-03-15
> **Status:** Architecture blueprint — ready to build

---

## WHAT YOU HAVE

- A clean CSS component library (tiles, pills, zones, drug cards, BP scale, print layout)
- Static HTML rendering at 8.5in page width with print-ready page breaks
- A visual design language that works across conditions (color-coded severity, traffic-light categorization)

## WHAT YOU'RE BUILDING

An AI agent that reads a patient chart, identifies chronic conditions, and generates personalized education handouts automatically.

---

## ARCHITECTURE (4 Layers)

```
┌─────────────────────────────────────────────┐
│  LAYER 1: DATA IN                           │
│  Patient chart data (EHR export, manual     │
│  entry, or API pull). Structured JSON.      │
│  Fields: conditions[], meds[], vitals[],    │
│  demographics{}, recent_labs[]              │
├─────────────────────────────────────────────┤
│  LAYER 2: AGENT (Claude/Gemini)             │
│  - Scrubs chart for relevant conditions     │
│  - Selects which template components apply  │
│  - Personalizes content (reading level,     │
│    language, patient-specific med list)      │
│  - Outputs structured JSON config           │
├─────────────────────────────────────────────┤
│  LAYER 3: RENDERER                          │
│  - Jinja2 HTML templates (your CSS system)  │
│  - JSON config → populated handout          │
│  - WeasyPrint or Playwright → PDF           │
├─────────────────────────────────────────────┤
│  LAYER 4: DELIVERY                          │
│  - Print queue (front desk prints before    │
│    patient leaves)                           │
│  - Patient portal upload                    │
│  - SMS/email PDF link                       │
│  - Fax to referring provider                │
└─────────────────────────────────────────────┘
```

---

## IMMEDIATE NEXT STEPS (in order)

### Step 1: Templatize Your HTML

Install Jinja2 (`pip install jinja2`). Replace hardcoded content with template variables. Your CSS stays untouched — you're only swapping the content inside the tags.

**Before (static):**
```html
<div class="bn v">148/92</div>
```

**After (template):**
```html
<div class="bn v">{{ bp.systolic }}/{{ bp.diastolic }}</div>
```

### Step 2: Define Your Config Schema

Create a JSON structure the agent will output. This is the contract between your AI layer and your renderer:

```json
{
  "patient": {
    "name": "John D.",
    "age": 62,
    "reading_level": "standard"
  },
  "condition": "HTN",
  "bp_current": {
    "systolic": 148,
    "diastolic": 92
  },
  "stage": "stage2",
  "medications": [
    {
      "generic": "lisinopril",
      "brand": "Prinivil",
      "dose": "20mg daily",
      "class": "ACE Inhibitor",
      "color": "#4CAF50"
    },
    {
      "generic": "amlodipine",
      "brand": "Norvasc",
      "dose": "5mg daily",
      "class": "CCB",
      "color": "#1565C0"
    }
  ],
  "components": [
    "bp_scale",
    "med_cards",
    "traffic_pills",
    "emergency_zones"
  ],
  "lifestyle_flags": [
    "high_sodium",
    "sedentary",
    "smoker"
  ]
}
```

### Step 3: Build the Agent Prompt

Give your AI model the config schema as the required output format. Feed it the raw chart data.

**Example system prompt:**

```
You are a clinical education agent. Given a patient chart, output a JSON
config following the provided schema. Only include components relevant to
this patient's conditions and risk factors. Personalize medication cards
to their actual prescriptions. Adjust reading level based on patient
demographics. Cite AHA/ACC or JNC-8 guidelines where applicable.
```

The AI decides what's relevant — that's the whole point.

### Step 4: Wire the Renderer

Python script: agent outputs JSON → Jinja2 renders HTML → WeasyPrint converts to PDF.

```python
import json
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML

def render_handout(config_path: str, output_path: str):
    # Load agent output
    with open(config_path) as f:
        config = json.load(f)

    # Render HTML from template
    env = Environment(loader=FileSystemLoader("templates/"))
    template = env.get_template(f"{config['condition']}_handout.html")
    html_content = template.render(**config)

    # Convert to PDF
    HTML(string=html_content).write_pdf(output_path)
    print(f"Handout saved: {output_path}")
```

Three function calls. Done.

### Step 5: Repeat for Each Condition

Diabetes, CHF, COPD — same pipeline, different templates and component sets. Your CSS system already supports it since the color/layout primitives are condition-agnostic.

---

## AGENT EXPANSION (Your 3 Remaining Agents)

| Agent | Chart Fields It Reads | Key Components |
|-------|----------------------|----------------|
| **HTN** | BP readings, cardiac meds, sodium labs | BP scale, drug cards, traffic pills |
| **Diabetes** | A1C, glucose logs, insulin regimen, foot exam dates | Glucose range bar, med cards, meal planning tiles |
| **CHF** | Ejection fraction, BNP, daily weights, fluid status | Weight tracking zone, med cards, emergency zones |
| **Coordinator** | All of the above | Deduplicates meds, flags interactions, orders packet |

The **Coordinator agent** is the unlock — it takes all condition-specific outputs, merges them, removes duplicate med cards, flags drug-drug interactions, and produces a single coherent packet.

**Build order:** Single-condition agents first, then the coordinator.

---

## TOOLS TO USE

| Need | Tool | Install |
|------|------|---------|
| Templating | Jinja2 | `pip install jinja2` |
| HTML → PDF | WeasyPrint | `pip install weasyprint` |
| AI agent | Claude API or Gemini | You already have Gemini |
| Config validation | Pydantic | `pip install pydantic` |

**Why Pydantic:** Validates that the agent's JSON output actually matches your schema before it hits the renderer. Catches malformed output before it breaks your templates.

---

## SUGGESTED FILE STRUCTURE

```
patient-education-agent/
├── agents/
│   ├── htn_agent.py          # HTN-specific chart scrubber
│   ├── diabetes_agent.py     # Diabetes chart scrubber
│   ├── chf_agent.py          # CHF chart scrubber
│   └── coordinator.py        # Multi-condition merger
├── templates/
│   ├── base.html             # Shared CSS component library
│   ├── HTN_handout.html      # HTN Jinja2 template
│   ├── DM_handout.html       # Diabetes template
│   └── CHF_handout.html      # CHF template
├── schemas/
│   ├── config_schema.json    # Agent output contract
│   └── models.py             # Pydantic validation models
├── renderer.py               # JSON config → HTML → PDF
├── main.py                   # CLI entry point
├── requirements.txt
└── output/                   # Generated PDFs land here
```

---

## THE MOAT

The code isn't the moat. Anyone can build templates. Your edge is:

1. **You work in the field** and see the actual workflow problems
2. **You know which information patients actually need** vs. what generic handouts dump on them
3. **You can tune the agent prompts** with real clinical judgment
4. **You can test it on real patients** and iterate based on what actually helps

Ship the HTN handout end-to-end first. One condition, one agent, one PDF. Then scale.

---

## REFERENCE: Your Existing CSS Components

These are already built and ready to templatize:

| Component | CSS Class | Use Case |
|-----------|-----------|----------|
| Card Tiles | `.tile`, `.tiles` | Info blocks (2-col or 3-col grid) |
| Big Numbers | `.big-nums`, `.bn` | Hero stats (BP reading, A1C, etc.) |
| Traffic Pills | `.pills`, `.pill` | Go/Slow/No categorization |
| Alert Zones | `.zone` | Emergency/Urgent/OK triage |
| BP Scale Bar | `.bp-bar`, `.bp-seg` | Visual range indicator |
| Drug Cards | `.drug-pills`, `.dp` | Medication info cards |
| Pie Plate | `.mini-plate` | CSS-only circular diagram |
| Page Break | `.pg` | Print-ready 8.5in pages |
