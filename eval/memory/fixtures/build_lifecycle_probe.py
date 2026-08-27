"""Regenerate ``lifecycle_probe.json`` — the Phase-4 supersede regression set.

The committed ``lifecycle_probe.json`` is the artifact the runner ingests; this
script is its human-readable source. Run it after editing a case::

    python -m eval.memory.fixtures.build_lifecycle_probe

Where ``contradiction_probe.json`` only tests one verdict (UPDATE) by asking
"did the stale value stop being read", this set exercises every lifecycle
outcome and scores the *bi-temporal state itself* (``eval/memory/lifecycle.py``):
after ingest, which rows are valid (``valid_to IS NULL``) and which are retired.

The four ingestion verdicts collapse into two outcome classes for scoring:

- **should-retire** (UPDATE / chained UPDATE): the old value must end up in a
  retired row and the new value valid. ``expect_retired`` is populated.
- **should-preserve** (ADD / coexist, MERGE-additive, NOOP, false-contradiction):
  every value must stay valid and *nothing* may be wrongly retired. This is the
  over-retiring guard the contradiction probe cannot see — there, every case
  *wants* something retired, so a trigger-happy verdict scores perfectly.

Each instance is LongMemEval schema (the runner ingests it unchanged) plus a
``lifecycle`` block the runner ignores and ``lifecycle.py`` reads:

    "lifecycle": {"category": ..., "expect_valid": [...],
                  "expect_retired": [...], "expect_unique": [...]}

UPDATE/chain cases also carry the ``probe`` block so ``contradiction.py`` still
scores their retrieval/reader view. Substrings are distinctive (proper nouns,
specific values) so they survive extraction's summarisation, and within a case
no value is a substring of another. Cross-case collisions are irrelevant — each
question is its own project scope (``project_uuid(run_id, question_id)``).

Update sessions deliberately state the NEW fact plainly without echoing the old
value: retirement comes from the verdict comparing embeddings of neighbouring
facts, not from parsing the text, and not echoing keeps ``expect_retired``'s
"retired and not also in a valid row" check clean.
"""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "lifecycle_probe.json"

# Substring-safe generic noise — none of these strings collide with a target
# value, so a filler can never satisfy or break a case's expectations.
FILLERS = [
    [
        ("user", "Any tips for sleeping better during hot summer nights?"),
        (
            "assistant",
            "Keep the blinds closed during the day and use breathable cotton bedding.",
        ),
    ],
    [
        ("user", "What's a good beginner recipe for sourdough bread?"),
        ("assistant", "Start with a simple no-knead loaf and a well-fed starter."),
    ],
    [
        ("user", "How do I keep houseplants alive while I'm travelling?"),
        (
            "assistant",
            "Self-watering globes and moving them out of direct sun both help.",
        ),
    ],
    [
        ("user", "Recommend a podcast about history."),
        (
            "assistant",
            "A narrative-history show with short, themed episodes is a good start.",
        ),
    ],
]


def _turns(pairs):
    return [{"role": role, "content": content} for role, content in pairs]


def case(
    qid,
    qtype,
    question,
    answer,
    sessions,
    lifecycle,
    probe=None,
    answer_tags=None,
):
    """Assemble one LongMemEval instance from a compact spec.

    ``sessions`` is a list of ``(date, tag, [(role, content), ...])``. The tag
    becomes the session id suffix (``<qid>_<tag>``); pass tag ``None`` to splice
    in the next round-robin filler. ``answer_tags`` lists the tags that hold the
    evidence (defaults to every non-filler tag).
    """
    sids, dates, haystack, tag_to_sid = [], [], [], {}
    fill = 0
    for date, tag, pairs in sessions:
        if tag is None:
            pairs = FILLERS[fill % len(FILLERS)]
            tag = f"filler{fill}"
            fill += 1
        sid = f"{qid}_{tag}"
        tag_to_sid[tag] = sid
        sids.append(sid)
        dates.append(date)
        haystack.append(_turns(pairs))

    if answer_tags is None:
        answer_tags = [t for _, t, _ in sessions if t is not None]
    answer_sids = [tag_to_sid[t] for t in answer_tags]

    inst = {
        "question_id": qid,
        "question_type": qtype,
        "question": question,
        "answer": answer,
        "question_date": dates[-1].split()[0] + " (later)",
        "haystack_session_ids": sids,
        "haystack_dates": dates,
        "haystack_sessions": haystack,
        "answer_session_ids": answer_sids,
        "lifecycle": lifecycle,
    }
    if probe is not None:
        # contradiction.py resolves session roles by id, not tag.
        probe = dict(probe)
        probe["update_session_id"] = tag_to_sid[probe["update_session_id"]]
        probe["original_session_id"] = tag_to_sid[probe["original_session_id"]]
        inst["probe"] = probe
    return inst


def lc(category, expect_valid=(), expect_retired=(), expect_unique=()):
    return {
        "category": category,
        "expect_valid": list(expect_valid),
        "expect_retired": list(expect_retired),
        "expect_unique": list(expect_unique),
    }


CASES = [
    # ---- UPDATE: old value must be retired, new value served --------------
    case(
        "lc_update_cloud",
        "knowledge-update",
        "What is the project's default cloud storage backend?",
        "OpenCloud.",
        [
            (
                "2026/01/06 (Tue) 09:10",
                "orig",
                [
                    (
                        "user",
                        "For the project we've settled on Nextcloud as the default cloud storage backend.",
                    ),
                    (
                        "assistant",
                        "Noted — Nextcloud is the default cloud storage backend.",
                    ),
                ],
            ),
            ("2026/01/13 (Tue) 12:00", None, None),
            (
                "2026/01/27 (Tue) 16:30",
                "update",
                [
                    (
                        "user",
                        "Decision changed: the default cloud storage backend is now OpenCloud.",
                    ),
                    (
                        "assistant",
                        "Understood — OpenCloud is now the default cloud storage backend.",
                    ),
                ],
            ),
        ],
        lc("update", expect_valid=["OpenCloud"], expect_retired=["Nextcloud"]),
        probe={
            "current_value": "OpenCloud",
            "stale_value": "Nextcloud",
            "update_session_id": "update",
            "original_session_id": "orig",
        },
    ),
    case(
        "lc_update_aux",
        "knowledge-update",
        "Which model runs the auxiliary LLM?",
        "gemma-4-moe.",
        [
            (
                "2026/01/04 (Sun) 11:00",
                "orig",
                [
                    ("user", "Our auxiliary LLM for background tasks runs on Mixtral."),
                    ("assistant", "Got it — the auxiliary LLM runs on Mixtral."),
                ],
            ),
            ("2026/01/11 (Sun) 10:00", None, None),
            (
                "2026/01/25 (Sun) 14:00",
                "update",
                [
                    ("user", "The auxiliary LLM now runs on gemma-4-moe."),
                    ("assistant", "Understood — auxiliary tasks now use gemma-4-moe."),
                ],
            ),
        ],
        lc("update", expect_valid=["gemma-4-moe"], expect_retired=["Mixtral"]),
        probe={
            "current_value": "gemma-4-moe",
            "stale_value": "Mixtral",
            "update_session_id": "update",
            "original_session_id": "orig",
        },
    ),
    case(
        "lc_update_car",
        "knowledge-update",
        "What car do I drive?",
        "A red Tesla Model 3.",
        [
            (
                "2026/01/02 (Fri) 10:05",
                "orig",
                [
                    ("user", "I finally picked up my new car — a blue Honda Civic."),
                    ("assistant", "Noted — I'll remember your car."),
                ],
            ),
            ("2026/01/09 (Fri) 18:40", None, None),
            (
                "2026/01/23 (Fri) 09:30",
                "update",
                [
                    (
                        "user",
                        "I traded in the old car; I now drive a red Tesla Model 3.",
                    ),
                    (
                        "assistant",
                        "Thanks for the update — I've replaced the old car information.",
                    ),
                ],
            ),
        ],
        lc(
            "update",
            expect_valid=["red Tesla Model 3"],
            expect_retired=["blue Honda Civic"],
        ),
        probe={
            "current_value": "red Tesla Model 3",
            "stale_value": "blue Honda Civic",
            "update_session_id": "update",
            "original_session_id": "orig",
        },
    ),
    case(
        "lc_update_diet",
        "knowledge-update",
        "What diet do I follow?",
        "Vegetarian.",
        [
            (
                "2026/01/03 (Sat) 08:00",
                "orig",
                [
                    ("user", "I follow a strict vegan diet."),
                    ("assistant", "Noted — strict vegan diet."),
                ],
            ),
            ("2026/01/10 (Sat) 19:00", None, None),
            (
                "2026/01/24 (Sat) 12:30",
                "update",
                [
                    ("user", "These days I eat a vegetarian diet."),
                    ("assistant", "Understood — vegetarian diet now."),
                ],
            ),
        ],
        lc("update", expect_valid=["vegetarian"], expect_retired=["vegan"]),
        probe={
            "current_value": "vegetarian",
            "stale_value": "vegan",
            "update_session_id": "update",
            "original_session_id": "orig",
        },
    ),
    # ---- UPDATE CHAIN: every stale hop retired, only the last served ------
    case(
        "lc_chain_deadline",
        "knowledge-update",
        "When is my thesis deadline?",
        "February 15, 2026.",
        [
            (
                "2025/12/10 (Wed) 09:00",
                "orig",
                [
                    ("user", "My thesis deadline is January 31, 2026."),
                    ("assistant", "Noted — deadline January 31, 2026."),
                ],
            ),
            (
                "2026/01/05 (Mon) 09:00",
                "mid",
                [
                    ("user", "The thesis deadline got pushed to February 7, 2026."),
                    ("assistant", "Updated — deadline February 7, 2026."),
                ],
            ),
            ("2026/01/12 (Mon) 09:00", None, None),
            (
                "2026/01/20 (Tue) 09:00",
                "update",
                [
                    (
                        "user",
                        "The thesis deadline moved again; it's now February 15, 2026.",
                    ),
                    ("assistant", "Updated — deadline February 15, 2026."),
                ],
            ),
        ],
        lc(
            "update_chain",
            expect_valid=["February 15"],
            expect_retired=["January 31", "February 7"],
        ),
        probe={
            "current_value": "February 15",
            "stale_value": "February 7",
            "update_session_id": "update",
            "original_session_id": "orig",
        },
    ),
    case(
        "lc_chain_port",
        "knowledge-update",
        "Which port does the orchestrator listen on?",
        "Port 8085.",
        [
            (
                "2026/01/02 (Fri) 09:00",
                "orig",
                [
                    ("user", "The orchestrator listens on port 8080."),
                    ("assistant", "Noted — orchestrator on port 8080."),
                ],
            ),
            (
                "2026/01/08 (Thu) 09:00",
                "mid",
                [
                    ("user", "We moved the orchestrator to port 8083."),
                    ("assistant", "Updated — orchestrator on port 8083."),
                ],
            ),
            ("2026/01/12 (Mon) 09:00", None, None),
            (
                "2026/01/18 (Sun) 09:00",
                "update",
                [
                    ("user", "The orchestrator now runs on port 8085."),
                    ("assistant", "Updated — orchestrator on port 8085."),
                ],
            ),
        ],
        lc("update_chain", expect_valid=["8085"], expect_retired=["8080", "8083"]),
        probe={
            "current_value": "8085",
            "stale_value": "8083",
            "update_session_id": "update",
            "original_session_id": "orig",
        },
    ),
    # ---- COEXIST: two true, non-conflicting facts; nothing retired -------
    case(
        "lc_coexist_vehicles",
        "multi-session",
        "What vehicles do I own?",
        "A red Tesla Model 3 and a Honda motorcycle.",
        [
            (
                "2026/01/08 (Thu) 10:00",
                "x",
                [
                    ("user", "I drive a red Tesla Model 3 to work every day."),
                    ("assistant", "Noted — you drive a red Tesla Model 3."),
                ],
            ),
            ("2026/01/15 (Thu) 10:00", None, None),
            (
                "2026/01/22 (Thu) 10:00",
                "y",
                [
                    (
                        "user",
                        "On weekends I ride my Honda motorcycle out to the coast.",
                    ),
                    (
                        "assistant",
                        "Noted — you also ride a Honda motorcycle on weekends.",
                    ),
                ],
            ),
        ],
        lc("coexist", expect_valid=["Tesla Model 3", "Honda motorcycle"]),
    ),
    case(
        "lc_coexist_components",
        "multi-session",
        "What are the orchestrator and cockpit built with?",
        "FastAPI and Angular.",
        [
            (
                "2026/01/07 (Wed) 11:00",
                "x",
                [
                    ("user", "The orchestrator service is built with FastAPI."),
                    ("assistant", "Noted — orchestrator uses FastAPI."),
                ],
            ),
            ("2026/01/14 (Wed) 11:00", None, None),
            (
                "2026/01/21 (Wed) 11:00",
                "y",
                [
                    ("user", "The cockpit front-end is built with Angular."),
                    ("assistant", "Noted — cockpit uses Angular."),
                ],
            ),
        ],
        lc("coexist", expect_valid=["FastAPI", "Angular"]),
    ),
    case(
        "lc_coexist_langs",
        "multi-session",
        "Which languages am I learning?",
        "Spanish and Japanese.",
        [
            (
                "2026/01/05 (Mon) 20:00",
                "x",
                [
                    ("user", "I'm learning Spanish in the evenings."),
                    ("assistant", "Noted — you're learning Spanish."),
                ],
            ),
            ("2026/01/12 (Mon) 20:00", None, None),
            (
                "2026/01/19 (Mon) 20:00",
                "y",
                [
                    ("user", "I also started learning Japanese on weekends."),
                    ("assistant", "Noted — you're learning Japanese as well."),
                ],
            ),
        ],
        lc("coexist", expect_valid=["Spanish", "Japanese"]),
    ),
    # ---- FALSE CONTRADICTION: high similarity, distinct entities ---------
    case(
        "lc_false_laptops",
        "multi-session",
        "What laptops do I use?",
        "A MacBook Pro for work and a ThinkPad personally.",
        [
            (
                "2026/01/06 (Tue) 09:00",
                "x",
                [
                    ("user", "My work laptop is a MacBook Pro."),
                    ("assistant", "Noted — work laptop is a MacBook Pro."),
                ],
            ),
            ("2026/01/13 (Tue) 09:00", None, None),
            (
                "2026/01/20 (Tue) 09:00",
                "y",
                [
                    ("user", "My personal laptop is a ThinkPad."),
                    ("assistant", "Noted — personal laptop is a ThinkPad."),
                ],
            ),
        ],
        lc("false_contradiction", expect_valid=["MacBook Pro", "ThinkPad"]),
    ),
    case(
        "lc_false_dbs",
        "multi-session",
        "Which databases does the system use?",
        "PostgreSQL for the app and MongoDB for the audit trail.",
        [
            (
                "2026/01/04 (Sun) 13:00",
                "x",
                [
                    ("user", "The application database is PostgreSQL."),
                    ("assistant", "Noted — app database is PostgreSQL."),
                ],
            ),
            ("2026/01/11 (Sun) 13:00", None, None),
            (
                "2026/01/18 (Sun) 13:00",
                "y",
                [
                    ("user", "The audit trail is stored in MongoDB."),
                    ("assistant", "Noted — audit trail uses MongoDB."),
                ],
            ),
        ],
        lc("false_contradiction", expect_valid=["PostgreSQL", "MongoDB"]),
    ),
    # ---- MERGE / ADDITIVE: a detail about an existing entity, both kept ---
    case(
        "lc_merge_flight",
        "multi-session",
        "When is my flight to Berlin?",
        "On March 10 at 3pm.",
        [
            (
                "2026/01/09 (Fri) 10:00",
                "x",
                [
                    ("user", "My flight to Berlin is on March 10."),
                    ("assistant", "Noted — Berlin flight on March 10."),
                ],
            ),
            ("2026/01/16 (Fri) 10:00", None, None),
            (
                "2026/01/23 (Fri) 10:00",
                "y",
                [
                    ("user", "That Berlin flight leaves at 3pm."),
                    ("assistant", "Noted — the Berlin flight is at 3pm."),
                ],
            ),
        ],
        lc("merge", expect_valid=["March 10", "3pm"]),
    ),
    case(
        "lc_merge_dentist",
        "multi-session",
        "Who is my dentist and where is the office?",
        "Dr. Reed, on Oak Street.",
        [
            (
                "2026/01/08 (Thu) 15:00",
                "x",
                [
                    ("user", "My dentist is Dr. Reed."),
                    ("assistant", "Noted — your dentist is Dr. Reed."),
                ],
            ),
            ("2026/01/15 (Thu) 15:00", None, None),
            (
                "2026/01/22 (Thu) 15:00",
                "y",
                [
                    ("user", "Dr. Reed's office is on Oak Street."),
                    ("assistant", "Noted — Dr. Reed's office is on Oak Street."),
                ],
            ),
        ],
        lc("merge", expect_valid=["Dr. Reed", "Oak Street"]),
    ),
    # ---- NOOP: a restatement must not spawn a twin or retire anything ----
    case(
        "lc_noop_employer",
        "knowledge-update",
        "Where do I work?",
        "Northwind Robotics.",
        [
            (
                "2026/01/09 (Fri) 09:00",
                "first",
                [
                    ("user", "I work at Northwind Robotics."),
                    ("assistant", "Noted — you work at Northwind Robotics."),
                ],
            ),
            ("2026/01/16 (Fri) 09:00", None, None),
            (
                "2026/01/23 (Fri) 09:00",
                "again",
                [
                    ("user", "Just to confirm, my employer is Northwind Robotics."),
                    ("assistant", "Confirmed — Northwind Robotics."),
                ],
            ),
        ],
        lc(
            "noop",
            expect_valid=["Northwind Robotics"],
            expect_unique=["Northwind Robotics"],
        ),
    ),
    case(
        "lc_noop_advisor",
        "knowledge-update",
        "Who is my thesis advisor?",
        "Dr. Yamamoto.",
        [
            (
                "2026/01/07 (Wed) 14:00",
                "first",
                [
                    ("user", "My thesis advisor is Dr. Yamamoto."),
                    ("assistant", "Noted — your advisor is Dr. Yamamoto."),
                ],
            ),
            ("2026/01/14 (Wed) 14:00", None, None),
            (
                "2026/01/21 (Wed) 14:00",
                "again",
                [
                    ("user", "Dr. Yamamoto is the one supervising my thesis."),
                    ("assistant", "Confirmed — Dr. Yamamoto supervises your thesis."),
                ],
            ),
        ],
        lc("noop", expect_valid=["Yamamoto"], expect_unique=["Yamamoto"]),
    ),
]


def build():
    OUT.write_text(
        json.dumps(CASES, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    by_cat = {}
    for c in CASES:
        by_cat.setdefault(c["lifecycle"]["category"], 0)
        by_cat[c["lifecycle"]["category"]] += 1
    print(f"Wrote {len(CASES)} cases to {OUT}")
    for cat, n in sorted(by_cat.items()):
        print(f"  {cat:20s} {n}")


if __name__ == "__main__":
    build()
