## 📌 الملخص

المرحلة 2 من Celia Repo Agent — ثلاث توسعات رئيسية فوق عمل المرحلة 1
(الذي يتضمن فحص pip عبر Dependabot):

1. **🟢 فحص أمان Node.js (npm)** — تنبيهات Dependabot لمنظومة `npm` تُعالَج
   **بتعديل حتمي 100%** لـ `package.json` (JSON parsing — بدون ذكاء اصطناعي):
   - `node_analyzer.py`: ترقية الاعتماديات المباشرة مع الحفاظ على معامل النطاق
     (`^`/`~`/...)، واستخدام `overrides` للثغرات غير المباشرة (transitive).
   - `npm_security.py`: جلب التنبيهات، تجميعها لكل manifest، فتح PR أمني واحد
     على فرع `fix/dependabot-package-json` مع منع التكرار عبر الـ PRs المفتوحة.

2. **🩺 معالجة فشل الـ CI (CI Healing)** — `ci_healer.py` + توسعات في
   `github_service.py` و `ai_resolver.py`:
   - رصد أحدث تشغيلات GitHub Actions الفاشلة (الفرع الافتراضي، 14 يوماً،
     استبعاد تشغيلات Dependabot الداخلية `dynamic/`).
   - تنزيل سجلات الوظائف الفاشلة واختزالها (تنظيف الطوابع الزمنية/ANSI،
     اقتطاف أسطر الأخطاء بسياقها، اقتطاع ذكي).
   - **بصمة فشل حتمية** (SHA-256) → نفس الفشل المتكرر لا يفتح PR مكرراً.
   - تصنيف العائلة (npm/python/docker/shell) ثم تشخيص Gemini يخرج
     workflow YAML مصححاً يُتحقق منه (`safe_load` + وجود `jobs`) قبل فتح PR
     على فرع `ci/fix-<workflow>-<بصمة>`.
   - تسامح كامل مع فشل تنزيل السجلات (حجب نطاق/شبكة) دون إيقاف الفحص.

3. **📊 لوحة تحكم ويب** — مجلد `web/`:
   - `recorder.py`: مسجل SQLite مشترك (مكتبة معيارية فقط) — الوكيل يكتب و اللوحة تعرض.
   - `dashboard.py`: FastAPI + **JWT** (HS256 عبر `CELIA_DASH_TOKEN`) +
     **WebSocket** (`/ws/events`) لبث الأحداث حياً + REST (`/api/runs`,
     `/api/events`, `/api/demo/activity`...). عند غياب التوكن تعمل في
     **DEMO MODE** صريح (بيانات تجريبية) للمعاينة.
   - `static/index.html`: واجهة صفحة واحدة RTL بتصميم Tailwind (بطاقات KPI،
     جدول التشغيلات، سجل أحداث حي مع فلترة مستويات).

## 🧪 الاختبارات

- `tests_smoke.py`: **26 اختباراً أخضر** دون أي شبكة/مفاتيح حقيقية (تغطي
  node_analyzer، npm_security، ci_healer، ai_resolver، web.recorder).
- فحص تكامل يدوي end-to-end لـ ci_healer مع مستودع وهمي (فتح PR workflow).
- اللوحة: تحقّق حي من `/api/auth` و `/api/runs` و `/api/events` و WebSocket
  (استلام 3+ رسائل بث فورية).

## 📁 ملفات جديدة

| الملف | الغرض |
| --- | --- |
| `node_analyzer.py` | تحليل/تحديث `package.json` حتمياً |
| `npm_security.py` | مسار أمان npm: تنبيهات → PR أمني |
| `ci_healer.py` | سجلات CI → بصمة → تشخيص → workflow مصحح |
| `web/recorder.py` | مسجل أحداث SQLite (وكيل + لوحة) |
| `web/dashboard.py` | خادم اللوحة (FastAPI + JWT + WebSocket) |
| `web/static/index.html` | واجهة اللوحة |
| `tests_smoke.py` | اختبارات الدخان (26) |
| `docs/NODE_SUPPORT_PLAN.md` | خطة/مرجع دعم Node.js والمرحلة 2 |

## 🔧 ملفات معدّلة

`agent.py` (دمج npm + CI healing + عدادات/أحداث)، `github_service.py`
(فلتر ecosystem + واجهات runs/jobs/logs)، `ai_resolver.py` (تشخيص CI)،
`requirements.txt` (FastAPI/uvicorn/PyJWT/PyYAML)، `.env.example`، `.gitignore`، `README.md`.

## ⚠️ ملاحظات للمراجعة

- إصلاح npm حتمي (JSON) — لا يوجد توليد AI في هذا المسار إطلاقاً.
- إصلاح CI (workflow) **مولّد آلياً عبر Gemini** ويُوسم بتوقيع البوت —
  يُرجى مراجعة أي PR إصلاح CI قبل الدمج.
- فشل تنزيل سجل وظيفة لا يوقف الفحص (يُسجَّل ويُتخطى).
- سطر واحد يهم CI عند التشغيل اليومي: يفتح الوكيل PRs npm + CI تلقائياً
  (مقيد بعدم التكرار بالبصمات والفروع الثابتة).
