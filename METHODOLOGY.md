# Методология подсчёта тикетов поддержки

Эталон: отчёт `june_focus_v1` (01–15.06.2026) — 706 тикетов из 551 диалога.

---

## 1. Что такое тикет

**1 тикет ≠ 1 диалог.**

| Сторона (`side`) | Формула |
|---|---|
| `executor` | 1 диалог = **1 тикет** |
| `customer` | 1 диалог = **max(1, N)** тикетов, где N = кол-во уникальных внешних email исполнителей, упомянутых в диалоге |

Логика customer-стороны: если заказчик в одном чате написал про 3 разных исполнителей (3 уникальных email) — это 3 тикета поддержки.

---

## 2. Какие категории включаются

| Категория | Включается? |
|---|---|
| Все тематические категории | **Да** |
| «Другое» | **Да** (включать!) |
| «Тест» | **Нет** — исключить |

> Ошибка: ранее «Другое» ошибочно исключалось. По эталону июня 2026 — «Другое» входит в общий счёт (54 тикета из 706).

---

## 3. Что исключается из подсчёта

### 3.1 Внутренние group-чаты платформы
Клиенты с внутренними `client_id` (MadeTask / Remozo служебные чаты):
```python
INTERNAL_CLIENT_IDS = frozenset({"25ee7201", "ed3c0fcd"})
```
Фильтр: `client_id` диалога начинается с одного из этих префиксов → исключить.

### 3.2 Рассылки и информационные сообщения
Диалоги, где **всё содержимое** — автоматическая рассылка от поддержки (праздничные графики, технические уведомления).

Признаки рассылки (BROADCAST_PATTERNS):
- «государственный выходной», «рекомендуем заранее пополнить»
- «нерабочие дни», «праздничные дни», «банковские выходные»
- «пополнение баланса недоступно», «работа в обычном режиме»
- «поздравляем», «с днём» (новогодние / праздничные)
- Оператор сам инициирует переписку без входящего клиентского запроса

Правило: если первое и единственное содержательное inbound-сообщение — рассылка → диалог не считается.

### 3.3 Кросс-кабинетные дубликаты (Flomni)
Один и тот же Telegram-чат (`client_id`) может присутствовать в нескольких Flomni-кабинетах. В одном периоде оставлять **только одну копию** (с наибольшим кол-вом сообщений или по source_priority).

Правило дедупликации: `GROUP BY client_id, dialog_date` → оставить одну запись.

### 3.4 Методология D — только inbound-first диалоги
Считать диалог тикетом только если:
- **первое сообщение inbound** (клиент написал первым), **ИЛИ**
- первое сообщение outbound, но категория = KYC / Проблема KYC / Дублирование KYC (оператор пишет клиенту для уточнения KYC-данных)

**Исключать**: диалоги где оператор пишет первым по другим поводам (outreach-пинги по статусу выплат, напоминания, приветствия).

---

## 4. Алгоритм подсчёта (псевдокод)

```python
KYC_CATEGORIES = {"KYC", "Проблема KYC", "Дублирование KYC"}
EXCLUDE_CATEGORIES = {"Тест"}
INTERNAL_CLIENT_IDS = frozenset({"25ee7201", "ed3c0fcd"})
BLACKLIST_DOMAIN_PREFIXES = ("madetask.", "made-task.", "remozo.")
BLACKLIST_DOMAINS_EXACT = {"chatapp.online"}
BLACKLIST_LOCAL_PARTS = {"support", "info", "hello", "noreply", "no-reply", "admin"}

def is_internal_email(email: str) -> bool:
    local, _, domain = email.lower().rpartition("@")
    if domain in BLACKLIST_DOMAINS_EXACT: return True
    if any(domain.startswith(p) for p in BLACKLIST_DOMAIN_PREFIXES): return True
    if local in BLACKLIST_LOCAL_PARTS: return True
    return False

def count_tickets(dialog, side, category) -> int:
    if side == "executor":
        return 1
    # customer: считаем уникальные внешние email исполнителей
    text = " ".join(msg["text"] for msg in dialog.messages)
    emails = {e.lower() for e in EMAIL_RE.findall(text) if not is_internal_email(e)}
    return max(1, len(emails))

def is_inbound_first(dialog) -> bool:
    messages = sorted(dialog.messages, key=lambda m: m["time"])
    if not messages:
        return False
    return messages[0]["direction"] == "inbound"

def qualifies_methodology_d(dialog, category) -> bool:
    if is_inbound_first(dialog):
        return True
    if category in KYC_CATEGORIES:
        return True  # KYC outreach операторов — считаем
    return False

def is_broadcast(dialog) -> bool:
    # Все inbound-сообщения — рассылка, нет реального клиентского запроса
    inbound_texts = [m["text"] for m in dialog.messages if m["direction"] == "inbound"]
    broadcast_keywords = [
        "государственный выходной", "рекомендуем заранее пополнить",
        "нерабочие дни", "праздничные дни", "банковские выходные",
        "пополнение баланса недоступно", "работа в обычном режиме",
    ]
    return all(
        any(kw in t.lower() for kw in broadcast_keywords)
        for t in inbound_texts
    ) if inbound_texts else False

def compute_tickets(date_from, date_to):
    dialogs = fetch_dialogs(date_from, date_to)  # processed=True

    # 1. Убрать внутренние group-чаты
    dialogs = [d for d in dialogs if not any(
        d.client_id.startswith(cid) for cid in INTERNAL_CLIENT_IDS
    )]

    # 2. Убрать рассылки
    dialogs = [d for d in dialogs if not is_broadcast(d)]

    # 3. Дедупликация кросс-кабинетных дублей (Flomni)
    seen = {}
    for d in dialogs:
        key = (d.client_id, d.dialog_date)
        if key not in seen or d.messages_count > seen[key].messages_count:
            seen[key] = d
    dialogs = list(seen.values())

    # 4. Убрать исключённые категории
    dialogs = [(d, side, cat) for d, side, cat in with_analysis(dialogs)
               if cat not in EXCLUDE_CATEGORIES]

    # 5. Методология D
    dialogs = [(d, side, cat) for d, side, cat in dialogs
               if qualifies_methodology_d(d, cat)]

    # 6. Считаем тикеты
    total = sum(count_tickets(d, side, cat) for d, side, cat in dialogs)
    return total
```

---

## 5. Эталонные цифры для сверки

| Период | Диалогов | Тикетов | Customer | Executor |
|---|---|---|---|---|
| 01–15.06.2026 | 551 | **706** | 462 (65%) | 244 (35%) |

Источники в эталоне: ChatApp 189 (26.8%) + Flomni 517 (73.2%).

---

## 6. Частые ошибки

| Ошибка | Правильно |
|---|---|
| Исключать «Другое» из подсчёта | «Другое» **включать** |
| COUNT(*) диалогов из SQL | Применять `count_tickets()` с email-логикой |
| Не применять методологию D | Всегда фильтровать outbound-first, кроме KYC |
| Не дедуплицировать Flomni | GROUP BY (client_id, dialog_date) |
| Считать рассылки как тикеты | Детектить и исключать broadcast-сообщения |
