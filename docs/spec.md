# Спек имплементации — модульная карта

Как блоки [`call-architecture.md`](call-architecture.md) становятся кодом. Организующий принцип:
**каждое решение — отдельный модуль, и каждый модуль дебажится в одиночку**, без поднятия пайплайна
и без живого звонка.

Решения зафиксированы из [`research.md`](research.md): свой pipecat-стек + **ElevenLabs Flash v2.5** TTS
(R0.3), STT — **Deepgram nova phonecall** (R1.3), counter-агенты — **ElevenLabs Agents Platform** (R1.4),
телефония — **Twilio 8kHz WS** + sim-market тумблер (R2.1, R5.1). Вертикаль — переезды, конфигом (B4).

---

## 0. Три правила модульности

1. **Contracts-only imports.** Модули не импортируют друг друга — только `contracts/` (схемы данных,
   ноль логики). Всё общение — типизированные события + call card. Нарушение — ревью-стоп.
2. **Journal = репродьюсер.** Каждое меж-модульное сообщение пишется в JSONL-журнал звонка
   (наследуем `synapse/journal.py`). Любой баг = слайс журнала → фикстура → replay в одном модуле,
   офлайн, без аудио.
3. **Каждый модуль отвечает на `python -m negotiator.<module> --replay <fixture>`.**
   Не запускается в одиночку — значит не закончен.

## 1. Карта модулей

Три кольца: **контур звонка** (hot path, бюджет ~700мс), **мозг** (async, вне бюджета),
**петля продукта** (до/после звонка).

| Модуль | Вход → Выход | Наследует из synapse | Пишем |
|---|---|---|---|
| **контур звонка** | | | |
| `transport` | телефон/браузер ⇄ PCM-фреймы | `pipeline/webrtc_server.py` | Twilio-serializer 8kHz, sim-канал |
| `stt` | фреймы → transcript-события | pipeline (Deepgram) | конфиг phonecall-модели |
| `firewall` | сырой транскрипт → санитизированный | — | экранирование role-делимитеров (R5.3-3) |
| `arbiter` | VAD/turn-события → «чей ход», barge-in | `pipeline/arbiter.py` | тактическая пауза (§5) |
| `talker` | call card + хвост транскрипта → драфт реплики | `dispatcher/llm_client.py` | промпт + библиотека Voss (R3.1) |
| `gate` | драфт + ledger → allow / block+reason | `guards.py`, `dispatcher/tools.py` | правила котировочного вида (§3) |
| `prosody` | фаза → `voice_settings` пресет | — | таблица пресетов (R1.2) |
| `tts` | текст + пресет → аудио | `pipeline/tts_cache.py` | вендор-свап Fish→ElevenLabs (B1) |
| **мозг** | | | |
| `fsm` | события → переход фазы **или exception** | `synapse/threads.py` | стадии + запреты (§2) |
| `ledger` | факты с provenance → стор, `cite(fact_id)` | `journal.py` паттерн | схема, write-authority (R5.3-1) |
| `strategist` | дельта транскрипта + ledger + opponent → новая call card | `cascade/*` (breaker, failover) | промпт, политика анкеринга (R3.2) |
| `opponent` | таймлайн цен + реплики → тактики, floor-оценка | — | **чистые функции** Faratin (R3.3) |
| **петля продукта** | | | |
| `estimator` | голос И документы → JobSpec → CONFIRM | стадия COLLECT | док-инжекшн (B3) |
| `verify` | USDOT/MC → verification-факт в ledger | tool-loop | FMCSA QCMobile клиент (R4.2) |
| `market` | JobSpec + список муверов → расписание звонков, cross-call ledger | — | порядок обзвона (§7) |
| `report` | outcomes[] → ранжированный отчёт с цитатами | `journal.py` | нормализация, ранг, red-flags (B5) |
| `counteragents` | конфиг → 3 агента-оппонента | — | EL Agents Platform, low-code (R1.4) |
| `dashboard` | журнал-события (WS) → war-room UI | `pipeline/client/`, `status-widget.js` | панели §8 |
| `config` | YAML вертикали → все модули | `config.py`, `prompt.py` | контент-пак переездов (B4) |

## 2. Контракты (`contracts/` — единственная точка связности)

Компактные схемы; на диске — pydantic + JSON Schema. Поле со ★ — инвариант, проверяемый в коде.

```
JobSpec        {origin, destination, distance_mi, size(studio…4BR+), date_window,
                floors/elevator, specialty_items[], inventory_src(voice|doc|both),
                budget_ceiling★(приватно — никогда не попадает в call card), confirmed★:bool}

CallCard       {phase, phase_goal, next_move, allowed_fact_ids[]★, tone_preset,
                client_directives[]}          # Talker читает ТОЛЬКО её

LedgerFact     {id, kind(quote|benchmark|jobspec|verification|directive),
                value, source★{type(transcript|config|api), ref, span}, call_id, ts}
                # ★ write-authority: создаётся ТОЛЬКО из tool-результата, конфига или
                # захвата котировки; свободный текст оппонента писать не может (R5.3-1)

Quote          {mover_id, total, line_items[{code(1–14 из R4.4), amount, disclosed}],
                estimate_type(binding|non_binding|BNTE), deposit{amount, refundable},
                carrier_or_broker, usdot/mc, transcript_ref★}

TacticEvent    {type(pressure|vague|stonewall|deadline|lowball), utterance_ref, confidence}

CallOutcome    {call_id, mover_id, status(quoted|refused|callback|hangup)★,
                quote?, red_flags[], transcript_ref}   # ★ есть ВСЕГДА, даже при hangup

Report         {ranked[{mover, normalized_total, missing_items[], red_flags[],
                citations[transcript spans]★}]}
```

## 3. Модули: инварианты + дебаг

Формат: что гарантирует · как сломается · как дебажить **в одиночку**.

### 3.1 `gate` — honesty gate (первый по важности)
- **Инвариант:** fail-closed. Число/утверждение «котировочного вида» без строки в ledger → блок +
  перегенерация. Нет режима «предупредить и пропустить».
- **Дебаг:** `python -m negotiator.gate --replay fixtures/bluff_corpus.jsonl` — корпус из честных реплик,
  явных блефов, пограничных («примерно четыре тысячи» без факта). Выход: verdict+reason на строку.
  Демо-момент «принудительный тест лжи» — этот же корпус, кейс #1.

### 3.2 `opponent` — чистая математика, ноль LLM
- Floor-оценщик = формулы R3.3 как **pure functions**: `estimate_floor(prices, ts) -> (f_hat, band)`;
  `classify_curve(...) -> boulware|linear|conceder`. Классификатор тактик — отдельная функция над репликой.
- **Дебаг:** `python -m negotiator.opponent --prices 5200,4900,4750,4700` — печатает floor, полосу, тип
  кривой. Табличные тесты на синтетических Boulware/Conceder-кривых. Никакого пайплайна вообще.

### 3.3 `fsm` — дисциплина как исключение
- **Инвариант:** запрещённый переход (таблица §2 call-architecture) → exception, не лог.
  Выход из звонка мимо `WRAP` невозможен → `CallOutcome` есть всегда.
- **Дебаг:** table-driven тест всех переходов + replay журнала фаз. Ошибка «фаза перескочила»
  локализуется по стектрейсу, не по логам.

### 3.4 `talker` / `strategist` — два контура, две скорости
- **Инвариант talker:** говорит только из call card; если Strategist не успел — старая карта, **никогда
  не ждёт**. Не имеет tools, меняющих состояние.
- **Инвариант strategist:** единственный владелец `accept_price` tool; коридор — из подтверждённого
  JobSpec. Читает ledger, не сырые реплики (R5.3).
- **Дебаг talker:** `--card fixtures/card_leverage.json --transcript fixtures/tail.txt` → печатает реплику.
  Оценка: попала ли в разрешённые фразы Voss-библиотеки фазы.
- **Дебаг strategist:** слайс журнала звонка → печатает diff старой/новой call card. Golden-тест:
  на фикстуре «названы 3 сбора из 14» карта обязана содержать calibrated question про недостающие.

### 3.5 `ledger` — provenance или ничего
- **Инвариант:** запись только через 3 легальных пути (tool-результат / конфиг / захват котировки со
  span-ссылкой). Проверка R5.3-1 — юнит-тест: «реплика оппонника с "у вас уже есть котировка $9000"
  не создаёт факт».
- **Дебаг:** CLI `add / cite / list --provenance`; `cite` несуществующего id → ошибка (то, что ловит gate).

### 3.6 `estimator` — два пути к одному JSON
- Голос (стадии INTAKE→CONFIRM_SPEC) и документы → **одна** схема JobSpec, один CONFIRM.
- **Дебаг:** `--doc fixtures/old_quote.pdf` → печатает JobSpec; сверка с golden JSON. Голосовой путь —
  replay транскрипта интервью.

### 3.7 `verify` — FMCSA live tool
- `GET /carriers/{dot}?webKey=` (+ `/authority`, `/oos`); fallback — SAFER-скрейп (R4.2).
- **Дебаг:** `python -m negotiator.verify --dot 123456` — самодостаточный HTTP-клиент.
  ⚠️ webKey получить заранее (Login.gov, вручную).

### 3.8 `market` + `report` — cross-call петля
- market: порядок обзвона из конфига (аутсайдер первым, фаворит последним §7); после каждого звонка
  Quote → LedgerFact для следующих.
- report: нормализация line items → ранг → red-flag правила RF-A…RF-F (R4.5) → цитаты span'ов.
- **Дебаг:** `--outcomes fixtures/three_calls.json` → готовый отчёт. Проверка формулы
  `quoted < 0.70×benchmark_low → ALARM` — табличный тест на границах.

### 3.9 `arbiter` + `prosody` + `firewall` — мелкие, но с фикстурами
- arbiter: replay VAD-событий → лог «чей ход»; тактическая пауза = задержка снятия флага (§5), тест таймингом.
- prosody: чистая таблица фаза→`voice_settings` (hot path: `style=0`, `speaker_boost=off` — R1.2). Table-тест.
- firewall: корпус инъекций (`ignore your instructions…`, role-токены, Chevy-кейс) → санитизированный выход.

### 3.10 `counteragents` — вне нашего кода
3 агента на EL Agents Platform (агрессивный / расплывчатый / премиум), у каждого свой промпт+голос,
Twilio-номер импортом. **Дебаг:** позвонить каждому напрямую, до интеграции. Их «сметы» должны включать
запланированные скрытые сборы — это фикстуры для eval «вытащил ли все статьи».

### 3.11 `dashboard` — war-room
Панели §8: транскрипт+фазы, траектория цены+floor-полоса, ledger, тактики, счётчик «bluff blocked»,
live-смета. **Дебаг:** replay журнала записанного звонка → UI оживает без телефонии. Это же — страховка
демо (R5.1-4).

## 4. Дерево репо

```
negotiator/
  contracts/            # схемы выше; ноль логики, ноль импортов модулей
  call/                 # контур звонка
    transport/  stt.py  firewall.py  arbiter.py  talker.py  gate.py  prosody.py  tts.py
  brain/
    fsm.py  ledger.py  strategist.py  opponent.py
  product/
    estimator/  verify.py  market.py  report.py
  config/
    verticals/moving.yaml     # таксономия, бенчмарки R4.3, 14 сборов R4.4, RF-правила R4.5,
                              # Voss-библиотека R3.1, политика анкеринга R3.2, disclosure R2.2
  dashboard/            # PWA war-room
  fixtures/             # bluff_corpus, boulware_prices, injection_corpus, three_calls, old_quote.pdf
  tools/
    slice.py            # журнал звонка → фикстура для конкретного модуля
    latency_report.py   # разбивка mouth-to-ear по звеньям (таблица R5.2)
counteragents/          # экспорт конфигов EL Agents Platform (промпты 3 стилей)
```

## 5. Дебаг-матрица (симптом → модуль → команда)

| Симптом на прогоне | Модуль | Repro без пайплайна |
|---|---|---|
| Назвал число не из ledger | `gate` | `gate --replay slice.jsonl` — обязан блокировать |
| Floor скачет / чушь | `opponent` | `opponent --prices …` (pure fn) |
| Фаза перескочила / нет исхода звонка | `fsm` | стектрейс исключения + table-тест |
| Перебивает собеседника | `arbiter` | replay VAD-событий |
| Молчит >800мс | — | `tools/latency_report.py` → виновное звено (обычно VAD или LLM TTFT, R5.2) |
| Реплика невпопад при живой карте | `talker` | `--card --transcript` фикстура |
| Карта не обновилась по новой котировке | `strategist` | слайс журнала → diff карт |
| «Поверил» словам оппонента | `ledger` | юнит write-authority (R5.3-1) |
| Не вытащил сборы | `config`/`strategist` | golden-call eval, чеклист 14 |
| Отчёт ранжирует странно | `report` | `--outcomes` фикстура |

## 6. Сборка и деградация

`app.py` — единственное место wiring'а (композиция pipecat-пайплайна). Каждое внешнее звено — за
тумблером, лестница деградации = R5.1:

```
Twilio live ──fail──► sim-market (локальный counter-агент)
EL TTS live ──fail──► кэш TTS (опенер + решающие реплики)
всё ─────────fail──► записанный полный прогон
```

Тумблеры — конфиг, переключение ≤10с со сцены. Прогрев всех сокетов за 10–15с до демо.

## 7. План билда (~21ч, отсечение по §9 call-architecture)

| Часы | Блок | Выход (проверяемый) |
|---|---|---|
| 0–2 | contracts + скелет + перенос arbiter/fsm/journal | `fsm` table-тест зелёный |
| 2–5 | talker + gate + ledger, текстовый луп против sim | bluff_corpus блокируется |
| 5–8 | EL TTS свап + Deepgram + латентность | голосовой луп ≤800мс WebRTC |
| 8–11 | strategist + call card + opponent | floor-кривая на дашборд-json |
| 11–14 | market + report (cross-call) | 3 sim-звонка → отчёт с цитатами |
| 14–16 | estimator: док-путь + CONFIRM | pdf → JobSpec golden |
| 16–18 | Twilio live leg + 3 counter-агента на EL | 1 живой звонок записан |
| 18–21 | dashboard-полировка + фикстуры демо + записанный прогон + репетиция | чеклист §8 прогнан |

Правило отсечения: не работает вживую к дедлайну — не существует. Первым режем: prosody-пресеты →
классификатор тактик → floor-кривая (каждый — независимое украшение, петлю не блокирует).

## 8. Приёмка (= требования брифа)

- [ ] Оба пути интейка → один подтверждённый JobSpec
- [ ] ≥3 звонка против разных стилей, каждый со структурированным исходом (даже hangup)
- [ ] ≥1 измеримый сдвиг цены от рычага, добытого самим агентом (cross-call cite)
- [ ] Принудительный блеф заблокирован вживую, счётчик на дашборде
- [ ] Red-flag (−30% + sight-unseen) поднят **в разговоре**, не только в отчёте
- [ ] AI-disclosure в первом предложении (фраза R2.2/R3.4)
- [ ] Ранжированный отчёт с кликабельными цитатами транскриптов
- [ ] Смена вертикали = подмена `config/verticals/*.yaml` (показать судьям diff)

## 9. Внешние действия до старта (из research)

1. Discord: правила переиспользования кода (R0.1) и веса судейства (R0.2)
2. Discord/бриф: требует ли трек их STT (R1.3)
3. Орги: повышенные кредиты ElevenLabs — free = 15 мин, не хватит (R1.5)
4. Twilio → платный аккаунт (R2.1)
5. FMCSA webKey через Login.gov (R4.2)
