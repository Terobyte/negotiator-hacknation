# Баг-хант проекта `nation` — отчёт

Дата: 2026-07-18
Метод: последовательный ручной аудит всех компонентов (`app.py`, `negotiator/call/*`, `negotiator/brain/*`, `negotiator/core/*`, `negotiator/product/*`, `negotiator/dashboard/*`, `negotiator/config/*`) с чтением исходников и точечными grep-проверками для подтверждения находок. Делегирование субагентам было запрошено, но инструмент `Agent`/`Task` в этой сессии оказался заблокирован хуком окружения (`PreToolUse:Callback ... non_file_tool: Agent`), поэтому анализ выполнен напрямую одним агентом по тем же компонентным границам, которые предполагалось раздать субагентам. Изменения в код не вносились — только поиск и документирование.

Всего находок: **27**, из них critical: 2, high: 8, medium: 11, low: 6.

---

## 1. `app.py` (FastAPI orchestration / entrypoint)

### BUG-01 — Twilio auth-token валидатор создаётся безусловно и падает, даже если live-звонки не используются
- **Файл**: `app.py:239`
- **Код**: `validator=twilio_validator or TwilioSignatureValidator(os.getenv("TWILIO_AUTH_TOKEN",""))`
- **Описание**: `TwilioSignatureValidator.__init__` (см. `negotiator/call/transport/twilio.py:113-114`) бросает `ValueError("Twilio auth token is required")`, если токен пустой. Эта строка выполняется безусловно при каждом вызове `create_api()`, независимо от `live_required`/`enable_twilio`. Если `TWILIO_AUTH_TOKEN` не задан (например, в офлайн/sim-развёртывании, где Twilio вообще не используется) и явный `twilio_validator` не передан — всё приложение не поднимается.
- **Severity**: **high** (доступность/надёжность — ломает деплой, не связанный с реальным использованием Twilio)
- **Рекомендация**: создавать `TwilioSignatureValidator` лениво/только когда `live_required=True`, либо оборачивать конструктор в try/except с понятным сообщением, не блокирующим офлайн-режимы.

### BUG-02 — Origin-проверка проходит молча, если заголовок `Origin` отсутствует
- **Файл**: `app.py:254-256`
- **Код**:
  ```python
  def origin(headers:Mapping[str,str])->None:
      value=headers.get("origin")
      if value and value not in origins:raise HTTPException(403,"origin denied")
  ```
- **Описание**: Проверка происхождения запроса пропускается полностью, если заголовок `Origin` не передан (что легко для любого не-браузерного клиента — curl, скрипты, серверные боты). Функция задумана как defense-in-depth поверх bearer-токена, но её легко обойти простым отсутствием заголовка. Тот же паттерн повторяется в `journal_socket` (`app.py:278`: `websocket.headers.get("origin") and ... not in origins`).
- **Severity**: **medium** (не полный bypass, поскольку bearer-токен всё ещё требуется, но защита не fail-closed — расходится с философией `requireSameOrigin` в дашборде, которая, наоборот, fail-closed)
- **Рекомендация**: требовать наличие `Origin` (или `Referer`) на чувствительных эндпоинтах и отклонять запрос при его отсутствии, аналогично `dashboard/app/api/_auth.ts::requireSameOrigin`.

### BUG-03 — `recording_callback` не валидирует формат `CallSid`/`RecordingSid`
- **Файл**: `app.py:335-343`, конкретно `app.py:341`
- **Код**: `metadata=RecordingMetadata(params.get("CallSid",""),params.get("RecordingSid",""),params.get("RecordingUrl",""),int(params["RecordingChannels"]),"both",params.get("RecordingStartTime"))`
- **Описание**: В отличие от `TwilioFrameSerializer.parse()`, где `stream_sid`/`call_sid` проверяются через `_valid_sid(...,"CA"/"MZ")`, здесь `CallSid`/`RecordingSid` берутся как есть (default `""`), без проверки формата. Пустые/произвольные значения дальше публикуются в `BusEvent` и попадают в журнал/citation URL.
- **Severity**: **low**
- **Рекомендация**: применить ту же `_valid_sid`-проверку к `CallSid` (префикс `CA`) и аналогичную к `RecordingSid` (префикс `RE`) перед публикацией события.

### BUG-04 — `DASHBOARD_ALLOWED_ORIGINS` по умолчанию — localhost
- **Файл**: `app.py:240-241`
- **Код**: `origins=set(allowed_origins or configured_origins or {"http://localhost:3000","http://127.0.0.1:3000"})`
- **Описание**: Если оператор забыл выставить `DASHBOARD_ALLOWED_ORIGINS` в проде, CORS/origin-allowlist по умолчанию тихо ограничивается локальными адресами — сам по себе это fail-safe, но при этом это работает "случайно тихо": ошибка конфигурации не подаёт явного сигнала (нет warning/лога), из-за чего продовый дашборд может молча получать 403 без понятной причины.
- **Severity**: **low**
- **Рекомендация**: логировать явное предупреждение при использовании дефолтных origins, чтобы отличать "осознанный dev-режим" от "забытой продовой конфигурации".

---

## 2. `negotiator/call/transport/twilio.py` (Twilio Media Streams)

### BUG-05 — Аудио-фреймы `media` не фильтруются по `track`, в отличие от `dtmf`
- **Файл**: `negotiator/call/transport/twilio.py:164-170` (ср. с проверкой на `176`)
- **Код**:
  ```python
  if event == "media":
      media = message.get("media") or {}; chunk=int(media.get("chunk",0)); timestamp=int(media.get("timestamp",-1))
      ...
  if event == "dtmf":
      if str((message.get("dtmf") or {}).get("track") or "")!="inbound_track": raise ValueError("DTMF must identify inbound_track")
  ```
- **Описание**: DTMF-обработчик явно требует `track=="inbound_track"`. Обработчик `media` не проверяет поле `track` вообще — при двунаправленном stream (`tracks` содержит `inbound` и `outbound`, см. проверку на строке 157: `if "inbound" not in tracks`) сервер может получить `outbound_track`-эхо/дублирующиеся медиа-события и обработать их как входящую речь абонента, что ломает STT/VAD-конвейер и может вызвать ложные срабатывания (например, распознавание собственной речи агента как речи оппонента).
- **Severity**: **high**
- **Рекомендация**: явно проверять `media.get("track") == "inbound_track"` (или отдельно обрабатывать `outbound_track`, если он используется для эхо-мониторинга), аналогично DTMF-проверке.

### BUG-06 — Отсутствует конвертация кодека между Twilio (µ-law) и Deepgram (linear16)
- **Файл**: `negotiator/call/transport/twilio.py:132` (`ENCODING` требуется `audio/x-mulaw`, проверка на `151`) vs. `negotiator/call/stt.py:27-28`
- **Код**:
  - `twilio.py:151`: `if media.get("encoding") != ENCODING or ... : raise ValueError("Twilio stream must be mono audio/x-mulaw at 8000 Hz")`
  - `stt.py:27-28`: `encoding: str = "linear16"` / `sample_rate: int = 8_000`
- **Описание**: Twilio Media Streams всегда используют µ-law (`audio/x-mulaw`, 8kHz), что подтверждается собственной проверкой парсера. Однако `DeepgramConfig` по умолчанию заявляет Deepgram-у, что входящий поток — `linear16`. Кода транскодирования µ-law → PCM16 в проекте не найдено (проверено через grep по всему репозиторию: нет ни одного упоминания `mulaw`/`ulaw` конвертации вне `twilio.py`, и `docs/call-architecture.md` тоже не описывает эту конвертацию). Если сырые µ-law байты отправляются в Deepgram с объявленным `encoding="linear16"`, STT будет декодировать шум, а не реальную речь.
- **Severity**: **critical**
- **Рекомендация**: либо транскодировать µ-law → PCM16 перед отправкой в Deepgram, либо (проще) сконфигурировать `DeepgramConfig.encoding="mulaw"` и оставить `sample_rate=8000`, что соответствует нативному формату Twilio и избавляет от лишней конвертации.

### BUG-07 — `stop`-событие не сбрасывает `last_chunk`/`last_timestamp`/`last_sequence`
- **Файл**: `negotiator/call/transport/twilio.py:180-182`
- **Код**: `if event == "stop": ... self.pending_marks.clear();self.state=Lifecycle.STOPPED;return dict(message)`
- **Описание**: При остановке потока очищаются только `pending_marks` и `state`. Если объект `TwilioFrameSerializer` каким-то образом переиспользуется (например, при повторном использовании instance в тестах или при race conditions в pooling-логике), устаревшие `last_chunk`/`last_sequence`/`stream_sid`/`call_sid` останутся в памяти. Это не создаёт проблему в штатном одноразовом use-case (новый serializer на каждый звонок), но является скрытой хрупкостью API без явной инвариантной защиты (`_require` не запрещает "повторный `start` после `stop`" по этим полям).
- **Severity**: **low**
- **Рекомендация**: сбрасывать `last_chunk=0; last_timestamp=-1; last_sequence=0` вместе с `state=Lifecycle.STOPPED`, либо явно документировать/зафиксировать one-shot-контракт класса.

---

## 3. `negotiator/call/gate.py` (HonestyGate — leak-guard)

### BUG-08 — Детектор словесных денежных сумм покрывает только числа 1–10 (thousand) и не покрывает русский язык
- **Файл**: `negotiator/call/gate.py:24-38`
- **Код**:
  ```python
  _NUMBER_WORDS = {"one":1, ..., "ten":10}
  _WORD_MONEY_RE = re.compile(r"(?i)\b(" + "|".join(_NUMBER_WORDS) + r")\s+thousand(?:\s+dollars?)?\b")
  ```
  используется в `_unsupported_claim_reason`/`_money_amounts` (строка 185 и далее).
  Также применимо к `_MONEY_RE` на строке 21-23, которая ловит только `$123`/`123 dollars` в цифровом виде.
- **Описание**: HonestyGate — это критический "leak-guard", который должен ловить любое озвученное денежное число, не подтверждённое в Ledger. Но регулярка для словесных чисел распознаёт только "one"…"ten thousand [dollars]" на английском. Она не покрывает:
  - числа 11+ ("eleven thousand", "twenty-five thousand"),
  - составные суммы без слова "thousand" ("four hundred", "fifty"),
  - любые русские числительные ("три тысячи", "сорок пять тысяч") — при этом остальной проект явно поддерживает русский язык (см. `_TACTIC_PATTERNS` в `opponent.py`, `_ROLE_DELIMITERS`/`_INJECTION` в `firewall.py` тоже смешанные).
  Агент теоретически может произнести несанкционированную сумму словами ("двадцать тысяч долларов" или "twenty-five thousand"), и Gate это не поймает — что напрямую противоречит цели leak-guard как "fail-closed" механизма.
- **Severity**: **critical** (компрометирует основную защиту от утечки цены/потолка — той самой, ради которой Gate существует)
- **Рекомендация**: расширить word-to-number парсинг (использовать библиотеку типа `word2number`/`text2num`, поддерживающую полный диапазон числительных и минимум EN+RU), либо строить регулярку на полном наборе числительных, а не только 1-10.

---

## 4. `negotiator/call/firewall.py`

*(Файл прочитан полностью на предыдущем этапе. NFKC-нормализация выполняется, но нет защиты от confusable/homoglyph символов — например, кириллическая "а" визуально идентична латинской "a", но не эквивалентна ей после NFKC. Это может позволить обойти `_ROLE_DELIMITERS`/`_INJECTION` regex путём замены латинских букв в служебных словах ("system", "ignore") на визуально похожие кириллические аналоги.)*

### BUG-09 — Firewall не защищён от homoglyph/confusable-обхода regex-фильтров
- **Файл**: `negotiator/call/firewall.py` (NFKC-нормализация, `_ROLE_DELIMITERS`, `_INJECTION` — определения regex в начале файла)
- **Описание**: `sanitize_transcript()` нормализует текст через Unicode NFKC, но NFKC не заменяет кириллические буквы, похожие на латинские (а/a, е/e, о/o, р/p, с/c, х/x), латинскими эквивалентами. Злоумышленник (например, недобросовестный контрагент по телефону, чья речь транскрибируется STT) может использовать смешанные алфавиты в попытке prompt injection ("ignоre previous instructions" с кириллической "о"), обходя word-boundary regex, ищущий "ignore".
- **Severity**: **medium** (эксплуатация зависит от того, насколько STT корректно транслитерирует смешанные последовательности, что на практике снижает вероятность, но не устраняет риск)
- **Рекомендация**: добавить шаг транслитерации confusable-символов (например, через таблицу Unicode Consortium confusables или библиотеку `confusable_homoglyphs`) перед применением injection-regex.

---

## 5. `negotiator/call/arbiter.py`

### BUG-10 — `COUNTERPARTY_STOPPED`/`AGENT_STOPPED` безусловно сбрасывают `turn` в `SILENCE`, теряя информацию о том, кто должен говорить дальше
- **Файл**: `negotiator/call/arbiter.py` (`Arbiter.apply()`, обработка `COUNTERPARTY_STOPPED`/`AGENT_STOPPED`)
- **Описание**: Оба события безусловно переводят `self.turn` в `Turn.SILENCE`, независимо от предыдущего состояния и без различия причины остановки (естественная пауза vs. барж-ин/перебивание). Это теоретически может создать состояние "неопределённости чья очередь" в пограничных случаях одновременной остановки обоих потоков (характерно для full-duplex телефонии), что может привести к взаимному ожиданию (deadlock в диалоге) либо к одновременному говорению обоих участников после недолгой паузы.
- **Severity**: **medium**
- **Рекомендация**: явно протестировать сценарий одновременного `COUNTERPARTY_STOPPED`+`AGENT_STOPPED` и задокументировать/скорректировать переход состояния, чтобы после `SILENCE` явно было закодировано, чья реплика ожидается следующей.

---

## 6. `negotiator/product/estimator/voice.py` (ElevenLabs webhook)

### BUG-11 — Вебхук `/webhooks/elevenlabs/submit_job_spec` не имеет никакой аутентификации/проверки подписи
- **Файл**: `negotiator/product/estimator/voice.py:211-233`, особенно `222-231`
- **Код**:
  ```python
  @router.post("/webhooks/elevenlabs/submit_job_spec")
  def webhook(payload: dict[str, Any]) -> dict[str, Any]:
      try:
          return submit_job_spec(payload, store).as_response()
      except ConfirmationRequired as exc:
          raise HTTPException(status_code=422, detail=str(exc)) from exc
      ...
  ```
- **Описание**: Ни в этом обработчике, ни где-либо в проекте (подтверждено grep по `hmac`/`signature`/`Authorization` в контексте ElevenLabs) не проверяется подпись запроса или bearer-токен, приходящий от ElevenLabs. Любой, кто узнает URL этого вебхука (или просканирует публичный домен), может отправить произвольный `JobSpec`-payload и записать его в `JobSpecStore` (SQLite) от имени любого `conversation_id`, минуя реальный voice-agent диалог. Это прямое нарушение доверенной границы между "подтверждённые голосом факты" и "произвольный внешний ввод" — то есть тот самый инвариант, который `Ledger.ingest_counterparty_utterance()` (в `brain/ledger.py:130-133`) старательно защищает на уровне разговора, здесь обходится напрямую через HTTP.
- **Severity**: **critical**
- **Рекомендация**: добавить проверку HMAC-подписи ElevenLabs webhook (аналогично `TwilioSignatureValidator`) или как минимум статический shared-secret токен в заголовке, проверяемый через `hmac.compare_digest`, до вызова `submit_job_spec`. Также стоит убедиться, что этот роутер реально подключается в `create_api()` в `app.py` — на момент анализа `create_router()` из `voice.py` не вызывается ни в одном месте `app.py`, то есть эндпоинт либо не используется в проде (тогда это dead code), либо подключается где-то вне рассмотренных файлов без документации — в любом случае стоит явно зафиксировать, где и как он монтируется, и защитить его до включения в прод-путь.

---

## 7. `negotiator/core/bus.py`

### BUG-12 — `EventBus.publish()` продолжает рассылку после исключения у подписчика, но поднимает только первое исключение, скрывая остальные
- **Файл**: `negotiator/core/bus.py:29-40`
- **Код**:
  ```python
  def publish(self, event: BusEvent) -> None:
      ...
      first_error: Exception | None = None
      for subscriber in subscribers:
          try:
              subscriber(event)
          except Exception as exc:
              if first_error is None:
                  first_error = exc
      if first_error is not None:
          raise first_error
  ```
- **Описание**: Дизайн "довести fan-out до конца, затем поднять первую ошибку" осмыслен (гарантирует, что Journal — глобальный подписчик — получит событие независимо от сбоя другого подписчика). Но если несколько подписчиков одновременно бросят исключения, все исключения после первого просто проглатываются без логирования — вызывающий код увидит только одну причину сбоя и не узнает о остальных failure. При отладке мультимодульных сбоев (например, если и `HonestyGate`-подписчик, и `Ledger`-подписчик одновременно упадут на одном событии) это может замаскировать вторичный, потенциально более важный баг.
- **Severity**: **medium**
- **Рекомендация**: логировать (не просто отбрасывать) все исключения кроме первого — например, через `logging.exception` — перед `raise first_error`, либо агрегировать все ошибки в `ExceptionGroup` (Python 3.11+) и поднимать её целиком.

---

## 8. `negotiator/core/journal.py`

### BUG-13 — `Journal.append()` перечитывает весь файл на каждую запись (O(n) на append) — риск деградации производительности при длинных звонках
- **Файл**: `negotiator/core/journal.py:30-45`, конкретно `_read_last_seq()` на строке 35 внутри `append()`, реализация `_read_last_seq` на `51-64`
- **Описание**: Каждый вызов `append()` вызывает `self._read_last_seq()`, который построчно читает **весь** файл журнала с начала до конца, чтобы определить последний `seq`. Для длинного звонка (десятки/сотни событий в журнале на один call, при этом журнал общий на процесс — межпроцессный через flock) это даёт квадратичную сложность записи по числу событий. При высокочастотных событиях (STT partial transcripts, latency spans) это может создать заметную задержку записи в конце длинного разговора и — что хуже — удерживать `flock` дольше, блокируя другие писатели/читатели журнала.
- **Severity**: **medium** (performance/latency, не корректность — но проект явно таргетирует sub-1.2s mouth-to-ear latency budget, см. `negotiator/tools/latency_report.py:20`, так что лишняя I/O-задержка на append не тривиальна)
- **Рекомендация**: кэшировать `self._seq` в памяти после инициализации и инкрементировать его на каждом `append()` без повторного чтения файла; либо хранить last-seq в отдельном компактном side-файле, обновляемом атомарно.

### BUG-14 — `fcntl` — POSIX-only импорт, журнал не будет работать на Windows
- **Файл**: `negotiator/core/journal.py:5`
- **Код**: `import fcntl`
- **Описание**: Модуль `fcntl` недоступен на Windows. Импорт на уровне модуля (а не внутри функции с try/except) означает, что весь `Journal`-модуль (и всё, что от него зависит — то есть практически весь backend) не импортируется на Windows-хостах.
- **Severity**: **low** (архитектурно проект явно ориентирован на POSIX/контейнерное развёртывание — это ожидаемое ограничение, но стоит задокументировать явно)
- **Рекомендация**: явно задокументировать POSIX-only требование в README/spec, либо добавить platform-fallback (например, `portalocker`/`filelock`) для кросс-платформенной совместимости.

---

## 9. `negotiator/brain/ledger.py`

### BUG-15 — `_store()` сравнивает факты по полной равности, включая автогенерируемый `ts`, что создаёт ложные `DuplicateFact`
- **Файл**: `negotiator/brain/ledger.py:135-140`, в связке с `add_config`/`add_api_result`/`capture_quote` (строки 37-113, где `ts=ts or datetime.now(timezone.utc)`)
- **Код**:
  ```python
  def _store(self, fact: LedgerFact) -> LedgerFact:
      existing = self._facts.get(fact.id)
      if existing is not None and existing != fact:
          raise DuplicateFact(f"ledger fact id already exists: {fact.id}")
      self._facts[fact.id] = fact
      return fact
  ```
- **Описание**: Если вызывающий код повторно вызывает, например, `add_config(fact_id="X", ..., ts=None)` с тем же `fact_id` и тем же `value` (то есть логически "тот же факт", просто повторно подтверждённый), `ts` каждый раз генерируется заново через `datetime.now(timezone.utc)` — микросекундами позже предыдущего. Поскольку `LedgerFact` — pydantic-модель, `existing != fact` сравнивает все поля включая `ts`, и они почти всегда будут различаться на микросекунды → `DuplicateFact` поднимается **всегда** при повторном добавлении логически идентичного факта без явного `ts`, даже если это не является реальным конфликтом данных (в отличие от ситуации, когда `value` реально изменился — что и должно быть настоящим триггером ошибки).
- **Severity**: **high** (ложные исключения в клиентском коде, который на практике будет часто пытаться повторно зафиксировать один и тот же факт — например, retry-логика или повторная обработка одного и того же tool-результата)
- **Рекомендация**: сравнивать только содержательные поля (`kind`, `value`, `source`, `call_id`), исключая `ts`, при определении дубликата — например, `existing.model_copy(update={"ts": fact.ts}) != fact`, либо явное сравнение конкретных полей вместо `!=` на всей модели.

---

## 10. `negotiator/brain/opponent.py`

### BUG-16 — Guard от негации (negation guard) в `classify_tactic()` реализован только для английского языка
- **Файл**: `negotiator/brain/opponent.py:96-103`
- **Код**:
  ```python
  normalized = " ".join(utterance.casefold().split())
  if any(negation in normalized for negation in ("does not expire", "doesn't expire", "not urgent")):
      normalized = normalized.replace("tomorrow", "").replace("expires", "")
  if "depends on nothing" in normalized:
      normalized = normalized.replace("depends", "")
  ```
  при этом русские паттерны присутствуют в `_TACTIC_PATTERNS` (строки 87-93): `"сегодня", "завтра", "истекает", "не обсуждается", "бронируйте сейчас", "примерно", "посмотрим", "зависит"`.
- **Описание**: Логика классификации тактик поддерживает русский язык (паттерны deadline/lowball/stonewall/pressure/vague на русском). Но guard, который снимает ложное срабатывание DEADLINE/VAGUE при явной негации ("не истекает", "не срочно", "ни от чего не зависит"), реализован только для трёх английских фраз. Русское высказывение вроде "предложение не истекает завтра" будет ошибочно классифицировано как `TacticType.DEADLINE`, потому что слово "завтра" останется в normalized-строке — негация не распознана.
- **Severity**: **medium** (ложноположительная классификация тактики влияет на стратегию переговоров через `Strategist`, но не создаёт утечку данных или сбой системы)
- **Рекомендация**: добавить русские эквиваленты в negation-guard ("не истекает", "не срочно", "не зависит ни от чего" и т.п.), либо реализовать негацию через общий языконезависимый механизм (например, поиск отрицательной частицы в окне N слов перед ключевым словом) вместо списка фиксированных фраз per-language.

### BUG-17 — Проверка "urgency context"/"pressure context" для DEADLINE/PRESSURE смешивает языки только частично
- **Файл**: `negotiator/brain/opponent.py:106-111`
- **Описание**: `urgency_context`/`pressure_context` наборы содержат смешанные EN+RU токены ("price","rate","quote","offer","slot","book","цен","ставк","мест"), что в целом корректно, но не полностью зеркалирует полноту paired EN/RU покрытия, доступного в `_TACTIC_PATTERNS`. Например, для PRESSURE context проверяется только фраза "another customer" (строка 109: `"another customer" in normalized`) — русский эквивалент "другой клиент" из `_TACTIC_PATTERNS` (строка 91) не подключён к этой context-проверке вообще, то есть русская фраза "другой клиент" сработает как PRESSURE без дополнительной context-фильтрации, которую проходит английская версия.
- **Severity**: **low** (несогласованность строгости фильтра между языками, не полный сбой логики)
- **Рекомендация**: зеркалировать все language-specific условные ветки (`"another customer" in normalized`) на соответствующие русские фразы.

---

## 11. `negotiator/brain/strategist.py`

### BUG-18 — Небезопасные `float()`-преобразования данных из `Ledger`/`opponent_summary` без обработки исключений
- **Файл**: `negotiator/brain/strategist.py:140, 144, 183, 185`
- **Код**:
  ```python
  benchmark = float(low)                                              # line 140
  competing = float(total) if competing is None else min(competing, float(total))  # line 144
  current = float(opponent_summary.get("prices", [float("inf")])[-1]) # line 183
  at_floor = floor is not None and current <= float(floor) * 1.03     # line 185
  ```
- **Описание**: Значения `low`/`total` берутся из `fact.value` — поле `LedgerFact.value: Any` (см. `core/contracts/models.py:133`), которое **не типизировано и не валидируется** на уровне контракта (см. также находку по `models.py` из предыдущего анализа: `value: Any` — единственное untyped-поле среди контрактов). Если `fact.value["low"]` или `["total"]` окажется строкой типа `"tbd"`, `None`-подобным текстом или NaN-строкой, `float(...)` бросит `ValueError`/`TypeError` без перехвата — что уронит весь `Strategist.revise()` посреди звонка (это вызывается из "slow loop planner", который, судя по спецификации, работает параллельно с live-звонком).
- **Severity**: **high**
- **Рекомендация**: оборачивать эти преобразования в try/except с fallback на `None`/пропуск факта, либо валидировать структуру `LedgerFact.value` для `BENCHMARK`/`QUOTE`-фактов через дискриминированную pydantic-модель вместо `Any`.

---

## 12. `negotiator/core/contracts/models.py`

### BUG-19 — `LedgerFact.value: Any` — единственное неконтролируемое поле среди строгих контрактов
- **Файл**: `negotiator/core/contracts/models.py:130-136`
- **Код**:
  ```python
  class LedgerFact(Contract):
      id: str = Field(min_length=1)
      kind: LedgerFactKind
      value: Any
      source: Source
      call_id: str = Field(min_length=1)
      ts: datetime
  ```
- **Описание**: Весь проект построен на строгих pydantic-контрактах (`ConfigDict(extra="forbid", frozen=True)`), но `value: Any` полностью выпадает из этой дисциплины — любое значение (строка, число, вложенный dict произвольной формы, даже произвольный объект) допустимо без какой-либо структурной проверки, специфичной для `LedgerFactKind` (QUOTE/BENCHMARK/JOBSPEC/VERIFICATION/DIRECTIVE). Это прямая причина BUG-18 (`Strategist` вынужден угадывать структуру через `.get()`/`float()` без гарантий) и потенциально BUG-15 (сравнение `Any`-значений при duplicate-check).
- **Severity**: **medium**
- **Рекомендация**: ввести `Union`/discriminated-model для `value` в зависимости от `kind` (например, `QuoteValue`, `BenchmarkValue` с обязательными типизированными полями `total: Decimal`, `low: Decimal` и т.д.), либо как минимум валидатор, проверяющий соответствие структуры `value` объявленному `kind`.

### BUG-20 — Сообщение об ошибке в `CallCard.unique_fact_ids` не отражает фактическую причину сбоя
- **Файл**: `negotiator/core/contracts/models.py:71-76`
- **Код**:
  ```python
  @field_validator("allowed_fact_ids")
  @classmethod
  def unique_fact_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
      if len(value) != len(set(value)) or any(not item for item in value):
          raise ValueError("allowed_fact_ids must be non-empty and unique")
      return value
  ```
- **Описание**: Сообщение "must be non-empty and unique" вводит в заблуждение: валидатор **не требует непустого tuple** — пустой `()` проходит валидацию (`len(()) == len(set(()))` → `0==0` True, и `any(...)` на пустом генераторе → `False`), сообщение относится только к отдельным **элементам** (не должны быть пустыми строками), а не к самому tuple целиком. Название метода `unique_fact_ids` тоже не упоминает "non-empty item" семантику.
- **Severity**: **low** (только качество диагностики/понятность ошибки, не функциональный баг)
- **Рекомендация**: переформулировать сообщение, например: `"allowed_fact_ids entries must be non-empty strings and unique"`, чтобы точно отражать проверяемое условие.

---

## 13. `negotiator/product/verify.py` и `negotiator/product/discovery.py`

### BUG-21 — `PlacesClient.search_movers()` откатывается на статичную fixture-выборку без какого-либо флага, отличающего живые данные от заглушки
- **Файл**: `negotiator/product/discovery.py:71-87`
- **Код**:
  ```python
  try:
      if not self.api_key:
          raise RuntimeError("GOOGLE_PLACES_API_KEY is not configured")
      data = self.http(...)
  except Exception:
      data = json.loads(self.fixture.read_text(encoding="utf-8"))
  return parse_places(data)
  ```
- **Описание**: В отличие от `FMCSAClient` (`verify.py`), который хотя бы помечает ответ `"fallback": True/False`, здесь возвращается `parse_places(data)` — список `Business` — без **какого-либо** признака того, что данные взяты из статичной fixture, а не из живого Google Places API. Любая ошибка (сетевая, неверный API-ключ, rate limit, JSON-парсинг) молча подменяет реальный список компаний на захардкоженный фикстур-набор, и вызывающий код (например, отбор перевозчиков для реального звонка) не может отличить одно от другого. Это особенно опасно в проде: если `GOOGLE_PLACES_API_KEY` истёк или квота исчерпана, система продолжит "работать", молча звоня по фиктивным/тестовым номерам/именам из fixture, а не по реальным компаниям.
- **Severity**: **critical** (в проде это означает реальные звонки могут быть направлены на данные из тестовой fixture без какого-либо сигнала оператору)
- **Рекомендация**: возвращать структуру с явным `fallback: bool` (аналогично `verify.py`) и логировать/публиковать в EventBus предупреждение при срабатывании fallback; в продовом режиме желательно делать это фатальной ошибкой, а не тихим откатом.

### BUG-22 — `FMCSAClient` откатывается на fixture по **любому** исключению, включая отсутствие API-ключа, не разграничивая "сервис недоступен" и "конфигурация неверна"
- **Файл**: `negotiator/product/verify.py:40-51, 53-68, 70-74`
- **Код**: `except Exception: return self._fallback("USDOT", identifier)` (аналогично для `verify_mc`); `_get()` на строке 71-72 сама бросает `RuntimeError("FMCSA_WEB_KEY is not configured")`, которая тоже перехватывается тем же `except Exception` выше и превращается в тихий fallback.
- **Описание**: Функция верификации перевозчика (USDOT/MC) — это часть системы, отвечающей за red-flag "RF-D: FMCSA verification is adverse" (см. `report.py:56-57`, `_bad_verification`). Если конфигурация (`FMCSA_WEB_KEY`) просто не выставлена, ошибка конфигурации маскируется под "результат верификации" с `fallback: True` — но, как отмечено в предыдущем анализе, потребители (`report.py`) не проверяют этот флаг `fallback`, то есть красный флаг о неблагоприятной проверке может быть основан целиком на статичных тестовых данных, а не на реальном FMCSA-ответе, и никто в отчёте об этом не узнает.
- **Severity**: **high**
- **Рекомендация**: различать конфигурационные ошибки (должны быть fatal/loud) от временных сетевых сбоев (могут падать на fallback), и обязать `report.py`/`build_report()` учитывать флаг `fallback` при формировании red flags — например, помечать red flag как "unverified (fallback data)" вместо утверждения фактической проверки.

---

## 14. `negotiator/product/report.py`

### BUG-23 — `load_records()` тихо отбрасывает неизвестные/опечатанные поля перед валидацией, обходя `extra="forbid"`
- **Файл**: `negotiator/product/report.py:117-124`, конкретно строка 123-124
- **Код**:
  ```python
  contract_keys = set(CallOutcome.model_fields)
  outcome = CallOutcome.model_validate({key: value for key, value in outcome_data.items() if key in contract_keys})
  ```
- **Описание**: Контракты проекта настроены с `ConfigDict(extra="forbid")`, что должно означать: любое неизвестное поле во входных данных — это ошибка валидации (fail loud). Но здесь входной dict **предварительно фильтруется** до набора известных полей контракта перед вызовом `model_validate()`, то есть `extra="forbid"` никогда не сработает для этого пути загрузки данных — опечатка в имени поля (например, `"call__id"` вместо `"call_id"`) не вызовет ошибку, а просто тихо приведёт к использованию значения по умолчанию (или ошибке "недостающее required поле", если поле обязательное и не имеет default). Это подрывает главную цель строгих pydantic-контрактов — ловить ошибки данных как можно раньше.
- **Severity**: **high**
- **Рекомендация**: убрать пред-фильтрацию и валидировать `outcome_data` как есть через `CallOutcome.model_validate(outcome_data)`, чтобы `extra="forbid"` действительно отрабатывал; если предфильтрация нужна для совместимости с "лишними" полями от старых версий формата — явно логировать/предупреждать об отброшенных полях, а не отбрасывать их молча.

### BUG-24 — `build_report()` аварийно прерывает построение всего отчёта из-за одной записи без цитаты
- **Файл**: `negotiator/product/report.py:90-91`
- **Код**: `if not record.citations: raise ValueError(f"{record.outcome.call_id} has no transcript+recording citation")`
- **Описание**: Если хотя бы одна запись (например, одна из трёх сравниваемых компаний-перевозчиков) не имеет citations, весь вызов `build_report()` бросает необработанное исключение — отчёт не строится вообще, даже если остальные записи полностью валидны и достаточно для сравнения. Для batch-процесса сравнения нескольких компаний это означает: одна "плохая" запись полностью блокирует формирование финального рекомендательного отчёта для клиента.
- **Severity**: **medium**
- **Рекомендация**: пропускать (skip + log) отдельные записи без citations вместо прерывания всего отчёта, если после фильтрации остаётся хотя бы одна валидная запись — сохранив итоговую проверку `if not ranked: raise ValueError(...)` как последний rug-pull guard.

---

## 15. `negotiator/tools/slice.py`

### BUG-25 — `slice_journal()` не перехватывает `JSONDecodeError`, в отличие от `latency_report.read_latency_samples()`
- **Файл**: `negotiator/tools/slice.py:16-27`, особенно строка 20: `row = json.loads(line)`
- **Описание**: Аналогичная функция чтения журнальных JSONL-файлов в `negotiator/tools/latency_report.py:74-77` явно перехватывает `json.JSONDecodeError` и поднимает информативную `ValueError(f"invalid JSONL at line {line_number}")`. `slice_journal()` делает `json.loads(line)` без try/except — при повреждённой/неполной строке (например, файл журнала, который читается во время активной записи другим процессом) упадёт с низкоуровневым `json.JSONDecodeError` без номера строки/контекста, что усложняет диагностику CLI-инструмента, предназначенного именно для дебага журналов.
- **Severity**: **low**
- **Рекомендация**: обернуть `json.loads(line)` в try/except и поднимать информативную ошибку с номером строки, аналогично `latency_report.py`.

---

## 16. `negotiator/dashboard/app/api/_auth.ts` и связанные роуты

### BUG-26 — Аутентификация полностью полагается на непроверяемый заголовок `oai-authenticated-user-email` без криптографической проверки на уровне приложения
- **Файл**: `negotiator/dashboard/app/api/_auth.ts:1-11`
- **Код**:
  ```ts
  const USER_HEADER = "oai-authenticated-user-email";
  export function authorizeWorkspaceRequest(request: Request): Response | null {
    const email = request.headers.get(USER_HEADER)?.trim().toLowerCase();
    ...
  }
  ```
- **Описание**: Вся авторизация дашборда строится на доверии к заголовку `oai-authenticated-user-email`, который предполагается выставленным неким внешним доверенным прокси/gateway (судя по имени заголовка — платформа OpenAI/аналогичный gateway). Однако в самом коде проекта (`negotiator/dashboard/worker/index.ts` — Cloudflare Worker fetch handler, проверен явно) **не найдено** ни валидации, ни принудительной перезаписи/удаления этого заголовка при входе запроса извне. Если Worker разворачивается за пределами того самого доверенного gateway (например, напрямую на `*.workers.dev` или за другим CDN/прокси), то любой внешний клиент может просто подделать заголовок `oai-authenticated-user-email: someone@allowed-domain.com` в HTTP-запросе и получить полный доступ к дашборду (включая проксируемые вызовы к `NEGOTIATOR_API` с реальным bearer-токеном backend'а).
- **Severity**: **critical**
- **Рекомендация**: не доверять заголовку без криптографической подписи (JWT/signed header от гейтвея) либо явно зафиксировать в Worker'e (`worker/index.ts`) принудительное удаление/перезапись этого заголовка на входе для запросов, не пришедших от доверенного источника (mTLS, известный source IP-range, verified JWT в другом заголовке и т.п.), и задокументировать это как жёсткое инфраструктурное требование деплоя.

---

## 17. `negotiator/dashboard/app/page.tsx` (UI дашборда)

### BUG-27 — Список звонков в сайдбаре — статичные хардкод-данные, никогда не синхронизируются с реальным журналом
- **Файл**: `negotiator/dashboard/app/page.tsx:10-14, 124`
- **Код**:
  ```tsx
  const calls = [
    ["01", "Atlantic Moving Co", "lowball broker", "$2,079", "RF-A · RF-B · RF-C"],
    ["02", "Hudson Van Lines", "rushed dispatcher", "$4,100", "14 fees challenged"],
    ["03", "Empire Relocation", "pressure closer", "$3,900", "$700 concession"],
  ];
  ...
  {calls.map((call, index) => <button className={`call ${index===2 ? "active" : ""}`} key={call[0]}>
  ```
- **Описание**: Сайдбар со списком звонков — полностью статичный массив из трёх демо-записей, зашитый в код, никогда не обновляется на основе реальных `liveEvents`/`replaySource`, приходящих через WebSocket/`/api/replay`. Кнопки звонков не имеют `onClick`-обработчика (нет способа переключиться на другой звонок), и "активный" звонок хардкожен по индексу (`index===2`) — всегда третий пункт списка, независимо от того, какой звонок реально отображается. Плюс, форма whisper-директивы (строка 107) хардкодит `call_id:"call-3-pressure_closer"` в теле запроса независимо от выбранного в UI звонка — то есть в текущем виде оператор физически не может отправить whisper-директиву для любого звонка кроме зашитого в код `call-3-pressure_closer`.
- **Severity**: **high** (функциональный баг — интерфейс, представленный как рабочий war-room дашборд с несколькими звонками, на практике управляет только одним хардкоженным звонком)
- **Рекомендация**: связать `calls` с реальными данными (например, агрегировать уникальные `call_id` из `liveEvents`), добавить `onClick`, управляющий выбранным `call_id` в state, и передавать этот `call_id` в whisper-запрос вместо константы.

### BUG-28 — `money()` не обрабатывает `NaN`/`undefined`, рискуя вывести "$NaN" в UI
- **Файл**: `negotiator/dashboard/app/page.tsx:25`
- **Код**: `function money(value: unknown) { return \`$${Number(value).toLocaleString("en-US")}\`; }`
- **Описание**: Если `value` — `undefined`, пустая строка, объект или не-числовая строка, `Number(value)` даст `NaN`, а `NaN.toLocaleString("en-US")` вернёт строку `"NaN"` — итоговый вывод будет `"$NaN"`, показанный оператору дашборда как якобы денежная сумма в реальном времени во время звонка.
- **Severity**: **low**
- **Рекомендация**: добавить явную проверку `Number.isFinite(Number(value))` и возвращать плейсхолдер (например, `"—"`) при нечисловом входе.

---

## Сводная таблица

| ID | Файл | Строки | Severity | Кратко |
|----|------|--------|----------|--------|
| BUG-01 | app.py | 239 | high | Twilio validator падает даже без live-режима |
| BUG-02 | app.py | 254-256, 278 | medium | Origin-проверка пропускается при отсутствии заголовка |
| BUG-03 | app.py | 335-343 | low | Нет валидации формата CallSid/RecordingSid в recording callback |
| BUG-04 | app.py | 240-241 | low | Тихий дефолт origins на localhost без предупреждения |
| BUG-05 | negotiator/call/transport/twilio.py | 164-170 | high | `media`-события не фильтруются по track (в отличие от dtmf) |
| BUG-06 | negotiator/call/transport/twilio.py:151 + negotiator/call/stt.py:27-28 | — | critical | Нет транскодирования µ-law → linear16 между Twilio и Deepgram |
| BUG-07 | negotiator/call/transport/twilio.py | 180-182 | low | `stop` не сбрасывает last_chunk/timestamp/sequence |
| BUG-08 | negotiator/call/gate.py | 24-38 | critical | Word-money regex покрывает только 1-10 (EN), нет RU |
| BUG-09 | negotiator/call/firewall.py | — | medium | Нет защиты от homoglyph-обхода после NFKC |
| BUG-10 | negotiator/call/arbiter.py | — | medium | Безусловный сброс turn в SILENCE при одновременной остановке |
| BUG-11 | negotiator/product/estimator/voice.py | 211-233 | critical | Вебхук ElevenLabs без аутентификации/подписи |
| BUG-12 | negotiator/core/bus.py | 29-40 | medium | publish() проглатывает все ошибки кроме первой без логирования |
| BUG-13 | negotiator/core/journal.py | 30-45, 51-64 | medium | O(n) перечитывание файла на каждый append() |
| BUG-14 | negotiator/core/journal.py | 5 | low | `fcntl` — POSIX-only импорт на уровне модуля |
| BUG-15 | negotiator/brain/ledger.py | 135-140 | high | Duplicate-check сравнивает по ts, создавая ложные DuplicateFact |
| BUG-16 | negotiator/brain/opponent.py | 96-103 | medium | Negation guard только для английского |
| BUG-17 | negotiator/brain/opponent.py | 106-111 | low | Несимметричная context-фильтрация EN/RU для PRESSURE |
| BUG-18 | negotiator/brain/strategist.py | 140, 144, 183, 185 | high | Непроверенные float() из untyped Ledger-данных |
| BUG-19 | negotiator/core/contracts/models.py | 130-136 | medium | `LedgerFact.value: Any` — единственное нетипизированное поле |
| BUG-20 | negotiator/core/contracts/models.py | 71-76 | low | Некорректное сообщение об ошибке в unique_fact_ids |
| BUG-21 | negotiator/product/discovery.py | 71-87 | critical | search_movers() тихо подменяет живые данные на fixture без флага |
| BUG-22 | negotiator/product/verify.py | 40-74 | high | verify_dot/verify_mc маскируют ошибку конфигурации под fallback-результат |
| BUG-23 | negotiator/product/report.py | 117-124 | high | load_records() обходит extra="forbid" предфильтрацией полей |
| BUG-24 | negotiator/product/report.py | 90-91 | medium | Одна запись без citations рушит весь отчёт |
| BUG-25 | negotiator/tools/slice.py | 16-27 | low | Нет обработки JSONDecodeError |
| BUG-26 | negotiator/dashboard/app/api/_auth.ts | 1-11 | critical | Авторизация целиком доверяет непроверяемому HTTP-заголовку |
| BUG-27 | negotiator/dashboard/app/page.tsx | 10-14, 107, 124 | high | Список звонков и whisper-форма хардкожены, нет реальной навигации |
| BUG-28 | negotiator/dashboard/app/page.tsx | 25 | low | money() может вывести "$NaN" |

**Дополнительно** (ранее задокументированные конфигурационные риски, не повторены выше как отдельные BUG-ID, но стоит отметить):
- `negotiator/config/verticals/moving.yaml:8` и `negotiator/config/verticals/plumbing.yaml:8` — оба вертикальных конфига по умолчанию имеют `live_enabled: true`, то есть при случайном использовании дефолтного конфига в dev/staging среде без явного переопределения возможен реальный исходящий звонок вместо симуляции. **Severity: medium.** Рекомендация: дефолт для не-продовых конфигов должен быть `live_enabled: false`, с явным включением через переменную окружения/деплой-оверлей для прода.

---

# Часть 2 — повторный аудит (та же дата, продолжение сессии)

Метод: 4 параллельных субагента. Два верифицировали все 5 находок с severity=critical из части 1 путём прямого чтения текущего кода (не доверяя тексту отчёта); два других провели первичный аудит файлов, не охваченных частью 1 (`brain/fsm.py`, `call/prosody.py`, `call/stt.py`, `call/talker.py`, `call/tts.py`, `call/transport/el_ws.py`, `call/transport/webrtc.py`, `product/estimator/documents.py`, `product/estimator/__main__.py`, `product/market.py`, `config/verticals/*.yaml`, `dashboard/app/api/journal-ticket|replay|whisper/route.ts`, `dashboard/app/layout.tsx`, `dashboard/worker/index.ts`).

## Важный вывод: код в части 1 уже частично исправлен

Между написанием части 1 и этой повторной проверкой код изменился. Из 5 находок с severity=critical:

- **BUG-06** (µ-law/linear16 рассинхрон STT) — **исправлено**. `stt.py:27` теперь `encoding: str = "mulaw"`, есть даже регрессионный тест (`tests/test_bug_regressions_01_14.py:145`).
- **BUG-08** (word-money regex ловит только 1–10 и только EN) — **исправлено**. `gate.py` переписан: полноценные `_EN_SMALL`/`_RU_SMALL`/`_THOUSAND_WORDS` с композицией многословных числительных на английском и русском.
- **BUG-21** (тихий fallback на fixture без флага) — **исправлено**. `discovery.py` теперь возвращает `PlacesSearchResult(fallback: bool, error: str | None)`, аналогично `verify.py`.
- **BUG-26** (доверие заголовку без проверки) — **исправлено**. `_auth.ts` теперь делает полную HMAC-SHA256 проверку подписи + timestamp с окном 300с и constant-time сравнением, fail-closed при отсутствии секрета.
- **BUG-11** (вебхук ElevenLabs без аутентификации) — **частично исправлено**: сам вебхук теперь требует `Authorization: Bearer <secret>` через `hmac.compare_digest`. Но подтверждена вторая половина находки — **`create_router()` из `voice.py` по-прежнему нигде не подключается в `app.py`** — это мёртвый код, не часть боевого HTTP-пути.

Вывод: часть 1 нельзя считать актуальной построчно — нужно сверяться с текущим кодом. Ниже — новые находки по ранее не проверенным файлам.

---

## 18. `negotiator/call/tts.py` — зеркальный баг к уже исправленному BUG-06, но на исходящей стороне

### BUG-29 — TTS отдаёт `pcm_16000`, а Twilio ожидает `audio/x-mulaw` 8kHz на исходящем потоке
- **Файл**: `negotiator/call/tts.py:27` (`output_format: str = "pcm_16000"`) vs. `negotiator/call/transport/twilio.py:17` (`SAMPLE_RATE, CHANNELS, ENCODING = 8_000, 1, "audio/x-mulaw"`)
- **Описание**: STT-сторону (входящее аудио) починили — `DeepgramConfig.encoding` теперь `"mulaw"`. Но TTS-сторону (исходящее аудио, речь агента) — нет: `ElevenLabsTTSConfig.output_format` по умолчанию `"pcm_16000"` (16-bit PCM, 16kHz), конвертации в µ-law/8kHz нигде нет (grep по `audioop|ulaw|mulaw` не находит ничего в `tts.py`). Есть регрессионный тест, фиксирующий исправление на STT-стороне (`test_bug_regressions_01_14.py:145`), но зеркального теста для TTS нет. Если/когда `tts.py` реально подключат к воспроизведению в Twilio-звонок, абонент услышит шум/искажённую по скорости речь вместо голоса агента.
- **Severity**: **critical** (по масштабу идентично исходному BUG-06, только на выходном аудио)
- **Рекомендация**: выставить `output_format="ulaw_8000"` (ElevenLabs поддерживает нативно для телефонии) для Twilio-звонков, либо добавить транскодирование перед `TwilioMediaTransport.send_audio()`. Добавить регрессионный тест по аналогии с STT-стороной.

**Важная оговорка** (относится и к следующим находкам #19–24 из этого раздела): grep по всему репозиторию показывает, что `brain/fsm.py`, `call/stt.py`, `call/talker.py`, `call/tts.py`, `call/prosody.py`, `call/transport/el_ws.py`, `call/transport/webrtc.py` **нигде не импортируются из `app.py`** — только из собственных тестов. Судя по `docs/spec.md:46`, реально подключённый путь звонка — `TwilioFrameSerializer` + `ElevenLabsAgentBridge` (`el_ws.py`), голос-в-голос напрямую через ElevenLabs conversational agent, без отдельной сборки STT+Talker+TTS. То есть весь этот конвейер (включая BUG-29) сейчас не исполняется в боевом пути и не покрыт интеграционными тестами — потенциальная проблема "спящая", но станет реальной в момент, когда эти модули подключат.

## 19. `negotiator/brain/fsm.py`

### BUG-30 — `NegotiationFSM` не подключена к боевому пути звонка
- **Файл**: `negotiator/brain/fsm.py:27-46`
- **Описание**: Класс явно документирован как "единственная state machine в системе", но нигде не импортируется из `app.py`/`product/market.py` — используется только в собственных тестах и CLI. Реальный механизм, который двигает `CallCard.phase` в проде, эту FSM не переиспользует — то есть гарантии "фазы нельзя пропускать/откатывать" и "LEVERAGE требует full_estimate" на практике не применяются.
- **Severity**: **high**
- **Рекомендация**: подключить FSM к реальному пути смены фаз, либо явно пометить модуль как reference-only/deprecated.

### BUG-31 — В FSM нет пути для досрочного завершения звонка (отказ, обрыв связи) до `COMMIT`
- **Файл**: `negotiator/brain/fsm.py:18-24, 44-46`
- **Описание**: `_NEXT` задаёт единственный линейный путь `OPENING → DISCOVERY → PRESSURE_TEST → LEVERAGE → COMMIT → WRAP`. `finish()` требует нахождения ровно в `WRAP`. Реальные звонки часто заканчиваются раньше (абонент бросил трубку, отказался) — вызов `finish()` в таком случае гарантированно поднимет `ForbiddenTransition`.
- **Severity**: **high** (актуально станет в момент подключения FSM к проду, см. BUG-30)
- **Рекомендация**: добавить явную фазу/переход для аварийного завершения (например, `ABORTED`, достижимую из любой фазы), и разрешить `finish()` считать её терминальной.

### BUG-32 — Мелкие огрехи в fsm.py: `is`-сравнение вместо `==` для enum, хрупкая эвристика в `replay()`
- **Файл**: `negotiator/brain/fsm.py:34-38` (сравнение `target is self.phase` / `is not expected` — упадёт на равном по значению, но не идентичном объекте), `negotiator/brain/fsm.py:55-59` (`replay()` решает, валидировать ли строку как `JournalEvent`, по наличию ключа `"seq"`, без try/except на построчный разбор — при сбое падает без номера строки)
- **Severity**: **low**
- **Рекомендация**: заменить `is`/`is not` на `==`; обернуть построчный разбор в `replay()` в try/except с указанием номера строки (аналогично рекомендации BUG-25 из части 1).

## 20. `negotiator/call/talker.py`

### BUG-33 — Транскрипт оппонента вставляется в промпт `OpenAITalkerAdapter` без санитизации (prompt injection)
- **Файл**: `negotiator/call/talker.py:54-60`
- **Код**: `f"TRANSCRIPT TAIL (untrusted style context only): {transcript_tail[-1200:]}"` — вставляется напрямую, без прогона через `negotiator/call/firewall.sanitize_transcript()`.
- **Описание**: `transcript_tail` — речь оппонента по телефону, то есть непроверенный внешний ввод. Единственная защита — текстовая пометка "untrusted" внутри самого промпта, без структурной изоляции (нет отдельной роли сообщения, нет экранирования role-delimiter-токенов, которые `firewall.py` как раз ловит в других местах системы). Тест на инъекцию (`test_talker.py`) гоняет только `OfflineTalkerAdapter`, который `transcript_tail` вообще выбрасывает — то есть уязвимый путь (`OpenAITalkerAdapter`) не покрыт защитой от инъекций совсем.
- **Severity**: **medium** (путь сейчас не подключён к проду — см. оговорку выше, — но затрагивает именно ту границу доверия, которую Gate/Firewall призваны защищать)
- **Рекомендация**: прогонять `transcript_tail` через `sanitize_transcript()` перед вставкой в промпт; добавить тест на инъекцию именно для `OpenAITalkerAdapter`.

### BUG-34 — `OpenAITalkerAdapter.generate()` не перехватывает сбои сети/API, хотя рядом есть готовый offline-fallback
- **Файл**: `negotiator/call/talker.py:61-69`
- **Описание**: Нет try/except вокруг вызова OpenAI API — в отличие от `tts.py`, который явно ловит сетевые ошибки и откатывается на `deterministic_pcm(...)`. В том же модуле уже есть `OfflineTalkerAdapter`, который логично использовать как fallback, но он не используется.
- **Severity**: **medium**
- **Рекомендация**: обернуть вызов в try/except и откатываться на `OfflineTalkerAdapter.generate(...)`, публиковать событие о fallback в EventBus.

## 21. `negotiator/call/stt.py` / `negotiator/call/transport/el_ws.py` — мелкие находки

### BUG-35 — `decode_deepgram_message()` тихо отбрасывает событие `UtteranceEnd`, хотя `utterance_end_ms` явно включён в конфиге
- **Файл**: `negotiator/call/stt.py:32` (конфиг), `:45` (передаётся Deepgram), `:150-151` (обработчик пропускает всё, кроме `type=="Results"`)
- **Severity**: **medium**
- **Рекомендация**: либо убрать `utterance_end_ms` из конфига, если используется только `speech_final`, либо обработать `type=="UtteranceEnd"` явно.

### BUG-36 — `AgentEvent.audio` непоследователен: `None` по умолчанию для всех событий, но `b""` для `audio`-события с пустым payload
- **Файл**: `negotiator/call/transport/el_ws.py:145-148`
- **Severity**: **low**
- **Рекомендация**: использовать `None` вместо `b""` при отсутствующем `audio_base_64/audio_base64`.

## 22. `negotiator/product/market.py` и конфиги вертикалей

### BUG-37 — `demo_number_map` объявлен в YAML вертикалей, но нигде не читается — конфиг выглядит как защита от реального дозвона, но не работает
- **Файл**: `negotiator/config/verticals/moving.yaml:71`, `negotiator/config/verticals/plumbing.yaml:71`; `app.py:load_vertical()` не читает `raw["demo_number_map"]`; используется только как явный Python-аргумент `run_plan(..., demo_number_map=...)`.
- **Описание**: Если оператор редактирует `demo_number_map` в YAML, ожидая, что это подменит реальные номера на демо, — правка не имеет эффекта: значение из файла никуда не попадает. Без явно переданного аргумента `build_call_plan()` в `market.py` дозванивается на реальный номер компании (`dial_phone=phone`), единственная защита — отдельный флаг `runtime.live_enabled`.
- **Severity**: **high** (ложное чувство защищённости в конфиге, который выглядит как safety-контроль)
- **Рекомендация**: либо прокинуть `raw["demo_number_map"]` из YAML в `run_plan()` по умолчанию, либо убрать ключ из схемы и явно задокументировать, что демо-роутинг задаётся только кодом вызывающей стороны.

### BUG-38 — `stt_watchdog_s` загружается из конфига в `RuntimeConfig`, но нигде не используется
- **Файл**: `negotiator/config/verticals/moving.yaml:12`, `plumbing.yaml:12`, `app.py:224` — значение читается и складывается в `RuntimeConfig.stt_watchdog_s`, но ни один модуль (`stt.py`, `arbiter.py`, `transport/*`) его не потребляет.
- **Описание**: `docs/spec.md:317` явно требует watchdog/reconnect на случай "тихого" обрыва STT-соединения (failure mode "R5.1: falls first, silently"). Конфиг существует, реализации — нет.
- **Severity**: **medium**
- **Рекомендация**: реализовать watchdog-таймер на основе `stt_watchdog_s`, либо убрать ключ из схемы до реализации и явно отметить пробел в `docs/spec.md`.

### BUG-39 — `supervise_call`/`supervise_call_async`/`_recover_outcome` глотают все исключения раннера без логирования
- **Файл**: `negotiator/product/market.py:84-92, 105-115, 225-237`
- **Описание**: Любое исключение раннера (включая реальные баги, `TimeoutError`, ошибки валидации результата) перехватывается голым `except Exception:` без `logging`/публикации в `EventBus` — в модуле вообще нет `import logging`. Ошибка тихо превращается в `HANGUP`-исход через `_recover_outcome`. Тот же паттерн, что уже отмечен в части 1 для `EventBus.publish()` (BUG-12).
- **Severity**: **medium**
- **Рекомендация**: логировать исключение перед фолбэком на recovery, публиковать событие `supervise_error` в шину.

## 23. `negotiator/product/estimator/documents.py` и `__main__.py`

### BUG-40 — `confirmed` в `document_to_job_spec()` — просто bool-параметр без привязки к реальному сигналу подтверждения
- **Файл**: `negotiator/product/estimator/documents.py:60-69`
- **Описание**: В отличие от `voice.py` (`_explicit_confirmation()`, проверяет реальные поля read-back в теле вебхука), здесь "подтверждение" — это то, что передал вызывающий код, без проверки против транскрипта/read-back. Сейчас не эксплуатируемо (нет HTTP-пути, только offline CLI и тесты), но при подключении к реальному upload-эндпоинту инвариант "явное подтверждение клиента" будет тривиально обходим.
- **Severity**: **medium** (латентная проблема архитектуры)
- **Рекомендация**: при появлении HTTP-пути валидировать `confirmed` по структурированному сигналу, как в `voice.py`, а не по голому булеву.

### BUG-41 — `load_document()` читает любое неизвестное расширение как UTF-8 текст без белого списка и без обработки ошибок декодирования
- **Файл**: `negotiator/product/estimator/documents.py:55-56`
- **Severity**: **low**
- **Рекомендация**: белый список расширений + понятная ошибка вместо сырого `UnicodeDecodeError`.

### BUG-42 — `estimator/__main__.py` не перехватывает доменные исключения — CLI падает сырым traceback
- **Файл**: `negotiator/product/estimator/__main__.py:10-29`
- **Severity**: **low**
- **Рекомендация**: обернуть `main()` в try/except по доменным исключениям с понятным сообщением и `exit(1)`.

### BUG-43 — Нет проверки формата телефона перед реальным дозвоном
- **Файл**: `negotiator/product/market.py:250-255` (`_field`) — проверяет только непустоту, не формат E.164; значение доходит до `TwilioCallsClient.create_call(to=...)` в `app.py:208` без локальной валидации.
- **Severity**: **low** (Twilio всё равно провалидирует на своей стороне)
- **Рекомендация**: добавить локальную E.164-проверку как последний чек перед реальным звонком.

### BUG-44 — `parse_key_value_text` может заполнить приватное поле `budget_ceiling` из непроверенного текста документа без учёта источника
- **Файл**: `negotiator/product/estimator/documents.py:21-36`
- **Описание**: `budget_ceiling` помечено `private: True` и входит в `privacy.never_speak` в обоих YAML вертикалей, но парсер извлечёт его из любого текста, содержащего строку `budget_ceiling: ...`, без проверки, что документ действительно от клиента, а не от контрагента (например, PDF-смета от самой переezдной компании). Сейчас риск низкий (нет живого OCR-пути), но станет реальным при подключении реального PDF-экстрактора.
- **Severity**: **low** (латентно)
- **Рекомендация**: при подключении реального OCR помечать поля признаком источника и не позволять "неклиентским" документам задавать приватные поля.

## 24. Дашборд — `journal-ticket/route.ts`, `whisper/route.ts`, `replay/route.ts`, `worker/index.ts`

**Важное уточнение к BUG-26 (часть 1)**: в `_auth.ts` уже присутствует полноценная HMAC-SHA256-проверка подписи/timestamp (см. "важный вывод" выше) — риск из BUG-26 закрыт на уровне приложения. Однако `negotiator/dashboard/worker/index.ts:27-44` подтверждённо **не трогает и не удаляет** заголовок `oai-authenticated-user-email` (весь трафик, кроме `/_vinext/image`, пробрасывается как есть в `handler.fetch(request, ...)`) — то есть на уровне Worker нет defense-in-depth; вся защита держится на одном файле `_auth.ts`, и если там случится регресс/неверная конфигурация секрета — ничего в Worker'е это не поймает.

### BUG-45 — Прокси-роуты дашборда не оборачивают fetch к бэкенду в try/catch
- **Файл**: `negotiator/dashboard/app/api/journal-ticket/route.ts:9-10`, `whisper/route.ts:9-10`, `replay/route.ts:14-15`
- **Описание**: При недоступности `NEGOTIATOR_API` необработанное исключение `fetch` даёт обобщённую 500-ошибку фреймворка (потенциально со стектрейсом) вместо аккуратного JSON-ответа, который эти роуты используют в остальных случаях (например, 503 при отсутствии конфигурации).
- **Severity**: **medium**
- **Рекомендация**: обернуть fetch в try/catch, возвращать `502` со структурированным телом.

### BUG-46 — `whisper/route.ts` пробрасывает тело запроса бэкенду без валидации формы и без ограничения размера
- **Файл**: `negotiator/dashboard/app/api/whisper/route.ts:9`
- **Описание**: `body: await request.text()` уходит на бэкенд без проверки JSON-структуры (`call_id`/`directive`) и без серверного лимита длины — ограничение в 500 символов в `page.tsx:173` работает только на клиенте и обходится прямым запросом к API.
- **Severity**: **low/medium**
- **Рекомендация**: валидировать форму на роуте и применять серверный лимит длины перед проксированием.

### BUG-47 — Мелкие огрехи стиля/надёжности в дашборд-роутах
- Хардкоженный `Content-Type: application/json` при потоковой передаче `response.body` без проверки реального типа ответа бэкенда (`journal-ticket`, `whisper`, `replay/route.ts:10,15`) — если бэкенд/прокси вернёт HTML/текст ошибки, клиент получит неинформативную ошибку парсинга JSON.
- `replay/route.ts:17` — импорт `authorizeWorkspaceRequest` расположен в конце файла, а не в начале (работает благодаря hoisting, но сбивает с толку и нарушает единообразие с соседними роутами).
- Нет CSP/security-заголовков (`next.config.ts` без `headers()`, `layout.tsx` без CSP-meta) — стоит добавить базовые заголовки (CSP, `frame-ancestors`, `Referrer-Policy`) для панели, которая проксирует bearer-токен и живые данные звонков.
- **Severity**: **low** (все три)

---

## Обновлённая сводная таблица (часть 2, новые находки)

| ID | Файл | Severity | Кратко | Статус |
|----|------|----------|--------|--------|
| BUG-29 | call/tts.py:27 | critical | Исходящее аудио — pcm_16000 вместо ulaw_8000 для Twilio (зеркало исправленного BUG-06) | новое |
| BUG-30 | brain/fsm.py | high | NegotiationFSM не подключена к боевому пути | новое |
| BUG-31 | brain/fsm.py | high | Нет фазы для досрочного завершения звонка | новое |
| BUG-32 | brain/fsm.py | low | `is`-сравнение enum, хрупкий replay() | новое |
| BUG-33 | call/talker.py:54-60 | medium | Транскрипт оппонента без санитизации в промпте OpenAI-адаптера | новое |
| BUG-34 | call/talker.py:61-69 | medium | Нет fallback/try-except на сбой OpenAI API | новое |
| BUG-35 | call/stt.py | medium | UtteranceEnd события тихо отбрасываются | новое |
| BUG-36 | call/transport/el_ws.py:145-148 | low | None vs b"" непоследовательность | новое |
| BUG-37 | config/verticals/*.yaml + market.py | high | `demo_number_map` из YAML нигде не читается | новое |
| BUG-38 | config/verticals/*.yaml + app.py | medium | `stt_watchdog_s` не реализован, хотя заявлен в spec | новое |
| BUG-39 | product/market.py | medium | supervise_call проглатывает исключения без логов | новое |
| BUG-40 | product/estimator/documents.py:60-69 | medium | confirmed — bool без проверки реального сигнала | новое |
| BUG-41 | product/estimator/documents.py:55-56 | low | Нет белого списка расширений документа | новое |
| BUG-42 | product/estimator/__main__.py | low | Нет обработки исключений в CLI | новое |
| BUG-43 | product/market.py:250-255 | low | Нет E.164-проверки телефона перед дозвоном | новое |
| BUG-44 | product/estimator/documents.py:21-36 | low | budget_ceiling можно задать из непроверенного документа | новое |
| BUG-45 | dashboard/app/api/*/route.ts | medium | Нет try/catch вокруг fetch к бэкенду | новое |
| BUG-46 | dashboard/app/api/whisper/route.ts:9 | low/medium | Нет валидации/лимита тела whisper-запроса | новое |
| BUG-47 | dashboard/app/api/*/route.ts, next.config.ts | low | Хардкод content-type, порядок импорта, нет CSP | новое |

**Статус находок части 1 после повторной проверки**: BUG-06, BUG-08, BUG-21, BUG-26 — исправлены в текущем коде. BUG-11 — исправлено частично (аутентификация вебхука добавлена, но роутер по-прежнему не подключён к `app.py`, то есть остаётся мёртвым кодом). Остальные находки части 1 (BUG-01–05, 07, 09-10, 12-20, 22-25, 27-28) не переверялись повторно в этой сессии и должны считаться актуальными на момент части 1.
