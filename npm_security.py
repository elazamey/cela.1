"""معالجة تنبيهات Dependabot الخاصة بمنظومة npm وتحديث package.json حتمياً.

npm (Node.js) security flow for Celia Repo Agent.

Pipeline (mirrors the pip flow but without any AI step):

    GitHub Dependabot alerts (ecosystem=npm)
        -> group by manifest (package.json)
        -> deterministic `package.json` bump via :mod:`node_analyzer`
        -> one security Pull Request per manifest, deduplicated by branch
"""

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from github import GithubException

from github_service import AGENT_BOT_SIGNATURE
from node_analyzer import (
    PackageJsonError,
    apply_all_updates,
    apply_version_update,
    is_supported_manifest,
    parse_package_json,
)

logger = logging.getLogger(__name__)

#: Ecosystem identifier used by GitHub Dependabot for npm packages.
NPM_ECOSYSTEM = "npm"


class NpmSecurity:
    """يجمع تنبيهات npm ويولّد تحديثات package.json حتمية ويفتح PRs أمنية."""

    def __init__(self, github_service: Any, dry_run: bool = False) -> None:
        self.github = github_service
        self.dry_run = dry_run

    # ------------------------------------------------------------------ #
    # Alert fetching
    # ------------------------------------------------------------------ #
    def get_npm_alerts(self, repo: Any) -> List[Dict[str, Any]]:
        """جلب تنبيهات Dependabot المفتوحة الخاصة بحزم npm فقط."""
        return self.github.get_dependabot_alerts(repo, ecosystem=NPM_ECOSYSTEM)

    # ------------------------------------------------------------------ #
    # Deterministic manifest update
    # ------------------------------------------------------------------ #
    @staticmethod
    def group_alerts_by_manifest(alerts: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        """تجميع التنبيهات حسب مسار ملف الاعتماديات."""
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for alert in alerts:
            manifest = (alert.get("manifest") or "package.json").strip()
            if not is_supported_manifest(manifest):
                continue
            grouped.setdefault(manifest, []).append(alert)
        return grouped

    @staticmethod
    def fix_manifest(content: str, alerts: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
        """تطبيق كل الترقيات الأمنية على package.json بشكل حتمي.

        Args:
            content: المحتوى الحالي لـ package.json.
            alerts: تنبيهات تحتوي package_name و patched_version.

        Returns:
            (المحتوى المحدَّث، قائمة التنبيهات التي طُبّق إصلاحها فعلياً).
        """
        # تحقق مبكر من أن الملف JSON صالح (حتى لو لم يُطبَّق أي تحديث).
        parse_package_json(content)
        applied: List[Dict[str, Any]] = []
        updated = content
        for alert in alerts:
            name = (alert.get("package_name") or "").strip()
            target = (alert.get("patched_version") or "").strip()
            if not name or not target:
                continue
            try:
                updated, _ = apply_version_update(updated, name, target)
            except PackageJsonError as exc:
                logger.warning(
                    "تخطّي تحديث %s في package.json (%s): %s", name, target, exc
                )
                continue
            applied.append(alert)
        return updated, applied

    # ------------------------------------------------------------------ #
    # Pull request orchestration (one PR per manifest)
    # ------------------------------------------------------------------ #
    def process_repository(self, repo: Any) -> Dict[str, int]:
        """معالجة كل تنبيهات npm في مستودع وفتح PR أمني واحد لكل package.json.

        Returns:
            إحصائية: {"prs_created": int, "prs_already_open": int, "skipped": int}.
        """
        stats = {"prs_created": 0, "prs_already_open": 0, "skipped": 0}
        alerts = self.get_npm_alerts(repo)
        if not alerts:
            return stats

        for manifest, manifest_alerts in self.group_alerts_by_manifest(alerts).items():
            fixable = [
                a
                for a in manifest_alerts
                if (a.get("patched_version") or "").strip()
            ]
            unfixable = len(manifest_alerts) - len(fixable)
            if unfixable:
                logger.warning(
                    "🔶 %d تنبيه npm في %s بـ %s بلا نسخة مُصلِحة بعد — تحتاج تدخلاً يدوياً.",
                    unfixable,
                    repo.name,
                    manifest,
                )
            if not fixable:
                stats["skipped"] += unfixable
                continue
            stats["skipped"] += unfixable
            outcome = self._fix_manifest_pr(repo, manifest, fixable)
            stats[outcome] += 1
        return stats

    def _fix_manifest_pr(
        self, repo: Any, manifest: str, alerts: List[Dict[str, Any]]
    ) -> str:
        """فتح (أو إرجاع) PR أمني واحد لملف package.json بعد تحديثه حتمياً."""
        branch_name = self._security_branch_name(manifest)
        owner = self.github.user.login if hasattr(self.github, "user") else repo.owner.login

        # منع التكرار عبر التشغيلات اليومية.
        try:
            existing_prs = repo.get_pulls(state="open", head=f"{owner}:{branch_name}")
            if existing_prs.totalCount > 0:
                logger.info(
                    "⏭️ يوجد PR أمني npm مفتوح بالفعل لـ %s في %s (%s).",
                    manifest,
                    repo.name,
                    existing_prs[0].html_url,
                )
                return "prs_already_open"
        except GithubException:
            pass

        # قراءة المحتوى الحالي.
        try:
            file_obj = repo.get_contents(manifest)
            current_content = file_obj.decoded_content.decode("utf-8")
        except GithubException:
            logger.warning(
                "🔶 تعذّر العثور على %s في %s رغم وروده في تنبيهات Dependabot — تخطّي.",
                manifest,
                repo.name,
            )
            return "skipped"

        # التحديث الحتمي (JSON خالص — بدون أي استدعاء نموذج).
        try:
            updates = {a["package_name"]: a["patched_version"] for a in alerts}
            updated_content, _ = apply_all_updates(current_content, updates)
        except PackageJsonError as exc:
            logger.warning("🔶 تخطّي %s في %s: %s", manifest, repo.name, exc)
            return "skipped"

        if updated_content.strip() == current_content.strip():
            logger.info("↪️ لم ينتج أي تغيير في %s بـ %s — تخطّي.", manifest, repo.name)
            return "skipped"

        # معلومات الـ PR.
        severities = sorted({(a.get("severity") or "?").lower() for a in alerts})
        ghsas = ", ".join(a.get("ghsa_id", "?") for a in alerts)
        summary_rows = "\n".join(
            f"| `{a['package_name']}` | {a.get('vulnerable_range') or '—'} "
            f"| **{a['patched_version']}** | {a.get('severity') or '—'} "
            f"| [{a.get('ghsa_id')}](https://github.com/advisories/{a.get('ghsa_id')}) "
            f"| {a.get('summary') or ''} |"
            for a in alerts
        )
        pr_title = (
            f"Security: fix {len(alerts)} vulnerable npm package"
            f"{'s' if len(alerts) > 1 else ''} in {manifest}"
        )
        pr_body = (
            f"{AGENT_BOT_SIGNATURE}\n\n"
            f"هذا الـ Pull Request يحدّث **{manifest}** لمعالجة {len(alerts)} "
            f"من تنبيهات Dependabot npm (الخطورة: {', '.join(severities)}).\n\n"
            "**طريقة الإصلاح:** تعديل حتمي (JSON parsing) بدون توليد ذكاء اصطناعي — "
            "الاعتماديات المباشرة تُرقّى في `dependencies`/`devDependencies` مع الحفاظ "
            "على معامل النطاق، والثغرات غير المباشرة تُعالج عبر `overrides`.\n\n"
            "| الحزمة | النطاق المُصاب | النسخة الآمنة | الخطورة | الإرشاد | الملخص |\n"
            "| --- | --- | --- | --- | --- | --- |\n"
            f"{summary_rows}\n"
            f"\n> إصلاحات ذات صلة: GHSA {ghsas} — يُرجى تشغيل `npm install` "
            "وتحديث `package-lock.json` بعد الدمج."
        )

        if self.dry_run:
            logger.info(
                "🧪 [DRY-RUN] سيُفتح PR أمني npm على الفرع %s لتحديث %s (%d حزمة).",
                branch_name,
                manifest,
                len(alerts),
            )
            return "prs_created"

        pr_url = self.github.create_fix_branch_and_pr(
            repo=repo,
            branch_name=branch_name,
            file_path=manifest,
            content=updated_content,
            commit_msg=f"security: upgrade npm packages in {manifest} ({ghsas})",
            pr_title=pr_title,
            pr_body=pr_body,
        )
        logger.info("🎉 تم فتح PR الإصلاح الأمني npm: %s", pr_url)
        return "prs_created"

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _security_branch_name(manifest_path: str) -> str:
        """اسم فرع ثابت: fix/dependabot-package-json."""
        safe = (manifest_path or "package.json").lower()
        for ch in ("/", ".", "_", " "):
            safe = safe.replace(ch, "-")
        while "--" in safe:
            safe = safe.replace("--", "-")
        return f"fix/dependabot-{safe.strip('-')}"

    @staticmethod
    def npm_alerts_to_json(alerts: List[Dict[str, Any]]) -> str:
        """تمثيل JSON للتشخيص (يُستخدم في اللوحة والسجلات)."""
        return json.dumps(alerts, ensure_ascii=False, default=str)
