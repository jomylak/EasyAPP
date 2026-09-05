"""Deterministic resume-track routing: pick which resume variant fits a job.

No LLM call. Picking between three static resumes does not need a model --
the scorer already extracted `keywords` from the posting, and the title is
the employer's own label for the role. A regex router is free, runs on every
job, and is debuggable after the fact: you can read a title and predict which
resume it gets, which you cannot do with a learned or LLM-based scorer.

Two design rules do the real work here:

1. **Only discriminative terms.** Every `keywords` string in the DB begins
   with "Python", and Git/SQL/Linux/AWS appear across all three tracks. Terms
   like those add a constant hit to every bucket and cancel out, so they are
   excluded entirely rather than listed everywhere (see NON_DISCRIMINATIVE).
2. **Cascade by signal quality, don't pool it.** Title is checked alone
   first, then keywords, then the description. Pooling them would let a
   description that says "AI" once outvote a title that says "Frontend
   Engineer" three times.
"""

import re

# Track names -- these compose with a grad year into a resume_variants key
# in settings.json (e.g. "aiml" + "2027" -> "aiml_2027").
SWE = "swe"
AIML = "aiml"
DATA = "data"

# Checked in this order; the first track to match wins. This is the user's
# "upward chain of specificity": a posting that mentions data work is Data,
# unless it also talks about building production software (SWE), unless it
# also talks about AI/ML (AI/ML). SWE is deliberately *not* last despite
# being the fallback -- see the note on FALLBACK below.
PRECEDENCE = (AIML, SWE, DATA)

# The default when nothing matches. ~33% of titles hit no track at all
# ("Technical Intern 4", "Cloud Operations Intern", "Engineering Co-Op"),
# and SWE is both the closest fit for those and the strongest material.
FALLBACK = SWE

# Terms deliberately NOT in any track list. Each of these appears in postings
# for all three tracks, so including them would add noise to every bucket
# equally while making the lists look more thorough than they are.
NON_DISCRIMINATIVE = frozenset({
    "python", "java", "javascript", "typescript", "sql", "bash", "git",
    "linux", "docker", "aws", "azure", "gcp", "google cloud", "cloud",
    "algorithms", "data structures", "debugging", "unit test", "testing",
    "agile", "scrum", "object oriented", "rest", "api", "ci/cd",
})

_TRACK_TERMS: dict[str, tuple[str, ...]] = {
    AIML: (
        "artificial intelligence", "machine learning",
        "mlops", "deep learning", "neural network", "transformer",
        "large language model", r"\bllm", "genai", "generative ai", "agentic",
        "ai agent", r"\brag\b", "retrieval augmented", "retrieval-augmented",
        "computer vision", r"\bnlp\b", "natural language",
        "pytorch", "tensorflow", "scikit", "xgboost", "hugging face",
        "model training", "foundation model", "fine-tun", "fine tun",
        "embedding", "vector database", "prompt engineer", "recommendation system",
    ),
    DATA: (
        "data scien", "data analy", "data engineer", "data analytics",
        "analytics", "business intelligence", r"\betl\b", r"\belt\b",
        "data warehouse", "data pipeline", "data model", "data integration",
        "data migration", "tableau", "power bi", "looker",
        r"\bspark\b", "pyspark", "hadoop", "airflow", r"\bdbt\b",
        "snowflake", "redshift", "bigquery", "statistical analysis",
        r"a/b test", "quantitative analy",
    ),
    SWE: (
        "software engineer", "software develop", "software development",
        "full-stack", "full stack", "fullstack", "backend", "back-end",
        "back end", "frontend", "front-end", "front end", "web develop",
        "application develop", "microservice", "distributed system",
        "rest api", "restful", "api develop", "mobile develop",
        r"\bios\b", "android", r"\breact\b", "node.js", "angular", "vue",
        "django", "spring boot", "kubernetes", "devops",
        "site reliability", "platform engineer", "embedded",
    ),
}

# Terms that are decisive in a *title* but not anywhere else. A title is short
# and curated, so "AI" in one is a real claim about the role. A 30-item
# `keywords` list is not: bare "AI" fired 38 times across the tailorable set --
# more than every genuine AI term combined (machine learning 9, LLM 2, agentic
# 3) -- because postings list "AI tools" as boilerplate. Trusting it there sent
# "IoT Engineer" and "Technical Intern 3" to the AI/ML resume. Same story for
# "dashboard", which is an ordinary thing for a SWE intern to build.
_WEAK_TERMS: dict[str, tuple[str, ...]] = {
    AIML: (r"\bai\b", r"\ba\.i\.", r"\bml\b"),
    DATA: ("dashboard",),
    SWE: (),
}

# Compiled once at import. Terms without a regex metacharacter get wrapped in
# word boundaries so "spark" doesn't match "sparkling" and "ios" doesn't match
# "curious"; terms that already carry their own anchors are used verbatim.
def _compile(terms: tuple[str, ...]) -> re.Pattern | None:
    if not terms:
        return None
    return re.compile(
        "|".join(t if re.search(r"[\\\[\](){}|+*?]", t) else re.escape(t)
                 for t in terms),
        re.IGNORECASE,
    )


_PATTERNS = {t: _compile(v) for t, v in _TRACK_TERMS.items()}
_WEAK_PATTERNS = {t: _compile(_WEAK_TERMS.get(t, ())) for t in _TRACK_TERMS}


def _matching_tracks(text: str, *, weak_ok: bool = False) -> list[str]:
    """Every track whose vocabulary appears in `text`, in precedence order.

    weak_ok admits the _WEAK_TERMS tier as well, and is set only for titles.
    """
    if not text:
        return []
    out = []
    for t in PRECEDENCE:
        pat, weak = _PATTERNS[t], _WEAK_PATTERNS[t]
        if (pat and pat.search(text)) or (weak_ok and weak and weak.search(text)):
            out.append(t)
    return out


def route_resume_track(job: dict) -> str:
    """Pick the resume track ("swe" | "aiml" | "data") for a job.

    Cascades through the available signals in order of how much each one can
    be trusted, taking the first that matches anything at all:

    1. `title` -- the employer's own one-line label for the role.
    2. `keywords` -- what the scorer pulled out of the posting.
    3. `full_description` -- last resort; noisy, since a SWE posting can
       mention AI once in a boilerplate "about us" paragraph.

    Falls back to SWE when nothing matches, which covers the ~33% of titles
    that hit no track ("Technical Intern 4", "Cloud Operations Intern").

    Returns the track only -- the caller composes it with a grad year.
    """
    for field in ("title", "keywords", "full_description"):
        matches = _matching_tracks(job.get(field) or "", weak_ok=(field == "title"))
        if matches:
            return matches[0]
    return FALLBACK


def explain_route(job: dict) -> dict:
    """Same decision as route_resume_track, plus why -- for auditing misroutes.

    Returns {"track", "matched_on", "candidates"}, where matched_on is the
    field that decided it (or "fallback") and candidates lists every track
    that matched that field, in precedence order.
    """
    for field in ("title", "keywords", "full_description"):
        matches = _matching_tracks(job.get(field) or "", weak_ok=(field == "title"))
        if matches:
            return {"track": matches[0], "matched_on": field, "candidates": matches}
    return {"track": FALLBACK, "matched_on": "fallback", "candidates": []}
