# Personal Nutrition & Fitness Tracker — Final V1 Specification

**Status:** Finalized baseline / source of truth for implementation

## 1. Product Goal

Build a personal nutrition and fitness tracking web application that is useful for daily use and demonstrates strong software engineering and AI engineering.

The user can:
- Create an account and profile.
- Set daily nutrition and hydration targets.
- Track weight, meals, water, and supplements.
- Log manually.
- Use AI to interpret natural-language input and meal images.
- Review/edit AI-generated logs before saving.
- View day/week/month analytics and progress.
- Ask an AI Coach questions about actual tracked data.

## 2. V1 Principles

1. PostgreSQL is the source of truth for personal data.
2. Normal CRUD does not use an LLM.
3. AI output is a draft until the user confirms it.
4. Deterministic calculations are performed by backend/Python/database logic.
5. LLMs handle interpretation, extraction, natural language, and general nutrition knowledge.
6. Original meal images live in object storage; PostgreSQL stores the reference/URL.
7. No RAG, vector DB, OKF, microservices, Kubernetes, Kafka, or Redis in V1.
8. Keep the design modular, but do not add infrastructure without a real requirement.
9. Streamlit never connects directly to PostgreSQL.
10. AI providers are replaceable without rewriting business logic.
11. Never silently save an AI guess as confirmed data.

## 3. Technology Stack

### Core
- Python
- Streamlit — frontend/UI
- FastAPI — backend/API
- PostgreSQL — database
- SQLAlchemy 2.x — ORM
- Alembic — migrations
- Pydantic 2.x — validation/schemas

### AI
- LangChain — model integrations, prompts, structured output, tools
- LangGraph — stateful/multi-step AI workflows
- NVIDIA — primary provider
- Groq — secondary/fallback provider

Initial Groq test model:
`openai/gpt-oss-120b`

### Supporting
- psycopg
- python-dotenv
- python-multipart
- PyJWT
- pwdlib[argon2]

Do not add packages unless they solve an actual requirement.

## 4. Architecture

```text
USER
  |
  v
STREAMLIT
  |
  v
FASTAPI
  |
  +-------------------+
  |                   |
  v                   v
POSTGRESQL          AI LAYER
                       |
                +------+------+
                |             |
            LangChain     LangGraph
                |             |
                +------+------+
                       |
                  AI Provider
                  /         \
              NVIDIA        Groq
```

### Responsibilities

**Streamlit:** navigation, forms, charts, image upload/display, review UI, Coach UI, API calls.

**FastAPI:** authentication, authorization, validation, business logic, database operations, analytics, image-upload coordination, AI orchestration.

**PostgreSQL:** all confirmed user-owned application data.

**LangChain:** model integrations, prompts, structured output, tools.

**LangGraph:** AI Log workflow and AI Coach workflow where stateful/multi-step execution is useful.

Do not force LangChain/LangGraph into ordinary CRUD.

## 5. User Flow

```text
OPEN APP
  |
  +-- New User -> Create Account -> Profile Setup -> Daily Targets -> Dashboard
  |
  +-- Existing User -> Login -> Dashboard
```

Main navigation:
- Dashboard
- History
- AI Coach
- Settings

Dashboard defaults to today.

## 6. Authentication

V1:
- Registration
- Login
- Password hashing
- Authentication/session handling
- Protected endpoints
- User-level data isolation

Use:
- `pwdlib[argon2]`
- `PyJWT`

Never store plaintext passwords. Every user-owned query must be scoped to the authenticated user.

## 7. Profile

Initial fields:
- Date of birth
- Sex
- Height
- Current weight
- Optional goal weight

The design may later support activity level, dietary preferences, allergies, and unit preferences, but do not add unnecessary fields to V1.

## 8. Daily Targets

Initial targets:
- Calories
- Protein
- Water

Target means desired daily amount; actual means confirmed logged amount.

Targets should be calculated by deterministic backend logic and designed so the user can later adjust them manually.

## 9. Dashboard

Show for the selected day:
- Weight
- BMI
- Calories / target
- Protein / target
- Water / target
- Supplements
- Today's confirmed meals and meal images

Prominent:
`[ + ADD LOG ]`

Options:
- AI Log
- Manual Food
- Water
- Weight
- Supplement

Progress bars must not clip at 100%; values above target must remain visible.

## 10. Quick/Manual Logging

Quick Log does not use an LLM.

### Water
Quick buttons:
- +250 ml
- +500 ml
- +750 ml
- Custom

### Weight
Validated weight input -> PostgreSQL.

### Supplements
Configurable supplements with quick taken/not-taken logging.

### Manual Food
Fields may include:
- Food name
- Quantity
- Unit
- Calories
- Protein
- Carbohydrates
- Fat
- Fiber
- Meal type

Manual logs go through backend validation directly to PostgreSQL.

## 11. AI Log

Inputs:
1. Text only
2. Image only
3. Image + text
4. Multiple events in one input

Example:
`I ate this for lunch, drank 500 ml water, and took fish oil.`

AI may identify:
- Meal
- Water
- Weight
- Supplement

## 12. AI Log Workflow

```text
USER INPUT
   |
   v
FASTAPI
   |
   v
LANGGRAPH
   |
   v
LLM
   |
   v
STRUCTURED DRAFT EVENTS
   |
   v
USER REVIEW / EDIT
   |
   v
CONFIRM
   |
   v
FASTAPI VALIDATION
   |
   v
POSTGRESQL
```

**Critical rule:** AI output must never automatically become confirmed personal data.

The user can review, edit, remove events, correct quantities/nutrition, and confirm.

## 13. AI Structured Output

Use Pydantic models and structured LLM output.

Example:

```json
{
  "events": [
    {
      "type": "meal",
      "meal_type": "lunch",
      "items": [
        {
          "food_name": "rice",
          "quantity": 200,
          "unit": "g",
          "calories": 260,
          "protein": 5,
          "carbs": 57,
          "fat": 1,
          "fiber": 1
        }
      ]
    },
    {
      "type": "water",
      "amount_ml": 500
    },
    {
      "type": "supplement",
      "name": "fish oil",
      "taken": true
    }
  ]
}
```

This is an example, not a fixed nutrition database.

AI nutrition values are estimates, not laboratory measurements.

## 14. Meal Images

Store original images in object storage.

```text
Meal image -> Object Storage -> image reference/URL -> PostgreSQL meal record
```

Do not store large image binaries directly in PostgreSQL.

The exact object-storage provider can be selected during implementation.

## 15. AI Audit Data

An `ai_inputs` table may store:
- ID
- User ID
- Input type
- Original text
- Image reference
- Provider
- Model
- AI response
- Status
- Created timestamp

This is for audit/debugging and does not replace confirmed application data.

## 16. History

History is an analytics dashboard, not merely a raw-record table.

### Day
- Weight
- BMI
- Calories
- Protein
- Water
- Supplements
- Meals
- Meal images

### Week
- Starting/latest weight
- Weight change
- Average calories
- Average protein
- Average water
- Days tracked
- Supplement adherence
- Meals logged
- Weight/calorie/protein/water/adherence charts

### Month
Same longer-term metrics plus:
- Weight goal progress
- Tracking consistency

Weight charts should emphasize trends and gradual progress rather than imply that weight must decrease every day.

## 17. BMI

Deterministic calculation:

`BMI = weight_kg / height_m²`

Do not ask the LLM to calculate BMI.

## 18. AI Coach

AI Coach is separate from AI Log.

**AI Log:** converts input into structured events.

**AI Coach:** answers questions using the user's actual tracked data.

Example questions:
- How did I do this week?
- What was my average protein?
- How has my weight changed this month?
- How consistent was my water intake?

## 19. AI Coach Architecture

```text
USER QUESTION
      |
      v
  LANGGRAPH
      |
      v
BACKEND TOOLS
      |
      +--> PostgreSQL
      +--> Deterministic calculations
      |
      v
STRUCTURED FACTS
      |
      v
LLM
      |
      v
COACH RESPONSE
```

Possible tools:
```text
get_today_stats()
get_weekly_stats()
get_monthly_stats()
get_weight_trend()
get_calorie_average()
get_protein_average()
get_water_average()
get_supplement_adherence()
get_meal_history()
calculate_bmi()
```

The LLM must not invent personal statistics. If data is missing, it should say so.

## 20. LLM Provider Abstraction

```text
AIProvider
   |
   +-- NVIDIAProvider
   |
   +-- GroqProvider
```

Application logic uses a common provider interface.

Initial strategy:
- NVIDIA = primary
- Groq = secondary/fallback

Groq initial model:
`openai/gpt-oss-120b`

Provider-specific code stays isolated.

## 21. Database Entities

### users
- id
- email
- password_hash
- created_at
- updated_at

### profiles
- id
- user_id
- date_of_birth
- sex
- height
- current_weight
- goal_weight
- created_at
- updated_at

### daily_targets
- id
- user_id
- calories_target
- protein_target
- water_target_ml
- effective_date
- created_at
- updated_at

### weight_records
- id
- user_id
- weight
- recorded_at
- created_at

### water_logs
- id
- user_id
- amount_ml
- logged_at
- created_at

### supplements
- id
- user_id
- name
- dosage
- unit
- active
- created_at
- updated_at

### supplement_logs
- id
- supplement_id
- user_id
- taken_at
- created_at

### meals
- id
- user_id
- meal_type
- image_url
- created_at
- updated_at

### meal_items
- id
- meal_id
- food_name
- quantity
- unit
- calories
- protein
- carbs
- fat
- fiber
- confidence
- created_at

### ai_inputs
- id
- user_id
- input_type
- input_text
- image_url/reference
- provider
- model
- response_data
- status
- created_at

Add appropriate foreign keys and indexes. All user-owned data must support reliable user isolation.

## 22. API Design

Initial endpoint groups:

```text
/auth
    POST /register
    POST /login

/profile
    GET /profile
    PUT /profile

/targets
    GET /targets
    PUT /targets

/dashboard
    GET /dashboard

/weight
    POST /weight
    GET /weight

/water
    POST /water
    GET /water

/supplements
    GET /supplements
    POST /supplements
    PUT /supplements/{id}

/supplement-logs
    POST /supplement-logs
    GET /supplement-logs

/meals
    POST /meals
    GET /meals
    GET /meals/{id}
    PUT /meals/{id}
    DELETE /meals/{id}

/ai-log
    POST /ai-log
    POST /ai-log/confirm

/history
    GET /history/day
    GET /history/week
    GET /history/month

/coach
    POST /coach
```

Routes may be refined during implementation.

## 23. Project Structure

```text
nutrition_tracker/
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── api/
│   │   │   ├── auth.py
│   │   │   ├── profile.py
│   │   │   ├── targets.py
│   │   │   ├── dashboard.py
│   │   │   ├── weight.py
│   │   │   ├── water.py
│   │   │   ├── supplements.py
│   │   │   ├── meals.py
│   │   │   ├── ai_log.py
│   │   │   ├── history.py
│   │   │   └── coach.py
│   │   ├── core/
│   │   │   ├── config.py
│   │   │   ├── security.py
│   │   │   └── dependencies.py
│   │   ├── db/
│   │   │   ├── database.py
│   │   │   ├── models/
│   │   │   └── migrations/
│   │   ├── schemas/
│   │   ├── services/
│   │   │   ├── analytics.py
│   │   │   ├── nutrition.py
│   │   │   └── storage.py
│   │   └── ai/
│   │       ├── providers/
│   │       │   ├── base.py
│   │       │   ├── nvidia.py
│   │       │   └── groq.py
│   │       ├── workflows/
│   │       │   ├── ai_log.py
│   │       │   └── coach.py
│   │       ├── prompts/
│   │       └── tools/
│   └── tests/
├── frontend/
│   ├── app.py
│   ├── pages/
│   │   ├── dashboard.py
│   │   ├── history.py
│   │   ├── coach.py
│   │   └── settings.py
│   ├── components/
│   └── services/
├── .env
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

## 24. Environment

`.env`:

```env
GROQ_API_KEY=
NVIDIA_API_KEY=
DATABASE_URL=postgresql+psycopg://username:password@localhost:5432/nutrition_tracker
JWT_SECRET_KEY=
OBJECT_STORAGE_ENDPOINT=
OBJECT_STORAGE_ACCESS_KEY=
OBJECT_STORAGE_SECRET_KEY=
OBJECT_STORAGE_BUCKET=
```

`.env.example` contains the same variable names with empty values.

`.gitignore` must include:
```text
.env
.venv/
__pycache__/
*.pyc
```

Never commit secrets.

## 25. Requirements

Start with only what is required:

```text
fastapi
uvicorn
pydantic
sqlalchemy
alembic
psycopg[binary]
python-dotenv
python-multipart
streamlit
langchain
langgraph
langchain-groq
pwdlib[argon2]
PyJWT
```

Add the NVIDIA LangChain integration when the NVIDIA provider is implemented.

Pin exact versions only after selecting the Python version and verifying a compatible package set.

## 26. Development Phases

### Phase 1 — Foundation
- Virtual environment
- Project structure
- `.env` / `.gitignore`
- FastAPI
- Streamlit
- PostgreSQL
- SQLAlchemy
- Alembic
- Health endpoint
- Frontend -> backend connection

**Done:** Streamlit calls FastAPI and FastAPI can connect to PostgreSQL.

### Phase 2 — Authentication & Profile
- Registration
- Password hashing
- Login
- JWT/session handling
- Protected endpoints
- Profile

**Done:** user can register, log in, and maintain profile.

### Phase 3 — Targets & Quick Logging
- Daily targets
- Weight
- Water
- Supplements
- Manual food

**Done:** validated quick logs persist correctly.

### Phase 4 — Dashboard
- Today selector
- Weight/BMI
- Calories
- Protein
- Water
- Supplements
- Meals
- Add Log
- Progress indicators

**Done:** dashboard accurately reflects confirmed PostgreSQL data.

### Phase 5 — History
- Day/week/month
- Weight charts
- Nutrition charts
- Water
- Supplement adherence
- Meal history
- Goal progress

**Done:** history communicates meaningful progress.

### Phase 6 — Groq + LangChain
First verify:
`Python -> LangChain -> ChatGroq -> openai/gpt-oss-120b`

Then implement:
- Structured output
- Pydantic schemas
- Prompts
- AI extraction tests

**Done:** text can become validated draft events.

### Phase 7 — AI Log + LangGraph
- Text
- Image
- Image + text
- Multiple events
- LangGraph state
- Structured drafts
- Review/edit
- Confirmation
- Persistence

**Done:** complete AI Log flow works safely.

### Phase 8 — NVIDIA Provider
- Provider abstraction
- NVIDIA integration
- Text/structured-output tests
- Meal-image tests
- Image + text tests
- Compare with Groq

**Done:** provider can be changed without application-logic changes.

### Phase 9 — AI Coach
- Backend data tools
- LangGraph workflow
- User-specific facts
- LLM response
- Coach UI

**Done:** Coach answers using actual stored data without inventing statistics.

### Phase 10 — Testing & Deployment
Test authentication, isolation, CRUD, calculations, AI output, review/confirmation, images, analytics, and failures. Then deploy.

## 27. Testing Strategy

Backend tests:
- Authentication
- Authorization
- Validation
- CRUD
- User isolation
- Calculations
- Analytics

AI evaluation set:
- Simple meal text
- Meal + water
- Meal + supplement
- Multiple events
- Ambiguous quantities
- Image-only meal
- Image + text

Evaluate event correctness, schema validity, fields, reasonable estimates, and uncertainty handling.

## 28. Security & Error Handling

Security:
- Password hashing
- Secure authentication
- Environment-variable secrets
- User-level authorization
- Input validation
- Safe file-upload handling
- No API keys in source
- No direct frontend DB access

Handle:
- Invalid input
- Unauthorized access
- Database errors
- LLM/API failures
- Invalid AI output
- Image failures
- Missing history

AI failures must not create partial confirmed records. Show useful errors instead of raw tracebacks.

## 29. V1 Scope

### Included
- Authentication
- Profile
- Daily targets
- Dashboard
- Weight
- Water
- Supplements
- Manual food
- AI logging
- Meal image storage
- AI review/confirmation
- Day/week/month history
- Analytics/charts
- AI Coach
- NVIDIA + Groq provider abstraction
- PostgreSQL

### Explicitly out of scope
- RAG
- Vector DB
- OKF
- Microservices
- Kubernetes
- Kafka
- Redis
- Social/community features
- Native mobile apps
- Large nutrition knowledge database
- Automatic AI saving without confirmation

## 30. Definition of Done

A user can:
1. Register and log in.
2. Complete their profile.
3. View daily targets.
4. Use the Dashboard.
5. Log weight, water, supplements, and food manually.
6. Upload a meal image.
7. Describe a meal/event with text.
8. Get structured AI draft events.
9. Review/edit them.
10. Confirm them.
11. See confirmed data on Dashboard.
12. View day/week/month history.
13. See meaningful trends.
14. Ask AI Coach questions about actual data.
15. Receive data-grounded answers.
16. Switch AI providers without changing application logic.
17. Use the deployed web app on laptop and phone-sized screens.

## 31. Coding-Agent Rules

1. Treat this document as the source of truth.
2. Build incrementally.
3. Implement only the current phase.
4. Do not introduce speculative architecture.
5. Keep DB/business logic in backend services.
6. Keep UI concerns in Streamlit.
7. Use Pydantic for API validation.
8. Use SQLAlchemy for DB access.
9. Use Alembic for migrations.
10. Use LangChain/LangGraph only where AI requires them.
11. Never let an LLM directly write confirmed personal data.
12. Validate all AI output.
13. Require user confirmation for AI-generated data.
14. Isolate provider-specific code.
15. Use deterministic calculations for metrics.
16. Never invent personal statistics.
17. Test important business logic.
18. Keep secrets in environment variables.
19. Prefer simple solutions.
20. Verify each phase before moving to the next.

## 32. Immediate Starting Point

Start with **Phase 1 only**.

Target:

```text
Project
├── .venv
├── FastAPI
├── Streamlit
├── PostgreSQL
├── SQLAlchemy
├── Alembic
├── .env
├── .gitignore
├── requirements.txt
└── README.md
```

Verify:

```text
Streamlit
   |
   v
FastAPI /health
   |
   v
PostgreSQL connection
```

Do not build AI Log, AI Coach, or complex UI until the foundation is working.

## Final Architecture

```text
                         USER
                          |
                          v
                    STREAMLIT
                          |
                          v
                       FASTAPI
                    /          \
                   /            \
                  v              v
            PostgreSQL        AI Layer
                                  |
                              LangChain
                                  |
                              LangGraph
                                  |
                              AIProvider
                              /        \
                         NVIDIA        Groq

Confirmed Personal Data
          |
          v
      PostgreSQL
       /   |   \
      v    v    v
Dashboard History AI Coach tools
```

**V1 objective:** Build a clean, practical nutrition/fitness tracker with a reliable database-backed core and a controlled AI layer. AI enhances logging and coaching; PostgreSQL remains the source of truth.
