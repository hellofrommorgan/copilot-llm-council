import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import copilot_council as cc


class EndpointInferenceTests(unittest.TestCase):
    def test_infer_endpoint_prefers_explicit(self):
        self.assertEqual(cc.infer_endpoint("whatever", explicit="chat_completions"), "chat_completions")

    def test_infer_endpoint_for_gpt5_defaults_to_responses(self):
        self.assertEqual(cc.infer_endpoint("gpt-5.4"), "responses")
        self.assertEqual(cc.infer_endpoint("gpt-5.2-codex"), "responses")

    def test_infer_endpoint_for_claude_defaults_to_chat(self):
        self.assertEqual(cc.infer_endpoint("claude-sonnet-4.6"), "chat_completions")


class ConfigAndUtilityTests(unittest.TestCase):
    def test_timeout_helpers(self):
        cfg = {"request_timeout_seconds": 600, "review": {"timeout_seconds": 300}}
        self.assertEqual(cc.get_stage_timeout(cfg, "generation"), 600)
        self.assertEqual(cc.get_stage_timeout(cfg, "review"), 300)

    def test_retry_policy_defaults(self):
        cfg = {"retry": {"max_attempts": 3, "backoff_seconds": [2, 6]}}
        policy = cc.get_retry_policy(cfg)
        self.assertEqual(policy["max_attempts"], 3)
        self.assertEqual(policy["backoff_seconds"], [2.0, 6.0])

    def test_review_card_compaction(self):
        text = "Recommendation:\nA\n\nStrongest rationale:\nB\n\nKey uncertainty:\nC\n\nNext step:\nD\n\nSupporting detail:\nE\n\nExtra:\nF"
        card = cc.build_review_card(text, max_chars=60, max_paragraphs=3)
        self.assertIn("Recommendation", card)
        self.assertLessEqual(len(card), 63)

    def test_incomplete_response_reason_for_responses_max_output_tokens(self):
        payload = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}
        self.assertEqual(cc.incomplete_response_reason(payload), "max_output_tokens")


class RoleAndPromptTests(unittest.TestCase):
    def test_role_brief_known_role(self):
        self.assertIn("failure modes", cc.role_brief("contrarian"))

    def test_generation_prompt_includes_role_and_required_sections(self):
        prompt = cc.build_generation_prompt("Should we do X?", "executor")
        self.assertIn("executor", prompt)
        self.assertIn("Recommendation:", prompt)
        self.assertIn("Next step:", prompt)

    def test_review_prompt_requests_collective_blind_spot(self):
        prompt = cc.build_review_prompt("Q", {"A": "x", "B": "y"}, ["accuracy"], "outsider")
        self.assertIn("collective_blind_spot", prompt)
        self.assertIn("candidate answer cards", prompt.lower())

    def test_review_prompt_requests_compact_json(self):
        prompt = cc.build_review_prompt("Q", {"A": "x", "B": "y"}, ["accuracy"], "outsider")
        self.assertIn("Keep `best_answer_why` and each critique to one sentence", prompt)
        self.assertIn("at most 3 items", prompt)


class ExtractionTests(unittest.TestCase):
    def test_extract_text_from_responses_payload(self):
        payload = {
            "output": [
                {
                    "content": [
                        {"type": "output_text", "text": "hello"},
                        {"type": "output_text", "text": " world"},
                    ]
                }
            ]
        }
        self.assertEqual(cc.extract_text_from_responses_payload(payload), "hello world")

    def test_extract_text_from_chat_payload(self):
        payload = {
            "choices": [
                {
                    "message": {
                        "content": "hello from chat"
                    }
                }
            ]
        }
        self.assertEqual(cc.extract_text_from_chat_payload(payload), "hello from chat")

    def test_extract_json_object_handles_code_fences(self):
        text = "```json\n{\n  \"a\": 1,\n  \"b\": 2\n}\n```"
        self.assertEqual(cc.extract_json_object(text), {"a": 1, "b": 2})


class ReviewValidationTests(unittest.TestCase):
    def test_normalize_review_payload_validates_labels_scores_and_blind_spot(self):
        payload = {
            "ranking": ["B", "A"],
            "best_answer": "B",
            "best_answer_why": "better",
            "scores": {
                "A": {
                    "accuracy": 7,
                    "completeness": 8,
                    "reasoning": 7,
                    "usefulness": 7,
                    "clarity": 8,
                    "uncertainty_calibration": 7,
                },
                "B": {
                    "accuracy": 9,
                    "completeness": 9,
                    "reasoning": 9,
                    "usefulness": 8,
                    "clarity": 8,
                    "uncertainty_calibration": 8,
                },
            },
            "critique_by_answer": {
                "A": "good but weaker",
                "B": "best",
            },
            "collective_blind_spot": "Neither answer addressed execution risk.",
            "unresolved_disagreements": ["none"],
        }
        normalized = cc.normalize_review_payload(
            payload,
            labels=["A", "B"],
            rubric=["accuracy", "completeness", "reasoning", "usefulness", "clarity", "uncertainty_calibration"],
        )
        self.assertEqual(normalized["ranking"], ["B", "A"])
        self.assertEqual(normalized["scores"]["B"]["accuracy"], 9)
        self.assertIn("execution risk", normalized["collective_blind_spot"])

    def test_normalize_review_payload_tolerates_partial_rankings(self):
        payload = {
            "ranking": ["B", "Z"],
            "best_answer": "Z",
            "best_answer_why": "bad label",
            "scores": {
                "B": {
                    "accuracy": 9,
                    "completeness": 9,
                    "reasoning": 9,
                    "usefulness": 9,
                    "clarity": 9,
                    "uncertainty_calibration": 9,
                }
            },
            "critique_by_answer": {"B": "best"},
            "unresolved_disagreements": [],
        }
        normalized = cc.normalize_review_payload(payload, labels=["A", "B"], rubric=["accuracy", "completeness", "reasoning", "usefulness", "clarity", "uncertainty_calibration"])
        self.assertEqual(normalized["ranking"], ["B"])
        self.assertEqual(normalized["best_answer"], "B")
        self.assertTrue(normalized["normalization_warnings"])


class AggregationTests(unittest.TestCase):
    def test_aggregate_reviews_orders_by_borda_then_votes_and_collects_blind_spots(self):
        reviews = [
            {
                "reviewer_model": "m1",
                "ranking": ["B", "A", "C"],
                "scores": {
                    "A": {"accuracy": 7, "completeness": 7},
                    "B": {"accuracy": 9, "completeness": 9},
                    "C": {"accuracy": 6, "completeness": 6},
                },
                "critique_by_answer": {"A": "solid", "B": "best", "C": "weak"},
                "collective_blind_spot": "Missing resource constraints.",
                "unresolved_disagreements": ["scope"],
                "normalization_warnings": ["warn-1"],
            },
            {
                "reviewer_model": "m2",
                "ranking": ["A", "B", "C"],
                "scores": {
                    "A": {"accuracy": 9, "completeness": 8},
                    "B": {"accuracy": 8, "completeness": 8},
                    "C": {"accuracy": 5, "completeness": 5},
                },
                "critique_by_answer": {"A": "great", "B": "great", "C": "weak"},
                "collective_blind_spot": "Missing resource constraints.",
                "unresolved_disagreements": ["confidence"],
                "normalization_warnings": ["warn-2"],
            },
        ]
        aggregate = cc.aggregate_reviews(reviews, labels=["A", "B", "C"], rubric=["accuracy", "completeness"])
        self.assertEqual(aggregate["ranking"], ["B", "A", "C"])
        self.assertEqual(aggregate["by_answer"]["B"]["first_place_votes"], 1)
        self.assertAlmostEqual(aggregate["by_answer"]["A"]["overall_mean"], 7.75)
        self.assertIn("scope", aggregate["unresolved_disagreements"])
        self.assertEqual(aggregate["collective_blind_spot"], "Missing resource constraints.")
        self.assertIn("warn-1", aggregate["normalization_warnings"])


class RosterResolutionTests(unittest.TestCase):
    def test_requested_and_resolved_roster_rows(self):
        cfg = {
            "roster": [
                {"model": "gpt-5.4", "transport": "copilot_api", "endpoint": "responses", "role": "first_principles"}
            ]
        }
        requested = cc.requested_roster_rows(cfg)
        self.assertEqual(requested[0]["role"], "first_principles")
        member = cc.CouncilMember(
            model="gpt-5.4",
            transport="copilot_api",
            endpoint="responses",
            role="first_principles",
            requested_model="gpt-5.4",
            requested_transport="copilot_api",
            requested_endpoint="responses",
            resolution_reason="configured",
        )
        resolved = cc.resolved_roster_rows([member])
        self.assertEqual(resolved[0]["requested_model"], "gpt-5.4")
        self.assertEqual(resolved[0]["resolution_reason"], "configured")

    def test_review_candidates_include_fallbacks(self):
        cfg = {
            "review_fallbacks": {
                "claude-opus-4.6": [
                    {"model": "claude-opus-4.6-1m", "transport": "copilot_cli", "endpoint": "cli"},
                    {"model": "gpt-5.3-codex", "transport": "copilot_api", "endpoint": "responses"},
                ]
            }
        }
        candidate = {"model": "claude-opus-4.6", "transport": "copilot_api", "endpoint": "chat_completions"}
        plan = cc.review_candidates_for_member(candidate, cfg)
        self.assertEqual(plan[0]["model"], "claude-opus-4.6")
        self.assertEqual(plan[1]["model"], "claude-opus-4.6-1m")
        self.assertEqual(plan[2]["model"], "gpt-5.3-codex")



class MatrixRosterTests(unittest.TestCase):
    def test_requested_roster_rows_expands_models_by_personas(self):
        cfg = {
            "models": [
                {"model": "gpt-5.4", "transport": "copilot_api", "endpoint": "responses"},
                {"model": "claude-opus-4.6", "transport": "copilot_api", "endpoint": "chat_completions"},
                {"model": "claude-opus-4.6-1m", "transport": "copilot_cli", "endpoint": "cli"},
            ],
            "personas": [
                {"id": "first_principles", "label": "First principles", "brief": "derive from fundamentals"},
                {"id": "contrarian", "label": "Contrarian", "brief": "find failure modes"},
                {"id": "executor", "label": "Executor", "brief": "make it practical"},
            ],
        }
        rows = cc.requested_roster_rows(cfg)
        self.assertEqual(len(rows), 9)
        self.assertEqual(rows[0]["seat_id"], "gpt-5.4__first_principles")
        self.assertEqual(rows[-1]["seat_id"], "claude-opus-4.6-1m__executor")

    def test_ensure_roster_expands_to_nine_seats(self):
        cfg = {
            "generation": {"timeout_seconds": 60},
            "models": [
                {"model": "gpt-5.4", "transport": "copilot_api", "endpoint": "responses"},
                {"model": "claude-opus-4.6", "transport": "copilot_api", "endpoint": "chat_completions"},
                {"model": "claude-opus-4.6-1m", "transport": "copilot_cli", "endpoint": "cli"},
            ],
            "personas": [
                {"id": "first_principles", "label": "First principles", "brief": "derive from fundamentals"},
                {"id": "contrarian", "label": "Contrarian", "brief": "find failure modes"},
                {"id": "executor", "label": "Executor", "brief": "make it practical"},
            ],
            "chairman": {"model": "gpt-5.4", "transport": "copilot_api", "endpoint": "responses"},
        }
        catalog = {
            "gpt-5.4": {"id": "gpt-5.4", "supported_endpoints": ["/responses"]},
            "claude-opus-4.6": {"id": "claude-opus-4.6", "supported_endpoints": ["/chat/completions"]},
            "claude-opus-4.6-1m": {"id": "claude-opus-4.6-1m", "supported_endpoints": []},
        }
        roster = cc.ensure_roster(cfg, catalog_map=catalog)
        self.assertEqual(len(roster), 9)
        self.assertEqual(roster[0].seat_id, "gpt-5.4__first_principles")
        self.assertEqual(sorted({item.role for item in roster}), ["contrarian", "executor", "first_principles"])


class CompactSummaryTests(unittest.TestCase):
    def test_render_summary_markdown_respects_compact_budget(self):
        result = {
            "timestamp": "2026-04-05T00:00:00Z",
            "mode": "peer",
            "question": "Q" * 4000,
            "github_identity": {"login": "tester"},
            "requested_roster": [{"seat_id": "gpt-5.4__first_principles", "model": "gpt-5.4", "transport": "copilot_api", "endpoint": "responses", "role": "first_principles"}],
            "resolved_roster": [{"seat_id": "gpt-5.4__first_principles", "model": "gpt-5.4", "transport": "copilot_api", "endpoint": "responses", "role": "first_principles", "requested_model": "gpt-5.4", "requested_transport": "copilot_api", "requested_endpoint": "responses", "resolution_reason": "configured"}],
            "stage1": {"candidates": [{"label": "A", "model": "gpt-5.4", "role": "first_principles", "seat_id": "gpt-5.4__first_principles", "review_card": "Recommendation: short", "answer_text": ("LONG\n\n" * 500)}]},
            "stage2": {"aggregate": {"ranking": ["A"], "by_answer": {"A": {"borda_points": 1, "first_place_votes": 1, "overall_mean": 9.0, "critiques": ["good"]}}, "unresolved_disagreements": ["d1", "d2"], "collective_blind_spots": ["b1"], "collective_blind_spot": "b1", "normalization_warnings": []}, "review_substitutions": []},
            "stage3": {"final_answer": (("paragraph\n\n") * 1200).strip(), "error": None, "model": "gpt-5.4", "attempts": []},
            "failures": [],
            "summary_config": {"max_chars": 6000, "top_n": 3, "max_list_items": 3, "answer_chars": 2500},
        }
        summary = cc.render_summary_markdown(result)
        self.assertLessEqual(len(summary), 6500)
        self.assertIn("Top ranked seats", summary)
        self.assertLess(summary.count("paragraph"), 50)



class ReviewJsonSalvageTests(unittest.TestCase):
    def test_extract_json_object_handles_trailing_prose(self):
        text = """{"ranking":["A","B"],"best_answer":"A","best_answer_why":"x","scores":{"A":{"accuracy":9,"completeness":9,"reasoning":9,"usefulness":9,"clarity":9,"uncertainty_calibration":9},"B":{"accuracy":8,"completeness":8,"reasoning":8,"usefulness":8,"clarity":8,"uncertainty_calibration":8}},"critique_by_answer":{"A":"good","B":"ok"},"collective_blind_spot":"none","unresolved_disagreements":[]}

Extra note after JSON."""
        obj = cc.extract_json_object(text)
        self.assertEqual(obj['best_answer'], 'A')

    def test_extract_json_object_repairs_missing_comma_between_fields(self):
        text = """{"ranking":["A","B"],"best_answer":"A","best_answer_why":"x" "scores":{"A":{"accuracy":9,"completeness":9,"reasoning":9,"usefulness":9,"clarity":9,"uncertainty_calibration":9},"B":{"accuracy":8,"completeness":8,"reasoning":8,"usefulness":8,"clarity":8,"uncertainty_calibration":8}},"critique_by_answer":{"A":"good","B":"ok"},"collective_blind_spot":"none","unresolved_disagreements":[]}"""
        obj = cc.extract_json_object(text)
        self.assertIn('scores', obj)
        self.assertEqual(obj['best_answer'], 'A')



class PressureHarnessTests(unittest.TestCase):
    def test_pressure_harness_script_exists(self):
        harness = ROOT / "pressure_test_council.py"
        self.assertTrue(harness.exists())


class ReviewExecutionTests(unittest.TestCase):
    def test_run_stage2_peer_retries_truncated_review_with_higher_token_budget(self):
        rubric = ["accuracy", "completeness", "reasoning", "usefulness", "clarity", "uncertainty_calibration"]
        stage1 = {
            "candidates": [
                {
                    "model": "gpt-5.4",
                    "transport": "copilot_api",
                    "endpoint": "responses",
                    "role": "contrarian",
                    "label": "A",
                }
            ],
            "labeled_review_cards": {"A": "self", "B": "candidate-b", "C": "candidate-c"},
            "labeled_answers": {"A": "self", "B": "candidate-b", "C": "candidate-c"},
        }
        cfg = {
            "review": {"max_output_tokens": 900, "temperature": 0, "exclude_self": True},
            "retry": {"max_attempts": 1, "backoff_seconds": []},
            "rubric": rubric,
        }
        calls = []
        valid_json = '{"ranking":["B","C"],"best_answer":"B","best_answer_why":"x","scores":{"B":{"accuracy":9,"completeness":9,"reasoning":9,"usefulness":9,"clarity":9,"uncertainty_calibration":9},"C":{"accuracy":8,"completeness":8,"reasoning":8,"usefulness":8,"clarity":8,"uncertainty_calibration":8}},"critique_by_answer":{"B":"good","C":"ok"},"collective_blind_spot":"none","unresolved_disagreements":[]}'
        original = cc.call_model
        def fake_call_model(**kwargs):
            calls.append(kwargs["max_output_tokens"])
            if len(calls) == 1:
                return {
                    "answer_text": '{"ranking":["B","C"]',
                    "raw_response": {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
                    "attempts": [],
                }
            return {
                "answer_text": valid_json,
                "raw_response": {"status": "completed"},
                "attempts": [],
            }
        cc.call_model = fake_call_model
        try:
            stage2 = cc.run_stage2_peer("Q", stage1, cfg)
        finally:
            cc.call_model = original
        self.assertEqual(len(stage2["reviews"]), 1)
        self.assertEqual(stage2["review_failures"], [])
        self.assertEqual(calls[0], 900)
        self.assertGreater(calls[1], calls[0])


class TemplateDefaultsTests(unittest.TestCase):
    def test_template_config_has_gpt_review_fallback_and_higher_review_budget(self):
        template_path = ROOT.parent / "templates" / "council-config.json"
        cfg = cc.load_json(template_path)
        self.assertIn("gpt-5.4", cfg.get("review_fallbacks", {}))
        self.assertGreaterEqual(int(cfg.get("review", {}).get("max_output_tokens", 0)), 1200)

if __name__ == "__main__":
    unittest.main()
