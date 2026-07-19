# Спек имплементации — модульная карта

Как блоки [`call-architecture.md`](call-architecture.md) становятся кодом. Организующий принцип:
**каждое решение — отдельный модуль, и каждый модуль дебажится в одиночку**, без поднятия пайплайна
и без живого звонка.

Решения зафиксированы из [`research.md`](research.md): свой pipecat-стек для контура **переговоров** +
**ElevenLabs Flash v2.5** TTS (вендор — R0.3, модель — R1.1/R1.2), STT — **Deepgram nova phonecall** (R1.3),
**интейк-интервью — ElevenLabs Agents Platform** (§10 п.15, требование брифа модуль 01),
counter-агенты — **ElevenLabs Agents Platform** (R1.4),
телефония — **Twilio 8kHz WS** + sim-market тумблер (R2.1, R5.1). Вертикаль — переезды, конфигом
(B4; B-коды определены в [`inherit-vs-build.md`](inherit-vs-build.md)).

> Расхождения между доками разрешены — свод в [§10 Канон-решения](#10-канон-решения-журнал-решений).

---

## 0. Три правила модульности

1. **Core-only imports.** Модули не импортируют друг друга — только `core/` (`contracts/` — схемы данных,
   ноль логики; `bus` — pub/sub меж-модульных событий; `journal` — append-only writer). Всё общение —
   типизированные события через bus + call card. Нарушение — ревью-стоп.
2. **Journal = репродьюсер, конструкцией.** Каждое меж-модульное сообщение попадает в JSONL-журнал звонка
   не дисциплиной, а устройством: journal подписан на bus целиком — опубликовал событие → оно уже в журнале
   (наследуем `synapse/journal.py`; схема — `JournalEvent` §2). Любой баг = слайс журнала → фикстура →
   replay в одном модуле, офлайн, без аудио.
3. **Каждый decision-модуль отвечает на `python -m negotiator.<module> --replay <fixture>`.**
   Decision-модули: `gate, fsm, talker, strategist, ledger, opponent, estimator, report, firewall`.
   Не запускается в одиночку — значит не закончен. I/O-адаптеры (`transport, stt, tts, dashboard`) —
   smoke-тест + журнал; replay-CLI им не положен (обвязка дороже пользы, §10 п.20).

## 1. Карта модулей

Три кольца: **контур звонка** (hot path), **мозг** (async, вне бюджета), **петля продукта** (до/после звонка).

> **Два бюджета латентности** (R5.2 / R2.3 — не путать, §10 п.5): **sim/WebRTC** — цель ≤800мс (~700мс —
> оптимистичный таргет оптимизированного пути, **не гарантия**: типично 900мс–1.8с, 450–800мс p95).
> **Живой Twilio-leg** — +150–400мс PSTN → mouth-to-ear ~1.1с; sub-500мс для живого **не обещаем**.

| Модуль | Вход → Выход | Наследует из synapse | Пишем |
|---|---|---|---|
| **база (core — импортируется всеми наравне с contracts)** | | | |
| `bus` | publish(event) → подписчики + журнал | — | крошечный pub/sub меж-модульных **событий** (не аудио-фреймов — те живут в pipecat-пайплайне); journal подписан на всё → §0.2 держится конструкцией (§10 п.23) |
| `journal` | любое меж-модульное событие → append-only JSONL | `synapse/journal.py` | seq-нумерация, slice-дружелюбный формат (`JournalEvent` §2) |
| **контур звонка** | | | |
| `transport` | телефон/браузер ⇄ PCM-фреймы | браузер: `pipeline/webrtc_server.py` | **телефон: НЕ webrtc — `FastAPIWebsocketTransport` + `TwilioFrameSerializer` @ 8kHz (R2.1)**; sim-канал = `el_ws.py`, мост к EL counter-агенту напрямую по WS (§10 п.27) |
| `stt` | фреймы → transcript-события | pipeline (Deepgram) | конфиг phonecall-модели |
| `firewall` | сырой транскрипт → санитизированный | — | экранирование role-делимитеров (R5.3-3) |
| `arbiter` | VAD/turn-события → «чей ход», barge-in | `pipeline/arbiter.py` | тактическая пауза (§5) |
| `talker` | call card + хвост транскрипта → драфт реплики | `dispatcher/llm_client.py` | `gpt-4.1-mini` @ OpenAI direct (§10 п.18); промпт + библиотека Voss (R3.1); seed-карта (§3.4) |
| `gate` | драфт + ledger → allow / block+reason | `guards.py`, `dispatcher/tools.py` | вид котировки + **leak-guard приватных полей (R5.3-2, §3.1)** |
| `prosody` | фаза → `voice_settings` пресет | — | таблица пресетов (R1.2) |
| `tts` | `ApprovedUtterance` + пресет → аудио | `pipeline/tts_cache.py` | вендор-свап Fish→ElevenLabs (B1); другой входной тип не принимает — обход гейта невыразим (§3.1, §10 п.24) |
| **мозг** | | | |
| `fsm` | события → переход фазы **или exception** | `synapse/threads.py` | одна машина — фазы NEGOTIATE (§2, §10 п.21); жизненный цикл звонка — оркестрация в `market` |
| `ledger` | факты с provenance → стор, `cite(fact_id)` | `journal.py` паттерн | схема, write-authority (R5.3-1) |
| `strategist` | дельта транскрипта + ledger + opponent → новая call card | `cascade/*` (breaker, failover) | `gpt-5.6-sol` @ OpenAI direct, effort=medium (§10 п.19); промпт, политика анкеринга (R3.2) |
| `opponent` | таймлайн цен + реплики → тактики, floor-оценка | — | **чистые функции** Faratin (R3.3) |
| **петля продукта** | | | |
| `estimator` | голос И документы → JobSpec → CONFIRM | **голос: EL Agents Platform** (§10 п.15); док-путь — свой | структурный вывод EL→маппинг в JobSpec, док-инжекшн (B3) |
| `verify` | USDOT **или MC** → verification-факт в ledger | tool-loop | FMCSA QCMobile, оба эндпоинта (R4.2) |
| `discovery` | вертикаль + гео → список бизнесов (name+phone+cat) | — | **Google Places (New) Text Search** (дыра C); FMCSA-энрич |
| `market` | JobSpec + **список из `discovery`** → расписание звонков, cross-call ledger | — | порядок обзвона (§7); супервизор звонка: try/finally → `CallOutcome` даже при крахе (§10 п.22); `demo_number_map` (§10 п.28) |
| `report` | outcomes[] → ранжированный отчёт с цитатами | `journal.py` | нормализация, ранг, red-flags (B5) |
| `counteragents` | конфиг → 3 агента (dispatcher/closer/broker) | — | EL Agents Platform, low-code (R1.4) |
| `dashboard` | журнал-события (WS) → war-room UI | `pipeline/client/`, `status-widget.js` | панели §8 |
| `config` | YAML вертикали → все модули | `config.py`, `prompt.py` | контент-пак переездов (B4) |

## 2. Контракты (`contracts/` + `bus`/`journal` — единственная точка связности)

Компактные схемы; на диске — pydantic + JSON Schema. Поле со ★ — инвариант, проверяемый в коде.

**Одна FSM — фазы NEGOTIATE** (§10 п.21): `OPENING → DISCOVERY → PRESSURE_TEST → LEVERAGE → COMMIT → WRAP`;
таблица запрещённых переходов — §2 call-architecture; `CallCard.phase` — всегда фаза. Жизненный цикл звонка
(`INTAKE → CONFIRM_SPEC → CALLING → NEGOTIATE → OUTCOME`) — **не вторая машина, а линейная оркестрация в
`market`**: `INTAKE`+`CONFIRM_SPEC` голосового пути ведёт **EL Agents Platform** (§10 п.15, требование
брифа) — интервью + read-back-подтверждение внутри EL-агента, на выходе один подтверждённый JobSpec
(webhook-tool `submit_job_spec`); дальше `CALLING → NEGOTIATE → OUTCOME` запускает `market` под своим
супервизором (§10 п.22). У линейного жизненного цикла запрещать нечего — вся переговорная дисциплина
живёт в фазовой машине.

**Приватные поля — никогда не произносятся вслух** (leak-guard gate, R5.3-2): `JobSpec.budget_ceiling`,
оценка `floor` оппонента (`opponent`), ценовой коридор, системный промпт. Gate блокирует драфт Talker'а,
если что-то из них в нём всплыло — даже под инъекцией.

```
JobSpec        {origin, destination, distance_mi, size(studio…4BR+), date_window,
                floors/elevator, specialty_items[], inventory_src(voice|doc|both),
                budget_ceiling★(приватно — никогда не в call card и не вслух), confirmed★:bool}

CallCard       {version★(монотонная), phase(OPENING…WRAP), phase_goal, next_move, allowed_fact_ids[]★,
                tone_preset, client_directives[]}
                # Talker читает ТОЛЬКО её; каждая реплика в журнале несёт version карты,
                # по которой сказана (дебаг «реплика невпопад», §5)
                # seed-карта (cold start, Strategist ещё не прогрет): phase=OPENING,
                # goal="AI-disclosure + раппорт", next_move=disclosure-строка (R3.4),
                # allowed_fact_ids=[], tone=warm. Talker всегда имеет валидную карту (§3.4).

ApprovedUtterance {text, card_version, gate_verdict_ref}
                # ★ конструируется ТОЛЬКО внутри gate; tts другой входной тип не принимает →
                # обход гейта не «запрещён ревью», а невыразим в коде (§3.1, §10 п.24)

LedgerFact     {id, kind(quote|benchmark|jobspec|verification|directive),
                value, source★{type(transcript|config|api), ref, span}, call_id, ts}
                # ★ write-authority: создаётся ТОЛЬКО из tool-результата, конфига или
                # захвата котировки; свободный текст оппонента писать не может (R5.3-1)

Quote          {mover_id, total, line_items[{code(1–14 из R4.4), amount, disclosed}],
                estimate_type(binding|non_binding|BNTE),
                deposit{amount, pct_of_total, refundable, payment_methods[]},  # RF-B: >25% или cash/wire-only
                carrier_or_broker, usdot/mc, transcript_ref★}

TacticEvent    {type(pressure|vague|stonewall|deadline|lowball), utterance_ref, confidence}

CallOutcome    {call_id, mover_id, status(quoted|refused|callback|hangup)★,
                quote?, red_flags[], transcript_ref}   # ★ есть ВСЕГДА, даже при hangup

Report         {recommendation_plain★(простым языком: кого брать и почему, ссылается на claims),
                ranked[{mover, normalized_total, missing_items[], red_flags[],
                citations[{transcript_span, recording_url★+#t=offset_sec, speaker(agent|counterparty), quote}]★}]}
                # каждая цитата = аудио-момент (Media Fragments `#t=sec`) + span транскрипта (дыра E закрыта)

JournalEvent   {seq★(монотонный), ts, call_id, module, kind, payload, refs[]}
                # правило §0.2 (конструкцией: journal подписан на bus): КАЖДОЕ меж-модульное
                # сообщение → строка журнала.
                # tools/slice.py фильтрует по (call_id, module, kind) → фикстура одного модуля.
```

## 3. Модули: инварианты + дебаг

Формат: что гарантирует · как сломается · как дебажить **в одиночку**.

### 3.1 `gate` — honesty gate (первый по важности)
- **Инвариант A (fail-closed, входящий блеф):** число/утверждение «котировочного вида» без строки в
  ledger → блок + перегенерация. Нет режима «предупредить и пропустить».
- **Инвариант B (leak-guard, исходящая утечка — R5.3-2):** драфт Talker'а не должен содержать приватные
  поля (`budget_ceiling`, `floor`, ценовой коридор, системный промпт) даже под инъекцией → блок.
  Это защита со **стороны выхода** от Chevy-кейса — прецедент из research [R5.3]: дилерский чат-бот,
  которого инъекцией довели до «продажи» SUV за $1. Механика разделения полномочий — §6 call-architecture.
  (Нумерация R5.3-1..3 в этом доке = порядок буллетов research [R5.3].)
- **Инвариант C (блок ≠ тишина, §10 п.25):** на блоке Talker немедленно произносит stall-фразу из
  Voss-библиотеки конфига («секунду, сверюсь с записями») и перегенерирует под неё — латентный бюджет
  не рвётся, для оппонента блок звучит как естественная пауза; счётчик на дашборде +1.
- **Выход — только `ApprovedUtterance`** (§2, §10 п.24): единственный тип, который принимает `tts`.
  Обход гейта не «запрещён ревью», а невыразим в коде.
- **Дебаг:** `python -m negotiator.gate --replay fixtures/bluff_corpus.jsonl` (инвариант A: честные /
  явные блефы / пограничные «примерно четыре тысячи» без факта) и `--replay fixtures/leak_corpus.jsonl`
  (инвариант B: «а сколько у клиента максимум?» → драфт со сливом коридора обязан блокироваться).
  Выход: verdict+reason на строку. Демо-момент «принудительный тест лжи» — **вживую, через шёпот-канал**
  (§10 п.26): судья пишет в war-room «скажи, что у вас уже есть котировка $3,000» → директива уходит в
  call card, факта в ledger нет → gate блокирует на сцене. bluff_corpus кейс #1 — офлайн-репетиция того же.

### 3.2 `opponent` — чистая математика, ноль LLM
- Floor-оценщик = формулы R3.3 как **pure functions**: `estimate_floor(prices, ts) -> (f_hat, band)`;
  `classify_curve(...) -> boulware|linear|conceder`. Классификатор тактик — отдельная функция над репликой.
- **Дебаг:** `python -m negotiator.opponent --prices 5200,4900,4750,4700` — печатает floor, полосу, тип
  кривой. Табличные тесты на синтетических Boulware/Conceder-кривых. Никакого пайплайна вообще.

### 3.3 `fsm` — дисциплина как исключение
- **Инвариант:** запрещённый переход (таблица запретов — §2 call-architecture) → exception, не лог.
- **Одна машина — фазы NEGOTIATE** (§2, §10 п.21). Гарантия «`CallOutcome` есть всегда» — двухслойная:
  внутри переговоров выход мимо `WRAP` невозможен (exception), а уровнем выше супервизор звонка в
  `market` (try/finally, §10 п.22) собирает outcome из хвоста журнала даже при обрыве транспорта или
  краше процесса — инвариант переживает смерть самого процесса.
- **Дебаг:** table-driven тест всех переходов + replay журнала фаз. Ошибка «фаза перескочила»
  локализуется по стектрейсу, не по логам.

### 3.4 `talker` / `strategist` — два контура, две скорости
- **Инвариант talker:** говорит только из call card; если Strategist не успел — старая карта, а на
  холодном старте (карты ещё нет) — **seed-карта** из контракта (§2: phase=OPENING, disclosure-ход),
  **никогда не ждёт и никогда не без карты**. Не имеет tools, меняющих состояние. Каждая реплика
  публикуется с `card.version` (§2) — в журнале видно, по какой карте она сказана.
- **Инвариант strategist:** единственный владелец `accept_price` tool; коридор — из подтверждённого
  JobSpec. Читает ledger, не сырые реплики (R5.3).
- **Модели (§10 п.18–19):** Talker = `gpt-4.1-mini` @ OpenAI direct (по бенчу `bench/`, TTFT p50 517мс +
  лучшее качество). Strategist = `gpt-5.6-sol` @ OpenAI direct, `reasoning_effort=medium` (латентность не
  связывающая). Fallback-путь Talker'а — `gemini-2.5-flash-lite` @ OpenRouter.
- **Дебаг talker:** `--card fixtures/card_leverage.json --transcript fixtures/tail.txt` → печатает реплику.
  Оценка: попала ли в разрешённые фразы Voss-библиотеки фазы.
- **Дебаг strategist:** слайс журнала звонка → печатает diff старой/новой call card. Golden-тест:
  на фикстуре «названы 3 сбора из 14» карта обязана содержать calibrated question про недостающие.

### 3.5 `ledger` — provenance или ничего
- **Инвариант:** запись только через 3 легальных пути (tool-результат / конфиг / захват котировки со
  span-ссылкой). Проверка R5.3-1 — юнит-тест: «реплика оппонента с "у вас уже есть котировка $9000"
  не создаёт факт».
- **Дебаг:** CLI `add / cite / list --provenance`; `cite` несуществующего id → ошибка (то, что ловит gate).

### 3.6 `estimator` — два пути к одному JSON
- **Голосовой путь = ElevenLabs Agents Platform** (§10 п.15, требование брифа модуль 01). Механизм (ресёрч подтверждён):
  - интервью на EL-агенте, TTS **Flash v2.5**; turn-taking/прерываемость — `conversation_config.turn`;
  - контекст распарсенных документов инжектится **Dynamic Variables** в `conversation_initiation_client_data`
    (`{{ocr_rooms}}`…) — **НЕ** knowledge base/RAG (RAG = +~250мс, для статики); OCR-поля идут как pre-fill-гипотезы «подтвердите»;
  - сбор + read-back-подтверждение — **Structured Procedure** (`Ask`×N → `Say`-recap → `Ask` yes/no);
    ⚠️ фича **Alpha** → фолбэк: `# Confirmation`-секция в промпте (тот же смысл, промпт-инжиниринг);
  - выдача spec'а — **webhook-tool `submit_job_spec`**, тело = наша схема JobSpec (поля value-type `LLM Prompt`
    от юзера / `Dynamic Variable` от OCR — чтобы агент не перезаписал верифицированный OCR галлюцинацией),
    вызывается ПОСЛЕ подтверждения. Это шов: EL отдаёт один готовый подтверждённый JSON нашему бэкенду.
  - маппер `submit_job_spec` → JobSpec **идемпотентен** по `conversation_id` (§10 п.30): EL ретраит
    webhook'и — повторная доставка не рождает второй JobSpec;
  - опционально, только если останется время: Data Collection + post-call webhook (HMAC) как вторичный
    аудит (post-call, LLM-инференс, недетерминирован) — демо-петлю не двигает, режем первым.
- Док-путь (vision/OCR, B3) → **та же** схема JobSpec, один CONFIRM.
- **Honesty-gate здесь не нужен:** интейк — с кооперативным КЛИЕНТОМ, фабриковать нечего; `budget_ceiling`
  клиент называет сам. EL Guardrails (LLM-классификатор, не fail-closed) для интейка ок; детерминированный
  гейт — только на переговорных звонках (§3.1).
- **Дебаг:** док-путь — `--doc fixtures/old_quote.pdf` → JobSpec vs golden JSON. Голосовой путь — фикстура тела
  `submit_job_spec` → маппер в JobSpec (юнит) + сверка с golden; live-репетиция EL-агента отдельно.

### 3.7 `verify` — FMCSA live tool
- USDOT: `GET /carriers/{dot}?webKey=` (+ `/authority`, `/oos`); MC: `GET /carriers/docket-number/{mc}?webKey=`
  (⚠️ дефис `docket-number`); fallback — **фикстура ответа**, не SAFER-скрейп (§10 п.29): у fallback'а
  на сцене ровно одна попытка без репетиции — замороженные данные надёжнее второй реализации.
- **Дебаг:** `python -m negotiator.verify --dot 123456` **или** `--mc 654321` — самодостаточный HTTP-клиент
  (MC-путь обязателен для broker-кейса RF4/RF-C). ⚠️ webKey получить заранее (Login.gov, вручную).

### 3.8 `discovery` + `market` + `report` — cross-call петля
- **discovery** (дыра C): список обзвона строится **программно**, не из конфига. `POST places.googleapis.com/v1/places:searchText`,
  `{"textQuery":"movers in <city>","includedType":"moving_company","pageSize":8}`, field-mask `places.nationalPhoneNumber,
  places.displayName,places.formattedAddress` → name+phone в одном ответе (SKU Text Search Enterprise ~$35/1k, для демо копейки;
  ключ self-serve GCP, <10 мин). Yelp дисквалифицирован ($299/мес + триал запрещает деплой), OSM/Overpass — бесплатный фолбэк,
  покрытие `movers` рваное. FMCSA — **не** discovery (ищет по имени/USDOT, без гео), а слой легитимности → отдаётся в `verify`.
- **Шов real→demo (§10 п.28):** реальным муверам не звоним — discovery-список идёт судьям как витрина
  источника (панель дашборда + отчёт), а `market` маппит топ-3 позиции списка на Twilio-номера
  counter-агентов через `config.demo_number_map`. Пустой маппинг = боевой режим (звоним по реальным
  номерам): смена режима — конфиг, не код.
- market: порядок обзвона (аутсайдер первым, фаворит последним §7); после каждого звонка Quote → LedgerFact для следующих;
  супервизор звонка (§3.3): try/finally вокруг каждого звонка → `CallOutcome` из хвоста журнала при любом обрыве.
- report: нормализация line items → ранг → red-flag правила RF-A…RF-F (R4.5) → цитаты = span транскрипта
  **+ аудио-момент** (`{recording_url}#t={offset_sec}`, Media Fragments URI; offset из Twilio `start_time` или
  EL `time_in_call_secs`) **+ плейн-текст рекомендация**. Twilio dual-channel по умолчанию → speaker-атрибуция бесплатно.
- **Дебаг:** `--outcomes fixtures/three_calls.json` → готовый отчёт. Проверка формулы
  `quoted < 0.70×benchmark_low → ALARM` — табличный тест на границах.

### 3.9 `arbiter` + `prosody` + `firewall` — мелкие, но с фикстурами
- arbiter: replay VAD-событий → лог «чей ход»; тактическая пауза = задержка снятия флага (§5), тест таймингом.
- prosody: чистая таблица фаза→`voice_settings` (hot path: `style=0`, `speaker_boost=off` — R1.2). Table-тест.
- firewall: корпус инъекций (`ignore your instructions…`, role-токены, Chevy-кейс) → санитизированный выход.

### 3.10 `counteragents` — вне нашего кода (КАНОН ролей, §10 п.1)
3 агента на EL Agents Platform, каждый — свой промпт+голос, Twilio-номер импортом. Роли выбраны так,
чтобы покрыть все демо-моменты §8 ровно тремя агентами:
1. **`rushed_dispatcher`** (carrier, торопит, **перебивает**) — прячет сборы скороговоркой.
   → демо barge-in (§8) + eval «вытащил ли 14 статей».
2. **`pressure_closer`** (carrier, искусственный дедлайн «цена завтра вырастет», высокий якорь) —
   **уступает под цитатой конкурентной котировки**. → измеримый сдвиг цены (§8) + тактическая пауза + классификатор тактик.
3. **`lowball_broker`** (broker, не carrier; **−35%** ниже бенчмарка — с запасом за порогом RF-формулы
   `<0.70×benchmark_low` (§3.8): скриптованная котировка ровно на −30% легла бы на границу и ALARM бы
   не сработал; sight-unseen, депозит cash/wire non-refundable, скрывает реального carrier).
   → red-flag **в разговоре** RF-A/B/C (§8) + USDOT/MC verify tool-call.
- **Sim-паритет (§10 п.27):** sim-market — это ТЕ ЖЕ 3 EL-агента, к которым transport подключается
  напрямую по WS (`el_ws.py`), минуя Twilio-leg. Никаких локальных копий персон: одна реализация на
  live и sim, evals видят то же поведение, что и живой звонок. Черновики агентов поднимаются в час 0–2
  (low-code UI, параллельно contracts), чтобы с 5-го часа все evals шли против настоящих персон.
- **Дебаг:** позвонить каждому напрямую до интеграции. «Сметы» содержат запланированные скрытые сборы —
  это фикстуры eval. Порядок обзвона (cross-call §7): `lowball_broker` → `rushed_dispatcher` →
  `pressure_closer` (фаворит последним, против максимального рычага).

### 3.11 `dashboard` — war-room
Панели §8: транскрипт+фазы, траектория цены+floor-полоса, ledger, тактики, счётчик «bluff blocked»,
live-смета. **Дебаг:** replay журнала записанного звонка → UI оживает без телефонии. Это же — страховка
демо (R5.1-4).

## 4. Дерево репо

```
negotiator/
  core/                 # импортируется всеми; ноль импортов ПРОЧИХ модулей
    contracts/          # схемы §2; ноль логики
    bus.py              # pub/sub меж-модульных событий; journal подписан на всё — §0.2 конструкцией
    journal.py          # append-only JSONL, JournalEvent, seq (наследует synapse/journal.py)
  call/                 # контур звонка
    transport/          #   webrtc.py (браузер) + twilio.py (FastAPIWebsocketTransport+TwilioFrameSerializer 8kHz, R2.1)
                        #   + el_ws.py — sim: мост к EL counter-агенту напрямую по WS (§10 п.27)
    stt.py  firewall.py  arbiter.py  talker.py  gate.py  prosody.py  tts.py
  brain/
    fsm.py  ledger.py  strategist.py  opponent.py
  product/
    estimator/  discovery.py  verify.py  market.py  report.py
  config/
    verticals/moving.yaml     # таксономия, бенчмарки R4.3, 14 сборов R4.4, RF-правила R4.5,
                              # Voss-библиотека R3.1 (+ stall-фразы §3.1), анкеринг R3.2,
                              # disclosure R2.2/R3.4, demo_number_map (§3.8)
    verticals/plumbing.yaml   # скелет второй вертикали — живой diff для судей (§8, §10 п.31)
  dashboard/            # PWA war-room
  fixtures/             # bluff_corpus, leak_corpus, boulware_prices, injection_corpus, three_calls, old_quote.pdf
  tools/
    slice.py            # журнал звонка → фикстура для конкретного модуля
    latency_report.py   # разбивка mouth-to-ear по звеньям (таблица R5.2)
counteragents/          # экспорт конфигов EL Agents Platform (rushed_dispatcher/pressure_closer/lowball_broker)
```

## 5. Дебаг-матрица (симптом → модуль → команда)

| Симптом на прогоне | Модуль | Repro без пайплайна |
|---|---|---|
| Назвал число не из ledger | `gate` | `gate --replay bluff_corpus.jsonl` — обязан блокировать |
| Слил коридор/floor под инъекцией | `gate` | `gate --replay leak_corpus.jsonl` — обязан блокировать (инвариант B) |
| Floor скачет / чушь | `opponent` | `opponent --prices …` (pure fn) |
| Фаза перескочила | `fsm` | стектрейс исключения + table-тест |
| Звонок без `CallOutcome` (обрыв/краш) | `market` | юнит супервизора: kill процесса мид-звонка → outcome из хвоста журнала |
| Sim-звонок не соединяется | `transport` | `el_ws.py` smoke против EL-агента напрямую |
| Перебивает собеседника | `arbiter` | replay VAD-событий |
| Молчит >800мс (sim/WebRTC) / >1.1с (живой Twilio) | — | `tools/latency_report.py` → виновное звено (обычно VAD или LLM TTFT, R5.2) |
| Реплика невпопад при живой карте | `talker` | `--card --transcript` фикстура |
| Карта не обновилась по новой котировке | `strategist` | слайс журнала → diff карт |
| «Поверил» словам оппонента | `ledger` | юнит write-authority (R5.3-1) |
| Не вытащил сборы | `config`/`strategist` | golden-call eval, чеклист 14 |
| Отчёт ранжирует странно | `report` | `--outcomes` фикстура |

## 6. Сборка и деградация

`app.py` — единственное место wiring'а (композиция pipecat-пайплайна). Каждое внешнее звено — за
тумблером, лестница деградации = R5.1 (порядок = **что падает первым**):

```
STT-uplink WS ─fail─► хотспот как ОСНОВНОЙ канал + WS-watchdog/reconnect   # R5.1: падает первым, тихо
   (2-й STT-вендор не выбран — митигируем каналом + реконнектом + записью, не альтернативным STT)
Twilio live  ──fail─► sim-market (те же EL-агенты напрямую по WS, el_ws.py) # убирает телефонный leg, персона та же
EL TTS live  ──fail─► кэш TTS (опенер + решающие реплики)
всё ─────────fail─► записанный полный прогон
```

Тумблеры — конфиг, переключение ≤10с со сцены. Прогрев всех сокетов за 10–15с до демо.

## 7. План билда (~21ч, отсечение по §9 call-architecture)

| Часы | Блок | Выход (проверяемый) |
|---|---|---|
| 0–2 | contracts + bus/журнал + скелет + перенос arbiter/fsm; параллельно (low-code UI): черновики 3 counter-агентов на EL | `fsm` table-тест зелёный; каждому counter-агенту можно позвонить |
| 2–5 | talker + gate (A+B+C) + ledger, текстовый луп против counter-агента | bluff_corpus и leak_corpus блокируются |
| 5–8 | EL TTS свап + Deepgram + el_ws sim-мост + латентность | голосовой луп против EL-агента живёт; latency_report работает, медиана ≤1.2с (800мс — цель оптимизации, не ворота: типично 900мс–1.8с, §1) |
| 8–11 | strategist + call card + opponent | floor-кривая на дашборд-json |
| 11–14 | market (супервизор + demo_number_map) + discovery + report | 3 sim-звонка → отчёт с цитатами; Places-список на дашборде |
| 14–16 | estimator: EL-интейк-агент (Dynamic Vars + `submit_job_spec` + идемпотентный маппер) + док-путь | голос и pdf → один подтверждённый JobSpec golden |
| 16–18 | Twilio live leg (1 из 3 звонков) + полировка counter-агентов | 1 живой звонок записан; план звонков §8 |
| 18–21 | dashboard-полировка + фикстуры демо + записанный прогон + репетиция | чеклист §8 прогнан |

Правило отсечения: не работает вживую к дедлайну — не существует. Первым режем: prosody-пресеты →
классификатор тактик → floor-кривая (каждый — независимое украшение, петлю не блокирует).

## 8. Приёмка (= требования брифа)

**План звонков (канон, §10 п.6):** ровно **3 звонка = 3 стиля** (`lowball_broker` → `rushed_dispatcher`
→ `pressure_closer`), все против EL-counter-агентов. **≥1 leg — живой Twilio** (на верифицированный
номер — trial-ограничение, §.env), остальные 2 — sim-market/запись (страховка демо + экономия минут).
Cross-call cite делается на последнем звонке (`pressure_closer`, фаворит). Суммарно ≤15 EL-минут (R1.5).

- [ ] Оба пути интейка → один подтверждённый JobSpec
- [ ] Ровно 3 звонка против 3 стилей, каждый со структурированным исходом (даже hangup)
- [ ] Barge-in вживую: перебили — агент замолк, дослушал, вернулся к вопросу (`rushed_dispatcher`)
- [ ] ≥1 измеримый сдвиг цены от рычага, добытого самим агентом (cross-call cite на 3-м звонке)
- [ ] Принудительный блеф заблокирован вживую — директива судьи в шёпот-канал (§10 п.26); счётчик на дашборде; bluff_corpus — репетиция
- [ ] Утечка приватного поля под инъекцией заблокирована (leak_corpus, R5.3-2)
- [ ] Red-flag (−35% + sight-unseen) поднят **в разговоре**, не только в отчёте
- [ ] AI-disclosure в первом предложении (фраза R2.2/R3.4)
- [ ] Ранжированный отчёт с кликабельными цитатами транскриптов
- [ ] Живой leg судится по бюджету ~1.1с, sim — по ≤800мс (§10 п.5)
- [ ] Смена вертикали = подмена `config/verticals/*.yaml` (живой diff `moving.yaml` ↔ `plumbing.yaml`)

Сценарная драматургия демо (hook-pitch, тактическая пауза, реактивное «я вообще с роботом?», HITL-шёпот) —
[`narrative.md`](narrative.md) и таблица §8 call-architecture; здесь — только приёмочные биты брифа.

## 9. Внешние действия до старта (из research «Открытые действия», 7 пунктов)

1. Discord: правила переиспользования кода (R0.1) и веса судейства (R0.2)
2. Discord/бриф: требует ли трек их STT (R1.3)
3. Орги: повышенные кредиты ElevenLabs — free = 15 мин, не хватит (R1.5) ← *кредиты получены 2026-07-18*
4. Twilio → рабочий аккаунт (R2.1) ← *SID в `.env`; trial 30д: живой leg только на верифиц. номер*
5. FMCSA webKey через Login.gov (R4.2)
6. **[5-мин код-проверка, приоритет №1] R5.3-1:** нет пути записи речи оппонента в авторитетные поля
   ledger. Инвариант описан (ledger §3.5), но **проверить в коде до демо** — иначе Chevy-кейс со стороны входа.

## 10. Канон-решения (журнал решений)

Свод: п.1–17 — разрешение расхождений между доками (2026-07-18); п.18–19 — выбор LLM по бенчу;
п.20–33 — архитектурное ревью спека (2026-07-18). Любой пункт-решение можно переопределить.

1. **Роли counter-агентов** *(решение)*: `rushed_dispatcher` / `pressure_closer` / `lowball_broker` (§3.10).
   Отменяет «агрессивный/расплывчатый/премиум» (spec-старое) и «агрессивный/премиум/дисконтный» (R1.4) —
   выбор по покрытию демо-моментов §8: premium не триггерит red-flag, а приёмка его требует → взят lowball.
   Также отменяет лейблы «жёсткий/расплывчатый/фаворит» в диаграмме call-architecture §7 — канонический
   порядок обзвона: `lowball_broker` → `rushed_dispatcher` → `pressure_closer` (§8).
2. **transport-наследование**: телефон = `FastAPIWebsocketTransport`+`TwilioFrameSerializer` @8kHz (R2.1),
   НЕ `webrtc_server.py` (тот — только браузер/sim). §1, §4.
3. **R5.3-2 output-guardrail**: новый инвариант B у `gate` (leak-guard приватных полей). §3.1 + §2.
4. **§9 security-действие**: возвращён пункт «5-мин код-проверка R5.3-1» (§9 п.6).
5. **Латентность**: два бюджета — sim ≤800мс (700 = таргет, не гарантия), живой ~1.1с (R2.3). §1, §5, §8.
6. **План звонков** *(решение)*: 3 звонка = 3 стиля; ≥1 живой Twilio, 2 sim/запись; cite на 3-м; ≤15 EL-мин. §8.
7. **JournalEvent**: схема события журнала добавлена в §2 (правило §0.2 теперь декларативно).
8. **`journal` — модуль**: добавлен в core-кольцо §1 и дерево §4 (наследует `synapse/journal.py`).
9. **Quote.deposit**: `{amount, pct_of_total, refundable, payment_methods[]}` под RF-B. §2.
10. **Стадии vs фазы**: явно разделены две вложенные FSM; `INTAKE` = synapse `COLLECT`. §2, §3.3, §3.6.
11. **Лестница деградации**: добавлена верхняя ступень STT-uplink (падает первым, R5.1). §6.
12. **Cold-start call card**: seed-карта определена в §2 (Talker никогда не без карты). §3.4.
13. **verify MC**: CLI `--mc` + эндпоинт `/carriers/docket-number/{mc}`. §3.7.
14. **~700мс**: переформулировано как оптимистичный таргет, не гарантия (R5.2). §1.
15. **Интейк на EL Agents Platform** *(решение, 2026-07-18)*: голосовое интервью + read-back-подтверждение
    переносятся на **ElevenLabs Agents Platform** — прямое требование брифа (модуль 01 «voice interview built on
    ElevenLabs Agents»). Отменяет прежний дефолт «`INTAKE` = свой synapse `COLLECT`» (R0.3/D3). Механизм (ресёрч):
    Dynamic Variables (OCR-контекст) + Structured Procedure (сбор+подтверждение, Alpha→фолбэк на промпт) +
    webhook-tool `submit_job_spec` (выдача JobSpec нашему бэкенду). Детерминированный honesty-gate и контур
    ПЕРЕГОВОРОВ остаются на своём каскаде — интейку fail-closed гейт не нужен (§2, §3.6, §1). EL Guardrails —
    LLM-классификатор, не fail-closed → на переговоры их не вешаем.
16. **Отчёт цитирует аудио + плейн-текст рекомендация** *(решение)*: `Report` расширен — каждая цитата несёт
    `recording_url#t=offset` (Media Fragments) + span транскрипта + speaker (Twilio dual-channel), плюс поле
    `recommendation_plain`. Закрывает дыру E (Success Criteria «cite recordings AND transcripts»). §2, §3.8.
17. **Discovery списка обзвона** *(решение)*: список бизнесов строится программно через **Google Places (New)
    Text Search** (`includedType=moving_company`, поле `nationalPhoneNumber`), не из конфига. Закрывает дыру C
    (бриф требует показать источник списка). FMCSA — слой легитимности через `verify`, не discovery. §1, §3.8, §4.
18. **LLM Talker'а** *(решение, по бенчу `bench/`)*: **`gpt-4.1-mini` @ OpenAI direct** (`.env`:
    `TALKER_MODEL`/`TALKER_PROVIDER`). Победитель И по TTFT (p50 517мс, в бюджете §5), И по качеству
    (единственный назвал и отбил якорь $2,300, рычаг в обеих сценах, лаконичен — `bench/results/VERDICT.md`).
    Отменяет наследованный дефолт каскада `google/gemini-3.5-flash` (~1900мс TTFT = 2.7× бюджета). Fallback для
    не-OpenAI пути — `gemini-2.5-flash-lite` (790мс на том же OpenRouter-хопе), НЕ 3.5-flash. §1, §3.4.
19. **LLM Strategist'а** *(решение, 2026-07-18)*: **`gpt-5.6-sol` @ OpenAI direct** (`.env`:
    `STRATEGIST_MODEL`/`STRATEGIST_PROVIDER`, `reasoning_effort=medium`). Reasoning-модель — путь
    `max_completion_tokens`+`reasoning_effort`, НЕ `max_tokens`/`temperature`. Латентность не связывающая
    (async переписывает call card между репликами Talker'а — §3.4), поэтому reasoning-налог на TTFT здесь
    допустим. Отменяет альтернативу «локальный Claude Code». §1, §3.4.
20. **Replay-правило сужено до decision-модулей** *(ревью 2026-07-18, как и всё ниже)*: §0.3 обязателен для
    `gate, fsm, talker, strategist, ledger, opponent, estimator, report, firewall`; I/O-адаптерам
    (`transport, stt, tts, dashboard`) — smoke-тест: replay-обвязка транспорта дороже пользы. §0.
21. **Одна FSM вместо двух**: «верхняя машина стадий» понижена до линейной оркестрации в `market` — после
    ухода INTAKE+CONFIRM_SPEC на EL (п.15) в ней остались `CALLING→NEGOTIATE→OUTCOME` без единого
    запрещённого перехода; формализм лишь плодил путаницу «стадии vs фазы» (п.10 в этой части отменён).
    Вся переговорная дисциплина — в фазовой FSM. §1, §2, §3.3.
22. **Outcome-супервизор в `market`**: «CallOutcome всегда» гарантируется двухслойно — fsm-exception внутри
    NEGOTIATE + try/finally-супервизор, собирающий outcome из хвоста журнала при обрыве/краше. §1, §3.3, §3.8, §5.
23. **`bus` в core**: журналирование §0.2 держится конструкцией — journal подписан на pub/sub целиком.
    События, не аудио-фреймы (фреймы остаются в pipecat-пайплайне). §0, §1, §4.
24. **Обход гейта невыразим в типах**: `tts` принимает только `ApprovedUtterance`, конструируемый только
    внутри `gate`. Честность — конструкцией, в духе «TypeError, а не надежда на промпт». §1, §2, §3.1.
25. **Stall-фраза на блоке гейта** (инвариант C): блок не рождает тишину — Talker произносит паузную фразу
    из конфига, пока идёт перегенерация; латентный бюджет цел. §3.1, §4.
26. **Принудительный блеф вживую — шёпот-каналом**: директива судьи без факта в ledger → gate блокирует на
    сцене; интерактивнее заготовленного кейса, ноль новой инфраструктуры. bluff_corpus — репетиция. §3.1, §8.
27. **Sim-паритет counter-агентов**: sim-market = те же EL-агенты напрямую по WS (`transport/el_ws.py`),
    не локальные копии персон. Одна реализация персоны на live и sim. §1, §3.10, §4, §6.
28. **Шов discovery→звонки**: реальный Places-список — витрина источника (дашборд/отчёт); `market` маппит
    топ-3 на номера counter-агентов через `config.demo_number_map` (пустой маппинг = боевой режим). §1, §3.8, §4.
29. **verify-fallback = фикстура**, SAFER-скрейп не пишем: у fallback'а одна попытка на сцене — только
    замороженные данные. §3.7.
30. **Идемпотентный `submit_job_spec`** по `conversation_id`: EL ретраит webhook'и. Вторичный аудит
    (Data Collection) — опционален, режем первым. §3.6.
31. **Скелет второй вертикали** `plumbing.yaml` — иначе чекбокс «показать diff» нечем показывать. §4, §8.
32. **План билда пересобран**: черновики counter-агентов в час 0–2 (low-code, параллельно contracts);
    el_ws-мост в 5–8; discovery в 11–14; слот EL-интейк-агента в 14–16 (раньше отсутствовал вовсе);
    milestone латентности часов 5–8 = «latency_report работает + медиана ≤1.2с» — гейтовать прогресс на
    ≤800мс нельзя, наш же ресёрч зовёт это оптимистичным таргетом (п.5/п.14). §7.
33. **Фиксы ссылок по внешнему ревью**: Chevy-кейс определён на месте (§3.1) с указателем на research
    [R5.3] — ссылка на call-architecture §6 была битой; нумерация R5.3-1..3 = порядок буллетов research;
    barge-in добавлен в приёмку §8; `lowball_broker` сдвинут на **−35%** — ровно −30% легло бы на границу
    RF-формулы `<0.70×` и ALARM бы не сработал (заодно сводит 35% из narrative/call-architecture §8 с
    порогом −30%); лейблы звонков call-architecture §7 отменены (п.1); Flash v2.5 цитируется как
    R1.1/R1.2 (R0.3 — только вендор); B-коды определены в inherit-vs-build.md. §3.1, §3.10, §8.
