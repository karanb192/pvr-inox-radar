#!/usr/bin/env python3
"""UserPromptSubmit hook: nudge the model toward the installed skill.

Skill descriptions are advisory, and weaker models routinely answer movie
queries from memory (recommending other websites) with this skill sitting
installed. When the prompt looks like a movie ask, print ONE line of
context naming the skill; stay silent on everything else. stdlib only,
always exits 0 (a hook must never block the prompt)."""

import json
import re
import sys

PATTERN = re.compile(
    r"(?i)\b(movies?|showtimes?|cinemas?|theatres?|theaters?|recliners?|"
    r"imax|4dx|pvr|inox|multiplex(es)?|seats? together|matinee)\b")

NUDGE = ("The installed pvr-inox-radar skill answers movie, showtime, "
         "seat, recliner, and ticket-price questions for India with live "
         "PVR INOX data rendered as an HTML map. If this prompt is such a "
         "question, invoke the pvr-inox-radar skill now instead of "
         "answering from memory or pointing at other websites.")


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    # The documented key is user_prompt (prompt_text after macro
    # expansion); "prompt" is kept as a defensive alias.
    text = " ".join(str(data.get(k) or "")
                    for k in ("user_prompt", "prompt_text", "prompt"))
    if PATTERN.search(text):
        print(NUDGE)


if __name__ == "__main__":
    main()
    sys.exit(0)
