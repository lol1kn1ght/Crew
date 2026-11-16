"""Static regression checks for the shipped verifier instructions, not live model compliance.

Read only the three repository instruction sources. No plugin imports, live profiles,
board operations, proof execution, provider calls or third-party dependencies.
"""
import re
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
POLICY_PATHS = (
    "templates/profiles/verifier/SOUL.md",
    "skills/crew-verifier/SKILL.md",
    "roles/briefs/verifier.md",
)


def normalized(text):
    return " ".join(text.split())


class VerifierPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = {
            path: normalized((REPO / path).read_text(encoding="utf-8"))
            for path in POLICY_PATHS
        }

    def require_each(self, pattern):
        for path, source in self.sources.items():
            with self.subTest(path=path):
                self.assertRegex(source, pattern)

    def test_no_blanket_native_file_tool_denial(self):
        for path, source in self.sources.items():
            with self.subTest(path=path):
                self.assertNotRegex(
                    source,
                    r"(?i)(?:no write tools|(?:you have no|without any) write,? patch|"
                    r"you never (?:write|fix)\b)",
                )

    def test_only_assigned_card_exact_script_not_general_crew_writes(self):
        self.require_each(r"only file you may (?:write|create).*assigned card.*exact.*\.crew/")
        self.require_each(r"owner-confirmed proof command")
        self.require_each(r"No other.*\.crew/.*(?:file|path)")

    def test_product_and_artifact_boundaries_remain_explicit(self):
        self.require_each(r"Never (?:write|edit).*product.*artifacts.*tests.*docs.*specs")

    def test_initial_creation_is_missing_first_and_contract_derived(self):
        self.require_each(r"(?:missing|absent).*FIRST.*write_file")
        self.require_each(r"owner-confirmed.*Done when.*Inputs")
        self.require_each(r"Initial creation.*only before.*ever run.*recreating.*missing.*revision authorization")

    def test_native_file_approvals_remain_required(self):
        self.require_each(r"native.*write_file.*patch.*Hermes approvals")
        self.require_each(r"(?:Never|Do not) bypass.*approval")

    def test_revision_needs_real_native_coordinator_delegation(self):
        self.require_each(r"After (?:the )?first run.*revise_script.*delegate: true")
        self.require_each(r"pending native authorization event.*(?:this|same) card")
        self.require_each(r"(?:handoff|handed).*verifier.*review")

    def test_body_marker_alone_is_not_authorization(self):
        self.require_each(r"Proof script: verifier to revise - <reason>.*alone is not authorization")

    def test_hash_binding_and_no_self_authorized_revision(self):
        self.require_each(r"script-hash binding")
        self.require_each(r"(?:Without|Missing).*authorization.*(?:do not|never).*revise")
        self.require_each(r"proof script changed since it first ran")

    def test_broken_harness_is_not_product_defect_or_pass(self):
        self.require_each(r"(?:broken|Broken) proof harness.*infrastructure.*not evidence of a product defect")
        self.require_each(r"raw error.*coordinator")
        self.require_each(r"(?:Never|Do not) weaken.*Done when.*(?:fabricate|invent).*PASS")

    def test_product_failure_goes_to_writer_without_new_verdict_schema(self):
        self.require_each(r"(?:sound proof|sound proof's).*contract violation.*writer|"
                          r"unmet contract clause.*sound proof.*product defect.*writer")
        self.require_each(r"(?:native verdict history|native verdict as recorded|existing FAIL history)")
        self.require_each(r"(?:not.*new verdict type|not by inventing another verdict type)")

    def test_no_unbounded_tools_or_settings_escape(self):
        self.require_each(r"No send, delegate or (?:arbitrary )?code-execute tools")
        self.require_each(r"(?:Never|Do not) bypass.*(?:shell|terminal).*(?:settings|toolsets)")

    def test_original_verdict_tool_and_one_extra_check_preserved(self):
        self.require_each(r"crew_verdict\(card=")
        self.require_each(r"[Ee]xactly [Oo][Nn][Ee] extra check")
        skill = self.sources["skills/crew-verifier/SKILL.md"]
        self.assertIn("`crew_verdict` takes no command", skill)
        self.assertIn("only the card's owner-confirmed proof command", skill)
        self.assertIn("No verdict file means no verdict", skill)
        self.assertIn("`BLOCKED`", skill)
        self.assertIn("nothing ran, no line", skill)

    def test_no_writer_self_grading_and_no_closure_shortcut(self):
        self.require_each(r"writer.*never.*(?:writes|write).*proof scripts")
        self.require_each(r"Two FAIL")
        self.require_each(r"kanban_complete.*PASS line.*(?:after|claimed)")
        self.require_each(r"(?:Never|never).*writer's.*(?:summary|transcript).*(?:evidence|reassurance)")

    def test_lint_patterns_reject_known_unsafe_policy_mutations(self):
        # These are lint-oracle controls, not a policy engine or an admission test.
        patterns = (
            (r"pending native authorization event.*this card",
             "pending native authorization event for this card",
             "Proof script body line for this card"),
            (r"alone is not authorization",
             "The body line alone is not authorization",
             "The body line alone is authorization"),
            (r"Never weaken.*Done when.*fabricate.*PASS",
             "Never weaken Done when or fabricate PASS",
             "Weaken Done when to produce PASS"),
            (r"only file you may write.*assigned card.*exact.*\.crew/",
             "The only file you may write is the assigned card's exact .crew/ script",
             "You may write any file under .crew/"),
        )
        for pattern, safe, unsafe in patterns:
            with self.subTest(pattern=pattern):
                self.assertIsNotNone(re.search(pattern, safe))
                self.assertIsNone(re.search(pattern, unsafe))


if __name__ == "__main__":
    unittest.main()
