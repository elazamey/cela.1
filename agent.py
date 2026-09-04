"""الوكيل الرئيسي: فحص جميع المستودعات، توليد الإصلاحات عبر الذكاء الاصطناعي،
ثم إنشاء الفروع و Pull Requests أو نشر اقتراحات الحلول كتعليقات.

Entry point for Celia Repo Agent.

Usage:
    python agent.py             # audit + open PRs / post comments
    python agent.py --dry-run   # audit only, log what would be done
"""

import argparse
import logging

from github import GithubException

from ai_resolver import AIResolver
from config import Config
from github_service import AGENT_BOT_SIGNATURE, GitHubService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("celia-agent")


class RepoAgent:
    """ينسّق بين طبقة GitHub ومحرك الذكاء الاصطناعي."""

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run or Config.DRY_RUN
        self.github = GitHubService()
        self.ai = AIResolver()
        if self.dry_run:
            logger.info("🔎 وضع المعاينة DRY-RUN مفعّل: لن يتم إنشاء أي PR أو تعليق.")

    # ------------------------------------------------------------------ #
    # Main flow
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        logger.info("🚀 بدء تشغيل وكيل إدارة المستودعات (Celia Repo Agent)...")
        repos = self.github.get_all_repositories()
        logger.info("تم العثور على %d مستودع مملوك للمستخدم.", len(repos))

        for repo in repos:
            try:
                self._process_repository(repo)
            except GithubException as exc:
                # خطأ في مستودع واحد يجب ألا يوقف الفحص بالكامل.
                logger.error("❌ تخطّي المستودع %s بسبب خطأ GitHub: %s", repo.name, exc)
            except Exception as exc:  # noqa: BLE001 - keep the cron run alive
                logger.exception("❌ خطأ غير متوقع أثناء معالجة %s: %s", repo.name, exc)

        logger.info("🏁 انتهت عملية الفحص والإصلاح.")

    def _process_repository(self, repo) -> None:
        logger.info("🔍 فحص المستودع: %s", repo.full_name)
        audit_result = self.github.audit_repository(repo)
        issues = audit_result["issues"]

        if not issues:
            logger.info("✅ المستودع %s لا يحتوي على مشاكل ملفات/CI ظاهرة.", repo.name)
        else:
            logger.info("⚠️ تم رصد %d مشكلة في %s.", len(issues), repo.name)
            for issue in issues:
                try:
                    self._handle_issue(repo, issue)
                except Exception as exc:  # noqa: BLE001 - isolate per-issue failures
                    logger.exception(
                        "❌ فشل التعامل مع مشكلة من النوع %s في %s: %s",
                        issue.get("type"),
                        repo.name,
                        exc,
                    )

        # فحص ثغرات Dependabot دائماً (حتى لو كانت الملفات الأساسية سليمة).
        try:
            self._process_dependabot_alerts(repo)
        except Exception as exc:  # noqa: BLE001 - لا تُوقف الفحص بسبب فشل أمني
            logger.exception("❌ فشل فحص Dependabot في %s: %s", repo.name, exc)

    # ------------------------------------------------------------------ #
    # Issue handlers
    # ------------------------------------------------------------------ #
    def _handle_issue(self, repo, issue: dict) -> None:
        issue_type = issue.get("type")

        if issue_type in ("missing_file", "missing_workflow"):
            self._fix_missing_file(repo, issue["target"])
        elif issue_type == "open_issue":
            self._suggest_fix_for_open_issue(repo, issue)
        else:
            logger.warning("نوع مشكلة غير معروف، يتم التخطّي: %s", issue_type)

    def _fix_missing_file(self, repo, target_file: str) -> None:
        """توليد محتوى الملف المفقود وفتح Pull Request لإضافته."""
        logger.info("🛠️ إصلاح الملف المفقود '%s' في %s...", target_file, repo.name)
        content = self.ai.generate_missing_file(repo.name, target_file)
        branch = self._branch_name(target_file)

        pr_title = f"Fix: Add missing {target_file}"
        pr_body = (
            f"{AGENT_BOT_SIGNATURE}\n\n"
            f"هذا الـ Pull Request يضيف الملف المفقود `{target_file}` "
            f"الذي تم رصده أثناء الفحص التلقائي.\n\n"
            f"تم توليد المحتوى المقترح بواسطة نموذج `{Config.GEMINI_MODEL}` - "
            "يُرجى مراجعته قبل الدمج."
        )

        if self.dry_run:
            logger.info(
                "🧪 [DRY-RUN] سيتم فتح PR على الفرع %s لإضافة %s (%d حرف).",
                branch,
                target_file,
                len(content),
            )
            return

        pr_url = self.github.create_fix_branch_and_pr(
            repo=repo,
            branch_name=branch,
            file_path=target_file,
            content=content,
            commit_msg=f"chore: add missing {target_file}",
            pr_title=pr_title,
            pr_body=pr_body,
        )
        logger.info("🎉 تم إنشاء Pull Request بنجاح: %s", pr_url)

    def _suggest_fix_for_open_issue(self, repo, issue: dict) -> None:
        """توليد اقتراح حل عبر الذكاء الاصطناعي ونشره كتعليق على المشكلة."""
        issue_number = issue["id"]
        logger.info(
            "💡 تحليل المشكلة المفتوحة #%s: %s", issue_number, issue["title"]
        )

        # تجنّب تكرار التعليق في كل تشغيل يومي.
        if not self.dry_run and self._already_commented(repo, issue_number):
            logger.info(
                "↪️ تم التعليق مسبقاً على المشكلة #%s في %s، يتم التخطّي.",
                issue_number,
                repo.name,
            )
            return

        suggestion = self.ai.solve_issue_code(
            repo_name=repo.name,
            issue_title=issue["title"],
            issue_body=issue.get("body", ""),
        )
        comment = (
            f"{AGENT_BOT_SIGNATURE}\n\n"
            f"اقتراح حل آلي للمشكلة **#{issue_number}: {issue['title']}**:\n\n"
            f"---\n\n{suggestion}\n\n"
            f"> ⚠️ هذا الاقتراح مُولَّد آلياً بواسطة `{Config.GEMINI_MODEL}` "
            "وقد يحتاج إلى مراجعة وتعديل قبل الاعتماد."
        )

        if self.dry_run:
            logger.info(
                "🧪 [DRY-RUN] سيتم نشر تعليق اقتراح حل على المشكلة #%s (%d حرف).",
                issue_number,
                len(comment),
            )
            return

        url = self.github.post_issue_comment(repo, issue_number, comment)
        logger.info("📝 تم نشر اقتراح الحل على المشكلة: %s", url)

    def _process_dependabot_alerts(self, repo) -> None:
        """جلب تنبيهات Dependabot، وتحديث ملفات الاعتماديات، وفتح PR أمني.

        تُجمَّع كل إصلاحات ملف اعتماديات واحد (مثل requirements.txt) في
        Pull Request واحد تراكمي، لأن كل PR يستبدل الملف كاملاً عن الفرع
        الأساسي؛ ففتح PR منفصل لكل حزمة كان سيُلغي إصلاح البقية.
        """
        alerts = self.github.get_dependabot_alerts(repo)
        if not alerts:
            return

        # تجميع التنبيهات حسب ملف الاعتماديات (manifest) — pip فقط حالياً.
        by_manifest: dict = {}
        for alert in alerts:
            if not alert.get("patched_version"):
                # لا توجد نسخة آمنة معروفة بعد: يحتاج تدخلاً يدوياً.
                logger.warning(
                    "🔶 لا توجد نسخة مُصلِحة لـ %s في %s بعد (GHSA %s) — يتطلب تدخلاً يدوياً.",
                    alert["package_name"],
                    repo.name,
                    alert.get("ghsa_id"),
                )
                continue
            by_manifest.setdefault(alert["manifest"], []).append(alert)

        for manifest_path, manifest_alerts in by_manifest.items():
            self._fix_vulnerable_manifest(repo, manifest_path, manifest_alerts)

    def _fix_vulnerable_manifest(self, repo, manifest_path: str, alerts: list) -> None:
        """تحديث ملف اعتماديات واحد لمعالجة كل الثغرات المُعطاة، ثم فتح PR."""
        branch_name = self._security_branch_name(manifest_path)

        # منع التكرار: تخطّي إن كان هناك PR مفتوح بنفس فرع الإصلاح الأمني.
        try:
            existing_prs = repo.get_pulls(
                state="open", head=f"{repo.owner.login}:{branch_name}"
            )
            if existing_prs.totalCount > 0:
                logger.info(
                    "⏭️ يوجد PR أمني مفتوح بالفعل لـ %s في %s (%s).",
                    manifest_path,
                    repo.name,
                    existing_prs[0].html_url,
                )
                return
        except GithubException:
            pass

        packages = ", ".join(
            f"{a['package_name']}->{a['patched_version']}" for a in alerts
        )
        logger.info(
            "🛡️ رصد %d ثغرة pip في %s بـ %s: %s",
            len(alerts),
            repo.name,
            manifest_path,
            packages,
        )

        if self.dry_run:
            logger.info(
                "🧪 [DRY-RUN] سيتم تحديث %s في %s للإصلاحات: %s",
                manifest_path,
                repo.name,
                packages,
            )
            return

        # جلب المحتوى الحالي لملف الاعتماديات.
        try:
            req_file = repo.get_contents(manifest_path)
            current_content = req_file.decoded_content.decode("utf-8")
        except GithubException:
            logger.warning(
                "🔶 تعذّر العثور على %s في %s رغم وروده في تنبيهات "
                "Dependabot — يتم التخطّي.",
                manifest_path,
                repo.name,
            )
            return

        # تطبيق كل تحديثات النسخ بالتتابع على نفس المحتوى.
        updated_content = current_content
        applied = []
        for alert in alerts:
            updated_content = self.ai.update_requirements_file(
                updated_content, alert["package_name"], alert["patched_version"]
            )
            applied.append(alert)

        if updated_content.strip() == current_content.strip():
            logger.info(
                "↪️ لم ينتج عن التحديث أي تغيير في %s بـ %s — يتم التخطّي.",
                manifest_path,
                repo.name,
            )
            return

        # بناء رسالة الالتزام وعنوان/جسم الـ PR.
        pkg_versions = ", ".join(
            f"{a['package_name']} {a['patched_version']}" for a in applied
        )
        ghsas = ", ".join(a.get("ghsa_id", "?") for a in applied)
        commit_msg = f"security: upgrade {pkg_versions} to fix {ghsas}"

        severities = sorted({(a.get("severity") or "?").lower() for a in applied})
        pr_title = (
            f"Security: fix {len(applied)} vulnerable package"
            f"{'s' if len(applied) > 1 else ''} in {manifest_path}"
        )

        lines = [""]
        for a in applied:
            lines.append(
                f"| `{a['package_name']}` | {a.get('vulnerable_range') or '—'} "
                f"| **{a['patched_version']}** | {a.get('severity') or '—'} "
                f"| [{a.get('ghsa_id')}](https://github.com/advisories/{a.get('ghsa_id')}) "
                f"| {a.get('summary') or ''} |"
            )
        pr_body = (
            f"{AGENT_BOT_SIGNATURE}\n\n"
            f"هذا الـ Pull Request يحدّث **{manifest_path}** لمعالجة "
            f"{len(applied)} من تنبيهات Dependabot (الخطورة: {', '.join(severities)}).\n\n"
            "| الحزمة | النطاق المُصاب | النسخة الآمنة | الخطورة | الإرشاد | الملخص |\n"
            "| --- | --- | --- | --- | --- | --- |"
            + "\n".join(lines)
            + f"\n\n> ⚠️ تم توليد التحديث آلياً بواسطة `{Config.GEMINI_MODEL}` — "
            "يُرجى مراجعة التوافق وتشغيل الاختبارات قبل الدمج."
        )

        pr_url = self.github.create_fix_branch_and_pr(
            repo=repo,
            branch_name=branch_name,
            file_path=manifest_path,
            content=updated_content,
            commit_msg=commit_msg,
            pr_title=pr_title,
            pr_body=pr_body,
        )
        logger.info("🎉 تم فتح PR الإصلاح الأمني: %s", pr_url)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _security_branch_name(manifest_path: str) -> str:
        """اسم فرع إصلاح أمني ثابت لكل ملف اعتماديات، مثل fix/dependabot-requirements-txt."""
        safe = manifest_path.lower()
        for ch in ("/", ".", "_", " "):
            safe = safe.replace(ch, "-")
        while "--" in safe:
            safe = safe.replace("--", "-")
        return f"fix/dependabot-{safe.strip('-')}"

    @staticmethod
    def _branch_name(target_file: str) -> str:
        """تحويل مسار ملف إلى اسم فرع آمن مثل fix/missing-readme-md."""
        safe = target_file.lower()
        for ch in ("/", ".", "_", " "):
            safe = safe.replace(ch, "-")
        while "--" in safe:
            safe = safe.replace("--", "-")
        safe = safe.strip("-")
        return f"fix/missing-{safe}"

    @staticmethod
    def _already_commented(repo, issue_number: int) -> bool:
        """هل سبق للوكيل نشر تعليق على هذه المشكلة؟"""
        try:
            issue = repo.get_issue(number=issue_number)
            for comment in issue.get_comments():
                if AGENT_BOT_SIGNATURE in (comment.body or ""):
                    return True
        except GithubException:
            pass
        return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Celia Repo Agent - audit GitHub repos and auto-open fixes."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="فحص المستودعات وعرض الإجراءات فقط بدون إنشاء PRs أو تعليقات.",
    )
    args = parser.parse_args()

    agent = RepoAgent(dry_run=args.dry_run)
    agent.run()


if __name__ == "__main__":
    main()
