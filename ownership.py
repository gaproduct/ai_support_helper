# -*- coding: utf-8 -*-
"""Ownership Resolution Time по спецификации руководителя поддержки.

Тикет в каждый момент принадлежит ровно одному владельцу, и секунда времени
записывается только ему. В ORT поддержки идут состояния «новый» и «в работе».
Ожидание клиента и ожидание смежного отдела из метрики исключаются.

Отличия от текущего resolution_metrics:
  * первым ответом считается любая первая реплика живого оператора, включая
    «взяли в работу»: клиент в этот момент уже получил контакт;
  * график 08:00-21:00 МСК все дни недели, а не 10:00-19:00 Пн-Пт;
  * время после передачи в смежный отдел и время ожидания клиента вычитаются;
  * после решения часы останавливаются, «спасибо» и оценка их не двигают,
    а возврат клиента с «не решено» запускает их снова.
"""
import re
from datetime import datetime, timedelta, timezone

import resolution_metrics as rm

MSK = timezone(timedelta(hours=3))
WORK_START, WORK_END = 8, 21          # часы МСК
WORK_DAYS = set(range(7))             # поддержка работает и в выходные

SUPPORT, CLIENT, DEPT, CLOSED = "support", "client", "dept", "closed"

# Сильные маркеры возврата из отдела: всё, кроме скриптового «благодарим за
# ожидание». Скриптовая фраза закрывает передачу только если сообщение не
# является попутным вопросом клиенту (руками: MG, 3606).
_CLOSE_STRONG = rm._compile([p for p in rm.HANDOFF_CLOSE if "ожидание" not in p])

# Фраза, которой бот сообщает, что поставил диалог в очередь к оператору.
BOT_ROUTED = [re.compile(r"дождит\w*.{0,30}подключени\w*\s+оператора", re.I)]


def work_seconds(a: datetime, b: datetime) -> float:
    """Секунды в окне 08:00-21:00 МСК между двумя моментами."""
    if b <= a:
        return 0.0
    total = 0.0
    cur, end = a.astimezone(MSK), b.astimezone(MSK)
    while cur < end:
        midnight = cur.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        chunk_end = min(midnight, end)
        if cur.weekday() in WORK_DAYS:
            lo = cur.replace(hour=WORK_START, minute=0, second=0, microsecond=0)
            hi = cur.replace(hour=WORK_END, minute=0, second=0, microsecond=0)
            total += max(0.0, (min(chunk_end, hi) - max(cur, lo)).total_seconds())
        cur = chunk_end
    return total


def compute(msgs: list, is_live_op) -> dict:
    """msgs — сообщения одного инцидента, is_live_op(msg) -> bool для реплик людей.

    Бот из первого ответа исключается: «дождитесь подключения оператора» контактом
    с поддержкой не является.
    """
    out = {"started_at": None, "first_response_at": None, "resolved_at": None,
           "frt": None, "frt_start": None, "bot_routed": False,
           "ort_support": 0.0, "ort_client": 0.0, "ort_dept": 0.0,
           "ort_total": None, "depts": [], "client_waits": 0, "reopens": 0}

    start_msg = next((m for m in msgs if m.is_client and m.substantive), None)
    if start_msg is None:
        return out
    out["started_at"] = start = start_msg.ts

    # Первый ответ ищем от ПЕРВОГО сообщения клиента, даже несодержательного:
    # ответ оператора на голое «Здравствуйте» — тоже первый ответ (SG-008).
    # Внутренние ссылки (Slack/Notion) ответом не считаются.
    any_client = next((m for m in msgs if m.is_client), start_msg)
    resp_from = min(any_client.ts, start)
    first = next((m for m in msgs
                  if m.is_support and m.ts >= resp_from and is_live_op(m)
                  and not rm._is_internal_note(m.text)), None)

    # Отсчёт первого ответа идёт не от первой реплики клиента, а от момента,
    # когда бот отдал диалог оператору. До этого запрос лежит у бота, оператор
    # его не видит, и время ожидания на поддержку вешать нельзя. Точка отсчёта
    # это последняя реплика клиента не позже фразы бота о подключении оператора:
    # именно её оператор увидит первой.
    frt_start = start
    routed = None
    for m in msgs:
        if first is not None and m.ts > first.ts:
            break
        if m.is_support and not is_live_op(m) and rm._matches(BOT_ROUTED, m.text):
            routed = m.ts
    if routed is not None:
        cand = [m.ts for m in msgs if m.is_client and m.substantive and m.ts <= routed]
        if cand:
            frt_start = max(cand)
    # Оператор ответил раньше расчётной точки отсчёта (например, на голое
    # «Здравствуйте» до передачи ботом) — отсчёт от последней реплики клиента
    # перед этим ответом.
    if first is not None and first.ts < frt_start:
        cand = [m.ts for m in msgs if m.is_client and m.ts <= first.ts]
        frt_start = max(cand) if cand else first.ts
    out["frt_start"] = frt_start
    out["bot_routed"] = routed is not None

    if first is not None:
        out["first_response_at"] = first.ts
        out["frt"] = work_seconds(frt_start, first.ts)

    # ── таймлайн владения ──
    owner, since = SUPPORT, start
    open_depts: set[str] = set()
    buckets = {SUPPORT: 0.0, CLIENT: 0.0, DEPT: 0.0}

    def switch(to: str, at: datetime):
        nonlocal owner, since
        if at > since and owner != CLOSED:
            buckets[owner] += work_seconds(since, at)
        owner, since = to, at

    for m in msgs:
        if m.ts < start:
            continue
        if not m.substantive:
            # Передача отделу часто идёт шаблонной заглушкой-холдом («shared
            # the information with our document management department»).
            # Заглушка владение не двигает, кроме явного открытия передачи
            # (руками: MG, 7649).
            if not m.is_client and is_live_op(m) and owner != CLOSED:
                opened = [d for d, pats in rm._HANDOFF_OPEN.items()
                          if d not in open_depts and rm._matches(pats, m.text)]
                if opened:
                    open_depts.update(opened)
                    out["depts"].extend(opened)
                    switch(DEPT, m.ts)
            continue

        if m.is_client:
            # Клиент сам подтвердил успех («всё получилось», «всё вывел») —
            # решение зафиксировано, даже если поддержка после собирает
            # диагностику (руками: AT-001, AZ-002).
            if owner != CLOSED and rm._is_resolution(m.text, False):
                out["resolved_at"] = m.ts
                switch(CLOSED, m.ts)
                continue
            # Клиент отвечает на уточнение либо возвращается после решения.
            if owner == CLIENT:
                switch(SUPPORT, m.ts)
            elif owner == CLOSED:
                # «Спасибо», оценка и подтверждение успеха тикет не переоткрывают.
                if not (rm._matches(rm._CLIENT_CLOSE, m.text)
                        or rm._matches(rm._CLIENT_ACK, m.text)
                        or rm._matches(rm._CSAT, m.text)
                        or rm._is_resolution(m.text, False)):
                    out["reopens"] += 1
                    # Вопрос снова открыт: прежнее решение больше не финальное,
                    # иначе часы шли бы дальше уже после «даты закрытия».
                    out["resolved_at"] = None
                    switch(SUPPORT, m.ts)
            continue

        # Бот владение не двигает: «дождитесь подключения оператора» не является
        # ни решением, ни передачей, ни вопросом клиенту.
        if not is_live_op(m):
            continue

        # После решения реплики поддержки часы не двигают и момент решения не
        # переносят: вежливые закрытия и пост-диагностика идут после факта.
        # Снова открыть тикет может только клиент (ветка reopen выше).
        if owner == CLOSED:
            continue

        # ── реплики поддержки ──
        closes = (rm._matches(rm._HANDOFF_CLOSE, m.text)
                  and not rm._matches(rm._HANDOFF_STILL_WAITING, m.text))
        strong_close = (rm._matches(_CLOSE_STRONG, m.text)
                        and not rm._matches(rm._HANDOFF_STILL_WAITING, m.text))
        resolves = rm._is_resolution(m.text, True)

        # Пока отдел работает, попутные уточнения клиенту владение не
        # возвращают: вопрос решает отдел, и руководитель всё это время
        # относит на отдел (руками: MG, 3606).
        if open_depts and not strong_close and not resolves \
                and rm._is_support_ask(m.text):
            continue

        if open_depts and (closes or resolves):
            open_depts.clear()
            switch(SUPPORT, m.ts)

        opened = [d for d, pats in rm._HANDOFF_OPEN.items()
                  if d not in open_depts and rm._matches(pats, m.text)]
        if opened and not resolves:
            open_depts.update(opened)
            out["depts"].extend(opened)
            switch(DEPT, m.ts)
            continue

        if rm._is_support_ask(m.text) and not resolves:
            out["client_waits"] += 1
            switch(CLIENT, m.ts)
            continue

        # Шаблоны «взяли в работу», «ожидайте» сюда не доходят: они отсеяны как
        # несодержательные выше, и ход остаётся за поддержкой.
        # Любой другой содержательный ответ клиенту останавливает часы. Тикет
        # считается решённым, пока клиент не вернулся и не сказал обратное.
        out["resolved_at"] = m.ts
        switch(CLOSED, m.ts)

    # Решения так и не было: закрываем шкалу последним сообщением инцидента.
    if out["resolved_at"] is None and msgs:
        switch(owner, msgs[-1].ts)

    out["ort_support"] = buckets[SUPPORT]
    out["ort_client"] = buckets[CLIENT]
    out["ort_dept"] = buckets[DEPT]
    # Общее время только по учтённым состояниям. Ожидание оценки после решения
    # не принадлежит никому и в сумму не входит, поэтому брать голый интервал
    # от обращения до решения нельзя: он был бы больше суммы частей.
    out["ort_total"] = buckets[SUPPORT] + buckets[CLIENT] + buckets[DEPT]
    return out
