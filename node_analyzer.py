"""محلل مشاريع Node.js: كشف ملفات الاعتماديات وتحديث package.json بشكل حتمي.

Deterministic Node.js dependency analysis for Celia Repo Agent.

Unlike `requirements.txt` fixes (which are delegated to the AI resolver),
`package.json` updates are applied deterministically with a JSON parser so
that a security bump never depends on model output:

  * A dependency listed in `dependencies`/`devDependencies` is rewritten to
    the patched version (the leading range operator, if any, is preserved).
  * A vulnerability that lives only in a *transitive* dependency (no direct
    entry) is fixed by adding/updating an npm `overrides` entry, which pins
    the resolution for the whole dependency tree (npm >= 8.3).

This module is pure Python (stdlib only) and fully unit-testable.
"""

import json
import re
from typing import Dict, List, Optional, Tuple

#: Manifests this analyzer understands and can edit deterministically.
SUPPORTED_MANIFESTS = ("package.json",)

#: npm aliases that will never be touched by an automated bump.
SKIPPED_DEPENDENCY_KEYS = ("workspaces",)


class PackageJsonError(ValueError):
    """Raised when a package.json file cannot be parsed or is malformed."""


def is_supported_manifest(manifest_path: str) -> bool:
    """هل المسار ملف اعتماديات Node.js مدعوم (package.json)؟"""
    return (manifest_path or "").strip().lower() in SUPPORTED_MANIFESTS


def parse_package_json(content: str) -> dict:
    """تحليل محتوى package.json إلى قاموس.

    Raises:
        PackageJsonError: عندما يكون المحتوى JSON غير صالح أو ليس قاموساً.
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise PackageJsonError(f"package.json غير صالح: {exc}") from exc
    if not isinstance(data, dict):
        raise PackageJsonError("package.json يجب أن يكون JSON object.")
    return data


def dependency_sections(pkg: dict, include_dev: bool = True) -> List[Tuple[str, Dict[str, str]]]:
    """إرجاع أقسام الاعتماديات كقائمة (اسم_القسم، القاموس)."""
    sections: List[Tuple[str, Dict[str, str]]] = [("dependencies", pkg.get("dependencies") or {})]
    if include_dev:
        sections.append(("devDependencies", pkg.get("devDependencies") or {}))
    return [(name, sec) for name, sec in sections if isinstance(sec, dict)]


def _is_range_operator(value: str) -> Optional[str]:
    """استخراج معامل النطاق (^ ~ >= <= =) من أول السلسلة إن وُجد."""
    match = re.match(r"^(\^|~|>=|<=|=|>|<)?\s*(.*)$", value or "")
    if not match:
        return None
    op, rest = match.group(1) or "", match.group(2).strip()
    if not re.match(r"^\d", rest):
        # ليست نسخة رقمية صريحة (branch/tag/workspace:*...) — لا نلمسها.
        return None
    return op or ""


def get_package_summary(pkg: dict) -> Dict[str, object]:
    """ملخص وصفي لـ package.json (يستخدم في اللوحة والتقارير)."""
    deps = pkg.get("dependencies") or {}
    dev_deps = pkg.get("devDependencies") or {}
    scripts = pkg.get("scripts") or {}
    return {
        "name": pkg.get("name") or "",
        "version": pkg.get("version") or "",
        "dependencies_count": len(deps) if isinstance(deps, dict) else 0,
        "dev_dependencies_count": len(dev_deps) if isinstance(dev_deps, dict) else 0,
        "scripts_count": len(scripts) if isinstance(scripts, dict) else 0,
        "package_manager": pkg.get("packageManager") or "",
        "engines": pkg.get("engines") or {},
        "private": bool(pkg.get("private", False)),
    }


def serialize_package_json(pkg: dict) -> str:
    """إرجاع package.json بصيغة موحّدة (مسافتان) جاهزة للالتزام."""
    return json.dumps(pkg, indent=2, ensure_ascii=False) + "\n"


def apply_version_update(
    content: str,
    package_name: str,
    target_version: str,
    section: Optional[str] = None,
) -> Tuple[str, str]:
    """تحديث حتمي لنسخة حزمة واحدة داخل package.json.

    Args:
        content: المحتوى الحالي لـ package.json (نص).
        package_name: اسم الحزمة المُصابة.
        target_version: النسخة الآمنة (patched version).
        section: 'dependencies' أو 'devDependencies' أو None (يبحث في الاثنين).

    Returns:
        (المحتوى المحدَّث، وصف الإجراء الذي نُفّذ) — مثل:
        ``("...", "dependencies:lodash==4.17.21")`` أو ``("...", "overrides:lodash=4.17.21")``

    Raises:
        PackageJsonError: إن تعذّر تحليل الملف.
    """
    pkg = parse_package_json(content)
    name = (package_name or "").strip()
    target = (target_version or "").strip()
    if not name or not target:
        raise PackageJsonError("اسم الحزمة والنسخة المستهدفة مطلوبان.")

    # 1) محاولة التحديث المباشر في dependencies / devDependencies.
    if section in (None, "dependencies"):
        deps = pkg.get("dependencies")
        if isinstance(deps, dict) and name in deps:
            return _rewrite_entry(pkg, "dependencies", name, deps[name], target)

    if section in (None, "devDependencies"):
        dev = pkg.get("devDependencies")
        if isinstance(dev, dict) and name in dev:
            return _rewrite_entry(pkg, "devDependencies", name, dev[name], target)

    if section is not None:
        # قسم محدد وُجد لكن الحزمة ليست فيه — لا نضيفها لقسم خاطئ.
        raise PackageJsonError(
            f"الحزمة '{name}' غير موجودة في '{section}' — لا يمكن تحديثها."
        )

    # 2) غير موجودة كاعتماد مباشر: ثغرة transitive → نستخدم overrides.
    overrides = pkg.setdefault("overrides", {})
    if not isinstance(overrides, dict):
        overrides = pkg["overrides"] = {}
    previous = overrides.get(name)
    overrides[name] = target
    return serialize_package_json(pkg), _describe_override(name, target, previous)


def _rewrite_entry(
    pkg: dict, section_key: str, name: str, old_range: str, target: str
) -> Tuple[str, str]:
    """إعادة كتابة اعتماد مباشر مع الحفاظ على معامل النطاق إن وُجد."""
    section = pkg[section_key]
    op = _is_range_operator(old_range)
    if op is None:
        raise PackageJsonError(
            f"الحزمة '{name}' لها نطاق غير نسخي ('{old_range}') — لا يمكن تحديثها حتمياً."
        )
    section[name] = f"{op}{target}"
    return serialize_package_json(pkg), f"{section_key}:{name}={op or ''}{target}"


def _describe_override(name: str, target: str, previous: Optional[str]) -> str:
    if previous:
        return f"overrides:{name}:{previous}->{target}"
    return f"overrides:{name}={target} (إصلاح transitive جديد)"


def apply_all_updates(content: str, updates: Dict[str, str]) -> Tuple[str, List[str]]:
    """تطبيق عدة تحديثات (اسم الحزمة ← النسخة الآمنة) بالتتابع على نفس المحتوى.

    Returns:
        (المحتوى النهائي، قائمة أوصاف الإجراءات المنفَّذة).
    """
    current = content
    actions: List[str] = []
    for name, version in updates.items():
        current, action = apply_version_update(current, name, version)
        actions.append(action)
    return current, actions
