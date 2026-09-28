# Review report contract

Write JSON with this shape:

```json
{
  "verdict": "pass | changes_required | user_decision_required",
  "scope": ["#/llm", "mobile"],
  "findings": [
    {
      "severity": "blocking | major | minor | suggestion",
      "role": "product | ux | ui | art | accessibility",
      "location": "route, section, viewport, or selector",
      "evidence": "What was observed in the artifact or screenshot",
      "expected": "What the user should experience",
      "recommendation": "The smallest actionable correction"
    }
  ],
  "checks": ["node verify.mjs", "desktop screenshot", "mobile screenshot"]
}
```

`pass` requires zero `blocking` and zero `major` findings. `user_decision_required` is appropriate for a genuine product or aesthetic trade-off, not for an uninvestigated defect.
