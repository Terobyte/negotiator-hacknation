# Ресерч — вопросы и решения

Ресерч в форме **решений**: каждый пункт разблокирует конкретное решение по билду. Бюджет ≤3ч, всё после
R0 — параллельно билду. Метод: 6 параллельных агентов + Tavily/официальные доки. Формат пункта —
**Decision · Evidence · Confidence · Sources**.

**Статус:** 18 из 20 web-исследуемых пунктов закрыты. 2 не решаются поиском — только Discord/бриф
(см. [Открытые действия](#открытые-действия)).

---

## Сводка решений (одним экраном)

| # | Решение |
|---|---|
| **R0.3 / D3** | ✅ Переговоры — **свой pipecat-стек** + ElevenLabs TTS (Guardrails EL = LLM-классификатор, **не** fail-closed гейт). **Интейк-интервью → EL Agents Platform** (требование брифа, §10 п.15). EL Agents = интейк + counter-агенты. |
| **R1.1** | TTS TTFB по WS: ~100–150мс (US/EU) оптимистично, 250–500мс реалистично. Прерывание TTS — **сами на клиенте** (нет нативного mid-flight cancel у standalone TTS WS). |
| **R1.2** | v3 audio tags в realtime **нет**. Prosody Director = per-request `voice_settings` (stability/style/speed) у Flash v2.5. В hot path `style=0`, `speaker_boost=off` (оба добавляют латентность). |
| **R1.3** | STT: оставляем **Deepgram nova phonecall**, ElevenLabs — только TTS. ⚠️ подтвердить в брифе, что трек не требует их STT. |
| **R1.4** | ✅ Counter-агенты — на **ElevenLabs Agents Platform** (native Twilio + Batch Calling, low-code, «спонсорские очки»). |
| **R1.5** | ⚠️ Free-тариф = **15 агент-минут всего** — один прогон из 3 звонков его исчерпывает. **Просить у оргов повышенные кредиты.** Startup Grant — бэкап (медленно). |
| **R2.1** | ✅ Twilio: **перевести аккаунт на платный сейчас** (единственный холд — trial). Номер ~$1.15/мес, ~$0.014/мин. Транспорт — pipecat `FastAPIWebsocketTransport` + `TwilioFrameSerializer` @ 8kHz. |
| **R2.2** | Открывающая фраза = disclosure AI + запись **в одном предложении** (покрывает two-party consent + CA AB 2905 + FCC/TCPA). |
| **R2.3** | Живой телефонный leg = **+150–400мс** к WebRTC-симу. На сцене не обещать sub-500мс для живого звонка. |
| **R3.1** | Библиотека реплик Voss (mirror/label/calibrated/accusation audit), привязанная к фазам FSM — готова, см. ниже. |
| **R3.2** | Мы **информированная сторона** → **якорим первыми**, точным (не круглым) числом-диапазоном, **пока `they_know_we_know == FALSE`**. |
| **R3.3** | Формулы Faratin + **инвертированный оценщик floor** оппонента (geometric-decay + zero-concession-intercept). Boulware/Conceder-считывание гейтит PRESSURE→COMMIT. |
| **R3.4** | Duplex-паттерн: disclosure + запись в первом предложении, потом просьба. Готовая фраза ниже. |
| **R4.1** | Таксономия смет (binding/non-binding/BNTE), правило 110% (§375.407), broker vs carrier; **hostage load = федеральное нарушение**. 7 вопросов DISCOVERY, RF1–RF5. |
| **R4.2** | ✅ Live tool-call реален: **FMCSA QCMobile API** `/carriers/{dotNumber}?webKey=`. ⚠️ webKey = ручная регистрация через Login.gov — **получить заранее**. Fallback без ключа: SAFER Snapshot (HTML). |
| **R4.3** | Бенчмарк-таблица цены (дистанция × спальни) + формула `quoted < 0.70 × benchmark_low → ALARM`, вторичка `< $0.40/lb`. |
| **R4.4** | Чеклист из **14 скрытых сборов** (он же eval-метрика «вытащил ли все статьи?»). |
| **R4.5** | Паттерны скама → red-flag правила с уровнями тревоги (включая правило спеки `−30% + sight-unseen = HIGH`). |
| **R5.1** | Первым падает **STT-uplink WS**. Plan B: мобильный хотспот = основной канал, тумблер sim-market, кэш TTS, записанный прогон-страховка, прогрев сокетов. |
| **R5.2** | ≤800мс реально. **Не оптимизировать TTS/STT** — они ок. Рычаги: VAD/endpointing + LLM TTFT (50–70% бюджета). |
| **R5.3** | Split Talker/Strategist = учебниковый фикс OWASP LLM08. Прецедент: Chevy $1-SUV. Закрыть 3 остаточные дыры (см. ниже). |
| **🐟 Метафора** | Рынок рыб-чистильщиков (biological markets theory). Полный разбор ролей, питч и валидация — [`narrative.md`](narrative.md). |

---

## R0. Блокеры

### [R0.1] Приватный репо (CodeFlow) — можно ли стартовать?
- **Решение:** не web-исследуемо. **Спросить в Discord оргов.** Даже при запрете — стек в голове,
  Negotiator-версия собирается быстро (см. [`inherit-vs-build.md`](inherit-vs-build.md)).

### [R0.2] Точные веса судейства (technical depth / creativity / communication)
- **Решение:** не web-исследуемо (бриф/Discord). Публичной страницы правил Hack-Nation 6 поиск не дал.

### [R0.3] Agents Platform: кастомный tool-gate / контроль barge-in / сырой транскрипт? → D3
- **Решение (уточнено 2026-07-18):** **переговорный** контур — свой pipecat-стек + ElevenLabs TTS (причины a/b/c ниже валидны для звонков оппонентам). НО **интейк-интервью → EL Agents Platform** (spec §10 п.15): бриф модуль 01 это прямо требует, а причины «свой каскад» к кооперативному интейку не применяются (детерминированный fail-closed гейт и тонкий контроль barge-in там не нужны — собеседник это клиент, не оппонент). Итог: EL Agents = **интейк + counter-агенты**; переговоры — свой каскад.
- **Evidence:**
  - (a) Есть «Guardrails 2.0» (Focus/Manipulation/Content/**Custom**), но Custom Guardrail — это **LLM-судья** (`gemini-2.5-flash-lite`), проверяющий ответ промптом (≤10k симв.), а не синхронный хук в нашу детерминированную tool-логику против структурного состояния. Наш honesty gate должен детерминированно проверять «есть ли реально 3 котировки в ledger» до озвучки — это не то же самое.
  - (b) Barge-in нативный, но **не настраиваемый**: нет VAD-порогов/no-interrupt окон; `Interruption` event = `{event_id}` (уведомление, не контроль). Подтверждено сторонним разбором (Deepgram).
  - (c) Транскрипт в realtime есть (`UserTranscript`/`AgentResponse`), но схемы минимальны — без interim/final флага, без word-timestamps на уровне событий.
- **Confidence:** High для (b),(c); Med для (a) — Guardrails частично покрывает «вето до речи», но не заменяет детерминированный гейт.
- **Sources:** elevenlabs.io/docs/eleven-agents/api-reference/…/websocket · …/best-practices/guardrails · deepgram.com/learn/elevenlabs-barge-in-interruptions-turn-taking

---

## R1. ElevenLabs (спонсорский стек — надо знать глубже судей)

### [R1.1] Flash v2.5 — реальный TTFB по WS + поведение при прерывании
- **Решение:** Бюджет TTFB: **100–200мс** оптимистично (US/EU/SEA per офиц. таблица), **250–500мс** реалистично под джиттером. **Прерывание — сами на клиенте** (стоп локального воспроизведения + закрыть/переоткрыть TTS WS или послать пустой `{"text":""}` end-of-stream). Нативного mid-flight cancel у standalone Flash TTS WS **нет** (он есть только внутри Agents Platform).
- **Evidence:** офиц. region-таблица (NA/EU/SEA 100–150мс, S/NE-Asia 150–200мс); инференс модели ~75мс — подмножество. Сторонние замеры разнятся (250мс медиана US ~350мс; из Индии non-optimized WS pcm 711–919мс — оверхед хендшейка/дистанции). Паттерн прерывания как сейчас с Fish: дропаем буфер аудио + стоп воспроизведения.
- **Confidence:** Med (region-числа авторитетны; WS-оверхед варьируется; flush для standalone TTS выведен по отсутствию документации).
- **Sources:** elevenlabs.io/docs/eleven-api/…/latency-optimization · …/overview/models · deepgram.com/learn/how-elevenlabs-api-works

### [R1.2] Экспрессивный контроль в realtime → Prosody Director
- **Решение:** v3 tags в realtime **нет**. Prosody Director = per-request `voice_settings` у Flash v2.5, пресеты по фазе: OPENING — тепло (`stability≈0.3–0.4`), LEVERAGE — спокойно-твёрдо (`stability≈0.6–0.7`, `speed≤1.0`). В hot path: **`style=0`, `use_speaker_boost=false`** (оба явно добавляют латентность).
- **Evidence:** ElevenLabs сам рекомендует Flash v2.5/v2/Multilingual v2 для агентов, v3 — только пререндер. `voice_settings` (stability/similarity_boost/style/speed/use_speaker_boost) настраиваются per-call. `expressive_mode` — конфиг Agents Platform, нам не нужен.
- **Confidence:** High (офиц. API reference).
- **Sources:** elevenlabs.io/docs/api-reference/voices/settings/update · …/text-to-speech/streaming · waboom.ai/blog/elevenlabs-v3-vs-flash-voice-agents

### [R1.3] Scribe на 8kHz vs Deepgram phone — свап или нет?
- **Решение:** **Оставить Deepgram (nova phonecall)** для STT, ElevenLabs — только TTS. ⚠️ **Не решено:** требует ли трек Hack-Nation именно их STT — проверить в брифе/Discord.
- **Evidence:** прямого бенчмарка Scribe v2 vs Nova-3 на 8kHz телефонии на 2026 **нет** (источник прямо это пишет). Консенсус 4 источников: Deepgram лидирует по латентности/телефонной специализации (есть phone-модель); Scribe v2 — по мультиязычной точности (2.2% WER), но не позиционирован под narrowband. Публичных правил Hack-Nation 6 поиск не нашёл.
- **Confidence:** Med по сравнению STT; Low по требованию трека (не выводимо из поиска).
- **Sources:** deepgram.com/learn/elevenlabs-transcription-vs-deepgram · telnyx.com/resources/best-speech-to-text-engine · retellai.com/blog/best-speech-to-text-models

### [R1.4] Agents Platform под counter-агентов (outbound + Twilio + batch)
- **Решение:** ✅ **Да — все 3 counter-агента на ElevenLabs Agents Platform.** Low/no-code: 3 агента с разными промптами/голосами (агрессивный/премиум/дисконтный), native Twilio integration (импорт существующего номера), запуск через API или **Batch Calling** для параллельного «обзвона».
- **Evidence:** native Twilio integration задокументирована (импорт номера, in/out, без изменений телефонной инфры); Batch Calling — first-class фича (список получателей → агент → одновременные звонки, поверх Twilio/SIP).
- **Confidence:** High (доки + офиц. видео + блог).
- **Sources:** elevenlabs.io/docs/eleven-agents/phone-numbers/batch-calls · …/agents/integrations/twilio

### [R1.5] Лимиты кредитов — сколько минут на демо+отладку
- **Решение:** ⚠️ Free-тариф **НЕ хватит.** Agents Platform Free = **15 мин звонков всего**; один прогон (3×3–5 мин) его почти исчерпывает → 0 запаса на отладку. **Действие:** просить у оргов повышенные спонсорские кредиты/ключи (типично для ElevenLabs-хакатонов). Startup Grant (33M симв. / ~680ч, 12 мес, без equity) — бэкап, но одобрение ~неделя (медленно для того же уикенда).
- **Evidence:** Agents (минуты): Free 15мин/4 concurrent; Starter $6 = 75мин; Creator $22 = 275мин; Pro $99 = 1238мин; overage $0.08/мин. TTS-кредиты (отдельный пул): Free ~10–20k симв. (~10–20 мин), PAYG $0.05/1k симв.
- **Confidence:** High по числам; Med по «дадут ли орги кредиты» (спросить).
- **Sources:** elevenlabs.io/pricing/agents · /pricing · /pricing/api · elevenlabs.io/blog/elevenlabs-startup-grants…

---

## R2. Telephony

### [R2.1] Twilio: мгновенный номер, цена, pipecat-транспорт
- **Решение:** ✅ **GO. Перевести Twilio на платный аккаунт сегодня** (снимает единственный холд — trial зовёт только на верифицированные номера). US local номер (self-serve, секунды). Транспорт: `FastAPIWebsocketTransport` + `TwilioFrameSerializer`, sample rate 8000Hz (без ресемпла).
- **Evidence:** для US voice нет регуляторного холда; **A2P 10DLC = только SMS**, голос не гейтит. Единственный блок — trial (снимается картой мгновенно). Цена: номер ~$1.15/мес, outbound ~$0.013–0.014/мин, запись +$0.0025/мин. pipecat имеет purpose-built док dial-out на Media Streams (8kHz mono mulaw, 20мс фреймы).
- **Confidence:** High (офиц. Twilio + pipecat, согласованы).
- **Sources:** twilio.com/docs/messaging/compliance/a2p-10dlc/quickstart · help.twilio.com «Free Trial Limitations» · docs.pipecat.ai/pipecat/telephony/twilio-websockets

### [R2.2] Юридика записи + disclosure бота
- **Решение:** Считать каждый живой звонок как попадающий в two-party consent. Одна открывающая фраза покрывает и запись, и AI-disclosure:
  > «Здравствуйте, это AI-ассистент по переговорам, звоню от имени [X]. Звонок может записываться, и разговор ведёт AI-голос. Удобно сейчас пройтись по деталям котировки?»
- **Evidence:** 11 all-party-consent штатов (CA, DE, FL, IL, MD, MA, MT, NV, NH, PA, WA); CA §632 строже всех, распространяется на звонящего из one-party штата (*Kearney v. Salomon Smith Barney*). CA AB 2905 (с 01.01.2025) — disclosure AI-голоса в начале. FCC (08.02.2024): AI-голос = «artificial/prerecorded» под TCPA. Нюанс: TCPA B2B-исключение для опубликованного бизнес-лендлайна (звоним на главную линию мувера), но не полагаться жёстко (*Porch.com*, 9th Cir.) — disclosure всё равно делаем (это наша фича).
- **Confidence:** High по списку штатов/FCC; Med по применимости AB 2905 к живому conversational AI (для демо не важно — фраза покрывает оба риска).
- **Sources:** mwl-law.com RECORDING-CONVERSATIONS-CHART.pdf · getaira.io/…/california-ai-voice-disclosure · wiley.law FCC-Extends-Regulatory-Reach-Over-AI

### [R2.3] Латентность телефонного leg vs WebRTC
- **Решение:** Бюджетировать **+150–400мс** на живой звонок vs WebRTC-сим. На сцене: «живой звонок меняет чуть-чуть отзывчивости на достоверность — реальный PSTN». Держать mouth-to-ear ~1.1с; не обещать sub-500мс для живого.
- **Evidence:** μ-law/8kHz PSTN требует decode+upsample/downsample+encode на каждом фрейме (нет на WebRTC). Twilio-таргеты: platform turn gap 885мс медиана / mouth-to-ear 1115мс медиана (≈230мс — цена сетевого/PSTN leg). Retell 2025: Twilio voice ~950мс vs WebRTC-конкурент ~420мс (confounded, directional).
- **Confidence:** Med (нет источника, изолирующего чисто PSTN; вывод согласован по 2 источникам).
- **Sources:** twilio.com/…/guide-core-latency-ai-voice-agents · retellai.com/resources/sub-second-latency…

---

## R3. Наука переговоров → конфиг

### [R3.1] Voss → библиотека реплик (по фазам FSM)
- **MIRROR** (повтор последних ≤3 слов + тишина ≥4с; фаза DISCOVERY/PRESSURE):
  - «…plus a fuel surcharge?» / «Can't guarantee before the 15th?» / «Includes everything?»
  - Конфиг: `mirror = last_n_tokens(utterance, n≤3)+"?"` → `insert_silence(4s)`.
- **LABEL** (детачед-опенер «It seems like… / It sounds like…», не «I understand»; каждое ~4-е высказывание; DISCOVERY/LEVERAGE):
  - «It sounds like the final number depends on things you haven't pinned down yet.»
  - «It seems like there's room on that fee if the pickup timing works for you.»
  - «It sounds like you're protecting your margin on the fuel line.»
- **CALIBRATED Q** (только What/How; workhorses «How am I supposed to…?», «What about this works for you?»; DISCOVERY/PRESSURE/LEVERAGE):
  - «What are all the charges that could show up that aren't on this quote?»
  - «How am I supposed to compare this when the fuel surcharge isn't broken out?»
  - «What would it take to make this a binding, not-to-exceed number?»
  - Дефлект их анкера: «How am I supposed to do that?»
- **ACCUSATION AUDIT** (преэмптивно, перед низким числом; OPENING/COMMIT):
  - «You're probably going to think I'm just another caller trying to grind you on price.»
  - «I know this'll sound like I haven't done my homework when you hear my number.»
- **Confidence:** High (Black Swan cheat-sheet verbatim + их рассылка).
- **Sources:** famvestor.com/…/NeverSplitTheDifference…pdf · blackswanltd.com/newsletter/…

### [R3.2] Анкеринг при info-advantage
- **Решение / политика LEVERAGE:**
  ```
  we_know_ZOPA      = benchmark + ≥1 competing quote   # у нас TRUE
  they_know_we_know = раскрыли ли, что держим бенчмарки? # держать FALSE до анкера
  IF we_know_ZOPA AND NOT they_know_we_know:
      → ЯКОРИМ ПЕРВЫМИ. Точное низкое число ($3,180, не $3,200),
        как диапазон, чей ВЕРХ = наша цель ("$3.0–3.3k по 3 котировкам").
        Перед этим — 1 строка accusation audit.
  ELIF they_anchored: → ДЕФЛЕКТ (не контрить их число): "How am I supposed to get there when I have quotes at $X?" → ре-анкор от нашего бенчмарка.
  ELIF NOT we_know_ZOPA: → пусть якорят первыми.
  ```
- **Evidence:** PON/Harvard: якорить первым, если знаешь ZOPA лучше них. Maaravi & Levy: **неинформированная** сторона выигрывает ходом вторым; информированный первый-ходящий рискует заякориться на своём же диапазоне. Мы — информированные → «aggressive first offer with confidence». Точные анкеры > круглых (Mason 2013; Loschelder 2014). Сила анкера падает, как только раскрыли книги → якорить **до** раскрытия бенчмарков.
- **Confidence:** High.
- **Sources:** pon.harvard.edu/…/when-to-make-the-first-offer · sas.upenn.edu/~baron/journal/17/17327a/jdm17327a.html

### [R3.3] Faratin 1998 → оценщик floor оппонента
- **Функция оффера** (issue j, время t):
  ```
  x^t[j] = min_j + α_j(t)·(max_j − min_j)        # V убывает (покупатель по цене)
  x^t[j] = min_j + (1 − α_j(t))·(max_j − min_j)   # V растёт (продавец по цене)
  ```
  Граничные: α(0)=κ (константа старт-оффера), α(t_max)=1 (на дедлайне отдаёшь reservation value).
- **Семейства α(t):**
  ```
  Polynomial:  α(t) = κ + (1−κ)·( min(t,t_max)/t_max )^(1/β)
  Exponential: α(t) = exp( (1 − min(t,t_max)/t_max)^β · ln κ )
  ```
  **β<1 → Boulware** (держит, потом резко уступает у дедлайна); **β=1 → Linear**; **β>1 → Conceder** (уступает рано).
- **Live-оценщик floor (инверсия модели — артефакт дашборда).** Наблюдаем офферы продавца `p_1>…>p_n` в моменты `t_1…t_n` (уступает вниз к неизвестному F):
  1. **Геометрический** (нужно ≥3 оффера): `Δ_k=p_k−p_{k+1}`, `r=median(Δ_{k+1}/Δ_k)` (clamp [0,1)); `F_hat = p_n − Δ_{n−1}·r/(1−r)`.
  2. **Zero-concession-intercept** (кросс-чек): регрессия `Δ_k = a + b·p_k` → `F_hat = −a/b`.
  Репорт: `F_hat = max(оценка1, оценка2)`, полоса уверенности = разброс между ними.
  **Считывание тактики:** уступки в конце → **Boulware** (floor рано ненадёжен, дави); большие уступки рано, потом плато → **Conceder** (F_hat надёжен, ты у дна → COMMIT). Это и есть сигнал гейта **PRESSURE→COMMIT**.
- **Confidence:** High по формулам (verbatim из первичного PDF); Med по константам оценщика (наша конструкция — тюнить на симуляторе).
- **Sources:** jmvidal.cse.sc.edu/library/faratin98a.pdf (первичный) · Robotics and Autonomous Systems 24 (1998) 159–182

### [R3.4] Google Duplex → строка disclosure (первые 10с)
- **Решение / фраза:**
  > «Hi — I'm an automated assistant calling on behalf of [Client] about a long-distance move. Just so you know, I'm an AI and this call may be recorded. Is now an okay time to go over a couple of quote details?»
  Правила: disclosure в первом предложении до просьбы; простыми словами («automated assistant»/«AI»); назвать, от кого; запись — вместе с disclosure; закончить «No»-ориентированным вопросом (отдаёт контроль); спокойная просодия (бэклэш был против *обмана*, не против AI).
- **Evidence:** фактическая пост-бэклэш фраза Duplex: «I'm Google's automated booking service, so I'll record the call…»; офиц. заявление Google — «disclosure built-in», «appropriately identified». Мишень бэклэша — сокрытие + фейковые «umm/ahh» (Tufekci; TechCrunch).
- **Confidence:** High.
- **Sources:** mediaengagement.org/research/google-assistant-and-the-ethics-of-ai · theverge.com/2018/5/10/17342414/google-duplex…

---

## R4. Вертикаль: long-distance moving

### [R4.1] FMCSA сметы, 110%, broker vs carrier
- **Таксономия смет:** Binding (§375.403, ровно 100%) · Non-binding (§375.405, до **110%** на доставке) · Binding-not-to-exceed (потолок = смета, может быть ниже).
- **110% verbatim (§375.407):** при оплате до 110% non-binding COD-сметы мувер **обязан** отдать груз на доставке; отказ = нарушение «reasonable dispatch» + cargo-delay liability. **Это и есть юридическое определение hostage load.** Impracticable ops capped 15%.
- **Broker vs carrier (§375.409, Part 371):** carrier владеет транспортом, держит DOT-authority + страховку, отвечает по BOL; broker только сводит, кладёт **$75k bond**, **не отвечает** за груз; может давать смету только по письменному согласию carrier'а. Потребитель часто не знает реального carrier до дня погрузки → классический bait-and-switch.
- **7 вопросов DISCOVERY:** тип сметы? carrier или broker + USDOT/MC#? на какой вес/тариф? in-home/video/sight-unseen? максимум по закону на доставке (100/110)? какой carrier повезёт (если broker)? депозит и возврат?
- **Red-flags:** RF1[HIGH] не может назвать тип сметы; RF2[HIGH] намёк собрать >110/100; RF3[MED] депозит без условий возврата / >20–25% (нет фед. cap — эвристика); RF4[HIGH] broker, скрывающий carrier; RF5[MED] sight-unseen на полный household.
- **Confidence:** High (eCFR — статутный текст).
- **Sources:** ecfr.gov/current/title-49/…/part-375 · FMCSA R&R Handbook (PDF) *(fmcsa.dot.gov/protect-your-move отдаёт 403 на WebFetch — брать через браузер для скриншота)*

### [R4.2] FMCSA USDOT lookup API — live-верификация в звонке
- **Решение:** ✅ **Wire tool-call на QCMobile API.** Base: `https://mobile.fmcsa.dot.gov/qc/services`
  - `/carriers/{dotNumber}?webKey=KEY` — по USDOT
  - `/carriers/docket-number/{mc}?webKey=KEY` — по MC (⚠️ дефис `docket-number`)
  - `/carriers/{dotNumber}/authority` · `/oos` · `/basics` (BASIC-скоры)
  - Поля: `allowToOperate`, `outOfService`+дата, `complaintCount`, `legalName`, `mcNumber`, страховые флаги.
  - **Auth:** query-param `webKey` (не header). Получить: аккаунт **Login.gov** → dev-портал `mobile.fmcsa.dot.gov` → «My WebKeys». **Ручная регистрация, не мгновенно — получить до хакатона.**
  - **Fallback без ключа:** `safer.fmcsa.dot.gov/CompanySnapshot.aspx` (HTML, скрейп).
  - Демо-нарратив: «Проверяю DOT XXXXXX… authority active, 0 out-of-service, 3 complaints».
- **Confidence:** High по эндпоинтам; Med по полному JSON (проверить с реальным webKey до демо).
- **Sources:** mobile.fmcsa.dot.gov/QCDevsite/docs/qcApi · …/apiElements · stackoverflow.com/questions/43902900

### [R4.3] Бенчмарки цены (дистанция × размер)
- **Таблица (full-service, interstate; union диапазонов movebuddha/extraspace/mymovingjourney):**

  | Дистанция | Studio | 1BR | 2BR | 3BR | 4BR+ |
  |---|---|---|---|---|---|
  | 150–250 mi | $500–1,400 | $600–1,600 | $1,000–2,400 | $1,300–3,000 | $1,800–5,000 |
  | 250–500 mi | $800–2,650 | $900–2,850 | $1,300–3,650 | $1,600–4,250 | $2,100–7,000 |
  | 500–1,000 mi | $1,550–5,150 | $1,650–5,350 | $2,050–6,150 | $2,350–6,750 | $2,850–8,000 |
  | 1,000–1,500 mi | $3,050–7,650 | $3,150–7,850 | $3,550–8,650 | $3,850–9,250 | $4,350–10,250 |
  | 2,000+ mi | — | $3,500–5,500 | $4,500–8,500 | $5,500–9,500 | $6,000–13,000 |

  Якоря: movebuddha 100+mi = $1,000–14,000+; 2–3BR 500–1000mi ≈ $3,060–5,280. Вес-флор: **$0.50–0.80/lb**; дом ~2000 sq ft ≈ 7,000–10,000 lbs.
- **Формула red-flag:** `if quoted < 0.70*benchmark_low(band,size): ALARM`; вторичка `quoted/weight_lbs < $0.40/lb`.
- **Confidence:** High по числам; Med по сверке (вендоры мешают вес/объём/часы) — использовать диапазоны.
- **Sources:** movebuddha.com/moving-cost-calculator · extraspace.com/moving/tools/… · mymovingjourney.com/…

### [R4.4] Таксономия скрытых сборов (чеклист DISCOVERY = eval-метрика)
1. Fuel surcharge (5–10%, привязан к дизель-индексу) · 2. Stairs/flights (1-й пролёт free) · 3. Long-carry (>~75ft от входа) · 4. Shuttle ($250–500+) · 5. Elevator (~$50–100) · 6. Bulky/specialty (пиано/сейф/бильярд) · 7. Packing/materials (self-pack может исключать из FVP) · 8. **Storage-in-transit (SIT)** (per-100lb/день) · 9. **Valuation** (Released $0.60/lb free vs Full Value Protection платно) · 10. Expedited/guaranteed delivery · 11. Reweigh/weight-discrepancy (механизм 110%) · 12. Extra stops · 13. Waiting/delay · 14. Accessorial (disconnect/reconnect, crating, hoisting).
- **Confidence:** High (FreightWaves + муверы + CA BHGS тариф).
- **Sources:** freightwaves.com/…/hidden-fees-moving-companies-charge · allied.com/…/moving-valuation-coverage · bhgs.dca.ca.gov/…/max_4_2023.pdf

### [R4.5] Паттерны скама → red-flags
- **Паттерны:** hostage load (нарушение §375.407(b)) · sight-unseen lowball · большой депозит/«deposit mill» (брокеры >50%) · bait-and-switch · rogue/unlicensed.
- **Правила (leveled):** RF-A[HIGH] `quoted ≤ 0.70×benchmark_low AND sight-unseen` (= правило спеки); RF-B[HIGH] депозит >25% или cash/wire-only; RF-C[HIGH] не говорит broker/carrier или USDOT/MC → авто tool-call; RF-D[HIGH] tool-call вернул `allowToOperate=N`/`outOfService=Y`/много complaints; RF-E[MED] non-binding без упоминания 110%; RF-F[MED] нет адреса/уклончив об истории.
- **Demo-фикстуры:** BBB $1,670→$5,980; Reddit $500→$4,000 hostage; AZ Hostage Load Law (A.R.S. §§44-1611–1616).
- **Confidence:** High (DOT OIG + BBB + AG + eCFR).
- **Sources:** oig.dot.gov/investigations/household-goods-moving-fraud · bbb.org/article/news-releases/22659-know-your-mover…

---

## R5. Демо-риски (communication ось умирает на сцене, не в коде)

### [R5.1] Сеть на площадке — что падает первым, Plan B
- **Решение:** первым падает **STT-uplink WS** (непрерывный аплоад, не терпит буфер, тихо портит транскрипт → «AI не понял»). Лестница Plan B:
  1. **Мобильный хотспот как ОСНОВНОЙ канал** (не фолбэк; тест заранее, отдельная SIM).
  2. **Тумблер sim-market** (переговоры против прогретого локального counter-агента — убирает телефонный leg).
  3. **Кэш TTS** для опенера и типовых решающих реплик (offer/counter/accept/walk-away).
  4. **Записанный полный прогон** — страховка, готов к мгновенному пуску.
  5. **Прогрев всех сокетов за 10–15с** до сцены (dummy round-trip).
- **Confidence:** Med-High (лестница подтверждена; ранжирование «STT хуже всех» — инженерный вывод).
- **Sources:** itnext.io/tech-talk-fails… · developers.deepgram.com/docs/stt-troubleshooting-websocket… · news.ycombinator.com/item?id=17564185

### [R5.2] Латентный бюджет по звеньям
- **Решение:** ≤800мс реально (Twilio ConversationRelay <500мс медиана; DEF CON-команда sub-800мс на схожем стеке). **Не тратить оптимизацию на TTS/STT — они ок.** Рычаги: (1) VAD/endpointing, (2) LLM TTFT — вместе 50–70% бюджета.

  | Звено | Типично | Оптимизир. |
  |---|---|---|
  | Транспорт | 40–80мс WebRTC / 150–700мс PSTN | 20–50мс WebRTC |
  | VAD/turn-taking | 150–300мс / 80–120мс (SIP tuned) | ~100мс |
  | STT final | 50–150мс | 50–100мс |
  | **LLM TTFT (Talker)** | 200–400мс | 150–250мс |
  | TTS TTFB (Flash v2.5 WS) | 100–150мс NA/EU | 75мс (инференс) |
  | **Итого (телефон)** | 900мс–1.8с | 450–800мс p95 |
- **Confidence:** High (сходятся ElevenLabs/Twilio/Retell/LiveKit + реальный DEF CON-результат).
- **Sources:** elevenlabs.io/…/latency-optimization · twilio.com/…/guide-core-latency… · directdefense.com/…battle-of-the-bots

### [R5.3] Voice prompt injection — защищает ли split, что закрыть
- **Решение:** split Talker/Strategist + code-enforced `accept_price` = **учебниковый фикс OWASP LLM01/LLM08** (лид этим на вопрос судьи о безопасности). **3 остаточные дыры закрыть до демо:**
  1. **Боковая дверь в ledger:** убедиться, что речь оппонента не может записать «факт» в поле, которое Strategist считает авторитетным (напр. tool-«заметки»). *Атакующему не нужно уговорить принять $9000 — достаточно, чтобы система «поверила», что коридор включает $9000.* ← 5-мин проверка кода, приоритет №1.
  2. **Output-guardrail на Talker:** чтобы он не **сливал** ценовой коридор/floor/системный промпт даже под инъекцией (Retell даёт это как фичу, ~50мс).
  3. **Санитизация STT→LLM:** экранировать в транскрипте то, что похоже на role-делимитеры/системные токены.
- **Evidence:** **Chevy of Watsonville** — бота уговорили на $1 за SUV именно потому, что одна модель держала и разговор, и полномочия (ровно та связка, что убирает наш split). **CERT/CC VU#649739** (Retell, «excessive agency», LLM08) — предписанный фикс дословно = наш дизайн. Voice-specific: «Flanking Attack» (arXiv 2502.00735), тон/темп/multi-turn инъекции — split нейтрализует (Strategist читает ledger, не реплики).
- **Confidence:** High по оценке архитектуры; Med по полноте списка дыр (нужна проверка реального кода tool-определений).
- **Sources:** envive.ai/post/case-study-chevy-dealerships-ai-chatbot · kb.cert.org/vuls/id/649739 · docs.retellai.com/build/guardrails · arxiv.org/html/2502.00735v2

---

## Чего НЕ ресерчим (осознанно)

- Мультивертикальность глубже конфига — бриф требует «параметры конфигом», не вторую вертикаль.
- Тонкие модели ценообразования муверов — хватает бенчмарк-таблиц.
- Свой TTS/клонирование голоса — берём готовый голос ElevenLabs.
- Юридику авторекордера за пределами штата демо.

---

## Открытые действия

Не решаются поиском — единственный источник правды по остаткам.

1. **[Discord/орги]** R0.1 — можно ли стартовать с приватного репо CodeFlow?
2. **[Бриф/Discord]** R0.2 — точные веса судейства.
3. **[Бриф/Discord]** R1.3 — требует ли ElevenLabs-трек их **STT**, или достаточно TTS/Agents? (решает, оставлять ли Deepgram)
4. **[Орги]** R1.5 — раздают ли повышенные спонсорские кредиты/ключи ElevenLabs?
5. **[Сделать заранее]** R2.1 — перевести Twilio на платный аккаунт (до дня демо).
6. **[Сделать заранее]** R4.2 — получить FMCSA **webKey** (Login.gov, ручная регистрация — не мгновенно).
7. **[5-мин код-проверка]** R5.3 — убедиться, что нет пути записи речи оппонента в авторитетные поля ledger.
