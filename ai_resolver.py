"""محرك الذكاء الاصطناعي (Google Gemini) لتوليد محتوى الملفات واقتراح الحلول.

AI resolver engine for Celia Repo Agent.

Uses Google Gemini to:
  * Generate content for missing standard files (README, .gitignore, CI
    workflows...).
  * Suggest a concrete fix for an open issue.
"""

import logging
import re
from typing import Optional

from google import genai

from config import Config

logger = logging.getLogger(__name__)


class AIResolver:
    """Wrapper around the Gemini SDK used for all content generation."""

    def __init__(self) -> None:
        self.client = genai.Client(api_key=Config.GEMINI_API_KEY)
        self.model = Config.GEMINI_MODEL

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _strip_code_fences(text: str) -> str:
        """إزالة أغلفة markdown مثل ```markdown ... ``` إن أضافها النموذج.

        Even when prompted not to, models sometimes wrap output in fenced
        code blocks; strip a single surrounding fence so the returned text
        is ready to be written straight into a file.
        """
        cleaned = text.strip()
        fence = re.match(
            r"^```[a-zA-Z0-9_+\-.]*\s*\n?(.*?)\n?```\s*$",
            cleaned,
            re.DOTALL,
        )
        if fence:
            cleaned = fence.group(1).strip()
        return cleaned

    def _generate(self, prompt: str) -> str:
        """Call Gemini and return the cleaned response text."""
        response = self.client.models.generate_content(
            model=self.model,
            contents=prompt,
        )
        return self._strip_code_fences(getattr(response, "text", "") or "")

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def generate_missing_file(
        self, repo_name: str, file_name: str, hint: Optional[str] = None
    ) -> str:
        """توليد محتوى الملفات المفقودة مثل README أو .gitignore أو ملف CI.

        Args:
            repo_name: Name of the repository the file is generated for.
            file_name: Target path/name of the missing file.
            hint: Optional extra context (e.g. detected language/stack).

        Returns:
            The raw file content, ready to commit.
        """
        extra = f"\nمعلومات إضافية عن المستودع: {hint}" if hint else ""
        if file_name.endswith((".yml", ".yaml")):
            instruction = (
                "أنت مهندس DevOps محترف. ولّد ملف GitHub Actions YAML صالحاً "
                "وعملياً (لا تضف أسطر شرح داخل الملف)."
            )
        elif file_name == ".gitignore":
            instruction = (
                "أنت مهندس برمجيات محترف. ولّد ملف .gitignore شاملاً وعملياً "
                "يغطي البيئات الشائعة (Python/Node/IDE/OS)."
            )
        else:
            instruction = (
                "أنت مهندس برمجيات محترف. ولّد محتوى ملف احترافياً ومتكاملاً "
                "بلغة مناسبة لاسم المستودع."
            )

        prompt = f"""
{instruction}
اسم المستودع: '{repo_name}'.
اسم الملف المطلوب توليده: '{file_name}'.{extra}

قم بإرجاع محتوى الملف فقط، بدون أي شرح أو مقدمة، وبدون أغلفة markdown
مثل ```markdown أو ```yaml.
"""
        logger.info("توليد محتوى الملف %s عبر نموذج %s", file_name, self.model)
        return self._generate(prompt)

    def solve_issue_code(self, repo_name: str, issue_title: str, issue_body: str) -> str:
        """تحليل مشكلة مفتوحة وتوليد اقتراح حل لها (كود أو خطوات إصلاح)."""
        prompt = f"""
أنت مهندس برمجيات خبير تقوم بمراجعة مشكلة في مستودع مفتوح المصدر.

المستودع: {repo_name}
عنوان المشكلة: {issue_title}
تفاصيل المشكلة:
{issue_body or "(لا يوجد وصف إضافي)"}

المطلوب:
1. شخّص سبب المشكلة المحتمل باختصار.
2. قدّم حلاً عملياً ومحدداً: كود مصحّح، أو ملف/أسطر يجب تعديلها، أو خطوات
   واضحة يمكن لصاحب المستودع تنفيذها.
3. إذا لم تكن هناك معلومات كافية، اذكر المعلومات الناقصة بدلاً من التخمين.

اكتب الإجابة بتعليمات markdown واضحة جاهزة للنشر كتعليق على المشكلة.
"""
        logger.info("توليد اقتراح حل للمشكلة: %s", issue_title)
        return self._generate(prompt)
