"""Work-category tests — B1 of knowledge-base/knowledge/features/officer_backlog_pools.md.

Two pure modules, one model: ``src/shared/backlog_tags.py`` owns the SHAPE of a
machine tag (both sides of the wire must agree on it, and neither may import the
other), ``orchestrator/services/work_categories.py`` owns what those tags MEAN.

Beyond the mechanics, this file pins doctrine. The blocks in ``_CATEGORY_BLOCKS``
are the feature's answer to a month of green tests and no UI: they say an answer
is a deliverable, a screenshot is evidence, and tests are regression rails rather
than the score. Those sentences are load-bearing, so they are asserted here — a
future edit that quietly restores test-worship should fail a test, not ship.
"""

import pytest

from orchestrator.services.work_categories import (
    CATEGORY_DEFAULT_EXPERT,
    CATEGORY_EXPERTS,
    EXECUTOR,
    KNOWN_EXPERTS,
    RESEARCHER,
    TESTER,
    WORK_CATEGORIES,
    UnknownCategory,
    allows_parallel,
    category_block,
    classify_ticket,
    close_checklist,
    default_expert,
    expert_fits_category,
    normalize_category,
    resolve_expert,
    role_to_category,
)
from shared.backlog_tags import (
    CATEGORY_PREFIX,
    EXPERT_PREFIX,
    OFFICER_ONLY_TAGS,
    PARALLEL_SAFE_TAG,
    READY_TAG,
    category_tag,
    category_values,
    expert_tag,
    expert_values,
    has_tag,
    is_machine_tag,
    is_officer_only_tag,
    normalize_tag,
    normalize_tags,
    strip_machine_tags,
    strip_officer_tags,
)


class TestTagNamespace:
    def test_machine_tags_fold_to_lowercase(self):
        assert normalize_tag("Ready") == "ready"
        assert normalize_tag("PARALLEL-SAFE") == "parallel-safe"
        assert normalize_tag("Category:Researcher") == "category:researcher"
        assert normalize_tag("Expert:Product-QA") == "expert:product-qa"

    def test_every_tag_folds_not_only_the_machine_ones(self):
        # kb_update and the Neo4j :TAGGED writer have always lowercased, and
        # tags are matched by exact string — a preserved-case tag is a tag that
        # silently fails `tags @>` containment.
        assert normalize_tag("Onboarding UX") == "onboarding ux"
        assert normalize_tag("  Proposal  ") == "proposal"

    def test_normalize_tags_drops_blanks_and_non_strings(self):
        assert normalize_tags(["Ready", "", "  ", None, 7, "keep"]) == [
            "ready",
            "keep",
        ]
        assert normalize_tags(None) == []

    def test_machine_namespace_membership(self):
        for tag in ("ready", "PARALLEL-SAFE", "category:x", "Expert:scholar"):
            assert is_machine_tag(tag)
        for tag in ("proposal", "verdict", "readiness", "categorical"):
            assert not is_machine_tag(tag)

    def test_only_two_tags_carry_officer_provenance(self):
        assert OFFICER_ONLY_TAGS == {READY_TAG, PARALLEL_SAFE_TAG}
        assert is_officer_only_tag("Ready")
        # Classification is worker-writable; authorization is not.
        assert not is_officer_only_tag("category:executor")

    def test_worker_writes_cannot_self_authorize_dispatch(self):
        # The anti-amplification firewall: a worker filing a ticket may classify
        # it, but stamping `ready` is what starts a job, so only the officer may.
        kept = strip_officer_tags(
            ["Ready", "parallel-safe", "category:executor", "Onboarding UX"]
        )
        assert kept == ["category:executor", "onboarding ux"]

    def test_search_text_excludes_the_whole_machine_namespace(self):
        assert strip_machine_tags(
            ["ready", "category:researcher", "expert:scholar", "Design System"]
        ) == ["design system"]

    def test_tag_builders_round_trip(self):
        assert category_tag("Researcher") == f"{CATEGORY_PREFIX}researcher"
        assert expert_tag(" Product-QA ") == f"{EXPERT_PREFIX}product-qa"
        assert category_values([category_tag("tester")]) == ["tester"]
        assert expert_values([expert_tag("scholar")]) == ["scholar"]

    def test_values_are_plural_so_ambiguity_stays_visible(self):
        # Returning a single value here would mean silently picking one of two
        # categories — dispatching into the wrong pool under the officer's name.
        assert category_values(["category:researcher", "category:executor"]) == [
            "researcher",
            "executor",
        ]

    def test_values_deduplicate_and_ignore_empty(self):
        assert category_values(["category:tester", "Category:Tester", "category:"]) == [
            "tester"
        ]

    def test_has_tag_is_case_insensitive(self):
        assert has_tag(["Ready"], READY_TAG)
        assert not has_tag(["category:executor"], PARALLEL_SAFE_TAG)


class TestCategories:
    def test_three_categories_in_pipeline_order(self):
        assert WORK_CATEGORIES == (RESEARCHER, TESTER, EXECUTOR)

    def test_normalize_category_folds_or_returns_none(self):
        assert normalize_category(" Researcher ") == RESEARCHER
        assert normalize_category("designer") is None
        assert normalize_category(None) is None

    def test_executors_are_serialized_analysts_are_not(self):
        # Executors touch shared project state; researchers/testers write to the
        # KB, where concurrent writes are additive.
        assert allows_parallel(RESEARCHER)
        assert allows_parallel(TESTER)
        assert not allows_parallel(EXECUTOR)

    @pytest.mark.parametrize(
        "category",
        [None, "", "   ", "execuutor", "executor,tester", "future-category"],
    )
    def test_parallel_policy_rejects_absent_malformed_and_unknown(self, category):
        with pytest.raises(UnknownCategory, match="unknown work category"):
            allows_parallel(category)

    def test_parallel_policy_is_exhaustive_for_every_supported_category(self):
        assert {
            category: allows_parallel(category) for category in WORK_CATEGORIES
        } == {
            RESEARCHER: True,
            TESTER: True,
            EXECUTOR: False,
        }

    def test_every_category_has_a_default_expert_in_its_own_map(self):
        for category in WORK_CATEGORIES:
            expert = default_expert(category)
            assert expert in CATEGORY_EXPERTS[category]
            assert CATEGORY_DEFAULT_EXPERT[category] == expert

    def test_unknown_category_raises_where_a_caller_must_have_an_answer(self):
        with pytest.raises(UnknownCategory):
            default_expert("designer")
        with pytest.raises(UnknownCategory):
            category_block("designer")
        with pytest.raises(UnknownCategory):
            close_checklist("designer")

    def test_known_experts_is_the_union_of_the_membership_map(self):
        assert KNOWN_EXPERTS == frozenset(
            {"scholar", "designer", "developer", "general-worker"}
            | {"product-qa", "bughunter", "critic"}
            | {"writer"}
        )

    def test_writer_is_an_executor_only(self):
        # B7. Prose for a reader is a shipped artifact under projects/<slug>/,
        # so the writer belongs to the delivery contract and to no other. It is
        # deliberately NOT a researcher: it is handed the findings.
        assert expert_fits_category("writer", EXECUTOR)
        assert not expert_fits_category("writer", RESEARCHER)
        assert not expert_fits_category("writer", TESTER)

    def test_membership_is_many_to_many(self):
        # The whole point: one expert, two contracts. A developer is a
        # researcher on a spike and an executor on a story.
        assert expert_fits_category("developer", RESEARCHER)
        assert expert_fits_category("developer", EXECUTOR)
        assert not expert_fits_category("scholar", EXECUTOR)
        assert not expert_fits_category("developer", "designer")

    def test_session_only_experts_stay_out_of_the_map(self):
        # These extend session_base, not worker_base — they cannot run a job.
        for expert in ("centurion", "assistant", "designer-interactive"):
            assert expert not in KNOWN_EXPERTS


class TestLegacyRoleBridge:
    def test_analysis_roles_map_to_their_category(self):
        assert role_to_category("scholar") == RESEARCHER
        assert role_to_category("critic") == TESTER
        assert role_to_category("product-qa") == TESTER

    def test_execution_is_the_open_set(self):
        # Mirrors is_loop_execution_role: analysis is the closed list, anything
        # else — including a role nobody has defined yet — is execution.
        assert role_to_category("developer") == EXECUTOR
        assert role_to_category("general-worker") == EXECUTOR
        assert role_to_category("writer") == EXECUTOR
        assert role_to_category("some-future-role") == EXECUTOR
        assert role_to_category(None) == EXECUTOR
        assert role_to_category("") == EXECUTOR

    def test_legacy_role_fallback_does_not_weaken_category_policy(self):
        assert role_to_category("future-role") == EXECUTOR
        with pytest.raises(UnknownCategory):
            allows_parallel("future-role")

    def test_case_and_whitespace_tolerant(self):
        assert role_to_category(" Product-QA ") == TESTER

    def test_bughunter_divergence_from_loop_analysis_roles_is_deliberate(self):
        # bughunter is unambiguously a tester by category, but is absent from
        # project_loops.LOOP_ANALYSIS_ROLES, so a loop running it would still be
        # treated as an execution role by the retro/delivery path. [A3] warned
        # against generalizing the name-based checks in place, so the gap is
        # pinned rather than closed here. If someone fixes LOOP_ANALYSIS_ROLES,
        # this test should be deleted, not worked around.
        from orchestrator.services.project_loops import LOOP_ANALYSIS_ROLES

        assert role_to_category("bughunter") == TESTER
        assert "bughunter" not in LOOP_ANALYSIS_ROLES

    def test_role_bridge_agrees_with_membership_for_single_category_experts(self):
        # Drift guard: an expert belonging to exactly one category must map to
        # that category, so editing CATEGORY_EXPERTS alone cannot desync the
        # bridge without a failure here.
        for expert in KNOWN_EXPERTS:
            homes = [c for c in WORK_CATEGORIES if expert in CATEGORY_EXPERTS[c]]
            if len(homes) == 1:
                assert role_to_category(expert) == homes[0], expert


class TestClassifyTicket:
    def test_fully_specified_ready_ticket_is_dispatchable(self):
        result = classify_ticket(
            ["ready", "category:researcher", "expert:designer", "Onboarding UX"]
        )
        assert result.category == RESEARCHER
        assert result.expert == "designer"
        assert result.ready is True
        assert result.parallel_safe is False
        assert result.problems == ()
        assert result.dispatchable is True

    def test_unready_ticket_is_not_dispatchable(self):
        # Worker-filed tickets are invisible to the tick until the officer
        # triages them — this is the firewall, expressed as a flag.
        result = classify_ticket(["category:executor"])
        assert result.ready is False
        assert result.dispatchable is False
        assert result.problems == ()

    def test_uncategorized_ticket_is_not_dispatchable(self):
        result = classify_ticket(["ready"])
        assert result.category is None
        assert result.dispatchable is False

    def test_two_categories_is_a_reported_problem_not_a_coin_flip(self):
        result = classify_ticket(["ready", "category:researcher", "category:executor"])
        assert result.category is None
        assert result.dispatchable is False
        assert any("multiple category" in p for p in result.problems)
        assert "executor" in result.problems[0] and "researcher" in result.problems[0]

    def test_two_expert_pins_is_a_reported_problem(self):
        result = classify_ticket(
            ["ready", "category:tester", "expert:critic", "expert:bughunter"]
        )
        assert result.expert is None
        assert any("multiple expert" in p for p in result.problems)
        assert result.dispatchable is False

    def test_unknown_category_names_itself(self):
        result = classify_ticket(["ready", "category:designer"])
        assert result.category is None
        assert result.problems == ("unknown category: 'designer'",)

    def test_typo_in_expert_pin_is_caught_before_dispatch(self):
        # Otherwise the typo surfaces at agent boot as a job failure and
        # chain-trips the pool's circuit breaker on a spelling mistake.
        result = classify_ticket(["ready", "category:executor", "expert:develloper"])
        assert result.expert is None
        assert result.problems == ("unknown expert: 'develloper'",)
        assert result.dispatchable is False

    def test_parallel_safe_is_read_but_independent_of_readiness(self):
        result = classify_ticket(["category:executor", "parallel-safe"])
        assert result.parallel_safe is True
        assert result.ready is False

    def test_classification_ignores_human_tags(self):
        result = classify_ticket(["ready", "category:tester", "Q3", "Onboarding UX"])
        assert result.dispatchable is True

    def test_resolve_expert_prefers_the_pin_then_the_default(self):
        pinned = classify_ticket(["ready", "category:executor", "expert:designer"])
        assert resolve_expert(pinned) == "designer"
        bare = classify_ticket(["ready", "category:executor"])
        assert resolve_expert(bare) == "developer"
        assert resolve_expert(classify_ticket(["ready", "category:tester"])) == (
            "product-qa"
        )

    def test_resolve_expert_refuses_an_uncategorized_ticket(self):
        with pytest.raises(UnknownCategory):
            resolve_expert(classify_ticket(["ready"]))


class TestContractBlocks:
    def test_every_category_has_a_block_and_a_checklist(self):
        for category in WORK_CATEGORIES:
            assert len(category_block(category)) > 400
            assert len(close_checklist(category)) >= 3

    def test_blocks_are_format_safe(self):
        # These strings sit next to _ROLE_BLOCK_DEFAULT, which goes through
        # .format(). A literal brace there is a KeyError at dispatch time.
        for category in WORK_CATEGORIES:
            block = category_block(category)
            assert "{" not in block and "}" not in block

    def test_checklist_rides_along_by_default_and_is_suppressible(self):
        with_list = category_block(RESEARCHER)
        without = category_block(RESEARCHER, include_checklist=False)
        assert "CLOSE CHECKLIST" in with_list
        assert "CLOSE CHECKLIST" not in without
        assert with_list.startswith(without)
        for item in close_checklist(RESEARCHER):
            assert item in with_list

    def test_researcher_contract_is_an_answer_under_spike_discipline(self):
        block = category_block(RESEARCHER)
        assert "ANSWER, not a product increment" in block
        assert "SPIKE" in block
        assert "then STOP" in block
        # A worker that can extend its own spike has no timebox at all.
        # Phrased context-free on purpose: the block is composed into loop
        # kickoffs too, where there is no officer to defer to.
        assert "extending a spike is not your call to make" in block
        assert "residual unknowns" in block
        # Design research is first-class and anchored, not vibes.
        assert "Material 3" in block and "WCAG" in block

    def test_tester_contract_files_tickets_and_may_find_nothing(self):
        block = category_block(TESTER)
        assert "ISSUE TICKETS, not file commits" in block
        assert "3-7 findings MAXIMUM" in block
        # The finding that broke the loop: absence of a product surface is a
        # finding, and green tests are not a defence against it.
        assert "'No UI exists' is a HIGH finding even when every unit test" in block
        assert "No blocking issues found" in block
        # Anchored critique — a named heuristic or a numbered success criterion.
        assert "Nielsen" in block and "SC 2.5.8" in block

    def test_executor_contract_prices_evidence_by_the_claim(self):
        block = category_block(EXECUTOR)
        assert "EVIDENCE APPROPRIATE TO THE CLAIM" in block
        assert "SCREENSHOTS for UI" in block
        # Tests are demoted here on purpose: this is the sentence that answers
        # "40 green unit tests doesn't mean shit".
        assert "regression rails, never the score" in block
        assert "self-consistent" in block
        assert "REPRODUCIBLE capture" in block
        assert "style guide" in block

    def test_executor_block_states_the_absence_of_a_cap_honestly(self):
        # §13.4 rejected per-ticket budget and wall-clock caps outright: the
        # officer is the brake. The block must say so rather than imply a limit
        # that does not exist.
        assert "No budget or clock will stop you" in category_block(EXECUTOR)

    def test_checklist_items_read_as_binary_criteria(self):
        # Binary items produce judge agreement; "was this good?" produces mood.
        for category in WORK_CATEGORIES:
            for item in close_checklist(category):
                assert not item.endswith("?")
                assert item == item.strip() and item
