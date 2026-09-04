"""الوكيل الرئيسي: فحص جميع المستودعات، توليد الإصلاحات عبر الذكاء الاصطناعي،
ثم إنشاء الفروع و Pull Requests أو نشر اقتراحات الحلول كتعليقات.

Entry point for Celia Repo Agent.

Usage:
    python agent.py             # audit + open PRs / post comments
    python agent.py --dry-run   # audit only, log what would be done
    python agent.py --repo elazamey/cela.1   # audit a single repository
"""

import argparse
import logging
import os
from typing import Optional

from github import GithubException

from ai_resolver import AIResolver
from ci_healer import CiHealer
from config import Config
from github_service import AGENT_BOT_SIGNATURE, GitHubService
from npm_security import NpmSecurity

# تسجيل أحداث SQLite اختياري (تفعّله اللوحة؛ الفشل فيه لا يكسر التشغيل).
try:
    from web.recorder import Recorder as _Recorder

    _recorder = _Recorder() if os.environ.get("CELIA_DB_PATH") else None
except Exception:  # noqa: BLE001 - الوكيل يعمل حتى بدون مسجل
    _recorder = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("celia-agent")


class RepoAgent:
    """ينسّق بين طبقة GitHub ومحرك الذكاء الاصطناعي."""

    def __init__(
        self,
        dry_run: bool = False,
        npm_security: bool = True,
        ci_heal: bool = True,
        repo: Optional[str] = None,
    ) -> None:
        self.dry_run = dry_run or Config.DRY_RUN
        self.github = GitHubService()
        self.ai = AIResolver()
        self.npm_security = npm_security
        self.ci_heal = ci_heal
        self.repo = (repo or "").strip() or None
        self.run_id = None
        self.finish_status = "finished"
        self.stats = {
            "repos_scanned": 0,
            "prs_created": 0,
            "comments_posted": 0,
            "errors": 0,
        }
        if _recorder is not None:
            if self.repo:
                mode = "targeted"
            else:
                mode = "dry-run" if self.dry_run else "auto"
            self.run_id = _recorder.start_run(
                mode=mode,
                meta={"repo": self.repo} if self.repo else None,
            )
        if self.dry_run:
            logger.info("🔎 وضع المعاينة DRY-RUN مفعّل: لن يتم إنشاء أي PR أو تعليق.")
        if self.repo:
            logger.info("🎯 وضع الفحص المستهدف: %s", self.repo)

    # ------------------------------------------------------------------ #
    # Event recording
    # ------------------------------------------------------------------ #
    def _event(self, level: str, message: str, repo_name: str = "") -> None:
        if _recorder is not None:
            _recorder.add_event(self.run_id, level=level, message=message, repo=repo_name)

    # ------------------------------------------------------------------ #
    # Main flow
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        logger.info("🚀 بدء تشغيل وكيل إدارة المستودعات (Celia Repo Agent)...")
        try:
            if self.repo:
                repo = self.github.get_repository(self.repo)
                if repo is None:
                    self.stats["errors"] = 1
                    self.finish_status = "error"
                    logger.error(
                        "❌ لم يُعثر على المستودع المطلوب: %s — لن يتم أي فحص.",
                        self.repo,
                    )
                    self._event(
                        "error", f"المستودع المطلوب غير موجود: {self.repo}"
                    )
                    return
                repos = [repo]
                logger.info("🎯 فحص مستهدف: %s", repo.full_name)
                self._event("info", f"🎯 فحص مستهدف: {repo.full_name}", repo.name)
            else:
                repos = self.github.get_all_repositories()
                logger.info("تم العثور على %d مستودع مملوك للمستخدم.", len(repos))

            for repo in repos:
                self.stats["repos_scanned"] += 1
                try:
                    self._process_repository(repo)
                except GithubException as exc:
                    # خطأ في مستودع واحد يجب ألا يوقف الفحص بالكامل.
                    self.stats["errors"] += 1
                    logger.error("❌ تخطّي المستودع %s بسبب خطأ GitHub: %s", repo.name, exc)
                    self._event("error", f"تخطّي {repo.name}: خطأ GitHub", repo.name)
                except Exception as exc:  # noqa: BLE001 - keep the cron run alive
                    self.stats["errors"] += 1
                    logger.exception("❌ خطأ غير متوقع أثناء معالجة %s: %s", repo.name, exc)
                    self._event("error", f"خطأ غير متوقع في {repo.name}: {exc}", repo.name)
        finally:
            if _recorder is not None and self.run_id is not None:
                _recorder.finish_run(
                    run_id=self.run_id,
                    status=self.finish_status,
                    repos_scanned=self.stats["repos_scanned"],
                    prs_created=self.stats["prs_created"],
                    comments_posted=self.stats["comments_posted"],
                    errors=self.stats["errors"],
                )
                if self.finish_status != "error":
                    self._event("info", "🏁 انتهت عملية الفحص والإصلاح.")
        if self.finish_status != "error":
            logger.info("🏁 انتهت عملية الفحص والإصلاح.")

    def _process_repository(self, repo) -> None:
        logger.info("🔍 فحص المستودع: %s", repo.full_name)
        self._event("info", f"🔍 فحص المستودع: {repo.full_name}", repo.name)
        audit_result = self.github.audit_repository(repo)
        issues = audit_result["issues"]

        if not issues:
            logger.info("✅ المستودع %s لا يحتوي على مشاكل ملفات/CI ظاهرة.", repo.name)
        else:
            logger.info("⚠️ تم رصد %d مشكلة في %s.", len(issues), repo.name)
            self._event("warning", f"رصد {len(issues)} مشكلة في {repo.name}", repo.name)
            for issue in issues:
                try:
                    self._handle_issue(repo, issue)
                except Exception as exc:  # noqa: BLE001 - isolate per-issue failures
                    self.stats["errors"] += 1
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
            self.stats["errors"] += 1
            logger.exception("❌ فشل فحص Dependabot في %s: %s", repo.name, exc)

        # فحص أمان npm (Node.js) — تحديث package.json حتمي.
        if self.npm_security:
            try:
                self._process_npm_security(repo)
            except Exception as exc:  # noqa: BLE001
                self.stats["errors"] += 1
                logger.exception("❌ فشل فحص أمان npm في %s: %s", repo.name, exc)

        # معالجة فشل الـ CI (تشخيص + PR إصلاح workflow).
        if self.ci_heal:
            try:
                self._process_ci_healing(repo)
            except Exception as exc:  # noqa: BLE001
                self.stats["errors"] += 1
                logger.exception("❌ فشل معالجة CI في %s: %s", repo.name, exc)

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
        self.stats["prs_created"] += 1
        logger.info("🎉 تم إنشاء Pull Request بنجاح: %s", pr_url)
        self._event("success", f"🎉 PR لإضافة {target_file}: {pr_url}", repo.name)

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
        self.stats["comments_posted"] += 1
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
        self.stats["prs_created"] += 1
        logger.info("🎉 تم فتح PR الإصلاح الأمني: %s", pr_url)

    # ------------------------------------------------------------------ #
    # npm (Node.js) security + CI healing (Phase 2)
    # ------------------------------------------------------------------ #
    def _process_npm_security(self, repo) -> None:
        """معالجة تنبيهات Dependabot npm بتحديث package.json حتمي وفتح PR."""
        handler = NpmSecurity(self.github, dry_run=self.dry_run)
        stats = handler.process_repository(repo)
        if stats["prs_created"]:
            if not self.dry_run:
                self.stats["prs_created"] += stats["prs_created"]
            self._event(
                "success",
                f"🛡️ فتح {stats['prs_created']} PR أمني npm في {repo.name}",
                repo.name,
            )
        elif stats["prs_already_open"]:
            self._event(
                "info",
                f"⏭️ PRs npm مفتوحة مسبقاً في {repo.name} — تخطّي.",
                repo.name,
            )

    def _process_ci_healing(self, repo) -> None:
        """تشخيص فشل الـ CI عبر Gemini وفتح PR workflow مصحح."""
        healer = CiHealer(self.github, self.ai, dry_run=self.dry_run)
        stats = healer.heal_repository(repo)
        if stats["failed_runs"]:
            self._event(
                "warning"
                if stats["prs_created"] == 0
                else "success",
                f"🩺 CI في {repo.name}: {stats['failed_runs']} تشغيل فاشل، "
                f"{stats['prs_created']} PR إصلاح.",
                repo.name,
            )
            if stats["prs_created"] and not self.dry_run:
                self.stats["prs_created"] += stats["prs_created"]

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
    parser.add_argument(
        "--no-npm-security",
        action="store_true",
        help="تعطيل فحص ثغرات Dependabot npm وتحديث package.json.",
    )
    parser.add_argument(
        "--no-ci-heal",
        action="store_true",
        help="تعطيل تشخيص فشل الـ CI وفتح PRs إصلاح workflows.",
    )
    parser.add_argument(
        "--repo",
        default=None,
        metavar="OWNER/NAME",
        help=(
            "فحص مستودع واحد محدد فقط (مثل elazamey/cela.1 أو cela.1) "
            "بدلاً من فحص جميع المستودعات المملوكة."
        ),
    )
    args = parser.parse_args()

    agent = RepoAgent(
        dry_run=args.dry_run,
        npm_security=not args.no_npm_security,
        ci_heal=not args.no_ci_heal,
        repo=args.repo,
    )
    agent.run()


if __name__ == "__main__":
    main()
