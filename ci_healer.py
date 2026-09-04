"""معالج فشل CI: تحليل سجلات GitHub Actions الفاشلة واقتراح/فتح إصلاحات.

CI healer for Celia Repo Agent.

High-level flow per repository:

    1. ``scan_failed_runs`` - find recent completed-but-failed workflow runs
       on the default branch (Dependabot-internal runs are excluded).
    2. For every failed job, download its log and reduce it to a concise
       *failure excerpt* (context around error lines + tail).
    3. Compute a deterministic *failure fingerprint* from the excerpt so that
       repeated identical failures map to the same fix branch and are not
       re-opened every day.
    4. Classify the failure family (npm / python / docker / shell / unknown)
       and ask the AI resolver for a root-cause diagnosis + corrected
       workflow YAML.
    5. If the AI returns valid, changed workflow YAML -> open a fix PR on
       branch ``ci/fix-<workflow>-<fingerprint>``; otherwise log/skip
       (a bad model answer must never corrupt a workflow file).

Log downloads that fail (network / blob host blocked) are tolerated: the job
is still recorded so the agent can diagnose from step metadata alone.
"""

import hashlib
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from config import Config
from github_service import AGENT_BOT_SIGNATURE, JobLogDownloadError

try:
    import yaml as _yaml  # PyYAML اختياري للتحقق من صحة workflow
except Exception:  # noqa: BLE001
    _yaml = None

logger = logging.getLogger(__name__)

#: أسطر السجل تُنظَّف من طوابع الوقت وأنواع التلوين ANSI.
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\s+"
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

#: أنماط تدل على سطر خطأ يستحق الاقتطاف.
_ERROR_HINT_RE = re.compile(
    r"(error|failed|failure|fatal|exception|traceback|exit code|"
    r"npm error|npm err|##\[error\]|command not found|cannot find|"
    r"could not resolve|unable to|no such file|not recognized)",
    re.IGNORECASE,
)

#: تصنيفات عائلة الفشل مع أنماطها.
FAILURE_FAMILY_PATTERNS: List[Tuple[str, re.Pattern]] = [
    (
        "npm",
        re.compile(
            r"(npm error|npm err|yarn|node:internal|cannot find module|"
            r"Module not found|exit code 1.*npm|npm ci|npm install)",
            re.IGNORECASE,
        ),
    ),
    (
        "python",
        re.compile(
            r"(ModuleNotFoundError|ImportError|Traceback \(most recent call|"
            r"pip install|No matching distribution|python: can't open)",
            re.IGNORECASE,
        ),
    ),
    (
        "docker",
        re.compile(
            r"(docker:|buildx|failed to solve|unable to prepare context|"
            r"container .* exited|oci runtime)",
            re.IGNORECASE,
        ),
    ),
    (
        "shell",
        re.compile(
            r"(command not found|line \d+:|syntax error|no such file or directory|"
            r"permission denied|exit code \d+$)",
            re.IGNORECASE,
        ),
    ),
]


class CiHealer:
    """ينسّق فحص فشل الـ CI وتشخيصه وفتح PR الإصلاح."""

    def __init__(
        self,
        github_service: Any,
        ai_resolver: Any,
        dry_run: bool = False,
        lookback_days: int = 14,
        max_runs: int = 3,
        max_excerpt_chars: int = 9000,
    ) -> None:
        self.github = github_service
        self.ai = ai_resolver
        self.dry_run = dry_run or Config.DRY_RUN
        self.lookback_days = int(
            os.environ.get("CELIA_CI_LOOKBACK_DAYS") or lookback_days
        )
        self.max_runs = int(os.environ.get("CELIA_CI_MAX_RUNS") or max_runs)
        self.max_excerpt_chars = max_excerpt_chars

    # ------------------------------------------------------------------ #
    # Scanning
    # ------------------------------------------------------------------ #
    def scan_failed_runs(self, repo: Any) -> List[Dict[str, Any]]:
        """فحص المستودع وإرجاع وصف منظم لأحدث إخفاقات CI (مع مقتطفات السجلات)."""
        runs = self.github.get_recent_failed_runs(
            repo, lookback_days=self.lookback_days, max_runs=self.max_runs
        )
        failures: List[Dict[str, Any]] = []
        for run in runs:
            jobs: List[Dict[str, Any]] = []
            for job in self.github.get_failed_jobs(run):
                log_text: Optional[str] = None
                log_error: Optional[str] = None
                try:
                    log_text = self.github.download_job_log(job)
                except JobLogDownloadError as exc:
                    log_error = str(exc)
                    logger.warning("⚠️ %s", exc)
                except Exception as exc:  # noqa: BLE001
                    log_error = f"{type(exc).__name__}: {exc}"
                    logger.warning("⚠️ فشل تنزيل سجل: %s", log_error)

                excerpt = (
                    self.summarize_log(log_text, self.max_excerpt_chars)
                    if log_text
                    else "(تعذّر تنزيل السجل — التشخيص من بيانات الوظيفة فقط.)"
                )
                jobs.append(
                    {
                        "job_id": getattr(job, "id", None),
                        "job_name": getattr(job, "name", "?"),
                        "job_url": getattr(job, "html_url", ""),
                        "excerpt": excerpt,
                        "log_error": log_error,
                    }
                )
            failures.append(
                {
                    "run_id": getattr(run, "id", None),
                    "run_name": getattr(run, "name", "?"),
                    "workflow_path": getattr(run, "path", ""),
                    "head_branch": getattr(run, "head_branch", ""),
                    "head_sha": getattr(run, "head_sha", ""),
                    "html_url": getattr(run, "html_url", ""),
                    "created_at": getattr(run, "created_at", None),
                    "jobs": jobs,
                }
            )
        return failures

    # ------------------------------------------------------------------ #
    # Log reduction
    # ------------------------------------------------------------------ #
    @classmethod
    def clean_log_line(cls, line: str) -> str:
        """إزالة طوابع الوقت وترميزات الألوان وبياض الأطراف من سطر سجل."""
        line = _ANSI_RE.sub("", line)
        line = _TIMESTAMP_RE.sub("", line)
        return line.strip()

    @classmethod
    def extract_error_lines(cls, log_text: str, context: int = 3) -> List[str]:
        """إرجاع الأسطر المهمة (أخطاء + سياقها) بعد التنظيف.

        - أسطر تحتوي أنماط خطأ تُقتطف مع ``context`` أسطر قبلها وبعدها.
        - الأسطر الفارغة والطويلة جداً (>2000 حرف) تُستبعد.
        """
        lines = log_text.splitlines()
        interesting: List[str] = []
        mark = set()
        for idx, line in enumerate(lines):
            cleaned = cls.clean_log_line(line)
            if not cleaned:
                continue
            if _ERROR_HINT_RE.search(cleaned) and len(cleaned) <= 2000:
                for pos in range(max(0, idx - context), min(len(lines), idx + context + 1)):
                    mark.add(pos)
        for idx in sorted(mark):
            cleaned = cls.clean_log_line(lines[idx])
            if cleaned:
                interesting.append(cleaned)
        return interesting

    @classmethod
    def summarize_log(cls, log_text: str, max_chars: int = 9000) -> str:
        """اختزال سجل كامل إلى مقتطف مركّز على الخطأ (حتمي وقابل للاختبار).

        Strategy: error lines with context first; if none matched, keep the
        last ``tail`` non-empty lines (usually where the failure surfaces).
        The result is capped at ``max_chars``.
        """
        if not log_text:
            return ""
        lines = [cls.clean_log_line(l) for l in log_text.splitlines()]
        lines = [l for l in lines if l]

        error_lines = cls.extract_error_lines(log_text)
        if error_lines:
            selected = error_lines
        else:
            selected = lines[-120:]

        # إزالة التكرار المتتالي مع الحفاظ على الترتيب.
        deduped: List[str] = []
        for line in selected:
            if not deduped or deduped[-1] != line:
                deduped.append(line)

        text = "\n".join(deduped)
        if len(text) <= max_chars:
            return text

        # اقتطاع ذكي: بداية (تحتوي غالباً أول خطأ) + نهاية (تحتوي ملخص الفشل).
        head = deduped[: len(deduped) // 3]
        tail = deduped[-len(deduped) // 2 :]
        middle_note = "\n...[تم اقتطاع وسط السجل]...\n"
        budget = max_chars - len(middle_note)
        joined = ""
        for chunk in (head, tail):
            part = "\n".join(chunk)
            if len(joined) + len(part) + (1 if joined else 0) > budget // 2:
                part = part[: budget // 2]
            joined = f"{joined}\n{part}" if joined else part
        return joined[:budget] + middle_note

    # ------------------------------------------------------------------ #
    # Fingerprinting
    # ------------------------------------------------------------------ #
    @classmethod
    def fingerprint(cls, excerpt: str, workflow_path: str = "") -> str:
        """بصمة فشل حتمية من المقتطف + مسار الـ workflow.

        تُستخرج من أول 40 سطراً مهماً بعد تطبيع المسافات — نفس الفشل المتكرر
        (يومياً/عبر commits) ينتج نفس البصمة فيُتجنب تكرار PR.
        """
        normalized_lines: List[str] = []
        for line in (excerpt or "").splitlines():
            line = cls.clean_log_line(line)
            # تجاهل الأسطر المتغيّرة رقمياً (أرقام أسطر/أسطر مسار طويلة).
            if re.search(r"\b\d{4,}\b", line):
                continue
            normalized = re.sub(r"\s+", " ", line)
            if normalized and normalized not in normalized_lines:
                normalized_lines.append(normalized)
            if len(normalized_lines) >= 40:
                break
        seed = "\n".join(normalized_lines[:40])
        return hashlib.sha256(f"{workflow_path}\n{seed}".encode("utf-8")).hexdigest()[:12]

    # ------------------------------------------------------------------ #
    # Failure family classification
    # ------------------------------------------------------------------ #
    @staticmethod
    def classify_failure(excerpt: str) -> str:
        """تصنيف عائلة الفشل (npm/python/docker/shell/unknown) من المقتطف."""
        for family, pattern in FAILURE_FAMILY_PATTERNS:
            if pattern.search(excerpt or ""):
                return family
        return "unknown"

    # ------------------------------------------------------------------ #
    # Orchestration
    # ------------------------------------------------------------------ #
    def heal_repository(self, repo: Any) -> Dict[str, Any]:
        """معالجة إخفاقات CI في مستودع واحد وفتح PRs إصلاح (عبر AI).

        Returns:
            إحصائية بما تم: runs/jobs مشخصة، PRs مفتوحة، تخطّيات وأسبابها.
        """
        stats: Dict[str, Any] = {
            "failed_runs": 0,
            "failed_jobs": 0,
            "diagnosed": 0,
            "prs_created": 0,
            "prs_already_open": 0,
            "skipped": [],
        }
        failures = self.scan_failed_runs(repo)
        stats["failed_runs"] = len(failures)
        stats["failed_jobs"] = sum(len(f.get("jobs", [])) for f in failures)
        if not failures:
            return stats

        logger.info(
            "🩺 رصد %d تشغيل CI فاشل في %s (%d وظيفة).",
            stats["failed_runs"],
            repo.name,
            stats["failed_jobs"],
        )
        for failure in failures:
            for job in failure["jobs"]:
                outcome = self._handle_failed_job(repo, failure, job)
                if outcome == "created":
                    stats["prs_created"] += 1
                    stats["diagnosed"] += 1
                elif outcome == "already_open":
                    stats["prs_already_open"] += 1
                    stats["diagnosed"] += 1
                else:
                    stats["skipped"].append(
                        {"job": job["job_name"], "workflow": failure["workflow_path"], "reason": outcome}
                    )
        return stats

    def _handle_failed_job(self, repo: Any, failure: Dict[str, Any], job: Dict[str, Any]) -> str:
        """تشخيص وظيفة فاشلة واحدة: AI -> مقتطف يمهّد لفتح PR إن أمكن."""
        excerpt = job.get("excerpt", "")
        fp = self.fingerprint(excerpt, failure.get("workflow_path", ""))
        workflow_path = failure.get("workflow_path") or ".github/workflows/ci.yml"
        branch_name = f"ci/fix-{self._workflow_base(workflow_path)}-{fp}"

        # منع التكرار: نفس البصمة => نفس الفرع => نفس الـ PR المفتوح.
        try:
            owner = self.github.user.login
        except Exception:  # noqa: BLE001
            owner = repo.owner.login
        try:
            existing_prs = repo.get_pulls(state="open", head=f"{owner}:{branch_name}")
            if existing_prs.totalCount > 0:
                logger.info(
                    "⏭️ PR إصلاح CI مفتوح مسبقاً للبصمة %s في %s (%s).",
                    fp,
                    repo.name,
                    existing_prs[0].html_url,
                )
                return "already_open"
        except Exception:  # noqa: BLE001
            pass

        family = self.classify_failure(excerpt)
        current_workflow = self.github.read_file_content(repo, workflow_path)
        diagnosis = self.ai.diagnose_ci_failure(
            repo_name=repo.name,
            workflow_path=workflow_path,
            workflow_content=current_workflow or "(غير موجود)",
            run_name=failure.get("run_name", "?"),
            head_sha=failure.get("head_sha", ""),
            failure_family=family,
            failure_excerpt=excerpt,
        )
        fixed_yaml = self.ai.extract_fenced_yaml(diagnosis)
        if not fixed_yaml or not self._is_plausible_workflow(fixed_yaml):
            logger.warning(
                "🤖 لم يُرجع النموذج YAML workflow صالحاً لتشخيص %s (%s) — تخطّي بلا PR.",
                job["job_name"],
                failure.get("run_name"),
            )
            return "no_valid_yaml_from_ai"

        changed = fixed_yaml.strip() != (current_workflow or "").strip()
        if not changed:
            return "ai_produced_no_change"

        title = f"Fix: CI failure in {workflow_path} ({family})"
        body_lines = [
            AGENT_BOT_SIGNATURE,
            "",
            "شخّص الوكيل فشلاً متكرراً في CI وأنتج workflow مصححاً:",
            "",
            f"- **المسار**: `{workflow_path}`",
            f"- **التشغيل**: [{failure.get('run_name')}]({failure.get('html_url', '')})",
            f"- **الوظيفة**: `{job['job_name']}`",
            f"- **عائلة الفشل**: `{family}`",
            f"- **بصمة الفشل**: `{fp}`",
            f"- **الفرع**: `{branch_name}`",
            "",
            "مقتطف من السجل:",
            "",
            "```text",
            excerpt[:2500],
            "```",
            "",
            "> ⚠️ الإصلاح مُولَّد آلياً بواسطة `%s` — راجعه ونفّذ الاختبارات قبل الدمج."
            % Config.GEMINI_MODEL,
        ]
        if self.dry_run:
            logger.info(
                "🧪 [DRY-RUN] سيُفتح PR إصلاح CI على الفرع %s لـ %s.",
                branch_name,
                workflow_path,
            )
            return "created"

        pr_url = self.github.create_fix_branch_and_pr(
            repo=repo,
            branch_name=branch_name,
            file_path=workflow_path,
            content=fixed_yaml,
            commit_msg=f"ci: fix failing workflow {workflow_path} ({fp})",
            pr_title=title,
            pr_body="\n".join(body_lines),
        )
        logger.info("🎉 تم فتح PR إصلاح CI: %s", pr_url)
        return "created"

    @staticmethod
    def _is_plausible_workflow(yaml_text: str) -> bool:
        """تحقق أولي من أن النص YAML صالح ويشبه GitHub Actions workflow.

        لا يكتب الوكيل أي ملف workflow من النموذج إلا بعد اجتياز هذا الفحص
        (يحمي المستودع من مخرجات YAML تالفة/غير مكتملة).
        """
        if _yaml is None:
            return bool(yaml_text and yaml_text.strip())
        try:
            data = _yaml.safe_load(yaml_text)
        except Exception:  # noqa: BLE001
            return False
        if not isinstance(data, dict):
            return False
        if "jobs" not in data or not isinstance(data.get("jobs"), dict):
            return False
        # ملاحظة: مفتاح `on` في YAML يصبح bool True بعد safe_load.
        if "on" not in data and True not in data and "name" not in data:
            # workflows غير مألوفة — نسمح بها فقط إن وُجد اسم.
            return False
        return True

    @staticmethod
    def _workflow_base(workflow_path: str) -> str:
        """اسم ملف workflow بدون امتداد وبدون مجلدات، مثال ci.yml -> ci."""
        base = (workflow_path or "").rsplit("/", 1)[-1]
        for ext in (".yml", ".yaml"):
            if base.endswith(ext):
                base = base[: -len(ext)]
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", base).strip("-")
        return safe or "workflow"
