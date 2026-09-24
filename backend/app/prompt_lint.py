"""Heuristic prompt linter (AGENT_DEV_SPEEDUP 4.4).

Runs when an instruction file or persona is created/updated. Warnings are
advisory (returned as `lint_warnings` on the API response); they never block
a save. Rules: cost bloat, empty prompt, references to non-existent skills,
and common self-contradicting directive pairs.
"""
import re

from .db import query

# ~2000 tokens at the usual ~4 chars/token heuristic. This text is injected
# into EVERY LLM call for the role, so size is recurring cost.
LARGE_CHARS = 4000
HUGE_CHARS = 8000

# Quotes skill names the way instruction authors write them:
#   use the `Code Review` skill / skill named 'Test Authoring'
_SKILL_REF = re.compile(
    r"(?:skill|skills)\s+(?:named\s+|called\s+)?(?:to\s+)?['\"`]([A-Za-z][A-Za-z0-9 _.-]{2,50})['\"`]",
    re.I)
# Also catch backticked words adjacent to the word "skill": skill `X`
_SKILL_REF2 = re.compile(r"skill\s+`([A-Za-z][A-Za-z0-9 _.-]{2,50})`", re.I)

# Pairs of directives that pull in opposite directions when both appear.
_CONTRADICTIONS = [
    (("approve quickly", "approve fast", "ship fast", "be brief", "minimal review"),
     ("be thorough", "exhaustive", "leave nothing unchecked", "double-check everything",
      "test every path")),
    (("never ask", "do not ask", "without asking the user"),
     ("ask for clarification", "request confirmation from the user")),
]


def lint_prompt(text: str) -> list[str]:
    """Return human-readable advisory warnings for one prompt/instruction body."""
    warnings: list[str] = []
    body = (text or "").strip()
    if not body:
        return warnings
    n = len(body)
    if n > HUGE_CHARS:
        warnings.append(
            f"Prompt is {n} chars (~{n // 4} tokens). It is injected into every "
            "LLM call for this role — trim it to control recurring cost.")
    elif n > LARGE_CHARS:
        warnings.append(
            f"Prompt is {n} chars (~{n // 4} tokens) and growing; keep it focused.")
    refs = {m.strip().lower() for m in
            _SKILL_REF.findall(body) + _SKILL_REF2.findall(body)}
    if refs:
        known = {(r["name"] or "").strip().lower() for r in query("SELECT name FROM skills")}
        for name in sorted(refs):
            if name not in known:
                warnings.append(
                    f"References skill '{name}' which does not exist in the skill catalog.")
    low = body.lower()
    for push, pull in _CONTRADICTIONS:
        a = next((p for p in push if p in low), None)
        b = next((p for p in pull if p in low), None)
        if a and b:
            warnings.append(
                f"Possible contradiction: '{a}' and '{b}' appear in the same prompt.")
    return warnings
