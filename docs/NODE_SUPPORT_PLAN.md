# خطة دعم Node.js والمرحلة 2 — Celia Repo Agent

> المستند المرجعي لتوسعات المرحلة 2: **فحص أمان Node.js (npm)**،
> **معالجة فشل الـ CI (CI healing)**، و**لوحة التحكم (Dashboard)**.
> يوثّق ما نُفّذ فعلياً في هذا الفرع وما هو مخطط له مستقبلاً.

## 1) فحص أمان Node.js (npm) — منفّذ ✅

### الوضع الحالي

| الميزة | الحالة | الملفات |
| --- | --- | --- |
| جلب تنبيهات Dependabot لمنظومة `npm` | ✅ منفّذ | `github_service.get_dependabot_alerts(repo, ecosystem="npm")` |
| تحديث `package.json` **حتمي** (بدون AI) | ✅ منفّذ | `node_analyzer.py` |
| تجميع الإصلاحات في PR أمني واحد لكل manifest مع منع التكرار | ✅ منفّذ | `npm_security.py` |
| تشغيل يومي تلقائي عبر cron/workflow_dispatch | ✅ منفّذ | `agent.py` (`--no-npm-security` للتعطيل) |

### منهج التحديث الحتمي في `node_analyzer.py`

- `dependencies`/`devDependencies`: تُحدَّث الحزمة إلى النسخة الآمنة مع **الحفاظ
  على معامل النطاق** (`^`، `~`، `>=`...). الحزم ذات القيم غير النسخية
  (branch/tag/workspace) تُستبعد آمنةً.
- ثغرة **غير مباشرة** (transitive): تُضاف/تُحدَّث عبر `overrides` في npm —
  بلا توليد ذكاء اصطناعي، التعديل JSON خالص (`json.loads`/`dumps`).
- `is_supported_manifest` يسمح مستقبلاً بإضافة `package-lock.json` و `npm-shrinkwrap.json`.

### خطة التوسعة القادمة (غير منفذة بعد)

- [ ] دعم `package-lock.json` (نسخة النسخ عبر `npm audit --json` محلياً في CI).
- [ ] دعم `yarn.lock` / `pnpm-lock.yaml` (منظومات ecosystem منفصلة في Dependabot).
- [ ] نسخة تحديث مجمّعة (`npm install` في GitHub Actions) قبل فتح الـ PR لضمان
      صحة الـ lockfile — يُفضَّل كـ GitHub Action مخصصة.
- [ ] تقرير أثر الترقية (breaking changes) عبر Gemini قبل الالتزام.

## 2) معالجة فشل الـ CI (CI healing) — منفّذ ✅

### الوضع الحالي

| الميزة | الحالة | الملفات |
| --- | --- | --- |
| رصد أحدث التشغيلات الفاشلة على الفرع الافتراضي (استبعاد Dependabot الداخلية `dynamic/`) | ✅ | `github_service.get_recent_failed_runs` |
| تنزيل سجلات الوظائف الفاشلة مع تسامح مع فشل الشبكة | ✅ | `github_service.download_job_log` |
| اختزال السجل: تنظيف طوابع الوقت/ANSI + اقتطاف أسطر الأخطاء بسياقها | ✅ | `ci_healer.summarize_log` |
| **بصمات فشل** حتمية لمنع تكرار PRs لنفس الفشل | ✅ | `ci_healer.fingerprint` |
| تصنيف عائلة الفشل (npm/python/docker/shell/unknown) | ✅ | `ci_healer.classify_failure` |
| تشخيص Gemini → إرجاع workflow YAML مصحح داخل ` ```yaml ` | ✅ | `ai_resolver.diagnose_ci_failure` |
| فتح PR على فرع `ci/fix-<workflow>-<بصمة>` + منع التكرار بالفرع | ✅ | `ci_healer._handle_failed_job` |
| الحماية: لا يُفتح PR إن لم يخرج YAML صالح/مغيّر | ✅ | `extract_fenced_yaml` + فحص الاختلاف |

### ملاحظات تصميم مهمة

- **سجل الوظيفة**: `WorkflowJob.logs_url()` في PyGithub تُرجع رابط تنزيل موقّعاً
  (Azure Blob). فشل التنزيل (حجب نطاق/شبكة) **لا يوقف المعالجة**: يُسجَّل الخطأ
  ويُكتفى بالتشخيص من بيانات الوظيفة.
- **البصمة**: SHA-256 لأول 40 سطراً مهماً بعد التطبيع (تجاهل أسطر الأرقام
  الطويلة) + مسار الـ workflow → نفس الفشل المتكرر = نفس الفرع = تخطٍّ ذكي.
- **الأمان**: أي YAML يعود من النموذج يُمرَّر عبر `yaml.safe_load` في الفحص،
  ولا يُكتب أي ملف إلا بعد تأكيد اختلافه عن المحتوى الحالي.

### خطة التوسعة القادمة

- [ ] تشخيص فشل PRs (فروع غير default) وليس فقط default branch.
- [ ] إعادة تشغيل تلقائية (`rerun_failed_jobs`) للفشل العابر قبل فتح PR.
- [ ] ضغط أسطر التكرار الكبيرة في المقتطف (log rotate لسجلات ضخمة).
- [ ] وضع "اقتراح فقط" (تعليق على الـ run بدل PR) عبر خيار.

## 3) لوحة التحكم (Dashboard) — منفّذ ✅ (المعمارية الحالية)

### المكونات

```
web/
├── dashboard.py      # FastAPI: REST + JWT + WebSocket + استضافة الواجهة
├── recorder.py       # SQLite مشترك (معياري فقط — يستورده agent.py أيضاً)
├── static/
│   └── index.html    # واجهة صفحة واحدة (Tailwind CDN) — RTL عربي
└── __init__.py
```

- **التخزين**: SQLite (`CELIA_DB_PATH`، الافتراضي `data/celia.db`، WAL، آمن
  لمُعدِّلات متعددة) — الوكيل في عملية واللوحة في عملية أخرى.
- **المصادقة**: JWT (HS256) عبر `POST /api/auth` بتوكن اللوحة `CELIA_DASH_TOKEN`.
  في غياب المتغير: **DEMO MODE** صريح في الواجهة والسجلات (للمعاينة المحلية فقط).
- **البث الحي**: WebSocket `/ws/events?token=...` يبث أحداث الخادم فورياً،
  والواجهة تجمع بين WS + Polling دوري لالتقاط أحداث الوكيل الخارجي.
- **نقاط REST**: `/api/auth`، `/api/health`، `/api/runs`، `/api/runs/{id}`،
  `/api/events`، `/api/events/latest`، `/api/demo/activity` (محاكاة حية للمعاينة).

### خطة الترقية المخطط لها (Next.js)

الهدف النهائي: نقل الواجهة إلى **Next.js** (App Router) مع بقاء الخلفية FastAPI:

1. **هيكلة API مستقرة أولاً**: تثبيت عقد REST/WS أعلاه كعقد رسمي (v1).
2. **مشروع `web-ui/`**: Next.js 14+ (TypeScript, Tailwind) يستهلك `/api` عبر
   `rewrites` إلى خادم FastAPI (خارج الصندوق في التطوير/الإنتاج).
3. **مصادقة**: تمرير JWT عبر httpOnly cookie بدل localStorage.
4. **التحسينات**: إعادة تصيير جزئي للجداول (SWR/React Query)، صفحات تفصيلية
   لكل run مع فلترة المستودعات والمستويات، ومخططات بسيطة (إحصاءات PRs/أخطاء).
5. **النشر**: `Dockerfile` من مرحلتين (Next.js static → يُقدَّم عبر FastAPI
   StaticFiles أو CDN منفصل)، ويبقى WebSocket للبث المباشر.

> ملاحظة: لا يُنفَّذ هذا في هذا الفرع؛ هذه الخطة مرجعية للتوسعة القادمة.

## 4) معايير القبول للمرحلة 2

- [x] تعديل **حتمي** لـ `package.json` (بدون توليد AI).
- [x] بصمات فشل حتمية لمنع تكرار PRs في CI healing.
- [x] WebSocket لبث السجلات/الأحداث حياً في اللوحة.
- [x] JWT لمصادقة اللوحة (مع DEMO MODE صريح عند غياب التوكن).
- [x] `tests_smoke.py` — اختبارات خضراء دون شبكة (لكل الوحدات الجديدة).
- [x] توثيق هذا الملف + README محدَّث.
