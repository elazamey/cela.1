# 🤖 Celia Repo Agent — وكيل إدارة مستودعات GitHub الذكي

وكيل ذكاء اصطناعي يفحص **جميع مستودعاتك على GitHub** تلقائياً، يكتشف النواقص
والمشاكل (الملفات الأساسية المفقودة، غياب إعدادات CI/CD، المشاكل المفتوحة)،
ثم يولّد الإصلاحات ويفتح **Pull Requests** أو ينشر اقتراحات الحلول كتعليقات —
باستخدام **Google Gemini**.

## ✨ ما الذي يفعله الوكيل؟

| الميزة | الوصف |
| --- | --- |
| 🔍 فحص شامل | يجلب كل المستودعات المملوكة لك ويفحصها واحداً تلو الآخر. |
| 📄 الملفات المفقودة | يرصد غياب `README.md` و `.gitignore` ويولّد محتواهما عبر Gemini ثم يفتح PR. |
| ⚙️ CI/CD المكسور/المفقود | يرصد المستودعات بلا أي GitHub Actions workflow ويولّف ملف CI ويفتح PR. |
| 🐛 المشاكل المفتوحة | يحلّل كل Issue مفتوحة، يولّد اقتراح حل عملي، وينشره كتعليق على المشكلة. |
| 🛡️ فحص ثغرات Dependabot | يجلب تنبيهات Dependabot لحزم Python (pip)، يحدّد الحزم المُصابة والنسخ الآمنة، ويحدّث `requirements.txt` بذكاء عبر Gemini ويفتح PR أمني (تُجمَع إصلاحات الملف الواحد في PR واحد). |
| 🟢 فحص أمان Node.js (npm) | تنبيهات Dependabot لمنظومة `npm` تُعالج **بتعديل حتمي** لـ `package.json` (JSON parsing خالص بدون AI): ترقية مباشرة مع الحفاظ على معامل النطاق، و`overrides` للثغرات غير المباشرة — PR أمني واحد لكل manifest. |
| 🩺 إصلاح الـ CI التالف (CI Healing) | يرصد أحدث تشغيلات GitHub Actions الفاشلة، ينزّل سجلاتها ويختزلها، يحسب **بصمة فشل** حتمية (لا تكرار لنفس الفشل)، يصنّف العائلة (npm/python/docker/shell)، يشخّصها Gemini ويخرج workflow YAML مصححاً في PR على فرع `ci/fix-...`. |
| 📊 لوحة تحكم ويب | FastAPI + SQLite + JWT + WebSocket: مراقبة حية للتشغيلات والأحداث (مستودعات، PRs، أخطاء) مع بث مباشر، ووضع معاينة DEMO. |
| 🚫 عدم التكرار | لا يكرر PR لنفس الفرع ولا يكرر التعليق على نفس المشكلة. |
| 🧪 وضع المعاينة | `--dry-run` يفحص ويعرض ما سيفعله بدون أي كتابة على GitHub. |
| ⏰ أتمتة يومية | يعمل تلقائياً كل يوم عبر GitHub Actions (أو يدوياً بزر `workflow_dispatch`). |
| 🧱 عزل الأخطاء | فشل مستودع أو مشكلة واحدة لا يوقف بقية الفحص. |

## 📁 هيكل المشروع

```text
.
├── .env.example            # نموذج متغيرات البيئة (انسخه إلى .env)
├── .github/
│   └── workflows/
│       └── audit_cron.yml  # جدولة التشغيل اليومي + التشغيل اليدوي
├── .gitignore
├── agent.py                # الوكيل الرئيسي (نقطة الدخول)
├── ai_resolver.py          # محرك Gemini (ملفات/حلول/تشخيص CI)
├── ci_healer.py            # (المرحلة 2) معالجة فشل CI: سجلات + بصمات + YAML مصحح
├── config.py               # تحميل الإعدادات والتحقق من المفاتيح
├── github_service.py       # طبقة التفاعل مع GitHub (PyGithub)
├── node_analyzer.py        # (المرحلة 2) تحليل/تحديث package.json حتمياً
├── npm_security.py         # (المرحلة 2) تنبيهات npm → PR أمني
├── requirements.txt
├── tests_smoke.py          # (المرحلة 2) اختبارات دخان خضراء بلا شبكة
├── web/                    # (المرحلة 2) لوحة التحكم
│   ├── dashboard.py        #   FastAPI + JWT + WebSocket
│   ├── recorder.py         #   مسجل أحداث SQLite (يستخدمه الوكيل أيضاً)
│   └── static/index.html   #   واجهة صفحة واحدة (Tailwind CDN)
├── docs/
│   └── NODE_SUPPORT_PLAN.md # (المرحلة 2) خطة دعم Node.js والمرحلة 2
└── README.md
```

## 🚀 الإعداد المحلي

1. ثبّت الاعتماديات:

   ```bash
   pip install -r requirements.txt
   ```

2. انسخ ملف الإعدادات واملأ القيم الحقيقية:

   ```bash
   cp .env.example .env
   ```

   | المتغير | المصدر |
   | --- | --- |
   | `GITHUB_TOKEN` | [Personal Access Token](https://github.com/settings/tokens) كلاسيكي بصلاحية **`repo`** + **`security_events`** (لجلب تنبيهات Dependabot) |
   | `GEMINI_API_KEY` | [مفتاح Gemini من Google AI Studio](https://aistudio.google.com/apikey) |
   | `GITHUB_USERNAME` | اسم مستخدم GitHub الخاص بك |
   | `GEMINI_MODEL` | اختياري، الافتراضي `gemini-2.5-flash` |
   | `DRY_RUN` | اختياري، `true` للمعاينة بدون أي تغيير |
   | `CELIA_DB_PATH` | اختياري: مسار قاعدة SQLite لتسجيل التشغيلات (تفعّل تسجيل أحداث اللوحة) |
   | `CELIA_DASH_TOKEN` | اختياري: توكن لوحة التحكم (JWT)؛ بغيابه تعمل اللوحة في DEMO MODE |
   | `CELIA_CI_LOOKBACK_DAYS` / `CELIA_CI_MAX_RUNS` | اختياري: ضبط نافذة/سقف فحص CI |

   > ⚠️ ملف `.env` الحقيقي متجاهَل في Git ولا يجب أبداً رفعه.

3. شغّل الوكيل:

   ```bash
   # فحص حقيقي: إنشاء PRs وتعليقات (يشمل npm + إصلاح CI)
   python agent.py

   # معاينة آمنة: عرض الإجراءات فقط بدون أي كتابة
   python agent.py --dry-run

   # تعطيل أحد المسارات (اختياري)
   python agent.py --no-npm-security
   python agent.py --no-ci-heal
   ```

4. لوحة التحكم (اختياري):

   ```bash
   # تسجيل أحداث الوكيل (افعلها مرة واحدة ثم شغّل اللوحة)
   export CELIA_DB_PATH=data/celia.db
   python agent.py --dry-run        # مثال: يكتب التشغيل إلى القاعدة

   # تشغيل اللوحة (بدون CELIA_DASH_TOKEN تدخل DEMO MODE للمعاينة)
   uvicorn web.dashboard:app --host 0.0.0.0 --port 8000
   # ثم افتح http://localhost:8000
   ```

## ⏰ الأتمتة عبر GitHub Actions

ملف [`.github/workflows/audit_cron.yml`](.github/workflows/audit_cron.yml)
يشغّل الوكيل يومياً عند منتصف الليل UTC، كما يمكن تشغيله يدوياً من تبويب
**Actions** (مع خيار معاينة `dry_run`).

أضف الأسرار التالية في **Settings → Secrets and variables → Actions**:

| السر | القيمة |
| --- | --- |
| `AGENT_GITHUB_TOKEN` | PAT بنطاق `repo` (يُستخدم بدلاً من `GITHUB_TOKEN` الافتراضي حتى يستطيع الوصول لكل مستودعاتك وإنشاء PRs حقيقية). |
| `AGENT_GEMINI_API_KEY` | مفتاح Gemini. |

## 🧠 كيف يعمل؟

1. `GitHubService.get_all_repositories()` يجلب كل المستودعات المملوكة.
2. `audit_repository()` يفحص الملفات الأساسية، الـ workflows، والمشاكل المفتوحة.
3. لكل مشكلة:
   - ملف/Workflow مفقود → `AIResolver.generate_missing_file()` يولّد المحتوى
     عبر Gemini → يُنشأ فرع `fix/missing-...` و commit و **Pull Request**.
   - Issue مفتوحة → `AIResolver.solve_issue_code()` يولّد اقتراح حل →
     يُنشر **كتعليق** على المشكلة (آمن وغير مدمِّر، مع إخلاء مسؤولية المراجعة).
   - تنبيهات Dependabot (pip) → `get_dependabot_alerts()` يجلب الثغرات والنسخ
     الآمنة → `AIResolver.update_requirements_file()` يحدّث أسطر الحزم المُصابة
     فقط مع الحفاظ على بقية الملف → يُفتح **PR أمني** (إصلاحات كل ملف في PR واحد).
4. كل ما يولّده الذكاء الاصطناعي موسوم بتوقيع البوت ويحتاج مراجعتك قبل الدمج.

> ℹ️ **متطلب صلاحية Dependabot:** يجب أن يتضمن الـ PAT نطاق **`security_events`**
> (التوكن الكلاسيكي) أو صلاحية *Dependabot alerts → read* للتوكن الدقيق، وفي
> GitHub Actions أُضيفت `security-events: read` داخل الـ workflow.

## 🔭 تحسينات مستقبلية مقترحة

- [x] **فحص أمني (Dependabot Alerts):** جلب ثغرات حزم pip وتحديث `requirements.txt`
  تلقائياً عبر Gemini وفتح PR أمني.
- [x] **فحص أمان Node.js:** تنبيهات npm → تحديث `package.json` حتمي (مباشر/`overrides`) → PR.
- [x] **إصلاح أخطاء الـ Build (CI Healing):** سجلات الفحوصات الفاشلة → بصمة فشل →
  تشخيص Gemini → workflow YAML مصحح في PR. (تفاصيل في `docs/NODE_SUPPORT_PLAN.md`)
- [x] **لوحة تحكم ويب:** FastAPI + SQLite + JWT + WebSocket (أحداث حية).
- [ ] كشف لغة/حزمة المستودع لتوليد `.gitignore` و README أدق.
- [ ] حدّ أقصى للـ PRs اليومية وخيار السماح/المنع لكل مستودع.
- [ ] ترقية اللوحة إلى Next.js و دعم lockfiles (`package-lock.json`, yarn, pnpm).

## 📜 الرخصة

راجع ملف [LICENSE](LICENSE).
