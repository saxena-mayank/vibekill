"""Aggregation tests: the evidence gate, dedup, corroboration and severity policy.

This is the layer that decides whether people trust the output, so the invariants
here are the ones most worth defending:

* a model finding that quotes code which is not there does not reach the report
* a model cannot single-handedly escalate something to `blocker`
* one defect produces one entry, however many agents reported it
"""

from __future__ import annotations

from conftest import make_context
from vibekill.aggregate import aggregate, decide_verdict
from vibekill.models import AgentResult, Category, Finding, Severity, Verdict
from vibekill.settings import Settings

LINES = [
    "def handler(request):",
    "    name = request.get('name')",
    "    query = 'SELECT * FROM users WHERE n = ' + name",
    "    return run(query)",
]


def context():
    return make_context("app/api.py", LINES)


def llm_finding(**kwargs) -> Finding:
    """A model finding that would pass the evidence gate unless overridden."""
    defaults: dict = {
        "title": "SQL injection in the user lookup",
        "body": "The name value reaches the query text unescaped.",
        "category": Category.SECURITY,
        "severity": Severity.MAJOR,
        "path": "app/api.py",
        "line": 3,
        "evidence": "query = 'SELECT * FROM users WHERE n = ' + name",
        "agent": "security",
        "source": "llm",
        "confidence": 0.8,
    }
    defaults.update(kwargs)
    return Finding(**defaults)


def run_aggregate(llm=(), deterministic=(), **overrides):
    settings = Settings(llm={"provider": "none"}, **overrides)
    agent = AgentResult(name="security", findings=list(llm))
    return aggregate(context(), [agent], list(deterministic), settings)


class TestEvidenceGate:
    def test_keeps_finding_whose_quote_matches_the_cited_line(self):
        result = run_aggregate(llm=[llm_finding()])
        assert len(result.findings) == 1
        assert "evidence-verified" in result.findings[0].tags
        assert result.dropped_unverified == 0

    def test_drops_finding_quoting_code_that_is_not_there(self):
        """The core defence against a model inventing a location."""
        result = run_aggregate(llm=[llm_finding(evidence="os.system(user_input)")])
        assert result.findings == []
        assert result.dropped_unverified == 1

    def test_drops_finding_on_a_file_not_in_the_diff(self):
        result = run_aggregate(llm=[llm_finding(path="app/other.py")])
        assert result.findings == []
        assert result.dropped_unverified == 1

    def test_relocates_a_correct_quote_with_a_wrong_line_number(self):
        """The quote is real, only the number is off; fix it rather than discard."""
        result = run_aggregate(llm=[llm_finding(line=99)])
        assert len(result.findings) == 1
        assert result.findings[0].line == 3
        assert "evidence-relocated" in result.findings[0].tags

    def test_tolerates_reindented_and_requoted_evidence(self):
        result = run_aggregate(
            llm=[llm_finding(evidence='query = "SELECT * FROM users WHERE n = " + name')]
        )
        assert len(result.findings) == 1

    def test_file_level_finding_without_evidence_is_dropped(self):
        result = run_aggregate(llm=[llm_finding(line=None, evidence="", confidence=0.9)])
        assert result.findings == []
        assert result.dropped_unverified == 1

    def test_gate_can_be_disabled(self):
        result = run_aggregate(
            llm=[llm_finding(evidence="not_in_the_file()")], require_evidence=False
        )
        assert len(result.findings) == 1
        assert "evidence-unverified" in result.findings[0].tags

    def test_deterministic_findings_bypass_the_gate(self):
        """Tool output is ground truth; it is not asked to prove itself."""
        tool = Finding(
            title="S608 possible SQL injection",
            body="",
            category=Category.SECURITY,
            severity=Severity.MAJOR,
            path="app/api.py",
            line=3,
            agent="ruff",
            source="tool",
            rule_id="ruff.S608",
            confidence=0.95,
        )
        result = run_aggregate(deterministic=[tool])
        assert len(result.findings) == 1


class TestCorroboration:
    def test_tool_agreement_raises_confidence(self):
        tool = Finding(
            title="S608 possible SQL injection",
            body="",
            category=Category.SECURITY,
            severity=Severity.MAJOR,
            path="app/api.py",
            line=3,
            agent="ruff",
            source="tool",
            rule_id="ruff.S608",
            confidence=0.95,
        )
        result = run_aggregate(llm=[llm_finding(title="Unescaped input reaches SQL")], deterministic=[tool])
        assert len(result.findings) == 1, "the two reports describe one defect"
        assert result.findings[0].corroborated_by


class TestSeverityPolicy:
    def test_uncorroborated_model_blocker_is_capped(self):
        """No model blocks a merge on its own judgement."""
        result = run_aggregate(
            llm=[llm_finding(severity=Severity.BLOCKER, category=Category.QUALITY, confidence=0.9)]
        )
        assert result.findings[0].severity is Severity.MAJOR
        assert "capped-uncorroborated" in result.findings[0].tags

    def test_high_confidence_verified_security_blocker_is_capped_without_corroboration(self):
        result = run_aggregate(
            llm=[llm_finding(severity=Severity.BLOCKER, category=Category.SECURITY, confidence=0.9)]
        )
        assert result.findings[0].severity is Severity.MAJOR

    def test_low_confidence_findings_are_suppressed(self):
        result = run_aggregate(llm=[llm_finding(confidence=0.1)], min_confidence=0.5)
        assert result.findings == []
        assert result.suppressed == 1


class TestDedup:
    def test_same_defect_from_two_agents_collapses(self):
        first = llm_finding(agent="security", title="SQL injection in user lookup")
        second = llm_finding(
            agent="quality", title="Query built by concatenation", category=Category.CORRECTNESS
        )
        settings = Settings(llm={"provider": "none"})
        result = aggregate(
            context(),
            [
                AgentResult(name="security", findings=[first]),
                AgentResult(name="quality", findings=[second]),
            ],
            [],
            settings,
        )
        assert len(result.findings) == 1
        assert result.deduped == 1

    def test_adjacent_defects_quoting_different_code_are_kept(self):
        """Two real problems on neighbouring lines must not collapse into one."""
        first = llm_finding(line=2, evidence="name = request.get('name')", title="Unvalidated input")
        second = llm_finding(line=3, title="SQL injection")
        settings = Settings(llm={"provider": "none"})
        result = aggregate(
            context(), [AgentResult(name="security", findings=[first, second])], [], settings
        )
        assert len(result.findings) == 2

    def test_merge_keeps_highest_allowed_severity(self):
        mild = llm_finding(agent="quality", severity=Severity.MINOR, category=Category.CORRECTNESS)
        severe = llm_finding(agent="security", severity=Severity.BLOCKER, confidence=0.9)
        settings = Settings(llm={"provider": "none"})
        result = aggregate(
            context(),
            [
                AgentResult(name="quality", findings=[mild]),
                AgentResult(name="security", findings=[severe]),
            ],
            [],
            settings,
        )
        assert len(result.findings) == 1
        assert result.findings[0].severity is Severity.MAJOR


class TestVerdict:
    def test_blocker_requests_changes(self):
        settings = Settings(fail_on=Severity.BLOCKER)
        findings = [llm_finding(severity=Severity.BLOCKER)]
        assert decide_verdict(findings, settings) is Verdict.REQUEST_CHANGES

    def test_minor_only_is_a_comment(self):
        settings = Settings(fail_on=Severity.BLOCKER)
        assert decide_verdict([llm_finding(severity=Severity.MINOR)], settings) is Verdict.COMMENT

    def test_nothing_found_approves(self):
        assert decide_verdict([], Settings()) is Verdict.APPROVE

    def test_threshold_is_configurable(self):
        settings = Settings(fail_on=Severity.MINOR)
        findings = [llm_finding(severity=Severity.MINOR)]
        assert decide_verdict(findings, settings) is Verdict.REQUEST_CHANGES


class TestReporting:
    def test_failed_agent_produces_a_warning(self):
        settings = Settings(llm={"provider": "none"})
        failed = AgentResult(name="security", error="model call failed: timeout")
        result = aggregate(context(), [failed], [], settings)
        assert any("security" in w for w in result.warnings)

    def test_skipped_files_are_reported(self):
        settings = Settings(llm={"provider": "none"})
        ctx = context()
        ctx.skipped_files = ["huge/generated.py"]
        result = aggregate(ctx, [], [], settings)
        assert any("huge/generated.py" in w for w in result.warnings)


def test_unrelated_nearby_tool_finding_does_not_corroborate_model_blocker():
    """Proximity alone must not manufacture confidence or blocker provenance."""
    model = llm_finding(severity=Severity.BLOCKER, category=Category.QUALITY)
    tool = Finding(
        title="Unused import",
        body="",
        category=Category.REDUNDANCY,
        severity=Severity.INFO,
        path="app/api.py",
        line=3,
        evidence="",
        agent="ruff",
        source="tool",
        rule_id="ruff.F401",
        confidence=0.95,
    )

    result = run_aggregate(llm=[model], deterministic=[tool])
    model_result = next(finding for finding in result.findings if finding.source == "llm")

    assert model_result.severity is Severity.MAJOR
    assert model_result.corroborated_by == []
