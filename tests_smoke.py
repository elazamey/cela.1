"""اختبارات دخان (smoke) للمرحلة 2 — خضراء دون أي شبكة أو مفاتيح حقيقية.

Runs:  python tests_smoke.py

يغطي: node_analyzer (التعديل الحتمي)، npm_security (التجميع/الـ PRs)،
ci_healer (تنظيف السجلات/البصمات/التصنيف/التحقق من YAML)، ai_resolver
(استخراج YAML وبناء الـ prompts)، و web.recorder (SQLite).

ملاحظة: config.py يتحقق من المفاتيح عند الاستيراد، لذا نضبط قيماً وهمية
غير placeholder قبل أي استيراد للمشروع.
"""

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

# مفاتيح وهمية (ليست placeholder من .env.example فلا يرفضها Config).
os.environ.setdefault("GITHUB_TOKEN", "fake_github_token_for_tests")
os.environ.setdefault("GEMINI_API_KEY", "fake_gemini_key_for_tests")

# ضمان استيراد وحدات المشروع من جذر المستودع.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from node_analyzer import (  # noqa: E402
    PackageJsonError,
    apply_all_updates,
    apply_version_update,
    is_supported_manifest,
    parse_package_json,
    serialize_package_json,
)

PACKAGE_JSON = json.dumps(
    {
        "name": "demo-app",
        "version": "1.0.0",
        "dependencies": {"lodash": "^4.17.15", "axios": "0.21.1"},
        "devDependencies": {"eslint": "~8.0.0"},
        "scripts": {"test": "jest"},
    },
    indent=2,
)


class TestNodeAnalyzer(unittest.TestCase):
    def test_parse_and_serialize_roundtrip(self):
        pkg = parse_package_json(PACKAGE_JSON)
        self.assertEqual(pkg["name"], "demo-app")
        self.assertEqual(pkg["dependencies"]["lodash"], "^4.17.15")
        serialized = serialize_package_json(pkg)
        self.assertEqual(parse_package_json(serialized), pkg)

    def test_invalid_json_raises(self):
        with self.assertRaises(PackageJsonError):
            parse_package_json("{not-json")

    def test_direct_dependency_preserves_caret(self):
        updated, action = apply_version_update(PACKAGE_JSON, "lodash", "4.17.21")
        self.assertIn("^4.17.21", updated)
        self.assertIn("lodash", action)
        self.assertNotIn("4.17.15", updated)
        self.assertIn("axios", updated)  # الحزم الأخرى لم تتأثر

    def test_dev_dependency_preserves_tilde(self):
        updated, _ = apply_version_update(PACKAGE_JSON, "eslint", "8.57.0")
        self.assertIn("~8.57.0", updated)

    def test_missing_dependency_uses_overrides(self):
        # ثغرة transitive في حزمة غير مباشرة -> overrides
        updated, action = apply_version_update(PACKAGE_JSON, "minimist", "1.2.8")
        parsed = parse_package_json(updated)
        self.assertEqual(parsed["overrides"]["minimist"], "1.2.8")
        self.assertIn("overrides", action)

    def test_non_version_range_is_skipped(self):
        weird = json.dumps({"dependencies": {"pkg": "workspace:*"}}, indent=2)
        with self.assertRaises(PackageJsonError):
            apply_version_update(weird, "pkg", "1.0.0")

    def test_apply_all_updates(self):
        alerts = {"lodash": "4.17.21", "axios": "0.21.4"}
        updated, actions = apply_all_updates(PACKAGE_JSON, alerts)
        parsed = parse_package_json(updated)
        self.assertEqual(parsed["dependencies"]["lodash"], "^4.17.21")
        self.assertEqual(parsed["dependencies"]["axios"], "0.21.4")
        self.assertEqual(len(actions), 2)

    def test_supported_manifests(self):
        self.assertTrue(is_supported_manifest("package.json"))
        self.assertFalse(is_supported_manifest("requirements.txt"))
        self.assertFalse(is_supported_manifest("package-lock.json"))


class TestNpmSecurity(unittest.TestCase):
    def setUp(self):
        from npm_security import NpmSecurity

        self.NpmSecurity = NpmSecurity

    def _alert(self, name, patched, manifest="package.json", severity="high", ghsa="GHSA-x"):
        return {
            "package_name": name,
            "patched_version": patched,
            "manifest": manifest,
            "severity": severity,
            "ghsa_id": ghsa,
            "vulnerable_range": "<1.0",
            "summary": f"vuln in {name}",
            "number": 1,
        }

    def test_group_alerts_by_manifest(self):
        alerts = [
            self._alert("lodash", "4.17.21", manifest="package.json"),
            self._alert("minimist", "1.2.8", manifest="package.json"),
            self._alert("other", "1.0.0", manifest="not/package.json"),  # غير مدعوم
        ]
        grouped = self.NpmSecurity.group_alerts_by_manifest(alerts)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(len(grouped["package.json"]), 2)

    def test_fix_manifest_deterministic(self):
        updated, applied = self.NpmSecurity.fix_manifest(
            PACKAGE_JSON,
            [self._alert("lodash", "4.17.21"), self._alert("minimist", "1.2.8")],
        )
        parsed = parse_package_json(updated)
        self.assertEqual(parsed["dependencies"]["lodash"], "^4.17.21")
        self.assertEqual(parsed["overrides"]["minimist"], "1.2.8")
        self.assertEqual(len(applied), 2)
        # ثبات: نفس المدخلات تعطي نفس المخرجات تماماً.
        same_alerts = [self._alert("lodash", "4.17.21"), self._alert("minimist", "1.2.8")]
        updated2, _ = self.NpmSecurity.fix_manifest(PACKAGE_JSON, same_alerts)
        self.assertEqual(updated, updated2)

    def test_no_patched_version_skipped(self):
        updated, applied = self.NpmSecurity.fix_manifest(
            PACKAGE_JSON, [self._alert("lodash", "")]
        )
        self.assertEqual(applied, [])
        self.assertEqual(json.loads(updated)["dependencies"]["lodash"], "^4.17.15")

    def test_fake_repo_flow_dry_run(self):
        # دمج كامل مع مستودع وهمي (repo.get_contents يعيد محتوى package.json).
        class FakeContents:
            decoded_content = PACKAGE_JSON.encode()

        alerts = [
            self._alert("lodash", "4.17.21"),
            self._alert("minimist", "1.2.8"),
        ]
        repo = SimpleNamespace(
            name="demo",
            full_name="owner/demo",
            get_contents=lambda path: FakeContents(),
            get_pulls=lambda state, head: SimpleNamespace(totalCount=0),
        )
        gh = SimpleNamespace(
            user=SimpleNamespace(login="owner"),
            get_dependabot_alerts=lambda r, ecosystem="npm": alerts,
        )
        handler = self.NpmSecurity(gh, dry_run=True)
        stats = handler.process_repository(repo)
        self.assertEqual(stats["prs_created"], 1)
        # لا بد أن يُحتسب فرع package.json.
        branch = handler._security_branch_name("package.json")
        self.assertEqual(branch, "fix/dependabot-package-json")

    def test_fake_repo_flow_pr_created(self):
        # مسار غير dry-run: يفتح PR عبر create_fix_branch_and_pr الوهمية.
        class FakeContents:
            decoded_content = PACKAGE_JSON.encode()

        created = {}

        def fake_create_pr(repo, branch_name, file_path, content, commit_msg, pr_title, pr_body):
            created["branch"] = branch_name
            created["content"] = content
            return "https://github.com/owner/demo/pull/99"

        alerts = [self._alert("lodash", "4.17.21")]
        repo = SimpleNamespace(
            name="demo",
            full_name="owner/demo",
            get_contents=lambda path: FakeContents(),
            get_pulls=lambda state, head: SimpleNamespace(totalCount=0),
        )
        gh = SimpleNamespace(
            user=SimpleNamespace(login="owner"),
            get_dependabot_alerts=lambda r, ecosystem="npm": alerts,
            create_fix_branch_and_pr=fake_create_pr,
        )
        handler = self.NpmSecurity(gh, dry_run=False)
        stats = handler.process_repository(repo)
        self.assertEqual(stats["prs_created"], 1)
        self.assertEqual(created["branch"], "fix/dependabot-package-json")
        self.assertIn("^4.17.21", created["content"])

    def test_existing_open_pr_skipped(self):
        class FakeContents:
            decoded_content = PACKAGE_JSON.encode()

        class FakePulls:
            totalCount = 1

            def __getitem__(self, index):
                return SimpleNamespace(html_url="https://github.com/owner/demo/pull/9")

        repo = SimpleNamespace(
            name="demo",
            full_name="owner/demo",
            get_contents=lambda path: FakeContents(),
            get_pulls=lambda state, head: FakePulls(),
        )
        gh = SimpleNamespace(
            user=SimpleNamespace(login="owner"),
            get_dependabot_alerts=lambda r, ecosystem="npm": [self._alert("lodash", "4.17.21")],
        )
        handler = self.NpmSecurity(gh, dry_run=False)
        stats = handler.process_repository(repo)
        self.assertEqual(stats["prs_already_open"], 1)
        self.assertEqual(stats["prs_created"], 0)


class TestCiHealer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ci_healer import CiHealer

        cls.CiHealer = CiHealer

    LOG = (
        "2024-01-01T10:00:01.123456Z Starting job\n"
        "2024-01-01T10:00:02.000000Z Run npm ci\n"
        "2024-01-01T10:00:03.000000Z npm error code ERESOLVE\n"
        "2024-01-01T10:00:04.000000Z npm error Could not resolve dependency\n"
        "2024-01-01T10:00:05.000000Z npm error Found: lodash@4.17.15\n"
        "2024-01-01T10:00:06.000000Z npm error Fix the upstream dependency\n"
        "2024-01-01T10:00:07.000000Z Error: Process completed with exit code 1.\n"
        "2024-01-01T10:00:08.000000Z Cleaning up\n"
    )

    def test_clean_log_line_strips_timestamp(self):
        self.assertEqual(
            self.CiHealer.clean_log_line("2024-01-01T10:00:00Z npm error x"), "npm error x"
        )
        self.assertEqual(self.CiHealer.clean_log_line("\x1b[31mred text"), "red text")

    def test_extract_error_lines_captures_context(self):
        lines = self.CiHealer.extract_error_lines(self.LOG)
        text = "\n".join(lines)
        self.assertIn("npm error code ERESOLVE", text)
        self.assertIn("npm error Could not resolve dependency", text)
        self.assertIn("exit code 1", text)
        # أسطر الأخطاء تغطيها سياقها؛ سطر أبعد (بعد 3 أسطر على الأقل من آخر خطأ)
        # لا يُقتطف كخطأ بحد ذاته في الاختبارات الواقعية (فحص ترتيب بسيط):
        self.assertLess(text.index("npm error code ERESOLVE"), text.index("exit code 1"))

    def test_summarize_log_reasonable_size(self):
        summary = self.CiHealer.summarize_log(self.LOG, max_chars=5000)
        self.assertIn("ERESOLVE", summary)
        self.assertLessEqual(len(summary), 5000)
        # سجل فارغ
        self.assertEqual(self.CiHealer.summarize_log("", max_chars=100), "")

    def test_summarize_without_error_keeps_tail(self):
        plain = "\n".join(f"2024-01-01T10:00:{i:02d}Z step {i}" for i in range(200))
        summary = self.CiHealer.summarize_log(plain, max_chars=4000)
        self.assertIn("step 199", summary)

    def test_fingerprint_stable_and_distinct(self):
        fp1 = self.CiHealer.fingerprint(self.LOG, ".github/workflows/ci.yml")
        fp2 = self.CiHealer.fingerprint(self.LOG, ".github/workflows/ci.yml")
        fp3 = self.CiHealer.fingerprint(self.LOG, ".github/workflows/other.yml")
        other_log = self.LOG.replace("lodash@4.17.15", "lodash@4.17.21")
        fp4 = self.CiHealer.fingerprint(other_log, ".github/workflows/ci.yml")
        self.assertEqual(fp1, fp2)
        self.assertNotEqual(fp1, fp3)  # workflow مختلف
        self.assertNotEqual(fp1, fp4)  # فشل مختلف
        self.assertEqual(len(fp1), 12)

    def test_classify_failure(self):
        self.assertEqual(self.CiHealer.classify_failure("npm error code ERESOLVE"), "npm")
        self.assertEqual(self.CiHealer.classify_failure("ModuleNotFoundError: No module named x"), "python")
        self.assertEqual(self.CiHealer.classify_failure("failed to solve: docker build"), "docker")
        self.assertEqual(self.CiHealer.classify_failure("bash: line 3: foo: command not found"), "shell")
        self.assertEqual(self.CiHealer.classify_failure("some weird output"), "unknown")

    def test_workflow_base(self):
        self.assertEqual(
            self.CiHealer._workflow_base(".github/workflows/ci.yml"), "ci"
        )
        self.assertEqual(
            self.CiHealer._workflow_base("deploy-test.yaml"), "deploy-test"
        )

    def test_plausible_workflow_check(self):
        good = """name: CI\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"""
        self.assertTrue(self.CiHealer._is_plausible_workflow(good))
        self.assertFalse(self.CiHealer._is_plausible_workflow("not: [valid"))
        self.assertFalse(self.CiHealer._is_plausible_workflow("hello: world"))


class TestAiResolver(unittest.TestCase):
    def test_extract_fenced_yaml(self):
        from ai_resolver import AIResolver

        text = "Some explanation\n```yaml\nname: CI\non: push\njobs: {}\n```\nfooter"
        self.assertEqual(AIResolver.extract_fenced_yaml(text), "name: CI\non: push\njobs: {}")
        self.assertIsNone(AIResolver.extract_fenced_yaml("no fence here"))
        self.assertIsNone(AIResolver.extract_fenced_yaml("```python\nx=1\n```"))

    def test_strip_code_fences(self):
        from ai_resolver import AIResolver

        self.assertEqual(AIResolver._strip_code_fences("```md\ncontent\n```"), "content")

    def test_build_ci_diagnosis_prompt_contains_context(self):
        from ai_resolver import AIResolver

        prompt = AIResolver.build_ci_diagnosis_prompt(
            repo_name="owner/demo",
            workflow_path=".github/workflows/ci.yml",
            workflow_content="name: CI\non: push",
            run_name="CI #12",
            head_sha="abc123",
            failure_family="npm",
            failure_excerpt="npm error code ERESOLVE",
        )
        self.assertIn("owner/demo", prompt)
        self.assertIn("npm error code ERESOLVE", prompt)
        self.assertIn("```yaml", prompt)


class TestWebRecorder(unittest.TestCase):
    def test_recorder_roundtrip(self):
        from web.recorder import Recorder

        with tempfile.TemporaryDirectory() as tmp:
            rec = Recorder(os.path.join(tmp, "celia.db"))
            run_id = rec.start_run(mode="test")
            rec.add_event(run_id, level="info", message="hello", repo="owner/demo")
            rec.add_event(run_id, level="warning", message="careful")
            rec.finish_run(run_id, repos_scanned=1, prs_created=2, errors=0)

            runs = rec.get_runs()
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["prs_created"], 2)
            self.assertEqual(runs[0]["mode"], "test")

            events = rec.get_events(run_id)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0]["message"], "hello")
            self.assertEqual(events[0]["repo"], "owner/demo")
            self.assertEqual(events[1]["level"], "warning")

            # after_id للتحديثات الجزئية
            self.assertEqual(len(rec.get_events(run_id, after_id=events[0]["id"])), 1)

            # demo seed يعمل على قاعدة فارغة فقط
            rec.delete_all()
            from web.recorder import demo_seed

            demo_seed(rec, runs=1, events_per_run=2)
            self.assertEqual(len(rec.get_runs(limit=10)), 1)
            rec.close()


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(__import__("__main__"))
    # التحميل الصريح لكل الفئات ليطبع التقدم.
    loader = unittest.TestLoader()
    all_tests = loader.loadTestsFromModule(__import__(__name__))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(all_tests)
    sys.exit(0 if result.wasSuccessful() else 1)
